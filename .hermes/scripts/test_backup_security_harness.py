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

    def test_remote_scan_flags_token_only_github_userinfo_without_echoing_value(self):
        harness = load_harness()
        token = 'ghp_' + 'A' * 36
        old_git = getattr(harness, 'git')
        try:
            setattr(harness, 'git', lambda args, check=True: (
                f"origin\thttps://{token}@github.com/org/repo.git (fetch)\n"
                "safe\thttps://github.com/org/repo.git (fetch)\n"
            ))
            findings = harness.scan_remote_urls()
        finally:
            setattr(harness, 'git', old_git)

        self.assertEqual(findings, ['git remote origin: credential embedded in URL'])
        self.assertNotIn(token, '\n'.join(findings))

    def test_durable_static_dist_allows_only_small_text_web_artifacts(self):
        harness = load_harness()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            html_path = repo / 'homepage/dist/index.html'
            html_path.parent.mkdir(parents=True)
            html_path.write_text('<!doctype html>\n', encoding='utf-8')
            binary_path = repo / 'homepage/dist/leak.bin'
            binary_path.write_bytes(b'\x00SECRET')
            large_path = repo / 'stock-screener/site/dist/huge.svg'
            large_path.parent.mkdir(parents=True)
            large_path.write_bytes(b'a' * (harness.MAX_FILE_BYTES + 1))

            old_repo = harness.REPO
            try:
                setattr(harness, 'REPO', repo)
                self.assertIsNone(harness.durable_static_dist_issue('homepage/dist/index.html'))
                self.assertEqual(
                    harness.durable_static_dist_issue('homepage/dist/leak.bin'),
                    'homepage/dist/leak.bin: durable static artifact extension not allowed',
                )
                self.assertEqual(
                    harness.durable_static_dist_issue('stock-screener/site/dist/huge.svg'),
                    'stock-screener/site/dist/huge.svg: durable static artifact too large',
                )
            finally:
                setattr(harness, 'REPO', old_repo)

    def test_durable_static_dist_rejects_symlink_artifact(self):
        harness = load_harness()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            target = repo / 'target.html'
            target.write_text('ok\n', encoding='utf-8')
            link = repo / 'homepage/dist/index.html'
            link.parent.mkdir(parents=True)
            link.symlink_to(target)

            old_repo = harness.REPO
            try:
                setattr(harness, 'REPO', repo)
                self.assertEqual(
                    harness.durable_static_dist_issue('homepage/dist/index.html'),
                    'homepage/dist/index.html: durable static artifact symlink not allowed',
                )
            finally:
                setattr(harness, 'REPO', old_repo)


if __name__ == '__main__':
    unittest.main()
