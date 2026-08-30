import hashlib
import os
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import repo_scout.cli as cli_mod
import repo_scout.feedback as feedback_mod

from repo_scout.api_budget import estimate_api_budget
from repo_scout.cli import DEFAULT_OUT_DIR, run_scout, resolve_feedback_path, resolve_output_dir
from repo_scout.config import ScoutConfig, load_config
from repo_scout.filters import has_min_commits_each_month, passes_hard_filters
from repo_scout.feedback import load_feedback_profile, parse_feedback_args, record_feedback
from repo_scout.github_api import GitHubClient, GitHubRateLimitError, RequestPacer, build_search_queries, search_repositories
from repo_scout.interests import build_interest_profile
from repo_scout.ranking import rank_repo
from repo_scout.secure_fs import open_output_directory


class RepoScoutTests(unittest.TestCase):
    def test_absolute_output_ancestor_symlink_cannot_redirect_writes(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            external = base / "external"
            external.mkdir()
            ancestor = base / "ancestor"
            ancestor.symlink_to(external, target_is_directory=True)
            root = ancestor / "out"

            with self.assertRaises(OSError):
                with open_output_directory(root, root / "run") as (_, selected):
                    selected.atomic_write_json("result.json", {"unsafe": True})

            self.assertEqual(list(external.iterdir()), [])

    def test_replacing_opened_output_ancestor_cannot_redirect_writes(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            ancestor = base / "ancestor"
            root = ancestor / "out"
            root.mkdir(parents=True)
            external = base / "external"
            external.mkdir()

            with open_output_directory(root, root / "run") as (_, selected):
                moved = base / "moved-ancestor"
                ancestor.rename(moved)
                ancestor.symlink_to(external, target_is_directory=True)
                selected.atomic_write_json("result.json", {"safe": True})

            self.assertTrue((moved / "out" / "run" / "result.json").exists())
            self.assertEqual(list(external.iterdir()), [])

    def test_unexpected_scout_exceptions_do_not_leak_cache_directory_fds(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            config_path = base / "config.yaml"
            out = base / "out"
            config_path.write_text("languages:\n  - Python\ninterest_roots: []\n", encoding="utf-8")

            before = len(os.listdir("/proc/self/fd"))
            with mock.patch.object(cli_mod, "DEFAULT_OUT_DIR", out), mock.patch.object(
                cli_mod, "REPO_ROOT", base
            ), mock.patch.object(cli_mod, "search_repositories", side_effect=RuntimeError("unexpected")):
                for _ in range(20):
                    with self.assertRaisesRegex(RuntimeError, "unexpected"):
                        run_scout(config_path, out)
            after = len(os.listdir("/proc/self/fd"))

            self.assertEqual(after, before)

    def test_shared_locks_reject_symlinks_without_truncating_target(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "out"
            out.mkdir()
            target = root / "protected.txt"
            target.write_text("preserve me\n", encoding="utf-8")
            lock = out / ".repo-scout.lock"
            lock.symlink_to(target)

            with mock.patch.object(cli_mod, "DEFAULT_OUT_DIR", out):
                with self.assertRaises(OSError):
                    with cli_mod._scout_lock(out):
                        pass
            with mock.patch.object(feedback_mod, "DEFAULT_OUT_DIR", out), mock.patch.object(
                feedback_mod, "LOCK_PATH", lock
            ):
                with self.assertRaises(OSError):
                    with feedback_mod.repo_scout_lock():
                        pass

            self.assertEqual(target.read_text(encoding="utf-8"), "preserve me\n")
    def test_has_min_commits_each_month_requires_every_month(self):
        commit_dates = [
            "2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z", "2026-05-03T00:00:00Z", "2026-05-04T00:00:00Z", "2026-05-05T00:00:00Z",
            "2026-04-01T00:00:00Z", "2026-04-02T00:00:00Z", "2026-04-03T00:00:00Z", "2026-04-04T00:00:00Z", "2026-04-05T00:00:00Z",
            "2026-03-01T00:00:00Z", "2026-03-02T00:00:00Z", "2026-03-03T00:00:00Z", "2026-03-04T00:00:00Z", "2026-03-05T00:00:00Z",
            "2026-02-01T00:00:00Z", "2026-02-02T00:00:00Z", "2026-02-03T00:00:00Z", "2026-02-04T00:00:00Z", "2026-02-05T00:00:00Z",
            "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z", "2026-01-04T00:00:00Z", "2026-01-05T00:00:00Z",
            "2025-12-01T00:00:00Z", "2025-12-02T00:00:00Z", "2025-12-03T00:00:00Z", "2025-12-04T00:00:00Z", "2025-12-05T00:00:00Z",
        ]
        now = datetime(2026, 5, 10, tzinfo=timezone.utc)
        self.assertTrue(has_min_commits_each_month(commit_dates, months=6, min_per_month=5, now=now))
        missing_one = [d for d in commit_dates if not d.startswith("2026-02-05")]
        self.assertFalse(has_min_commits_each_month(missing_one, months=6, min_per_month=5, now=now))

    def test_activity_gate_can_use_complete_months_only(self):
        commit_dates = [
            "2026-05-01T00:00:00Z",
            "2026-04-01T00:00:00Z", "2026-04-02T00:00:00Z", "2026-04-03T00:00:00Z",
            "2026-03-01T00:00:00Z", "2026-03-02T00:00:00Z", "2026-03-03T00:00:00Z",
            "2026-02-01T00:00:00Z", "2026-02-02T00:00:00Z", "2026-02-03T00:00:00Z",
            "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z",
            "2025-12-01T00:00:00Z", "2025-12-02T00:00:00Z", "2025-12-03T00:00:00Z",
            "2025-11-01T00:00:00Z", "2025-11-02T00:00:00Z", "2025-11-03T00:00:00Z",
        ]
        now = datetime(2026, 5, 10, tzinfo=timezone.utc)
        self.assertFalse(has_min_commits_each_month(commit_dates, months=6, min_per_month=3, now=now))
        self.assertTrue(has_min_commits_each_month(
            commit_dates,
            months=6,
            min_per_month=3,
            now=now,
            include_current_month=False,
        ))

    def test_passes_hard_filters_rejects_archived_forks_stale_and_wrong_language(self):
        cfg = ScoutConfig.default()
        repo = {
            "full_name": "owner/repo",
            "archived": False,
            "fork": False,
            "has_issues": True,
            "license": {"spdx_id": "MIT"},
            "language": "Python",
            "stargazers_count": 200,
            "pushed_at": "2026-05-05T00:00:00Z",
        }
        now = datetime(2026, 5, 10, tzinfo=timezone.utc)
        self.assertTrue(passes_hard_filters(repo, cfg, now=now))
        self.assertFalse(passes_hard_filters({**repo, "archived": True}, cfg, now=now))
        self.assertFalse(passes_hard_filters({**repo, "fork": True}, cfg, now=now))
        self.assertFalse(passes_hard_filters({**repo, "language": "PHP"}, cfg, now=now))
        self.assertFalse(passes_hard_filters({**repo, "pushed_at": "2025-12-01T00:00:00Z"}, cfg, now=now))

    def test_rank_repo_prefers_interest_terms_and_contribution_labels(self):
        cfg = ScoutConfig.default()
        profile = {"terms": {"agents": 5, "llm": 4, "devtools": 3}}
        repo = {
            "full_name": "owner/agent-devtool",
            "description": "LLM agents for developer tools",
            "topics": ["llm", "agents", "developer-tools"],
            "stargazers_count": 500,
            "open_issues_count": 12,
            "contribution_labels": ["good first issue", "help wanted"],
        }
        ranked = rank_repo(repo, profile, cfg)
        self.assertGreater(ranked["score"], 0)
        self.assertIn("interest_match", ranked["reasons"])
        self.assertIn("contribution_labels", ranked["reasons"])

    def test_feedback_records_affect_repo_and_topic_scores(self):
        from repo_scout import feedback as feedback_mod

        with tempfile.TemporaryDirectory() as td:
            old_default_out = feedback_mod.DEFAULT_OUT_DIR
            old_lock_path = feedback_mod.LOCK_PATH
            feedback_mod.DEFAULT_OUT_DIR = Path(td) / "out"
            feedback_mod.LOCK_PATH = feedback_mod.DEFAULT_OUT_DIR / ".repo-scout.lock"
            feedback_path = feedback_mod.DEFAULT_OUT_DIR / "feedback.jsonl"
            try:
                record_feedback(
                    feedback_path,
                    full_name="owner/good",
                    score=2,
                    note="useful",
                    topics=["llm", "agents"],
                    language="Python",
                    created_at="2026-05-10T00:00:00+00:00",
                )
                record_feedback(
                    feedback_path,
                    full_name="owner/bad",
                    score=-1,
                    note="too big",
                    topics=["enterprise"],
                    language="Java",
                    created_at="2026-05-10T00:00:01+00:00",
                )
            finally:
                feedback_mod.DEFAULT_OUT_DIR = old_default_out
                feedback_mod.LOCK_PATH = old_lock_path
            feedback = load_feedback_profile(feedback_path)
            good = rank_repo(
                {"full_name": "owner/good", "topics": ["llm"], "language": "Python", "stargazers_count": 10, "open_issues_count": 3},
                {"terms": {}},
                ScoutConfig.default(),
                feedback,
            )
            similar = rank_repo(
                {"full_name": "owner/similar", "topics": ["agents"], "language": "Python", "stargazers_count": 10, "open_issues_count": 3},
                {"terms": {}},
                ScoutConfig.default(),
                feedback,
            )
            bad = rank_repo(
                {"full_name": "owner/bad", "topics": ["enterprise"], "language": "Java", "stargazers_count": 10, "open_issues_count": 3},
                {"terms": {}},
                ScoutConfig.default(),
                feedback,
            )
            self.assertIn("user_feedback_positive", good["reasons"])
            self.assertIn("user_feedback_topic", similar["reasons"])
            self.assertIn("user_feedback_negative", bad["reasons"])
            self.assertGreater(good["score"], similar["score"])
            self.assertLess(bad["score"], similar["score"])

    def test_parse_feedback_args_accepts_repo_score_and_note(self):
        parsed = parse_feedback_args("owner/repo +2 looks aligned")
        self.assertEqual(parsed["command"], "record")
        self.assertEqual(parsed["full_name"], "owner/repo")
        self.assertEqual(parsed["score"], 2)
        self.assertEqual(parsed["note"], "looks aligned")
        self.assertEqual(parse_feedback_args("summary")["command"], "summary")

    def test_feedback_path_can_be_shared_under_canonical_out_dir(self):
        out_dir = DEFAULT_OUT_DIR / "slash"
        feedback_path = DEFAULT_OUT_DIR / "feedback.jsonl"
        self.assertEqual(resolve_feedback_path(feedback_path, out_dir), feedback_path.resolve())

    def test_record_feedback_rejects_paths_outside_canonical_out_dir(self):
        with tempfile.TemporaryDirectory() as td:
            outside = Path(td) / "feedback.jsonl"
            with self.assertRaises(ValueError):
                record_feedback(outside, full_name="owner/repo", score=1, created_at="2026-06-27T00:00:00+00:00")
            self.assertFalse(outside.exists())

    def test_record_feedback_uses_repo_scout_lock(self):
        from repo_scout import feedback as feedback_mod

        calls = []

        class FakeLock:
            def __enter__(self):
                calls.append("enter")

            def __exit__(self, exc_type, exc, tb):
                calls.append("exit")
                return False

        old_lock = feedback_mod.repo_scout_lock
        try:
            feedback_mod.repo_scout_lock = lambda: FakeLock()
            with tempfile.TemporaryDirectory() as td:
                old_default_out = feedback_mod.DEFAULT_OUT_DIR
                old_lock_path = feedback_mod.LOCK_PATH
                feedback_mod.DEFAULT_OUT_DIR = Path(td) / "out"
                feedback_mod.LOCK_PATH = feedback_mod.DEFAULT_OUT_DIR / ".repo-scout.lock"
                path = feedback_mod.DEFAULT_OUT_DIR / "feedback.jsonl"
                try:
                    record_feedback(path, full_name="owner/repo", score=1, created_at="2026-06-27T00:00:00+00:00")
                finally:
                    feedback_mod.DEFAULT_OUT_DIR = old_default_out
                    feedback_mod.LOCK_PATH = old_lock_path
                self.assertEqual(calls, ["enter", "exit"])
                self.assertEqual(load_feedback_profile(path)["count"], 1)
        finally:
            feedback_mod.repo_scout_lock = old_lock

    def test_interest_profile_reads_only_configured_roots(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            allowed = tmp_path / "allowed"
            denied = tmp_path / "denied"
            allowed.mkdir()
            denied.mkdir()
            (allowed / "note.md").write_text("LLM agents devtools\n", encoding="utf-8")
            (denied / "secret.md").write_text("quantum finance\n", encoding="utf-8")
            profile = build_interest_profile([allowed], max_files=10, max_bytes_per_file=1000)
            self.assertGreaterEqual(profile["terms"]["llm"], 1)
            self.assertNotIn("quantum", profile["terms"])

    def test_api_budget_estimator_counts_search_and_enrichment_requests(self):
        cfg = ScoutConfig(
            languages=["Python", "Rust"],
            topics=["llm"],
            keywords=["agent"],
            max_candidates=10,
            search_pages_per_query=2,
            max_api_repos_for_commit_check=3,
            commit_months=6,
        )
        estimate = estimate_api_budget(cfg)
        self.assertEqual(len(build_search_queries(cfg)), 4)
        self.assertEqual(estimate["search_queries"], 4)
        self.assertEqual(estimate["search_pages_per_query"], 2)
        self.assertEqual(estimate["search_requests"], 8)
        self.assertEqual(estimate["repos_checked_for_activity_and_labels"], 3)
        self.assertEqual(estimate["commit_pages_per_repo"], 4)
        self.assertEqual(estimate["commit_requests"], 12)
        self.assertEqual(estimate["contribution_label_requests"], 12)
        self.assertEqual(estimate["total_get_requests"], 32)

    def test_search_repositories_paginates_and_deduplicates(self):
        cfg = ScoutConfig(
            languages=["Python"],
            topics=["llm"],
            keywords=[],
            max_candidates=10,
            search_pages_per_query=2,
        )
        calls = []

        class FakeClient:
            def get_json(self, path):
                calls.append(path)
                if "page=1" in path:
                    return {"items": [
                        {"full_name": "owner/one"},
                        {"full_name": "owner/two"},
                        {"full_name": "owner/one"},
                        {"full_name": "owner/three"},
                        {"full_name": "owner/four"},
                        {"full_name": "owner/five"},
                    ]}
                return {"items": [{"full_name": "owner/six"}]}

        repos = search_repositories(FakeClient(), cfg)
        self.assertEqual([repo["full_name"] for repo in repos], ["owner/one", "owner/two", "owner/three", "owner/four", "owner/five", "owner/six"])
        self.assertEqual(len(calls), 2)

    def test_github_client_accepts_gh_token_env(self):
        import os

        old_github = os.environ.pop("GITHUB_TOKEN", None)
        old_gh = os.environ.get("GH_TOKEN")
        try:
            os.environ["GH_TOKEN"] = "test-token"
            client = GitHubClient(cache_dir=None)
            self.assertTrue(client.is_authenticated)
        finally:
            if old_github is not None:
                os.environ["GITHUB_TOKEN"] = old_github
            else:
                os.environ.pop("GITHUB_TOKEN", None)
            if old_gh is not None:
                os.environ["GH_TOKEN"] = old_gh
            else:
                os.environ.pop("GH_TOKEN", None)

    def test_github_client_loads_token_from_hermes_env_when_process_env_missing(self):
        import os

        old_github = os.environ.pop("GITHUB_TOKEN", None)
        old_gh = os.environ.pop("GH_TOKEN", None)
        old_home = os.environ.get("HOME")
        try:
            with tempfile.TemporaryDirectory() as td:
                home = Path(td)
                env_file = home / ".hermes" / ".env"
                env_file.parent.mkdir(parents=True)
                env_file.write_text("GITHUB_TOKEN=file-token\n", encoding="utf-8")
                os.environ["HOME"] = str(home)
                client = GitHubClient(cache_dir=None)
                self.assertTrue(client.is_authenticated)
        finally:
            if old_github is not None:
                os.environ["GITHUB_TOKEN"] = old_github
            else:
                os.environ.pop("GITHUB_TOKEN", None)
            if old_gh is not None:
                os.environ["GH_TOKEN"] = old_gh
            else:
                os.environ.pop("GH_TOKEN", None)
            if old_home is not None:
                os.environ["HOME"] = old_home
            else:
                os.environ.pop("HOME", None)

    def test_github_client_ignores_corrupt_cache_and_rewrites_atomically(self):
        from repo_scout import github_api

        class FakeResponse:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"ok": true}'

        with tempfile.TemporaryDirectory() as td:
            client = GitHubClient(cache_dir=td, cache_ttl_hours=24, core_request_interval=0)
            cache_path = client._cache_path("https://api.github.com/rate_limit")
            assert cache_path is not None
            cache_path.write_text("{broken", encoding="utf-8")
            calls = []
            original_urlopen = github_api.urllib.request.urlopen
            try:
                def fake_urlopen(req, timeout=30):
                    calls.append(req.full_url)
                    return FakeResponse()
                github_api.urllib.request.urlopen = fake_urlopen
                self.assertEqual(client.get_json("/rate_limit"), {"ok": True})
            finally:
                github_api.urllib.request.urlopen = original_urlopen

            self.assertEqual(calls, ["https://api.github.com/rate_limit"])
            self.assertEqual(cache_path.read_text(encoding="utf-8"), '{\n  "ok": true\n}\n')
            self.assertFalse(list(Path(td).glob("*.tmp")))

    def test_github_client_cache_paths_do_not_collide_on_long_query_suffixes(self):
        with tempfile.TemporaryDirectory() as td:
            client = GitHubClient(cache_dir=td)
            shared_query = "q=" + ("cache-key-regression-" * 20)
            first_url = f"https://api.github.com/search/repositories?{shared_query}&page=1"
            second_url = f"https://api.github.com/search/repositories?{shared_query}&page=2"

            # The previous truncated URL quoting produced the same path here.
            self.assertEqual(
                urllib.parse.quote(first_url, safe="")[:220],
                urllib.parse.quote(second_url, safe="")[:220],
            )
            first_path = client._cache_path(first_url)
            second_path = client._cache_path(second_url)
            self.assertIsNotNone(first_path)
            self.assertIsNotNone(second_path)
            self.assertNotEqual(first_path, second_path)
            assert first_path is not None
            assert second_path is not None
            self.assertIn(hashlib.sha256(first_url.encode("utf-8")).hexdigest(), first_path.name)
            self.assertIn(hashlib.sha256(second_url.encode("utf-8")).hexdigest(), second_path.name)

    def test_github_client_safely_migrates_untruncated_legacy_cache(self):
        with tempfile.TemporaryDirectory() as td:
            client = GitHubClient(cache_dir=td, cache_ttl_hours=24, core_request_interval=0)
            url = "https://api.github.com/rate_limit"
            legacy_path = client._legacy_cache_path(url)
            cache_path = client._cache_path(url)
            assert legacy_path is not None
            assert cache_path is not None
            legacy_path.write_text('{"cached": true}', encoding="utf-8")

            self.assertEqual(client.get_json("/rate_limit"), {"cached": True})
            self.assertEqual(cache_path.read_text(encoding="utf-8"), '{\n  "cached": true\n}\n')

    def test_github_client_does_not_migrate_collision_prone_legacy_cache(self):
        with tempfile.TemporaryDirectory() as td:
            client = GitHubClient(cache_dir=td)
            long_url = "https://api.github.com/search/repositories?q=" + ("x" * 300) + "&page=1"
            self.assertIsNone(client._legacy_cache_path(long_url))

    def test_github_client_rejects_non_github_absolute_urls_before_auth_header(self):
        import os
        from repo_scout import github_api

        calls = []
        old_token = os.environ.get("GITHUB_TOKEN")
        original_urlopen = github_api.urllib.request.urlopen
        try:
            os.environ["GITHUB_TOKEN"] = "test-token"
            github_api.urllib.request.urlopen = lambda req, timeout=30: calls.append(req)
            client = GitHubClient(cache_dir=None, core_request_interval=0)
            with self.assertRaises(ValueError):
                client.get_json("https://example.com/rate_limit")
        finally:
            github_api.urllib.request.urlopen = original_urlopen
            if old_token is None:
                os.environ.pop("GITHUB_TOKEN", None)
            else:
                os.environ["GITHUB_TOKEN"] = old_token

        self.assertEqual(calls, [])

    def test_github_client_waits_and_retries_primary_rate_limit_once(self):
        import io
        import time
        import urllib.error
        from email.message import Message
        from repo_scout import github_api

        calls = []
        sleeps = []
        original_urlopen = github_api.urllib.request.urlopen
        original_sleep = github_api.time.sleep

        class FakeResponse:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"ok": true}'

        def fake_urlopen(req, timeout=30):
            calls.append(req.full_url)
            if len(calls) == 1:
                headers = Message()
                headers["X-RateLimit-Limit"] = "30"
                headers["X-RateLimit-Remaining"] = "0"
                headers["X-RateLimit-Reset"] = str(int(time.time()))
                raise urllib.error.HTTPError(
                    req.full_url,
                    403,
                    "Forbidden",
                    headers,
                    io.BytesIO(b'{"message":"API rate limit exceeded"}'),
                )
            return FakeResponse()

        try:
            github_api.urllib.request.urlopen = fake_urlopen
            github_api.time.sleep = lambda seconds: sleeps.append(seconds)
            client = GitHubClient(cache_dir=None, max_rate_limit_sleep=5, core_request_interval=0)
            self.assertEqual(client.get_json("/rate_limit"), {"ok": True})
        finally:
            github_api.urllib.request.urlopen = original_urlopen
            github_api.time.sleep = original_sleep

        self.assertEqual(len(calls), 2)
        self.assertEqual(len(sleeps), 1)
        self.assertLessEqual(sleeps[0], 5)

    def test_request_pacer_throttles_repeated_uncached_requests_by_kind(self):
        from repo_scout import github_api

        sleeps = []
        times = iter([10.0, 10.0, 10.2, 12.5, 20.0, 20.0, 20.1, 20.6])
        original_monotonic = github_api.time.monotonic
        original_sleep = github_api.time.sleep
        try:
            github_api.time.monotonic = lambda: next(times)
            github_api.time.sleep = lambda seconds: sleeps.append(round(seconds, 2))
            pacer = RequestPacer(search_interval=2.2, core_interval=0.5)
            pacer.wait("search")
            pacer.wait("search")
            pacer.wait("core")
            pacer.wait("core")
        finally:
            github_api.time.monotonic = original_monotonic
            github_api.time.sleep = original_sleep

        self.assertEqual(sleeps, [2.0, 0.4])

    def test_rate_limit_error_writes_error_report(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            config_path = tmp_path / "config.yaml"
            out_dir = tmp_path / "out"
            config_path.write_text("languages:\n  - Python\nmax_candidates: 1\nmax_api_repos_for_commit_check: 1\n", encoding="utf-8")
            from repo_scout import cli

            original = cli.search_repositories
            original_repo_root = cli.REPO_ROOT
            original_default_out = cli.DEFAULT_OUT_DIR
            try:
                cli.REPO_ROOT = tmp_path
                cli.DEFAULT_OUT_DIR = out_dir
                def raise_rate_limit(client, cfg):
                    raise GitHubRateLimitError(
                        status=403,
                        reason="rate limit exceeded",
                        url="https://api.github.com/search/repositories?q=test",
                        message="API rate limit exceeded",
                        rate_limit_limit="10",
                        rate_limit_remaining="0",
                        rate_limit_reset="1770000000",
                    )

                cli.search_repositories = raise_rate_limit
                result = run_scout(config_path, out_dir)
            finally:
                cli.search_repositories = original
                cli.REPO_ROOT = original_repo_root
                cli.DEFAULT_OUT_DIR = original_default_out

            self.assertEqual(result["mode"], "live-readonly-error")
            self.assertEqual(result["error"]["status"], 403)
            self.assertEqual(result["error"]["rate_limit_remaining"], "0")
            self.assertTrue((out_dir / "error_report.json").exists())

    def test_output_dir_must_stay_under_repo_scout_out(self):
        from repo_scout import cli

        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            original_repo_root = cli.REPO_ROOT
            original_default_out = cli.DEFAULT_OUT_DIR
            try:
                cli.REPO_ROOT = tmp_path
                cli.DEFAULT_OUT_DIR = tmp_path / "out"
                self.assertEqual(resolve_output_dir(Path("out/run")), (tmp_path / "out" / "run").resolve())
                with self.assertRaises(ValueError):
                    resolve_output_dir(tmp_path / "elsewhere")
            finally:
                cli.REPO_ROOT = original_repo_root
                cli.DEFAULT_OUT_DIR = original_default_out

    def test_symlinked_canonical_output_root_cannot_change_external_files(self):
        from repo_scout import cli

        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            external = tmp_path / "external"
            external.mkdir()
            protected = external / "protected.txt"
            protected.write_text("preserve me\n", encoding="utf-8")
            (tmp_path / "out").symlink_to(external, target_is_directory=True)
            config_path = tmp_path / "config.yaml"
            config_path.write_text("languages:\n  - Python\ninterest_roots: []\n", encoding="utf-8")

            original_repo_root = cli.REPO_ROOT
            original_default_out = cli.DEFAULT_OUT_DIR
            try:
                cli.REPO_ROOT = tmp_path
                cli.DEFAULT_OUT_DIR = tmp_path / "out"
                with self.assertRaises(OSError):
                    run_scout(config_path, Path("out"), dry_run=True)
            finally:
                cli.REPO_ROOT = original_repo_root
                cli.DEFAULT_OUT_DIR = original_default_out

            self.assertEqual(protected.read_text(encoding="utf-8"), "preserve me\n")
            self.assertEqual(sorted(path.name for path in external.iterdir()), ["protected.txt"])

    def test_load_config_from_yaml(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.yaml"
            path.write_text("languages:\n  - Python\nmax_candidates: 50\n", encoding="utf-8")
            cfg = load_config(path)
            self.assertEqual(cfg.languages, ["Python"])
            self.assertEqual(cfg.max_candidates, 50)


if __name__ == "__main__":
    unittest.main()
