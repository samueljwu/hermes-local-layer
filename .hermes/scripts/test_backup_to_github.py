from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("backup_to_github.sh")


class BackupToGithubTests(unittest.TestCase):
    def test_bogus_inherited_lock_fd_cannot_reach_harness(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            repo.mkdir()
            marker = root / "harness-ran"
            harness = root / "harness"
            harness.write_text(f"#!/bin/sh\ntouch {marker!s}\nexit 0\n", encoding="utf-8")
            harness.chmod(0o755)
            env = os.environ.copy()
            env.update({
                "HERMES_BACKUP_REPO": str(repo),
                "HERMES_BACKUP_HARNESS": str(harness),
                "HERMES_BACKUP_DOC_GUARD": str(harness),
                "HERMES_BACKUP_LOCK": str(root / "backup.lock"),
                "HERMES_BACKUP_LOCK_FD": "999999",
                "HERMES_BACKUP_LOCK_HELPER": str(harness),
            })

            proc = subprocess.run([str(SCRIPT)], env=env, text=True, capture_output=True, check=False)

            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Refusing invalid inherited backup lock", proc.stderr)
            self.assertFalse(marker.exists(), "security harness ran despite bogus lock fd")

    def test_open_regular_fd_for_wrong_inode_cannot_reach_harness(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            repo.mkdir()
            lock = root / "backup.lock"
            lock.touch()
            wrong = root / "wrong.lock"
            wrong.touch()
            marker = root / "harness-ran"
            harness = root / "harness"
            harness.write_text(f"#!/bin/sh\ntouch {marker!s}\nexit 0\n", encoding="utf-8")
            harness.chmod(0o755)
            fd = os.open(wrong, os.O_RDWR)
            try:
                env = os.environ.copy()
                env.update({
                    "HERMES_BACKUP_REPO": str(repo),
                    "HERMES_BACKUP_HARNESS": str(harness),
                    "HERMES_BACKUP_DOC_GUARD": str(harness),
                    "HERMES_BACKUP_LOCK": str(lock),
                    "HERMES_BACKUP_LOCK_FD": str(fd),
                })
                proc = subprocess.run(
                    [str(SCRIPT)],
                    env=env,
                    pass_fds=(fd,),
                    text=True,
                    capture_output=True,
                    check=False,
                )
            finally:
                os.close(fd)

            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("does not match configured lock", proc.stderr)
            self.assertFalse(marker.exists(), "security harness ran with wrong lock inode")

    def test_rejects_symlinked_lock_without_truncating_target(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "protected.txt"
            target.write_text("preserve me\n", encoding="utf-8")
            lock = root / "backup.lock"
            lock.symlink_to(target)
            env = os.environ.copy()
            env["HERMES_BACKUP_LOCK"] = str(lock)

            proc = subprocess.run([str(SCRIPT)], env=env, text=True, capture_output=True, check=False)

            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Refusing unsafe backup lock", proc.stderr)
            self.assertEqual(target.read_text(encoding="utf-8"), "preserve me\n")

    def test_rejects_symlinked_lock_parent_without_creating_external_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outside = root / "outside"
            outside.mkdir()
            linked_parent = root / "linked-locks"
            linked_parent.symlink_to(outside, target_is_directory=True)
            env = os.environ.copy()
            env["HERMES_BACKUP_LOCK"] = str(linked_parent / "backup.lock")

            proc = subprocess.run([str(SCRIPT)], env=env, text=True, capture_output=True, check=False)

            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Refusing unsafe backup lock", proc.stderr)
            self.assertEqual(list(outside.iterdir()), [])

    def test_pushes_existing_ahead_commit_when_worktree_has_no_changes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            remote = root / "remote.git"
            repo = root / "repo"
            subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / "README.md").write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
            subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
            subprocess.run(["git", "push", "-qu", "origin", "main"], cwd=repo, check=True)
            (repo / "README.md").write_text("two\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-qam", "ahead after failed push"], cwd=repo, check=True)

            harness = root / "harness"
            harness.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            harness.chmod(0o755)
            env = os.environ.copy()
            env.update({
                "HERMES_BACKUP_REPO": str(repo),
                "HERMES_BACKUP_HARNESS": str(harness),
                "HERMES_BACKUP_DOC_GUARD": str(harness),
                "HERMES_BACKUP_LOCK": str(root / "backup.lock"),
            })
            proc = subprocess.run([str(SCRIPT)], env=env, text=True, capture_output=True, check=False)

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("unpushed commit", proc.stdout)
            local = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            pushed = subprocess.check_output(["git", "rev-parse", "refs/heads/main"], cwd=remote, text=True).strip()
            self.assertEqual(local, pushed)


if __name__ == "__main__":
    unittest.main()