from __future__ import annotations

import copy
import contextlib
import importlib.util
import io
import json
import stat
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("evaluate_retrieval.py")
SPEC = importlib.util.spec_from_file_location("evaluate_retrieval_tested", SCRIPT)
assert SPEC and SPEC.loader
evaluator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluator)


def fixture_copy() -> dict:
    return copy.deepcopy(evaluator.load_json(evaluator.FIXTURE_PATH))


def write_snapshot(tmp_path: Path, snapshot: dict) -> Path:
    path = tmp_path / "results.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    return path


class EvaluateRetrievalQualityTests(unittest.TestCase):
    def test_fixture_validation_uses_schema_before_scoring(self):
        fixture = fixture_copy()
        fixture["questions"][0].pop("sourceSet")

        with self.assertRaisesRegex(evaluator.EvaluationError, r"sourceSet"):
            evaluator.validate_fixture(fixture)

    def test_split_policy_schema_requires_descriptions_and_complete_counts(self):
        mutations = (
            ("tuning description", lambda fixture: fixture["splitPolicy"].pop("tuning")),
            ("held-out description", lambda fixture: fixture["splitPolicy"].pop("held-out")),
            ("held-out count", lambda fixture: fixture["splitPolicy"]["counts"].pop("held-out")),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                fixture = fixture_copy()
                mutate(fixture)
                with self.assertRaisesRegex(evaluator.EvaluationError, r"splitPolicy"):
                    evaluator.validate_fixture(fixture)

    def test_fixture_validation_reports_unhashable_path_value_cleanly(self):
        fixture = fixture_copy()
        fixture["questions"][0]["requiredPages"].append({"path": "src/not-valid.md"})

        with self.assertRaisesRegex(evaluator.EvaluationError, r"requiredPages") as raised:
            evaluator.validate_fixture(fixture)

        self.assertNotIn("unhashable", str(raised.exception))

    def test_fixture_validation_rejects_split_count_drift_and_relevance_overlap(self):
        fixture = fixture_copy()
        fixture["splitPolicy"]["counts"]["all"] = 999
        overlap = fixture["questions"][0]["requiredPages"][0]
        fixture["questions"][0]["distractorPages"].append(overlap)

        with self.assertRaises(evaluator.EvaluationError) as raised:
            evaluator.validate_fixture(fixture)

        message = str(raised.exception)
        self.assertIn("splitPolicy.counts.all", message)
        self.assertIn("relevant and distractor", message)

    def test_fixture_validation_rejects_noncanonical_path_aliases(self):
        aliases = (
            "src/entities/../entities/nokia.md",
            "src/./entities/nokia.md",
            "src//entities/nokia.md",
            r"src\entities\nokia.md",
        )
        for alias in aliases:
            with self.subTest(alias=alias):
                fixture = fixture_copy()
                fixture["questions"][0]["distractorPages"].append(alias)
                with self.assertRaises(evaluator.EvaluationError) as raised:
                    evaluator.validate_fixture(fixture)
                message = str(raised.exception)
                self.assertTrue(
                    "canonical POSIX" in message
                    or "empty, '.', or '..' components" in message
                    or "POSIX '/' separators" in message
                    or "does not match" in message
                )

    def test_fixture_validation_detects_duplicate_evidence_parent_identity(self):
        fixture = fixture_copy()
        duplicate = copy.deepcopy(fixture["questions"][0]["requiredEvidencePassages"][0])
        duplicate["path"] = "src/entities/../entities/nokia.md"
        fixture["questions"][0]["requiredEvidencePassages"].append(duplicate)

        with self.assertRaises(evaluator.EvaluationError) as raised:
            evaluator.validate_fixture(fixture)

        message = str(raised.exception)
        self.assertIn("canonical POSIX", message)
        self.assertIn("duplicate canonical parent path", message)

    def test_repository_path_identity_rejects_symlink_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "src"
            source_root.mkdir()
            target = source_root / "target.md"
            target.write_text("target\n", encoding="utf-8")
            (source_root / "alias.md").symlink_to(target.name)

            self.assertEqual(
                evaluator.canonical_repo_path_identity("src/target.md", "fixture path", root),
                "src/target.md",
            )
            with self.assertRaisesRegex(evaluator.EvaluationError, r"canonical POSIX"):
                evaluator.canonical_repo_path_identity("src/alias.md", "fixture path", root)
            with self.assertRaisesRegex(evaluator.EvaluationError, r"repository-relative"):
                evaluator.canonical_repo_path_identity(str(target), "fixture path", root)

    def test_replay_rejects_duplicate_case_ids(self):
        fixture = fixture_copy()
        questions = evaluator.selected_questions(fixture, "all")
        snapshot = copy.deepcopy(evaluator.load_json(evaluator.FIXTURE_PATH.parent / "baseline.v1.results.json"))
        snapshot["cases"][1]["id"] = snapshot["cases"][0]["id"]

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(evaluator.EvaluationError, r"duplicate case id"):
                evaluator.load_replay_results(
                    write_snapshot(Path(directory), snapshot), fixture, questions, "all", 8, 1
                )

    def test_replay_rejects_malformed_result_and_negative_measurement(self):
        fixture = fixture_copy()
        questions = evaluator.selected_questions(fixture, "all")
        snapshot = copy.deepcopy(evaluator.load_json(evaluator.FIXTURE_PATH.parent / "baseline.v1.results.json"))
        snapshot["cases"][0]["latencyMs"] = -1
        snapshot["cases"][0]["payload"]["results"][0] = "not-an-object"

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(evaluator.EvaluationError) as raised:
                evaluator.load_replay_results(
                    write_snapshot(Path(directory), snapshot), fixture, questions, "all", 8, 1
                )

        message = str(raised.exception)
        self.assertTrue("latencyMs" in message or "results[0]" in message)
        self.assertNotIn("AttributeError", message)

    def test_replay_rejects_duplicate_result_paths_and_split_mismatch(self):
        fixture = fixture_copy()
        questions = evaluator.selected_questions(fixture, "all")
        snapshot = copy.deepcopy(evaluator.load_json(evaluator.FIXTURE_PATH.parent / "baseline.v1.results.json"))
        snapshot["configuration"]["split"] = "tuning"
        snapshot["cases"][0]["payload"]["results"][1]["path"] = snapshot["cases"][0]["payload"]["results"][0]["path"]

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(evaluator.EvaluationError) as raised:
                evaluator.load_replay_results(
                    write_snapshot(Path(directory), snapshot), fixture, questions, "all", 8, 1
                )

        self.assertIn("split", str(raised.exception))

    def test_replay_rejects_equivalent_duplicate_result_paths(self):
        fixture = fixture_copy()
        questions = evaluator.selected_questions(fixture, "all")
        snapshot = copy.deepcopy(evaluator.load_json(evaluator.FIXTURE_PATH.parent / "baseline.v1.results.json"))
        payload = snapshot["cases"][0]["payload"]
        canonical = payload["results"][0]["path"]
        parts = canonical.split("/")
        alias = "/".join(parts[:-1] + ["..", parts[-2], parts[-1]])
        payload["results"][1] = copy.deepcopy(payload["results"][0])
        payload["results"][1]["path"] = alias
        payload["readThesePagesFirst"] = []
        snapshot["cases"][0]["contextBytes"] = evaluator.compact_json_bytes(payload["results"])

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(evaluator.EvaluationError) as raised:
                evaluator.load_replay_results(
                    write_snapshot(Path(directory), snapshot), fixture, questions, "all", 8, 1
                )

        message = str(raised.exception)
        self.assertIn("canonical POSIX", message)
        self.assertIn("duplicate canonical path", message)

    def test_output_preflight_rejects_collisions_and_protected_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            shared = Path(directory) / "shared.out"
            with self.assertRaisesRegex(evaluator.EvaluationError, r"pairwise distinct"):
                evaluator.preflight_paths(shared, shared, None, None, evaluator.git_root())

        with self.assertRaisesRegex(evaluator.EvaluationError, r"protected"):
            evaluator.preflight_paths(evaluator.SCRIPT_PATH, None, None, None, evaluator.git_root())

        with self.assertRaisesRegex(evaluator.EvaluationError, r"fixture|schema|protected"):
            evaluator.preflight_paths(evaluator.FIXTURE_PATH, None, None, None, evaluator.git_root())

    def test_output_preflight_rejects_new_vcs_metadata_paths_and_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            (root / "git-metadata-alias").symlink_to(root / ".git", target_is_directory=True)
            forbidden = (
                root / ".git" / "index.lock",
                root / "nested" / ".." / ".git" / "refs" / "heads" / "main",
                root / ".hg" / "store" / "lock",
                root / ".svn" / "wc.db",
                root / "git-metadata-alias" / "index.lock",
            )
            for path in forbidden:
                with self.subTest(path=path):
                    with self.assertRaisesRegex(evaluator.EvaluationError, r"VCS metadata"):
                        evaluator.preflight_paths(
                            path,
                            None,
                            None,
                            None,
                            root,
                            wiki_root=root,
                        )

    def test_file_only_options_reject_stdout_sentinel(self):
        for option in ("--input-results", "--save-results"):
            with self.subTest(option=option):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                    evaluator.parse_args([option, "-"])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn(f"{option} does not support '-'", stderr.getvalue())
                with self.assertRaisesRegex(evaluator.EvaluationError, r"does not support"):
                    evaluator.output_path(Path("-"), option)

        self.assertIsNone(evaluator.output_path(Path("-"), "--json-out", allow_stdout=True))

    def test_readme_documents_reproducible_jsonschema_runtime(self):
        readme = (evaluator.FIXTURE_PATH.parent / "README.md").read_text(encoding="utf-8")
        self.assertIn("uv run --with jsonschema python3 _tools/evaluate_retrieval.py", readme)

    def test_integrity_hashes_untracked_and_ignored_protected_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".vitepress").mkdir()
            untracked = root / ".vitepress" / "untracked.mjs"
            ignored = root / ".vitepress" / "ignored.mjs"
            untracked.write_text("before-untracked\n", encoding="utf-8")
            ignored.write_text("before-ignored\n", encoding="utf-8")
            (root / ".gitignore").write_text(".vitepress/ignored.mjs\n", encoding="utf-8")

            _, before = evaluator.repository_hash_state(
                root, set(), None, "live", wiki_root=root
            )
            untracked.write_text("after-untracked\n", encoding="utf-8")
            ignored.write_text("after-ignored\n", encoding="utf-8")
            _, after = evaluator.repository_hash_state(
                root, set(), None, "live", wiki_root=root
            )

        changed = evaluator.changed_hash_paths(before, after)
        self.assertIn(".vitepress/untracked.mjs", changed)
        self.assertIn(".vitepress/ignored.mjs", changed)

    def test_query_failure_still_verifies_integrity_and_preserves_cause(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".vitepress").mkdir()
            protected = root / ".vitepress" / "query-helper.mjs"
            protected.write_text("before\n", encoding="utf-8")

            def failing_query():
                protected.write_text("after\n", encoding="utf-8")
                raise evaluator.EvaluationError("synthetic query failure")

            with self.assertRaises(evaluator.EvaluationError) as raised:
                evaluator.guarded_repository_operation(
                    root,
                    set(),
                    None,
                    "live",
                    failing_query,
                    wiki_root=root,
                )

        self.assertIn("synthetic query failure", str(raised.exception))
        self.assertIn("repository integrity verification failed", str(raised.exception))
        self.assertIsInstance(raised.exception.__cause__, evaluator.EvaluationError)

    def test_atomic_write_preserves_existing_mode_and_uses_0644_for_new_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / "existing.txt"
            existing.write_text("old\n", encoding="utf-8")
            existing.chmod(0o640)
            evaluator.atomic_write(existing, "new\n")
            self.assertEqual(stat.S_IMODE(existing.stat().st_mode), 0o640)

            created = root / "created.txt"
            evaluator.atomic_write(created, "new\n")
            self.assertEqual(stat.S_IMODE(created.stat().st_mode), 0o644)

    def test_unavailable_passage_recall_is_null_and_identifier_is_markdown_safe(self):
        fixture = fixture_copy()
        questions = evaluator.selected_questions(fixture, "all")
        _, _, executions = evaluator.load_replay_results(
            evaluator.FIXTURE_PATH.parent / "baseline.v1.results.json",
            fixture,
            questions,
            "all",
            8,
            1,
        )
        case = evaluator.score_case(questions[0], executions[0], 8)

        self.assertIsNone(case["requiredEvidence"]["passageRecallAtK"]["value"])
        escaped = evaluator.escape_markdown_identifier("A|B\n# `x` *y*")
        self.assertNotIn("|", escaped)
        self.assertNotIn("\n", escaped)
        self.assertNotIn("`", escaped)
        self.assertNotIn("*", escaped)


if __name__ == "__main__":
    unittest.main()
