#!/usr/bin/env python3
"""Regression tests for the filtered public local-layer backup."""
from __future__ import annotations

import importlib.util
import fcntl
import subprocess
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
    def test_public_readme_describes_filtered_mirror_not_private_backup(self):
        mod = load_module()
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            mod.write_placeholders(work)
            readme = (work / "README.md").read_text(encoding="utf-8")
            self.assertIn("filtered public mirror", readme.lower())
            self.assertIn("not the private knowledge backup", readme.lower())
            self.assertIn("personal wiki, journal, task, feed", readme.lower())
            self.assertIn("FILTERED PUBLIC LOCAL LAYER", readme)
            self.assertIn("allowlist + staged-tree leak scan", readme)
            self.assertIn("OMITTED ENTIRELY", readme)
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
                        with mod.backup_lock():
                            pass
                    self.assertIn("private Hermes knowledge backup", str(ctx.exception))
                    fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            finally:
                mod_mut.LOCK_PATH = old_local
                mod_mut.KNOWLEDGE_BACKUP_LOCK_PATH = old_private


if __name__ == "__main__":
    unittest.main()
