import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from repo_scout.cache_retention import prune_expired_responses
from repo_scout.secure_fs import open_output_directory
from repo_scout.config import ScoutConfig
from repo_scout import cli


class CacheRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'out'
        self.root.mkdir()
        self.cache = self.root / '_cache'
        self.cache.mkdir()

    def write(self, name, mtime):
        p = self.cache / name
        p.write_text('{}')
        os.utime(p, (mtime, mtime))
        return p

    def test_expiry_boundary_and_idempotence(self):
        self.write('old.json', 10)
        self.write('boundary.json', 50)
        self.write('fresh.json', 51)
        self.write('future.json', 101)
        self.write('keep.tmp', 10)
        with open_output_directory(self.root, self.cache) as (root, cache):
            with root.lock():
                result = prune_expired_responses(cache, 50, now=100)
                self.assertEqual(result['removed_files'], 2)
                self.assertEqual(prune_expired_responses(cache, 50, now=100)['removed_files'], 0)
        self.assertEqual({p.name for p in self.cache.iterdir()}, {'fresh.json', 'future.json', 'keep.tmp'})

    def test_no_symlink_following_or_recursion(self):
        outside = Path(self.tmp.name) / 'outside.json'
        outside.write_text('protected')
        (self.cache / 'link.json').symlink_to(outside)
        nested = self.cache / 'nested.json'
        nested.mkdir()
        (nested / 'old.json').write_text('{}')
        with open_output_directory(self.root, self.cache) as (root, cache):
            with root.lock():
                self.assertEqual(prune_expired_responses(cache, 0)['removed_files'], 0)
        self.assertEqual(outside.read_text(), 'protected')
        self.assertTrue((nested / 'old.json').exists())

    def test_descriptor_stays_bound_after_directory_replacement(self):
        self.write('old.json', 1)
        with open_output_directory(self.root, self.cache) as (root, cache):
            with root.lock():
                moved = self.root / 'moved'
                self.cache.rename(moved)
                self.cache.mkdir()
                replacement = self.write('old.json', 1)
                self.assertEqual(prune_expired_responses(cache, 50, now=100)['removed_files'], 1)
                self.assertTrue(replacement.exists())
                self.assertFalse((moved / 'old.json').exists())

    def test_invalid_ttl_does_not_delete(self):
        p = self.write('old.json', 1)
        with open_output_directory(self.root, self.cache) as (_, cache):
            for ttl in (-1, float('nan'), float('inf')):
                with self.assertRaises(ValueError):
                    prune_expired_responses(cache, ttl)
        self.assertTrue(p.exists())

    def test_cli_live_prunes_and_dry_run_does_not(self):
        old = self.write('old.json', 1)
        fresh = self.write('fresh.json', 4102444800)
        report = self.root / 'feedback.jsonl'
        report.write_text('')
        fake_client = MagicMock()
        fake_client.is_authenticated = False
        with patch.object(cli, 'DEFAULT_OUT_DIR', self.root), patch.object(cli, 'load_config', return_value=ScoutConfig()), patch.object(cli, 'GitHubClient', return_value=fake_client), patch.object(cli, 'search_repositories', return_value=[]) as search:
            self.assertEqual(cli.run_scout(Path('unused'), self.root, dry_run=True)['mode'], 'dry-run')
            search.assert_not_called()
            self.assertTrue(old.exists())
            self.assertEqual(cli.run_scout(Path('unused'), self.root)['mode'], 'live-readonly')
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertEqual(report.read_text(), '')


if __name__ == '__main__':
    unittest.main()
