#!/usr/bin/env python3
"""Synthetic CLI forwarding regression; never invokes npm or writes wiki state."""
import argparse
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
spec = importlib.util.spec_from_file_location('stage2_wiki_ops', Path(__file__).with_name('wiki_ops.py'))
assert spec is not None and spec.loader is not None
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)
class RetrievalCLI(unittest.TestCase):
    def args(self, **overrides):
        values = dict(query='fixture', top=5, hops=None, json=True, bundle_dir=None,
                      role=[], page_kind=[], max_passages=12, max_evidence_chars=24000, full_source=False)
        values.update(overrides)
        return argparse.Namespace(**values)
    def call(self, **kwargs):
        with patch.object(ops.subprocess, 'run', return_value=SimpleNamespace(returncode=7)) as run:
            self.assertEqual(ops.semantic_query(self.args(**kwargs)), 7)
            self.assertEqual(run.call_args.kwargs['cwd'], ops.ROOT)
            return run.call_args.args[0]
    def test_legacy_default(self):
        self.assertEqual(self.call(), ['npm', 'run', 'semantic-query', '--', 'fixture', '--top', '5', '--hops', '1', '--json'])
    def test_bundle_all_options(self):
        cmd = self.call(bundle_dir='/fixture/bundle', role=['raw', 'content'], page_kind=['article', 'paper'],
                        max_passages=3, max_evidence_chars=100, full_source=True)
        self.assertNotIn('--hops', cmd)
        self.assertEqual(cmd, ['npm', 'run', 'semantic-query', '--', 'fixture', '--top', '5', '--bundle-dir', '/fixture/bundle',
            '--max-passages', '3', '--max-evidence-chars', '100', '--role', 'raw', '--role', 'content',
            '--page-kind', 'article', '--page-kind', 'paper', '--full-source', '--json'])
    def test_bundle_rejects_hops(self):
        with self.assertRaises(ValueError), patch.object(ops.subprocess, 'run') as run:
            ops.semantic_query(self.args(bundle_dir='/fixture/bundle', hops=1))
        run.assert_not_called()
    def test_inspection_requires_bundle(self):
        with self.assertRaises(ValueError), patch.object(ops.subprocess, 'run') as run:
            ops.semantic_query(self.args(full_source=True))
        run.assert_not_called()
    def test_parser_accepts_options(self):
        argv = ['wiki_ops.py', 'semantic-query', 'fixture', '--bundle-dir', '/fixture/bundle', '--role', 'raw',
                '--page-kind', 'paper', '--max-passages', '2', '--max-evidence-chars', '15', '--full-source']
        with patch('sys.argv', argv), patch.object(ops.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(ops.main(), 0)
        cmd = run.call_args.args[0]
        self.assertNotIn('--hops', cmd)
        self.assertIn('--full-source', cmd)
if __name__ == '__main__':
    unittest.main()
