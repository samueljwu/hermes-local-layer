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
    def _cron_repo(self, before: dict, after: dict):
        import json
        temporary = tempfile.TemporaryDirectory()
        repo = Path(temporary.name)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        path = repo / ".hermes/cron/jobs.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(before), encoding="utf-8")
        subprocess.run(["git", "add", ".hermes/cron/jobs.json"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)
        path.write_text(json.dumps(after), encoding="utf-8")
        subprocess.run(["git", "add", ".hermes/cron/jobs.json"], cwd=repo, check=True)
        return temporary, repo

    def test_curator_backups_are_state_only_not_doc_triggers(self):
        guard = load_guard()
        self.assertFalse(guard.is_trigger(".hermes/skills/.curator_backups/2026-07-01T19-09-24Z/manifest.json"))
        self.assertFalse(guard.is_trigger(".hermes/skills/.curator_backups/2026-07-01T19-09-24Z/cron-jobs.json"))
        self.assertFalse(guard.is_trigger(".hermes/skills/.curator_backups/2026-07-01T19-09-24Z/skills.tar.gz"))

    def test_script_changes_still_require_docs(self):
        guard = load_guard()
        self.assertTrue(guard.is_trigger(".hermes/scripts/scheduled_backup.sh"))

    def test_wiki_chronology_ledger_is_state_only_not_doc_trigger(self):
        guard = load_guard()
        self.assertFalse(guard.is_trigger("wiki/src/_meta/chronology-audit.json"))
        self.assertTrue(guard.is_trigger("wiki/_tools/wiki_ops.py"))

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

    def test_cron_runtime_bookkeeping_change_does_not_trigger_docs(self):
        guard = load_guard()
        before = {"updated_at": "earlier", "jobs": [
            {"id": "one", "name": "Daily", "script": "run.py", "last_run_at": None,
             "repeat": {"completed": 1, "times": None}},
        ]}
        after = {"updated_at": "later", "jobs": [
            {"id": "one", "name": "Daily", "script": "run.py", "last_run_at": "later",
             "repeat": {"completed": 2, "times": None}},
        ]}
        temporary, repo = self._cron_repo(before, after)
        with temporary:
            old_repo = guard.REPO
            try:
                setattr(guard, "REPO", repo)
                self.assertFalse(guard.is_trigger(guard.CRON_JOBS_PATH))
            finally:
                setattr(guard, "REPO", old_repo)

    def test_cron_behavior_change_triggers_docs(self):
        guard = load_guard()
        before = {"jobs": [{"id": "one", "enabled": True, "schedule": {"expr": "0 20 * * *"}, "script": "run.py"}]}
        after = {"jobs": [{"id": "one", "enabled": False, "schedule": {"expr": "0 21 * * *"}, "script": "run.py"}]}
        temporary, repo = self._cron_repo(before, after)
        with temporary:
            old_repo = guard.REPO
            try:
                setattr(guard, "REPO", repo)
                self.assertTrue(guard.is_trigger(guard.CRON_JOBS_PATH))
            finally:
                setattr(guard, "REPO", old_repo)


if __name__ == "__main__":
    unittest.main()
