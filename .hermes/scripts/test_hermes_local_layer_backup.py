#!/usr/bin/env python3
"""Regression tests for the filtered public local-layer backup."""
from __future__ import annotations

import importlib.util
import fcntl
import os
import subprocess
import stat
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from typing import Any, cast

SCRIPT = Path(__file__).with_name("hermes_local_layer_backup.py")


def load_module():
    spec = importlib.util.spec_from_file_location("hermes_local_layer_backup_tested", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LocalLayerBackupTests(unittest.TestCase):
    def test_remote_git_commands_reset_inherited_helpers_before_network(self):
        mod = load_module()
        calls = []

        def capture(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(mod.shutil, "which", return_value="/usr/bin/gh"), mock.patch.object(
            mod, "run", side_effect=capture
        ):
            mod.git_with_github_credentials(["fetch", "origin", "main"], cwd=Path("/tmp/example"))

        remote_args = calls[-1][0]
        self.assertEqual(
            remote_args[:5],
            ["git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"],
        )
        self.assertEqual(remote_args[5:], ["fetch", "origin", "main"])

    def test_remote_git_retries_transient_failure_and_timeout(self):
        mod = load_module()
        for failure in (
            subprocess.CompletedProcess([], 128, "", "fatal: Could not resolve host: github.com"),
            subprocess.TimeoutExpired(["git"], 90),
        ):
            with self.subTest(failure=type(failure).__name__):
                ok = subprocess.CompletedProcess([], 0, "", "")
                with mock.patch.object(mod.shutil, "which", return_value="/usr/bin/gh"), mock.patch.object(
                    mod, "run", side_effect=[ok, failure, ok]
                ) as run, mock.patch.object(mod.time, "sleep") as sleep:
                    self.assertEqual(mod.git_with_github_credentials(["fetch", "origin", "main"]).returncode, 0)
                    sleep.assert_called_once_with(2)
                    self.assertEqual(run.call_count, 3)
                    self.assertEqual(run.call_args.kwargs["timeout"], 90)

    def test_remote_git_failure_is_bounded_and_does_not_leak_stderr(self):
        mod = load_module()
        for detail, expected, attempts in (
            ("Could not resolve host", "DNS resolution failed", 3),
            ("Authentication failed", "authentication or permission rejected", 1),
            ("unexpected custom failure", "unclassified Git failure", 1),
            ("non-fast-forward", "manual reconciliation required", 1),
        ):
            sentinel = "do-not-print-this-credential"
            ok = subprocess.CompletedProcess([], 0, "", "")
            bad = subprocess.CompletedProcess([], 128, "", f"{detail}: https://{sentinel}@github.com")
            with self.subTest(detail=detail), mock.patch.object(mod.shutil, "which", return_value="/usr/bin/gh"), mock.patch.object(
                mod, "run", side_effect=[ok] + [bad] * attempts
            ) as run, mock.patch.object(mod.time, "sleep") as sleep:
                with self.assertRaisesRegex(RuntimeError, expected) as caught:
                    mod.git_with_github_credentials(["fetch", "origin", "main"])
                self.assertNotIn(sentinel, str(caught.exception))
                self.assertEqual(run.call_count, attempts + 1)
                self.assertEqual(sleep.call_count, attempts - 1)

    def test_git_status_codes_do_not_match_durations_or_commit_ids(self):
        mod = load_module()
        self.assertEqual(mod.classify_git_failure("fatal: Failed to connect after 1401 ms"), ("network connection failed", True))
        self.assertEqual(mod.classify_git_failure("fatal: cannot lock ref: is at abc500def"), ("local checkout or lock error", False))
        self.assertEqual(mod.classify_git_failure("fatal: requested URL returned error: 503"), ("remote service unavailable or rate limited", True))
        self.assertEqual(mod.classify_git_failure("fatal: requested URL returned error: 403"), ("authentication or permission rejected", False))

    def test_remote_readback_requires_exact_branch_and_commit(self):
        mod = load_module()
        sha = "a" * 40
        for output, accepted in (
            (f"{sha}\trefs/heads/main\n", True),
            (f"{'b' * 40}\trefs/heads/main\n", False),
            (f"{sha}\trefs/heads/other\n", False),
            ("", False),
        ):
            with self.subTest(output=output), mock.patch.object(
                mod, "run", return_value=subprocess.CompletedProcess([], 0, sha + "\n", "")
            ), mock.patch.object(mod, "git_with_github_credentials", return_value=subprocess.CompletedProcess([], 0, output, "")):
                if accepted:
                    self.assertEqual(mod.verify_remote_head(Path("/tmp/example")), sha[:12])
                else:
                    with self.assertRaisesRegex(RuntimeError, "does not match"):
                        mod.verify_remote_head(Path("/tmp/example"))

    def test_open_lock_rejects_symlink_without_truncating_target(self):
        mod = load_module()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "protected.txt"
            target.write_text("preserve me\n", encoding="utf-8")
            lock = root / "backup.lock"
            lock.symlink_to(target)

            with self.assertRaises(OSError):
                mod.open_lock(lock)

            self.assertEqual(target.read_text(encoding="utf-8"), "preserve me\n")

    def test_open_lock_rejects_symlinked_ancestor_without_external_creation(self):
        mod = load_module()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outside = root / "outside"
            outside.mkdir()
            linked = root / "linked"
            linked.symlink_to(outside, target_is_directory=True)

            with self.assertRaises(OSError):
                mod.open_lock(linked / "nested" / "backup.lock")

            self.assertEqual(list(outside.iterdir()), [])

    def test_validate_workdir_requires_owned_private_direct_child_of_tmp(self):
        mod = load_module()
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="hermes-local-layer-filtered-test-") as td:
            work = Path(td)
            work.chmod(0o755)
            old_work = mod.WORK
            mod_mut = cast(Any, mod)
            try:
                mod_mut.WORK = work
                self.assertEqual(mod.validate_workdir(), work)
                self.assertEqual(stat.S_IMODE(work.stat().st_mode), 0o700)
                nested = work / "nested"
                mod_mut.WORK = nested
                with self.assertRaises(RuntimeError):
                    mod.validate_workdir()
            finally:
                mod_mut.WORK = old_work

    def test_validate_workdir_rejects_symlink_direct_child(self):
        mod = load_module()
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            target = Path(td)
            link = Path("/tmp") / f"hermes-local-layer-filtered-link-{os.getpid()}"
            link.symlink_to(target, target_is_directory=True)
            old_work = mod.WORK
            mod_mut = cast(Any, mod)
            try:
                mod_mut.WORK = link
                with self.assertRaises(RuntimeError):
                    mod.validate_workdir()
            finally:
                mod_mut.WORK = old_work
                link.unlink()

    def test_configure_repo_credentials_resets_inherited_helpers(self):
        mod = load_module()
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            subprocess.run(["git", "init", "-q", str(work)], check=True)
            subprocess.run(["git", "config", "--local", "credential.helper", "store"], cwd=work, check=True)

            original_run = mod.run

            def skip_auth_status(args, **kwargs):
                if args[:3] == ["gh", "auth", "status"]:
                    return subprocess.CompletedProcess(args, 0, "", "")
                return original_run(args, **kwargs)

            with mock.patch.object(mod.shutil, "which", return_value="/usr/bin/gh"), mock.patch.object(
                mod, "run", side_effect=skip_auth_status
            ):
                mod.configure_repo_credentials(work)

            helpers = subprocess.check_output(
                ["git", "config", "--local", "--get-all", "credential.helper"],
                cwd=work,
                text=True,
            ).splitlines()
            self.assertEqual(helpers, ["", "!gh auth git-credential"])
    def test_private_stock_overrides_and_rule_name_variants_are_not_publishable(self):
        mod = load_module()
        rel = "stock-screener/config/long_biased_overrides.json"
        self.assertFalse(mod.is_candidate(rel))
        self.assertFalse(mod.is_candidate("stock-screener/config/long-biased-custom.json"))
        with mock.patch.object(mod, "has_private_wiki_term", return_value=False):
            for label in ("long-biased", "long_biased", "long biased"):
                self.assertEqual(mod.validate_file_bytes(label.encode(), "stock-screener/config/custom.json"), (False, "private stock pattern rule"))
            self.assertEqual(mod.validate_file_bytes(b"{}", "stock-screener/config/long-biased-custom.json"), (False, "private stock pattern rule"))
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            def git(*args):
                return subprocess.run(["git", *args], cwd=work, check=True, capture_output=True)
            git("init")
            path = work / rel
            path.parent.mkdir(parents=True)
            path.write_text("{}\n")
            git("add", ".")
            with self.assertRaisesRegex(RuntimeError, "blocked paths"):
                mod.verify_staged_tree(work)
            git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "fixture")
            path.unlink()
            git("add", "-A")
            mod.verify_staged_tree(work)

    def test_public_readme_describes_filtered_mirror_not_private_backup(self):
        mod = load_module()
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            mod.write_placeholders(work)
            readme = (work / "README.md").read_text(encoding="utf-8")
            self.assertIn("filtered public mirror", readme.lower())
            self.assertIn("not the private knowledge backup", readme.lower())
            for section in ("System map", "Harness-first design", "What is actually published", "Canonical versus generated", "Workflow examples", "Concurrency and write safety", "Privacy and publication boundaries"):
                self.assertIn(section, readme)
            for private_category in ("Personal wiki content", "journal entries", "task records", "recommendation history", "Private scanner rules"):
                self.assertIn(private_category, readme)
            self.assertIn("orient → validate → mutate → verify", readme)
            self.assertIn("not a restore image", readme)
            self.assertNotIn("Private backup of the durable", readme)
            self.assertNotIn("README.md", mod.ALLOW_FILES)

    def test_copy_filtered_rejects_symlink_candidates(self):
        mod = load_module()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "src"
            work = root / "work"
            src.mkdir()
            (src / ".hermes" / "scripts").mkdir(parents=True)
            secret = root / "outside.txt"
            secret.write_text("outside private text\n", encoding="utf-8")
            (src / ".hermes" / "scripts" / "leak.py").symlink_to(secret)

            old_src, old_work = mod.SRC, mod.WORK
            mod_mut = cast(Any, mod)
            try:
                mod_mut.SRC = src
                mod_mut.WORK = work
                included, omitted = mod.copy_filtered(work)
            finally:
                mod_mut.SRC = old_src
                mod_mut.WORK = old_work

            self.assertEqual(included, [])
            self.assertIn((".hermes/scripts/leak.py", "symlink"), omitted)
            self.assertFalse((work / ".hermes" / "scripts" / "leak.py").exists())

    def test_read_safe_file_rejects_symlinked_source_ancestor(self):
        mod = load_module()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "src"
            outside = root / "outside"
            src.mkdir()
            outside.mkdir()
            (outside / "leak.py").write_text("outside private text\n", encoding="utf-8")
            (src / ".hermes").mkdir()
            (src / ".hermes" / "scripts").symlink_to(outside, target_is_directory=True)
            old_src = mod.SRC
            mod_mut = cast(Any, mod)
            try:
                mod_mut.SRC = src
                ok, reason, _data, _mode = mod.read_safe_file(
                    src / ".hermes" / "scripts" / "leak.py", ".hermes/scripts/leak.py"
                )
            finally:
                mod_mut.SRC = old_src
            self.assertFalse(ok)
            self.assertIn("symlink", reason)

    def test_copy_filtered_writes_the_exact_bytes_that_passed_validation(self):
        mod = load_module()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "src"
            work = root / "work"
            candidate = src / ".hermes" / "scripts" / "safe.py"
            candidate.parent.mkdir(parents=True)
            candidate.write_text("changed after validation\n", encoding="utf-8")
            old_src = mod.SRC
            mod_mut = cast(Any, mod)
            try:
                mod_mut.SRC = src
                with mock.patch.object(
                    mod, "read_safe_file", return_value=(True, "ok", b"validated bytes\n", 0o644)
                ):
                    included, omitted = mod.copy_filtered(work)
            finally:
                mod_mut.SRC = old_src

            self.assertEqual(included, [".hermes/scripts/safe.py"])
            self.assertEqual(omitted, [])
            self.assertEqual((work / ".hermes" / "scripts" / "safe.py").read_bytes(), b"validated bytes\n")

    def test_verify_staged_tree_reports_secret_paths_without_values(self):
        mod = load_module()
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            subprocess.run(["git", "init"], cwd=work, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=work, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=work, check=True)
            secret_value = "AbCDefghij" + ".KLmnopqr/STuvwxyz+123456789="
            path = work / "README.md"
            path.write_text(f"api_key = '{secret_value}'\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=work, check=True)

            old_work = mod.WORK
            mod_mut = cast(Any, mod)
            try:
                mod_mut.WORK = work
                with self.assertRaises(RuntimeError) as ctx:
                    mod.verify_staged_tree(work)
            finally:
                mod_mut.WORK = old_work

            message = str(ctx.exception)
            self.assertIn("README.md", message)
            self.assertNotIn(secret_value, message)
    def test_verify_staged_tree_rejects_bare_provider_tokens(self):
        mod = load_module()
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            subprocess.run(["git", "init"], cwd=work, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=work, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=work, check=True)
            token = "github_pat_" + "A" * 44
            path = work / "README.md"
            path.write_text(f"Example copied token: {token}\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=work, check=True)
            path.write_text("benign worktree replacement\n", encoding="utf-8")

            with self.assertRaises(RuntimeError) as ctx:
                mod.verify_staged_tree(work)

            message = str(ctx.exception)
            self.assertIn("github_pat", message)
            self.assertIn("README.md", message)
            self.assertNotIn(token, message)

    def test_verify_staged_tree_rejects_slack_and_unquoted_secret_values(self):
        mod = load_module()
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            subprocess.run(["git", "init"], cwd=work, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=work, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=work, check=True)
            secret = "AbCDefghij" + ".KLmnopqr/STuvwxyz+123456789="
            slack = "xoxb-" + "A" * 24
            path = work / "README.md"
            path.write_text(f"client_secret = {secret}\nslack token {slack}\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=work, check=True)

            with self.assertRaises(RuntimeError) as ctx:
                mod.verify_staged_tree(work)

            message = str(ctx.exception)
            self.assertIn("README.md", message)
            self.assertNotIn(secret, message)
            self.assertNotIn(slack, message)

    def test_backup_lock_waits_until_private_lock_released(self):
        mod = load_module()
        with tempfile.TemporaryDirectory() as td:
            cast(Any, mod).LOCK_PATH = Path(td) / "local.lock"
            cast(Any, mod).KNOWLEDGE_BACKUP_LOCK_PATH = Path(td) / "private.lock"
            with mod.open_lock(mod.KNOWLEDGE_BACKUP_LOCK_PATH) as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                def release(seconds):
                    self.assertGreater(seconds, 0)
                    fcntl.flock(held.fileno(), fcntl.LOCK_UN)
                with mock.patch.object(mod.time, "sleep", side_effect=release) as sleep:
                    with mod.backup_lock():
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    sleep.assert_called_once()
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_backup_lock_timeout_releases_public_lock(self):
        mod = load_module()
        with tempfile.TemporaryDirectory() as td:
            cast(Any, mod).LOCK_PATH = Path(td) / "local.lock"
            cast(Any, mod).KNOWLEDGE_BACKUP_LOCK_PATH = Path(td) / "private.lock"
            with mod.open_lock(mod.KNOWLEDGE_BACKUP_LOCK_PATH) as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with mock.patch.object(mod.time, "monotonic", side_effect=[0, 0, 600]), mock.patch.object(mod.time, "sleep") as sleep:
                    with self.assertRaisesRegex(RuntimeError, "after 600s"):
                        with mod.backup_lock():
                            self.fail("must not enter while private lock is held")
                    sleep.assert_called_once_with(1.0)
                with mod.open_lock(mod.LOCK_PATH) as local:
                    fcntl.flock(local.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_backup_lock_rejects_duplicate_public_run_immediately(self):
        mod = load_module()
        with tempfile.TemporaryDirectory() as td:
            cast(Any, mod).LOCK_PATH = Path(td) / "local.lock"
            cast(Any, mod).KNOWLEDGE_BACKUP_LOCK_PATH = Path(td) / "private.lock"
            with mod.backup_lock(), mock.patch.object(mod.time, "sleep") as sleep:
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with mod.backup_lock():
                        self.fail("must not enter duplicate run")
                sleep.assert_not_called()

    def test_backup_lock_defers_when_private_backup_lock_is_held(self):
        mod = load_module()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            local_lock = root / "local.lock"
            private_lock = root / "private.lock"
            old_local = mod.LOCK_PATH
            old_private = mod.KNOWLEDGE_BACKUP_LOCK_PATH
            mod_mut = cast(Any, mod)
            try:
                mod_mut.LOCK_PATH = local_lock
                mod_mut.KNOWLEDGE_BACKUP_LOCK_PATH = private_lock
                private_lock.parent.mkdir(parents=True, exist_ok=True)
                with private_lock.open("w", encoding="utf-8") as held:
                    fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with self.assertRaises(RuntimeError) as ctx:
                        with mod.backup_lock(wait_seconds=0):
                            pass
                    self.assertIn("private Hermes knowledge backup", str(ctx.exception))
                    fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            finally:
                mod_mut.LOCK_PATH = old_local
                mod_mut.KNOWLEDGE_BACKUP_LOCK_PATH = old_private


if __name__ == "__main__":
    unittest.main()
