"""Synthetic-only selection/admission/retrieval fixtures; never real approval."""
import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import ingestion_gate as g
import evidence_selection as s


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='selection-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.raw = 'src/raw/notes/source.md'
        self.target = 'src/concepts/topic.md'
        self.source = self.root / self.raw
        self.source.parent.mkdir(parents=True)
        self.data = ('---\npublished: undated\n---\n## Page 1\n'
                     'Repeated header\nMain model description.\n'
                     'Method uses simulated tissue only; not clinical evidence.\n'
                     'Internal distribution only.\n'
                     '\n## Page 2\nRepeated header\nAppendix describes independent validation.\n'
                     '\n## Page 3\nRepeated header\nNavigation advertisement only.\n').encode()
        self.source.write_bytes(self.data)
        (self.root / self.target).parent.mkdir(parents=True)
        (self.root / self.target).write_text('Legacy fixture.\n')
        g.initialize(self.root)
        self.selection = {'schema': s.SCHEMA, 'path': self.raw, 'sha256': g.sha(self.data),
                          'original': None, 'original_unavailable_reason': 'TEST pasted fixture has no original.', 'pages': []}
        for bound in s.page_bounds(self.data):
            page = {**bound, 'status': 'excluded' if bound['page'] == 3 else 'reviewed',
                    'inspection': 'full-page', 'basis': 'TEST whole-page inspection; appendix substantive, mixed qualification retained.', 'spans': []}
            cursor = bound['start']
            for line in self.data[bound['start']:bound['end']].splitlines(keepends=True):
                kind = ('main-content' if line.startswith((b'Main', b'Appendix')) else
                        'qualification' if line.startswith(b'Method') else
                        'source-metadata' if line.startswith(b'Internal') else 'boilerplate')
                sid = 'S' + str(sum(len(p['spans']) for p in self.selection['pages']) + len(page['spans']))
                page['spans'].append({'id': sid, 'start': cursor, 'end': cursor + len(line), 'kind': kind,
                                      'requires': [], 'reason': 'TEST exact line category, not inferred from legal heading.'})
                cursor += len(line)
            self.selection['pages'].append(page)
        context = [x['id'] for p in self.selection['pages'] for x in p['spans'] if x['kind'] in {'qualification', 'source-metadata'}]
        for p in self.selection['pages']:
            for x in p['spans']:
                if x['kind'] == 'main-content':
                    x['requires'] = context[:]

    def prepare(self, name='demo', excerpt='Main model description.'):
        draft = self.root / '.ingest-work' / name / self.target
        draft.parent.mkdir(parents=True)
        draft.write_text('The supplied source describes a model.\n' if name == 'demo' else 'The supplied appendix describes validation.\n')
        g.stage(self.root, name, 'test-writer')
        record = g.load_record(self.root, name)
        record['claims'] = [{'id': 'C', 'text': draft.read_text().strip(), 'kind': 'source-statement',
                             'risks': [], 'risk_basis': 'TEST qualitative description.',
                             'qualifications': 'TEST model not clinical evidence; restricted source.',
                             'destinations': [self.target], 'evidence': [{'path': self.raw, 'sha256': g.sha(self.data),
                                                                        'locator': 'body', 'excerpt': excerpt}]}]
        if name == 'demo':
            record['selections'] = [copy.deepcopy(self.selection)]
        g.save_record(self.root, record)
        return record

    def report(self, record):
        return {'payload_sha256': record['payload_sha256'], 'reviewer': 'test-independent', 'verdict': 'pass',
                'required_fixes': [], 'checked_files': sorted(record['files']), 'checked_claims': ['C'],
                'checked_evidence': sorted(g.evidence_paths(record)), 'scope': 'TEST ONLY synthetic fixture coverage.',
                'all_material_claims_checked': True, 'chronology_checked': True, 'risk_classification_checked': True,
                'checked_risk_claims': [], 'risk_claim_checks': {}, 'checked_excluded_pages': [], 'exclusion_checks': {},
                'selection_checked': True, 'checked_selection_pages': sorted(s.keys(record)),
                'selection_checks': {key: {'inspection': 'full-page', 'regions_checked': True,
                    'qualifications_checked': True, 'restrictions_checked': True, 'appendices_checked': True,
                    'notes': 'TEST ONLY synthetic independent findings.'} for key in s.keys(record)}}

    def seal(self):
        g.seal(self.root, 'demo')
        return g.load_record(self.root, 'demo')

    def publish_fixture(self):
        self.prepare()
        record = self.seal()
        g.review(self.root, 'demo', self.report(record))
        g.apply(self.root, 'demo')
        return g.load_record(self.root, 'demo')

    def test_mixed_page_packet_retains_qualifications_restrictions_and_appendix(self):
        before = self.source.read_bytes()
        self.prepare()
        record = self.seal()
        packet = s.packet(self.root, 'demo')
        context = packet['claims'][0]['evidence'][0]['required_context']
        self.assertEqual(len(context), 2)
        self.assertIn('not clinical', context[0]['text'])
        self.assertIn('distribution only', context[1]['text'])
        self.assertTrue(record['claims'][0]['evidence'][0]['selection_span']['start'] > 0)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(s.export(self.root)['sources'], {})
        g.review(self.root, 'demo', self.report(record))
        g.apply(self.root, 'demo')
        selected = s.export(self.root)['sources'][self.raw]['spans']
        texts = [self.data[x['start']:x['end']].decode() for x in selected if x['kind'] == 'main-content']
        self.assertEqual(texts, ['Main model description.\n', 'Appendix describes independent validation.\n'])
        self.assertEqual(self.source.read_bytes(), before)

    def test_excluded_and_boilerplate_evidence_fail(self):
        for excerpt in ['Repeated header', 'Navigation advertisement only.',
                        'Main model description.\nMethod uses simulated tissue only; not clinical evidence.']:
            with self.subTest(excerpt=excerpt):
                record = self.prepare(excerpt=excerpt) if not g.record_path(self.root, 'demo').exists() else g.load_record(self.root, 'demo')
                record['claims'][0]['evidence'][0]['excerpt'] = excerpt
                g.save_record(self.root, record)
                with self.assertRaises(g.GateError):
                    self.seal()

    def test_partition_coverage_and_pending(self):
        mutations = [lambda p: p.pop(), lambda p: p[0].update(status='pending'),
                     lambda p: p[0].update(inspection='contact-sheet'),
                     lambda p: p[0]['spans'][1].update(start=p[0]['spans'][1]['start'] + 1),
                     lambda p: p[0]['spans'][1].update(end=p[0]['spans'][1]['end'] + 1),
                     lambda p: p[0]['spans'][1].update(requires=[]),
                     lambda p: p[2]['spans'][0].update(kind='main-content'),
                     lambda p: p[0]['spans'][0].update(kind='legal'),
                     lambda p: p[0]['spans'][0].update(reason='')]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                selection = copy.deepcopy(self.selection)
                mutate(selection['pages'])
                with self.assertRaises(g.GateError):
                    s.validate({'selections': [selection]}, lambda p: self.data, {})

    def test_every_page_requires_independent_review(self):
        self.prepare()
        record = self.seal()
        for key, value in [('selection_checked', False), ('checked_selection_pages', []), ('selection_checks', {})]:
            report = self.report(record)
            report[key] = value
            with self.subTest(key=key), self.assertRaises(g.GateError):
                g.review(self.root, 'demo', report)

    def test_real_mixed_region_selection_is_exact_not_whole_line(self):
        page = self.selection['pages'][0]
        main = page['spans'][1]
        boundary = main['start'] + len(b'Main ')
        page['spans'][1:2] = [
            {**main, 'id': 'prefix', 'end': boundary, 'kind': 'boilerplate', 'requires': []},
            {**main, 'start': boundary}]
        self.prepare(excerpt='model description.')
        record = self.seal()
        evidence = s.packet(self.root, 'demo')['claims'][0]['evidence'][0]
        self.assertEqual(evidence['excerpt'], 'model description.')
        self.assertEqual(evidence['selection_span']['start'], boundary)
        self.assertEqual(len(evidence['required_context']), 2)
        g.review(self.root, 'demo', self.report(record))
        g.apply(self.root, 'demo')
        spans = s.export(self.root)['sources'][self.raw]['spans']
        main = next(x for x in spans if x['id'] == 'S1')
        self.assertEqual(self.data[main['start']:main['end']], b'model description.\n')

    def test_reuse_inherits_region_restrictions(self):
        self.publish_fixture()
        self.prepare('reuse', 'Navigation advertisement only.')
        with self.assertRaises(g.GateError):
            g.seal(self.root, 'reuse')
        record = g.load_record(self.root, 'reuse')
        record['claims'][0]['evidence'][0]['excerpt'] = 'Appendix describes independent validation.'
        g.save_record(self.root, record)
        g.seal(self.root, 'reuse')
        record = g.load_record(self.root, 'reuse')
        self.assertEqual(record['selections'], [self.selection])
        self.assertEqual(len(record['claims'][0]['evidence'][0]['selection_context']), 2)
        g.review(self.root, 'reuse', self.report(record))
        g.apply(self.root, 'reuse')
        self.assertTrue(g.check(self.root)['ok'])
        self.assertIn(self.raw, s.export(self.root)['sources'])

    def test_source_and_receipt_drift_invalidates_export(self):
        self.publish_fixture()
        self.source.write_bytes(self.data + b'drift')
        with self.assertRaises(g.GateError):
            s.export(self.root)
        self.source.write_bytes(self.data)
        record = g.load_record(self.root, 'demo')
        record['selections'][0]['pages'][0]['spans'][0]['kind'] = 'main-content'
        g.save_record(self.root, record)
        with self.assertRaises(g.GateError):
            s.export(self.root)

    def test_context_pin_drift_blocks_apply(self):
        self.prepare()
        record = self.seal()
        g.review(self.root, 'demo', self.report(record))
        record = g.load_record(self.root, 'demo')
        record['claims'][0]['evidence'][0]['selection_context'] = []
        g.save_record(self.root, record)
        with self.assertRaises(g.GateError):
            g.apply(self.root, 'demo')

    def test_markers_only_exact_level_two(self):
        data = b'## Page 1\nOne\n### Page 2\nNot marker\n## Page 999 ###\nNot marker\n##  Page 998\nNot marker\n## Page 2 appendix\nNot marker\n## PDF page 2\nTwo\n'
        bounds = s.page_bounds(data)
        self.assertEqual([b['page'] for b in bounds], [1, 2])
        self.assertIn(b'Not marker', data[bounds[0]['start']:bounds[0]['end']])

    def test_utf8_byte_boundaries(self):
        data = b'## Page 1\n' + 'caf\N{LATIN SMALL LETTER E WITH ACUTE}\n'.encode()
        bound = s.page_bounds(data)[0]
        selection = {**self.selection, 'sha256': g.sha(data), 'pages': [{**bound, 'status': 'reviewed',
            'inspection': 'full-page', 'basis': 'TEST', 'spans': [
                {'id': 'A', 'start': bound['start'], 'end': bound['start'] + 4, 'kind': 'boilerplate', 'reason': 'TEST', 'requires': []},
                {'id': 'B', 'start': bound['start'] + 4, 'end': bound['end'], 'kind': 'boilerplate', 'reason': 'TEST', 'requires': []}]}]}
        with self.assertRaises(UnicodeError):
            s.validate({'selections': [selection]}, lambda p: data, {})

    def test_actual_python_export_to_node(self):
        self.publish_fixture()
        repo = Path(__file__).resolve().parents[1]
        # Module is from an isolated scratch copy prepared by the runner.
        script = self.root / 'retrieve.mjs'
        script.write_text("import {loadCorpus} from " + json.dumps((repo / '.vitepress/semantic/corpus.mjs').as_uri()) + ";\n"
            "import {buildPassages} from " + json.dumps((repo / '.vitepress/semantic/passages.mjs').as_uri()) + ";\n"
            "const c=loadCorpus({sourceRoot:process.argv[2],loadSelection:true}); console.log(JSON.stringify(buildPassages(c.pages)));\n")
        import shutil
        shutil.copytree(repo / '_tools', self.root / '_tools')
        result = subprocess.run(['node', str(script), str(self.root / 'src')], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        passages = [p for p in json.loads(result.stdout) if p['role'] == 'raw']
        self.assertEqual(len(passages), 2)
        self.assertEqual(passages[0]['requiredContext'][0]['text'], 'Method uses simulated tissue only; not clinical evidence.\n')
        self.assertEqual(passages[0]['requiredContext'][1]['text'], 'Internal distribution only.\n')
        self.assertFalse(any('Repeated header' in p['text'] for p in passages))
        for p in passages:
            self.assertEqual(self.data[p['byteStart']:p['byteEnd']].decode(), p['text'])


class VisualSelectionTests(unittest.TestCase):
    def test_native_original_binding_and_eligibility(self):
        from test_visual_ingestion_gate import VisualAdmissionTests
        fixture = VisualAdmissionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        record = fixture.prepare(text_only=True, crop=False)
        raw = (fixture.draft / fixture.raw).read_bytes()
        manifest = g.read_json(fixture.draft / fixture.manifest_path)
        selection = {'schema': s.SCHEMA, 'path': fixture.raw, 'sha256': g.sha(raw),
                     'original': manifest['original'], 'extraction': record['sources'][0]['extraction'],
                     'manifest': record['visuals'][0], 'pages': []}
        for bound in s.page_bounds(raw):
            selection['pages'].append({**bound, 'status': 'reviewed', 'inspection': 'full-page', 'basis': 'TEST native all-page fixture.',
                'spans': [{'id': 'P' + str(bound['page']), 'start': bound['start'], 'end': bound['end'],
                           'kind': 'main-content', 'reason': 'TEST native content including blank-page metadata.', 'requires': []}]})
        record['selections'] = [selection]
        g.save_record(fixture.root, record)
        g.seal(fixture.root, fixture.ingest)
        sealed = g.load_record(fixture.root, fixture.ingest)
        report = fixture.report(sealed)
        report.update(selection_checked=True, checked_selection_pages=sorted(s.keys(sealed)),
                      selection_checks={key: {'inspection': 'full-page', 'regions_checked': True, 'qualifications_checked': True,
                      'restrictions_checked': True, 'appendices_checked': True, 'notes': 'TEST native fixture.'} for key in s.keys(sealed)})
        g.review(fixture.root, fixture.ingest, report)
        g.apply(fixture.root, fixture.ingest)
        self.assertIn(fixture.raw, s.export(fixture.root)['sources'])
        selection = copy.deepcopy(selection)
        selection['pages'][0]['status'] = 'excluded'
        selection['pages'][0]['spans'][0]['kind'] = 'boilerplate'
        with self.assertRaises(g.GateError):
            s.validate({'selections': [selection], 'visuals': record['visuals']},
                       lambda p: (fixture.root / p).read_bytes(), {fixture.manifest_path: manifest})


if __name__ == '__main__':
    unittest.main()
