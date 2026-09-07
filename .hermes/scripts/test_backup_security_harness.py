from __future__ import annotations

import importlib.util
import io
import subprocess
import tempfile
import unittest
import tarfile
import zipfile
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
    def test_blocks_generated_wiki_tmp_tree(self):
        harness = load_harness()
        self.assertTrue(harness.path_is_blocked('wiki/.tmp/extracted.txt'))
        self.assertTrue(harness.path_is_blocked('wiki/.tmp/renders/page-001.png'))
        self.assertFalse(harness.path_is_blocked('wiki/src/raw/assets/page-001.png'))
        ignored = subprocess.run(
            ['git', 'check-ignore', '--no-index', '-q', 'wiki/.tmp/extracted.txt'],
            cwd=Path('/home/hermes'),
            check=False,
        )
        self.assertEqual(ignored.returncode, 0)

    def test_blocks_top_level_pdf_audit_extract_scratch(self):
        harness = load_harness()
        self.assertTrue(harness.path_is_blocked('pdf_audit_extract.json'))
        ignored = subprocess.run(
            ['git', 'check-ignore', '--no-index', '-q', 'pdf_audit_extract.json'],
            cwd=Path('/home/hermes'),
            check=False,
        )
        self.assertEqual(ignored.returncode, 0)

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

    def test_tracked_scan_reads_index_when_worktree_file_is_deleted(self):
        harness = load_harness()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
            tracked = repo / 'routine-state.md'
            tracked.write_text('benign tracked state\n', encoding='utf-8')
            subprocess.run(['git', 'add', 'routine-state.md'], cwd=repo, check=True)
            tracked.unlink()

            old_repo = harness.REPO
            try:
                setattr(harness, 'REPO', repo)
                findings = harness.scan_content(
                    ['routine-state.md'], tracked_index=True
                )
            finally:
                setattr(harness, 'REPO', old_repo)

        self.assertEqual(findings, [])

    def test_large_and_binary_files_are_scanned_instead_of_skipped(self):
        harness = load_harness()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            token = b'github_pat_' + b'A' * 44
            (repo / 'large.bin').write_bytes(b'\x00' + b'x' * harness.MAX_FILE_BYTES + token)
            old_repo = harness.REPO
            try:
                harness.REPO = repo
                findings = harness.scan_content(['large.bin'])
            finally:
                harness.REPO = old_repo
        self.assertEqual(findings, ['large.bin: content rule github_pat'])

    def test_archive_members_are_scanned_and_invalid_archives_fail_closed(self):
        harness = load_harness()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            payload = repo / 'member.txt'
            payload.write_text('github_pat_' + 'A' * 44, encoding='utf-8')
            with tarfile.open(repo / 'backup.tar.gz', 'w:gz') as archive:
                archive.add(payload, arcname='member.txt')
            (repo / 'broken.zip').write_bytes(b'not a zip')
            (repo / 'opaque.7z').write_bytes(b'opaque archive')
            old_repo = harness.REPO
            try:
                harness.REPO = repo
                archive_findings = harness.scan_content(['backup.tar.gz'])
                broken_findings = harness.scan_content(['broken.zip'])
                unsupported_findings = harness.scan_content(['opaque.7z'])
            finally:
                harness.REPO = old_repo
        self.assertEqual(archive_findings, ['backup.tar.gz: content rule github_pat'])
        self.assertEqual(broken_findings, ['broken.zip: content scan failed closed (invalid or unreadable archive)'])
        self.assertEqual(unsupported_findings, ['opaque.7z: content scan failed closed (unsupported archive format)'])

    def test_renamed_archive_is_detected_by_magic_and_scanned(self):
        harness = load_harness()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            with zipfile.ZipFile(repo / 'renamed.bin', 'w') as archive:
                archive.writestr('credential.txt', 'github_pat_' + 'A' * 44)
            old_repo = harness.REPO
            try:
                harness.REPO = repo
                findings = harness.scan_content(['renamed.bin'])
            finally:
                harness.REPO = old_repo
        self.assertEqual(findings, ['renamed.bin: content rule github_pat'])

    def test_prefixed_and_suffixless_nested_archives_fail_closed(self):
        harness = load_harness()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            inner = io.BytesIO()
            with zipfile.ZipFile(inner, 'w') as archive:
                archive.writestr('credential.txt', 'github_pat_' + 'A' * 44)
            (repo / 'prefixed.bin').write_bytes(b'harmless launcher preamble\n' + inner.getvalue())
            with zipfile.ZipFile(repo / 'outer.zip', 'w') as archive:
                archive.writestr('payload', inner.getvalue())
            old_repo = harness.REPO
            try:
                harness.REPO = repo
                prefixed = harness.scan_content(['prefixed.bin'])
                nested = harness.scan_content(['outer.zip'])
            finally:
                harness.REPO = old_repo
        self.assertEqual(prefixed, ['prefixed.bin: content rule github_pat'])
        self.assertEqual(nested, ['outer.zip: content scan failed closed (nested archive not safely scannable)'])

    def test_oversized_payload_fails_closed_before_read(self):
        harness = load_harness()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            oversized = repo / 'oversized.bin'
            with oversized.open('wb') as fh:
                fh.truncate(harness.MAX_SCAN_BYTES + 1)
            old_repo = harness.REPO
            try:
                harness.REPO = repo
                findings = harness.scan_content(['oversized.bin'])
            finally:
                harness.REPO = old_repo
        self.assertEqual(findings, ['oversized.bin: content scan failed closed (content exceeds bounded scan limit)'])

    def test_staged_deletion_of_blocked_tracked_path_is_allowed(self):
        harness = load_harness()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
            subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=repo, check=True)
            subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=repo, check=True)
            blocked = repo / '.env'
            blocked.write_text('old blocked file\n', encoding='utf-8')
            subprocess.run(['git', 'add', '.env'], cwd=repo, check=True)
            subprocess.run(['git', 'commit', '-qm', 'seed'], cwd=repo, check=True)
            subprocess.run(['git', 'rm', '-q', '.env'], cwd=repo, check=True)
            old_repo, old_argv = harness.REPO, __import__('sys').argv
            try:
                harness.REPO = repo
                __import__('sys').argv = ['backup_security_harness.py', '--staged', '--quiet']
                self.assertEqual(harness.main(), 0)
            finally:
                harness.REPO = old_repo
                __import__('sys').argv = old_argv

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
