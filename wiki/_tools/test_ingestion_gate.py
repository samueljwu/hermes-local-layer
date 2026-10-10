"""Accuracy-gate contract tests. All writes use temporary fixture roots."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ingestion_gate as gate


def digest(data):
    return hashlib.sha256(data).hexdigest()


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wiki-gate-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "src/concepts").mkdir(parents=True)
        (self.root / "src/raw/notes").mkdir(parents=True)
        self.legacy = self.root / "src/concepts/legacy.md"
        self.legacy.write_text("Legacy claim.\n")
        self.source = self.root / "src/raw/notes/legacy.md"
        self.source.write_text("---\npublished: undated\nsha256: " + "0" * 64 + "\n---\nSource says modeled, not measured.\n")
        gate.initialize(self.root)

    def prepare(self):
        draft = self.root / ".ingest-work/demo/src/concepts"
        draft.mkdir(parents=True)
        (draft / "legacy.md").write_text("The source reports a model.\n")
        gate.stage(self.root, "demo", "writer-session")
        record = gate.load_record(self.root, "demo")
        record["claims"] = [{
            "id": "C1", "text": "The source reports a model.",
            "risks": [], "risk_basis": "TEST-only qualitative model statement.",
            "destinations": ["src/concepts/legacy.md"], "kind": "model-output",
            "qualifications": "Undated source; no measurement demonstrated.",
            "evidence": [{"path": "src/raw/notes/legacy.md", "sha256": digest(self.source.read_bytes()),
                          "locator": "body", "excerpt": "Source says modeled, not measured."}],
        }]
        gate.save_record(self.root, record)
        gate.seal(self.root, "demo")
        return gate.load_record(self.root, "demo")

    def report(self, record):
        return {"payload_sha256": record["payload_sha256"], "reviewer": "independent-review-session",
                "verdict": "pass", "required_fixes": [], "checked_claims": ["C1"],
                "checked_files": list(record["files"]),
                "checked_evidence": ["src/raw/notes/legacy.md"],
                "scope": "Compared all material changed claims and qualifications against the supplied source, including omissions and unchanged context.",
                "risk_classification_checked": True, "checked_risk_claims": [], "risk_claim_checks": {},
                "checked_excluded_pages": [], "exclusion_checks": {},
                "all_material_claims_checked": True, "chronology_checked": True}

    def approve(self):
        record = self.prepare()
        gate.review(self.root, "demo", self.report(record))
        return gate.load_record(self.root, "demo")

    def test_legacy_is_not_retroactively_reviewed(self):
        result = gate.check(self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(result["legacy_files"], 2)
        self.assertEqual(result["reviewed_files"], 0)
        with self.assertRaises(gate.GateError):
            gate.initialize(self.root)

    def test_changed_or_new_unreviewed_page_blocks(self):
        self.legacy.write_text("Invented claim.\n")
        self.assertFalse(gate.check(self.root)["ok"])
        self.legacy.write_text("Legacy claim.\n")
        (self.root / "src/concepts/new.md").write_text("Unreviewed.\n")
        self.assertFalse(gate.check(self.root)["ok"])

    def test_draft_is_not_published_and_apply_requires_review(self):
        self.prepare()
        self.assertTrue(gate.check(self.root)["ok"])
        with self.assertRaises(gate.GateError):
            gate.apply(self.root, "demo")
        self.assertEqual(self.legacy.read_text(), "Legacy claim.\n")

    def test_reviewed_apply_and_late_edits(self):
        self.approve()
        gate.apply(self.root, "demo")
        self.assertTrue(gate.check(self.root)["ok"])
        self.legacy.write_text("Changed after review.\n")
        self.assertFalse(gate.check(self.root)["ok"])

    def test_same_reviewer_missing_coverage_and_blocking_fixes_rejected(self):
        record = self.prepare()
        for key, value in [("reviewer", "writer-session"), ("checked_claims", []),
                           ("checked_files", []), ("checked_evidence", []),
                           ("required_fixes", ["Fix units."]), ("verdict", "conditional")]:
            report = self.report(record)
            report[key] = value
            with self.subTest(key=key), self.assertRaises(gate.GateError):
                gate.review(self.root, "demo", report)

    def test_claim_edit_and_staged_edit_invalidate_receipt(self):
        self.approve()
        record = gate.load_record(self.root, "demo")
        record["claims"][0]["qualifications"] = "Changed."
        gate.save_record(self.root, record)
        with self.assertRaises(gate.GateError):
            gate.apply(self.root, "demo")

    def test_staged_file_edit_blocks_apply(self):
        self.approve()
        (self.root / ".ingest-work/demo/src/concepts/legacy.md").write_text("Changed after seal.\n")
        with self.assertRaises(gate.GateError):
            gate.apply(self.root, "demo")

    def test_live_concurrent_edit_is_not_overwritten(self):
        self.approve()
        self.legacy.write_text("Another writer's addition.\n")
        with self.assertRaises(gate.GateError):
            gate.apply(self.root, "demo")
        self.assertEqual(self.legacy.read_text(), "Another writer's addition.\n")

    def test_evidence_drift_invalidates_approval(self):
        self.approve()
        gate.apply(self.root, "demo")
        self.source.write_text(self.source.read_text() + "Changed evidence.\n")
        self.assertFalse(gate.check(self.root)["ok"])

    def test_raw_mutation_cannot_be_approved(self):
        draft = self.root / ".ingest-work/raw-edit/src/raw/notes"
        draft.mkdir(parents=True)
        (draft / "legacy.md").write_text("Repaired raw.\n")
        with self.assertRaises(gate.GateError):
            gate.stage(self.root, "raw-edit", "writer")

    def test_safe_paths_and_symlinks(self):
        for name in ("../escape", "/absolute", "bad\\path"):
            with self.subTest(name=name), self.assertRaises(gate.GateError):
                gate.stage(self.root, name, "writer")
        (self.root / "src/concepts/link.md").symlink_to(self.legacy)
        self.assertFalse(gate.check(self.root)["ok"])

    def test_exact_body_hash_contract(self):
        body = b"\nExact text.\r\n"
        raw = b"---\npublished: undated\nsha256: " + digest(body).encode() + b"\n---\n" + body
        self.assertEqual(gate.raw_body(raw), body)
        self.assertEqual(gate.raw_body_hash(raw), digest(body))
        self.assertNotEqual(gate.raw_body_hash(raw), digest(body.strip() + b"\n"))
        with self.assertRaises(gate.GateError):
            gate.raw_body(b"---\r\nsha256: bad\r\n---\r\ntext")

    def test_source_excerpt_must_exist_and_curated_cannot_be_evidence(self):
        self.prepare()
        for path, excerpt in [("src/concepts/legacy.md", "The source reports a model."),
                              ("src/raw/notes/legacy.md", "Invented supporting quote")]:
            record = gate.load_record(self.root, "demo")
            record["claims"][0]["evidence"][0].update(path=path, excerpt=excerpt)
            gate.save_record(self.root, record)
            with self.subTest(path=path), self.assertRaises(gate.GateError):
                gate.seal(self.root, "demo")

    def test_review_receipt_edit_is_detected(self):
        self.approve()
        record = gate.load_record(self.root, "demo")
        record["review"]["scope"] = "Changed after approval."
        gate.save_record(self.root, record)
        with self.assertRaises(gate.GateError):
            gate.apply(self.root, "demo")

    def test_restore_baseline_after_review_does_not_revive_exemption(self):
        self.approve()
        gate.apply(self.root, "demo")
        self.legacy.write_text("Legacy claim.\n")
        self.assertFalse(gate.check(self.root)["ok"])

    def test_index_and_attachment_are_protected(self):
        for path, data in [("src/index.md", b"Unsourced summary."),
                           ("src/raw/assets/extra.png", b"unreviewed attachment"),
                           ("public/extra.png", b"unreviewed public image"),
                           ("src/_meta/semantic/accepted-edges.json", b"[]")]:
            with self.subTest(path=path):
                target = self.root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                self.assertFalse(gate.check(self.root)["ok"])
                target.unlink()

    def test_invalid_locator_target_and_duplicate_claim_ids(self):
        self.prepare()
        original = gate.load_record(self.root, "demo")
        for mode in ("locator", "target", "duplicate"):
            record = json.loads(json.dumps(original))
            if mode == "locator":
                record["claims"][0]["evidence"][0]["locator"] = "PDF page 999"
            elif mode == "target":
                record["claims"][0]["text"] = "Not present in target."
            else:
                record["claims"].append(record["claims"][0])
            gate.save_record(self.root, record)
            with self.subTest(mode=mode), self.assertRaises(gate.GateError):
                gate.seal(self.root, "demo")

    def new_source(self):
        draft = self.root / ".ingest-work/new-source"
        source_path = "src/raw/notes/new.md"
        extract_path = "src/raw/assets/new/extraction.txt"
        body = b"## Page 1\nSource reports a model, not a measurement.\r\n"
        source = b"---\npublished: undated\nhash_policy: " + gate.HASH_POLICY.encode() + b"\nsha256: " + digest(body).encode() + b"\n---\n" + body
        files = {source_path: source, extract_path: body,
                 "src/concepts/new.md": b"The source reports a model.\n"}
        for path, data in files.items():
            target = draft / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        gate.stage(self.root, "new-source", "writer")
        record = gate.load_record(self.root, "new-source")
        record["sources"] = [{"path": source_path, "body_sha256": digest(body),
                              "method": "verbatim user paste with explicit page heading", "normalizations": [],
                              "date_basis": "Undated; no publication date supplied.",
                              "original": None, "original_unavailable_reason": "Pasted text, no original file.",
                              "extraction": {"path": extract_path, "sha256": digest(body)}}]
        record["claims"] = [{"id": "C1", "kind": "model-output", "text": "The source reports a model.",
                             "risks": [], "risk_basis": "TEST-only qualitative statement.",
                             "qualifications": "Undated; source does not demonstrate measurement.",
                             "destinations": ["src/concepts/new.md"],
                             "evidence": [{"path": source_path, "sha256": digest(source),
                                           "locator": "## Page 1", "excerpt": "Source reports a model, not a measurement."}]}]
        gate.save_record(self.root, record)
        gate.seal(self.root, "new-source")
        record = gate.load_record(self.root, "new-source")
        report = self.report(record)
        report["checked_evidence"] = sorted(gate.evidence_paths(record))
        gate.review(self.root, "new-source", report)
        return record

    def test_new_source_manifest_and_exact_extraction_are_required(self):
        self.new_source()
        gate.apply(self.root, "new-source")
        self.assertTrue(gate.check(self.root)["ok"])
        (self.root / "src/concepts/new.md").unlink()
        self.assertFalse(gate.check(self.root)["ok"])

    def test_mismatched_extraction_and_raw_hash_rejected(self):
        self.new_source()
        record = gate.load_record(self.root, "new-source")
        path = self.root / ".ingest-work/new-source/src/raw/notes/new.md"
        path.write_bytes(path.read_bytes() + b"Late annotation.")
        with self.assertRaises(gate.GateError):
            gate.seal(self.root, "new-source")
        self.assertTrue(gate.check(self.root)["ok"])

    def test_apply_failure_restores_exact_old_bytes(self):
        from unittest import mock
        self.approve()
        original = gate.atomic_write
        failed = False
        def injected(path, data, **kwargs):
            nonlocal failed
            if path == self.legacy and not failed:
                failed = True
                original(path, data, **kwargs)
                raise OSError("injected crash after target replacement")
            return original(path, data, **kwargs)
        with mock.patch.object(gate, "atomic_write", side_effect=injected):
            with self.assertRaises(OSError):
                gate.apply(self.root, "demo")
        self.assertEqual(self.legacy.read_text(), "Legacy claim.\n")
        self.assertTrue(gate.check(self.root)["ok"])
        self.assertEqual(gate.load_record(self.root, "demo")["state"], "reviewed")

    def test_pending_transaction_blocks_and_conflicts_do_not_overwrite(self):
        record = self.approve()
        transaction = self.root / "_meta/ingestion/transactions/demo"
        (transaction / "before/src/concepts").mkdir(parents=True)
        (transaction / "before/src/concepts/legacy.md").write_bytes(self.legacy.read_bytes())
        gate.write_json(transaction / "journal.json", {"id": "demo", "state": "pending", "files": record["files"]})
        self.assertFalse(gate.check(self.root)["ok"])
        self.legacy.write_text("Third-party edit.\n")
        with self.assertRaises(gate.GateError):
            gate.recover(self.root, "demo")
        self.assertEqual(self.legacy.read_text(), "Third-party edit.\n")
        self.legacy.write_text("The source reports a model.\n")
        gate.recover(self.root, "demo")
        self.assertEqual(self.legacy.read_text(), "Legacy claim.\n")
        self.assertTrue(gate.check(self.root)["ok"])

    def test_duplicate_json_keys_rejected(self):
        path = self.root / "bad.json"
        path.write_text('{"version": 1, "version": 2}')
        with self.assertRaises(gate.GateError):
            gate.read_json(path)

    def test_created_directory_entries_are_synced_before_file_commit(self):
        from unittest import mock
        target = self.root / "new/one/two/output.txt"
        original = gate.fsync_directory
        synced = []
        def track(path):
            synced.append(path)
            original(path)
        with mock.patch.object(gate, "fsync_directory", side_effect=track):
            gate.atomic_write(target, b"Synthetic fixture bytes.", expected=None)
        self.assertEqual(synced, [self.root, self.root / "new", self.root / "new/one", self.root / "new/one/two"])

    def test_recovery_syncs_parent_after_removing_new_target(self):
        from unittest import mock
        self.new_source()
        target = self.root / "src/concepts/new.md"
        write, sync = gate.atomic_write, gate.fsync_directory
        removed_synced = []
        failed = False
        def injected(path, data, **kwargs):
            nonlocal failed
            write(path, data, **kwargs)
            if path == target and not failed:
                failed = True
                raise OSError("Synthetic failure after new file commit.")
        def tracked_sync(path):
            if path == target.parent and not target.exists():
                removed_synced.append(path)
            sync(path)
        with mock.patch.object(gate, "atomic_write", side_effect=injected), mock.patch.object(gate, "fsync_directory", side_effect=tracked_sync):
            with self.assertRaises(OSError):
                gate.apply(self.root, "new-source")
        self.assertTrue(removed_synced)
        self.assertTrue(gate.check(self.root)["ok"])

    def test_deleted_legacy_page_blocks(self):
        self.legacy.unlink()
        self.assertFalse(gate.check(self.root)["ok"])
    def test_unknown_generated_directory_files_and_source_cache_are_protected(self):
        for rel in ("public/semantic/unreviewed.html", "public/semantic/extra.json",
                    "public/assets/fonts/unreviewed.html", "src/__pycache__/hidden.md"):
            with self.subTest(path=rel):
                path = self.root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("Unreviewed assertion.")
                self.assertFalse(gate.check(self.root)["ok"])
                self.assertIn(rel, gate.inventory(self.root, build_inputs=True))
                path.unlink()

    def test_edit_after_prospective_validation_is_not_overwritten(self):
        from unittest import mock
        self.approve()
        original = gate.check
        def injecting_check(root, candidate_record=None, overlay=None):
            result = original(root, candidate_record, overlay)
            if candidate_record:
                self.legacy.write_text("Concurrent addition after validation.\n")
            return result
        with mock.patch.object(gate, "check", side_effect=injecting_check):
            with self.assertRaises(gate.GateError):
                gate.apply(self.root, "demo")
        self.assertEqual(self.legacy.read_text(), "Concurrent addition after validation.\n")

    def test_edit_after_backups_is_not_overwritten(self):
        from unittest import mock
        self.approve()
        original = gate.write_json
        def injecting_journal(path, data):
            original(path, data)
            if path.name == "journal.json" and data.get("phase") == "ready":
                self.legacy.write_text("Concurrent addition after backups.\n")
        with mock.patch.object(gate, "write_json", side_effect=injecting_journal):
            with self.assertRaises(gate.GateError):
                gate.apply(self.root, "demo")
        self.assertEqual(self.legacy.read_text(), "Concurrent addition after backups.\n")

    def test_recovered_ingest_can_retry_without_discarding_history(self):
        from unittest import mock
        self.approve()
        original = gate.atomic_write
        failed = False
        def injected(path, data, **kwargs):
            nonlocal failed
            if path == self.legacy and not failed:
                failed = True
                original(path, data, **kwargs)
                raise OSError("injected failure after replacement")
            return original(path, data, **kwargs)
        with mock.patch.object(gate, "atomic_write", side_effect=injected):
            with self.assertRaises(OSError):
                gate.apply(self.root, "demo")
        gate.apply(self.root, "demo")
        self.assertTrue(gate.check(self.root)["ok"])
        self.assertGreaterEqual(len(list((self.root / "_meta/ingestion/transactions").rglob("journal.json"))), 2)

    def test_backup_creation_failure_is_recoverable_and_retryable(self):
        from unittest import mock
        self.approve()
        original = gate.atomic_write
        failed = False
        def injected(path, data, **kwargs):
            nonlocal failed
            if "before" in path.parts and not failed:
                failed = True
                raise OSError("injected backup failure")
            return original(path, data, **kwargs)
        with mock.patch.object(gate, "atomic_write", side_effect=injected):
            with self.assertRaises(OSError):
                gate.apply(self.root, "demo")
        self.assertEqual(self.legacy.read_text(), "Legacy claim.\n")
        gate.apply(self.root, "demo")
        self.assertTrue(gate.check(self.root)["ok"])

    def test_second_review_replaces_target_without_invalidating_historical_source(self):
        self.new_source()
        gate.apply(self.root, "new-source")
        draft = self.root / ".ingest-work/revision/src/concepts"
        draft.mkdir(parents=True)
        (draft / "new.md").write_text("No measurement is demonstrated.\n")
        gate.stage(self.root, "revision", "writer-2")
        record = gate.load_record(self.root, "revision")
        record["claims"] = [{"id": "C1", "kind": "model-output", "text": "No measurement is demonstrated.",
                             "risks": [], "risk_basis": "TEST-only qualitative statement.",
                             "destinations": ["src/concepts/new.md"], "qualifications": "Source-limited conclusion.",
                             "evidence": [{"path": "src/raw/notes/new.md", "sha256": digest((self.root / "src/raw/notes/new.md").read_bytes()),
                                           "locator": "## Page 1", "excerpt": "Source reports a model, not a measurement."}]}]
        gate.save_record(self.root, record)
        gate.seal(self.root, "revision")
        record = gate.load_record(self.root, "revision")
        report = self.report(record)
        report["checked_evidence"] = sorted(gate.evidence_paths(record))
        gate.review(self.root, "revision", report)
        gate.apply(self.root, "revision")
        self.assertTrue(gate.check(self.root)["ok"])


if __name__ == "__main__":
    unittest.main()
