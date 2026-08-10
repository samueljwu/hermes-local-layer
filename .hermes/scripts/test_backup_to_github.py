from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("backup_to_github.sh")


class BackupToGithubTests(unittest.TestCase):
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