from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path('/home/hermes/.hermes/scripts/backup_security_harness.py')


def load_harness():
    spec = importlib.util.spec_from_file_location('backup_security_harness_tested', SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class BackupSecurityHarnessTests(unittest.TestCase):
    def test_blocks_root_and_nested_live_config_yaml(self):
        harness = load_harness()
        self.assertTrue(harness.path_is_blocked('config.yaml'))
        self.assertTrue(harness.path_is_blocked('config.yaml.bak.1'))
        self.assertTrue(harness.path_is_blocked('foo/config.yaml'))
        self.assertTrue(harness.path_is_blocked('.hermes/config.yaml'))
        self.assertFalse(harness.path_is_blocked('repo-scout/config.yaml'))
        self.assertFalse(harness.path_is_blocked('.hermes/config.example.yaml'))

    def test_staged_scan_reads_index_blob_not_worktree(self):
        harness = load_harness()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
            subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=repo, check=True)
            subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=repo, check=True)
            leak = repo / 'leak.txt'
            leak.write_text('github_pat_' + 'A' * 44 + '\n', encoding='utf-8')
            subprocess.run(['git', 'add', 'leak.txt'], cwd=repo, check=True)
            leak.write_text('benign replacement\n', encoding='utf-8')

            old_repo = harness.REPO
            try:
                setattr(harness, 'REPO', repo)
                findings = harness.scan_content(['leak.txt'], staged=True)
            finally:
                setattr(harness, 'REPO', old_repo)

        self.assertEqual(findings, ['leak.txt: content rule github_pat'])


if __name__ == '__main__':
    unittest.main()
