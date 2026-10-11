"""Scratch-only exact working-view and structural preflight regressions."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import ingestion_gate as g
import ingestion_orchestrator as o
import evidence_selection as selection

import test_evidence_selection as selection_fixtures


class BookkeepingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = selection_fixtures.SelectionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.fixture.prepare()
        o.gate_action(self.root, 'seal', 'demo')
        tmp = tempfile.TemporaryDirectory(prefix='bookkeeping-', dir=os.environ['TMPDIR'])
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / 'store'
        self.digest = o.prepare(self.root, 'demo', self.store)['snapshot_sha256']

    def test_bundle_roundtrip_dedup_and_unresolved_fields(self):
        record = g.load_record(self.root, 'demo')
        record['claims'] = [{**copy.deepcopy(record['claims'][0]), 'id': f'C{i}',
                             'unresolved_judgments': ['TEST speaker identity unresolved.']}
                            for i in range(100)]
        compact = o.compact_claims(record, lambda p: g.candidate_bytes(self.root, record, p))
        bundle = o.working_bundle(compact)
        self.assertEqual(o.expand_working_bundle(bundle), compact)
        self.assertEqual(g.canonical(o.expand_working_bundle(bundle)), g.canonical(compact))
        self.assertEqual(len(bundle['evidence']), 1)
        spans = {(r['path'], r['start'], r['end']) for r in compact['regions'].values()}
        spans.update((e['path'], e['exact_excerpt']['start'], e['exact_excerpt']['end'])
                     for c in compact['claims'] for e in c['evidence'])
        self.assertEqual(len(bundle['passages']), len(spans))
        self.assertEqual(len(bundle['claims']), 100)
        self.assertLess(len(g.canonical(bundle)), len(g.canonical(compact)))
        self.assertIn('Internal distribution only.', str(bundle))
        self.assertIn('not clinical evidence', str(bundle))
        self.assertEqual(bundle['claims'][0]['unresolved_judgments'], ['TEST speaker identity unresolved.'])

    def test_bundle_missing_and_stale_refs_fail(self):
        snap = o.validate_snapshot(o.Store(self.store), self.digest)
        good = o.working_bundle(snap['compact'])
        for field in ('evidence', 'passages', 'regions'):
            bad = copy.deepcopy(good)
            bad[field].pop(next(iter(bad[field])))
            with self.subTest(field=field), self.assertRaises(g.GateError):
                o.expand_working_bundle(bad)
        bad = copy.deepcopy(good)
        next(iter(bad['passages'].values()))['text'] += ' changed'
        with self.assertRaises(g.GateError):
            o.expand_working_bundle(bad)
        bad = copy.deepcopy(good)
        bad['version'] = 'unknown'
        with self.assertRaises(g.GateError):
            o.expand_working_bundle(bad)

    def test_unicode_shared_excerpt_context_and_reserved_fields(self):
        text = 'TEST μ uncertainty — not clinical.\n'
        data = text.encode('utf-8')
        region = {'path': 'src/raw/notes/unicode.md', 'sha256': g.sha(data),
                  'start': 0, 'end': len(data), 'text': text, 'page': 1, 'kind': 'qualification'}
        item = {'path': region['path'], 'sha256': region['sha256'], 'locator': 'page 1',
                'excerpt': text, 'exact_excerpt': {'start': 0, 'end': len(data), 'text': text},
                'context_refs': ['R']}
        compact = {'claims': [{'id': 'CU', 'evidence': [item]}], 'regions': {'R': region}}
        bundle = o.working_bundle(compact)
        self.assertEqual(len(bundle['passages']), 1)
        self.assertEqual(o.expand_working_bundle(bundle), compact)
        self.assertEqual(next(iter(bundle['passages'].values()))['end'], len(data))
        for owner, field in (('claim', 'evidence_refs'), ('evidence', 'excerpt_ref'), ('region', 'text_ref')):
            bad = copy.deepcopy(compact)
            target = bad['claims'][0] if owner == 'claim' else bad['claims'][0]['evidence'][0] if owner == 'evidence' else bad['regions']['R']
            target[field] = 'TEST unknown annotation must not be overwritten.'
            with self.subTest(owner=owner), self.assertRaisesRegex(g.GateError, 'reserved'):
                o.working_bundle(bad)

    def test_visual_and_metadata_preservation_without_invented_offsets(self):
        item = {'visual': True, 'path': 'src/raw/assets/test.png', 'sha256': 'a' * 64,
                'excerpt': 'TEST uncertain symbol', 'locator': 'page 1', 'page': 1,
                'features': ['symbols'], 'details': [{'note': 'TEST unverified'}]}
        compact = {'claims': [{'id': 'CV', 'text': 'TEST only', 'evidence': [item, copy.deepcopy(item)],
                               'qualifications': 'TEST missing original.'}], 'regions': {}}
        bundle = o.working_bundle(compact)
        self.assertEqual(len(bundle['evidence']), 1)
        self.assertEqual(bundle['passages'], {})
        self.assertEqual(o.expand_working_bundle(bundle), compact)

    def test_unselected_page_index_once_per_operation(self):
        record = g.load_record(self.root, 'demo')
        record['selections'] = []
        base = record['claims'][0]
        base['evidence'] = [{'path': self.fixture.raw, 'sha256': g.sha(self.fixture.data),
                             'locator': '## Page 1', 'excerpt': 'Main model description.'}]
        record['claims'] = [{**copy.deepcopy(base), 'id': f'C{i}'} for i in range(100)]
        with patch.object(selection, 'page_bounds', wraps=selection.page_bounds) as bounds:
            first = o.compact_claims(record, lambda p: self.fixture.data)
            self.assertEqual(bounds.call_count, 1)
            second = o.compact_claims(record, lambda p: self.fixture.data)
            self.assertEqual(bounds.call_count, 2)
        self.assertEqual(first, second)
        self.assertEqual(len(first['claims']), 100)

    def test_preflight_sets_match_seal_and_no_approval(self):
        sealed = g.seal(self.root, 'demo')
        result = o.structural_preflight(self.root, 'demo')
        self.assertEqual(result['status'], 'structural-only-review-pending')
        for name, values in result['required_review'].items():
            self.assertEqual(values, sealed['checked_' + name])
        self.assertEqual(set(result['files']), g.evidence_paths(g.load_record(self.root, 'demo')) |
                         set(g.load_record(self.root, 'demo')['files']))
        for path, pin in result['files'].items():
            data = g.candidate_bytes(self.root, g.load_record(self.root, 'demo'), path)
            self.assertEqual(pin, {'sha256': g.sha(data), 'bytes': len(data)})
        self.assertEqual(result['pages'][self.fixture.raw], selection.page_bounds(self.fixture.data))
        self.assertEqual(g.load_record(self.root, 'demo')['state'], 'sealed')
        with self.assertRaises(g.GateError):
            o.gate_action(self.root, 'apply', 'demo', allow_apply=True)

    def test_preflight_drift_missing_and_ambiguous_fail(self):
        self.fixture.source.write_bytes(self.fixture.data + b'drift')
        with self.assertRaises(g.GateError):
            o.structural_preflight(self.root, 'demo')
        self.fixture.source.write_bytes(self.fixture.data)
        (self.root / self.fixture.target).write_bytes(b'Concurrent edit')
        with self.assertRaises(g.GateError):
            o.structural_preflight(self.root, 'demo')
        (self.root / self.fixture.target).write_bytes(b'Legacy fixture.\n')
        data = self.fixture.data + b'\n## Page 9\nMain model description.\n'
        self.fixture.source.write_bytes(data)
        record = g.load_record(self.root, 'demo')
        record['selections'] = []
        record['claims'][0]['evidence'] = [{'path': self.fixture.raw, 'sha256': g.sha(data),
                                          'locator': 'body', 'excerpt': 'Main model description.'}]
        g.save_record(self.root, record)
        g.seal(self.root, 'demo')
        with self.assertRaisesRegex(g.GateError, 'ambiguous excerpt'):
            o.structural_preflight(self.root, 'demo')
        self.fixture.source.unlink()
        with self.assertRaises((g.GateError, OSError)):
            o.structural_preflight(self.root, 'demo')

    def test_inspect_views_default_compatible_full_and_bundle(self):
        store = o.Store(self.store)
        summary = o.inspection_view(store, self.digest)
        self.assertEqual(set(summary), {'snapshot_sha256', 'payload_sha256', 'claims', 'status',
                                        'assets', 'visual_packets'})
        bundle = o.inspection_view(store, self.digest, 'bundle')
        full = o.inspection_view(store, self.digest, 'full')
        self.assertEqual(o.expand_working_bundle(bundle['working_bundle']), full['snapshot']['compact'])
        self.assertEqual(store.get_json(bundle['record_sha256']), full['record'])
        self.assertEqual(bundle['assets'], full['snapshot']['assets'])
        self.assertEqual(bundle['base_assets'], full['snapshot']['base_assets'])
        self.assertEqual(bundle['visuals'], full['snapshot']['visuals'])
        with self.assertRaises(g.GateError):
            o.inspection_view(store, self.digest, 'truncated')
        self.fixture.source.write_bytes(self.fixture.data + b'drift')
        for view in ('summary', 'bundle', 'full'):
            with self.subTest(view=view), self.assertRaises(g.GateError):
                o.inspection_view(store, self.digest, view)

    def test_cli_preflight_summary_and_private_full_view(self):
        for full in (False, True):
            stream = io.StringIO()
            with redirect_stdout(stream):
                result = o.main(['preflight', '--root', str(self.root), '--ingest', 'demo'] +
                                (['--full'] if full else []))
            self.assertEqual(result, 0)
            data = json.loads(stream.getvalue())
            self.assertEqual('working_bundle' in data, full)
            self.assertEqual('Main model description.' in stream.getvalue(), full)
        for view in ('summary', 'bundle', 'full'):
            stream = io.StringIO()
            with redirect_stdout(stream):
                self.assertEqual(o.main(['inspect', '--store', str(self.store), '--snapshot', self.digest,
                                         '--view', view]), 0)
            self.assertIsInstance(json.loads(stream.getvalue()), dict)



