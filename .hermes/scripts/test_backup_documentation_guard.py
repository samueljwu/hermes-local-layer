from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path("/home/hermes/.hermes/scripts/backup_documentation_guard.py")


def load_guard():
    spec = importlib.util.spec_from_file_location("backup_documentation_guard_tested", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class BackupDocumentationGuardTests(unittest.TestCase):
    def test_curator_backups_are_state_only_not_doc_triggers(self):
        guard = load_guard()
        self.assertFalse(guard.is_trigger(".hermes/skills/.curator_backups/2026-07-01T19-09-24Z/manifest.json"))
        self.assertFalse(guard.is_trigger(".hermes/skills/.curator_backups/2026-07-01T19-09-24Z/cron-jobs.json"))
        self.assertFalse(guard.is_trigger(".hermes/skills/.curator_backups/2026-07-01T19-09-24Z/skills.tar.gz"))

    def test_script_changes_still_require_docs(self):
        guard = load_guard()
        self.assertTrue(guard.is_trigger(".hermes/scripts/scheduled_backup.sh"))

    def test_staged_curator_backups_pass_without_doc_update(self):
        guard = load_guard()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            backup_dir = repo / ".hermes" / "skills" / ".curator_backups" / "2026-07-01T19-09-24Z"
            backup_dir.mkdir(parents=True)
            (backup_dir / "manifest.json").write_text('{"created_at":"2026-07-01T19:09:24Z"}\n', encoding="utf-8")
            subprocess.run(["git", "add", ".hermes/skills/.curator_backups/2026-07-01T19-09-24Z/manifest.json"], cwd=repo, check=True)

            old_repo = guard.REPO
            try:
                setattr(guard, "REPO", repo)
                self.assertEqual(guard.main(), 0)
            finally:
                setattr(guard, "REPO", old_repo)


if __name__ == "__main__":
    unittest.main()
