"""Stage-1 scratch regressions. Synthetic receipts are never source approval."""
import copy
import json
import unittest
from typing import Any
from unittest import mock
from pathlib import Path

import ingestion_gate as gate
import visual_evidence as visual
import test_visual_ingestion_gate as visual_gate_fixtures
import test_visual_evidence as visual_fixtures
import test_ingestion_gate as gate_fixtures


class HardeningTests(unittest.TestCase):
    def fixture(self, kind: Any = visual_gate_fixtures.VisualAdmissionTests) -> Any:
        case = kind('runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def test_uninspected_exclusion_rejected(self):
        case = self.fixture(visual_fixtures.VisualEvidenceTests)
        _, _, manifest = case.prepare_pdf()
        case.approve_fixture(manifest)
        manifest['pages'][1]['review'] = {'status': 'excluded', 'inspection': None,
            'classification': [], 'notes': 'TEST-only scope', 'exclusion_reason': 'TEST-only omit'}
        with self.assertRaisesRegex(visual.VisualError, 'excluded'):
            case.valid(manifest, reviewed=True)

    def test_mixed_and_terminal_exclusions_require_full_page(self):
        case = self.fixture(visual_fixtures.VisualEvidenceTests)
        _, _, manifest = case.prepare_pdf()
        case.approve_fixture(manifest)
        for index, classes in ((0, ['prose']), (1, ['mixed']), (2, ['blank']), (1, ['uncertain'])):
            candidate = copy.deepcopy(manifest)
            candidate['pages'][index]['review'] = {'status': 'excluded', 'inspection': 'contact-sheet',
                'classification': classes, 'notes': 'TEST-only', 'exclusion_reason': 'TEST-only scope'}
            with self.subTest(index=index, classes=classes), self.assertRaises(visual.VisualError):
                case.valid(candidate, reviewed=True)

    def numeric_text(self, case, dpi=150):
        record = case.prepare(text_only=True, crop=False)
        claim = record['claims'][0]
        claim['text'] = 'The fixture response is 25.'
        claim['risks'] = ['quantitative']
        claim['risk_basis'] = 'TEST numeric claim in native text.'
        (case.draft / 'src/concepts/test.md').write_text(claim['text'] + '\n')
        claim['evidence'][0]['excerpt'] = '25'
        gate.save_record(case.root, record)
        return record

    def test_native_numeric_cannot_downgrade_pixel_detail(self):
        case = self.fixture()
        self.numeric_text(case)
        with self.assertRaisesRegex(gate.GateError, '300 DPI|detail'):
            gate.seal(case.root, case.ingest)

    def test_ocr_method_label_cannot_downgrade_numeric_detail(self):
        case = self.fixture()
        record = self.numeric_text(case)
        record['sources'][0]['method'] = 'TEST-only OCR sidecar declaration; same immutable fixture bytes.'
        gate.save_record(case.root, record)
        with self.assertRaisesRegex(gate.GateError, '300 DPI|detail'):
            gate.seal(case.root, case.ingest)

    def test_text_topology_requires_pixel_detail_without_numeric_characters(self):
        case = self.fixture()
        record = case.prepare(text_only=True, crop=False)
        record['claims'][0].update(risks=['topology'], risk_basis='TEST-only topology interpretation from native citation.')
        gate.save_record(case.root, record)
        with self.assertRaisesRegex(gate.GateError, '300 DPI|detail'):
            gate.seal(case.root, case.ingest)

    def test_native_numeric_pinned_detail_and_independent_checks_succeed(self):
        case = self.fixture()
        record = self.numeric_text(case)
        visual.crop(case.draft / case.manifest_path, 1, [0, 0, 144, 216], 'numeric-context')
        manifest = gate.read_json(case.draft / case.manifest_path)
        case.change_manifest(record, lambda m: None)
        pin = manifest['crops'][-1]['render']
        record['files'][pin['path']] = {'before': None, 'after': pin['sha256']}
        record['claims'][0]['evidence'][0]['details'] = [{'page': 1, 'crop': 'numeric-context', **pin}]
        gate.save_record(case.root, record)
        before = (case.draft / case.raw).read_bytes()
        gate.seal(case.root, case.ingest)
        record = gate.load_record(case.root, case.ingest)
        report = case.report(record)
        self.assertEqual(report['checked_visual_claims'], [])
        self.assertEqual(report['checked_risk_claims'], ['C1'])
        report['risk_claim_checks']['C1']['pixel_detail_checked'] = False
        with self.assertRaisesRegex(gate.GateError, 'pixel detail'):
            gate.review(case.root, case.ingest, report)
        report['risk_claim_checks']['C1']['pixel_detail_checked'] = True
        gate.review(case.root, case.ingest, report)
        gate.apply(case.root, case.ingest)
        self.assertTrue(gate.check(case.root)['ok'])
        self.assertEqual((case.root / case.raw).read_bytes(), before)

    def test_all_claims_need_explicit_risk_classification(self):
        case = self.fixture()
        record = case.prepare(text_only=True)
        record['claims'][0].pop('risks', None)
        record['claims'][0].pop('risk_basis', None)
        gate.save_record(case.root, record)
        with self.assertRaisesRegex(gate.GateError, 'risk'):
            gate.seal(case.root, case.ingest)

    def test_equation_text_requires_symbol_and_uncertainty_review(self):
        case = self.fixture()
        record = case.prepare(text_only=True)
        record['claims'][0].update(risks=['equation'], risk_basis='TEST equation risk without numeric digits.')
        # Full-page 300 DPI context, even when the citation is native text.
        visual.crop(case.draft / case.manifest_path, 1, [0, 0, 144, 216], 'equation-context')
        manifest = gate.read_json(case.draft / case.manifest_path)
        case.change_manifest(record, lambda m: None)
        item = record['claims'][0]['evidence'][0]
        item['details'] = [{'page': 1, 'crop': 'equation-context', **manifest['crops'][-1]['render']}]
        pin = manifest['crops'][-1]['render']
        record['files'][pin['path']] = {'before': None, 'after': pin['sha256']}
        gate.save_record(case.root, record)
        gate.seal(case.root, case.ingest)
        sealed = gate.load_record(case.root, case.ingest)
        report = case.report(sealed)
        report.pop('risk_claim_checks', None)
        report.pop('checked_risk_claims', None)
        with self.assertRaisesRegex(gate.GateError, 'risk'):
            gate.review(case.root, case.ingest, report)

    def test_v1_cannot_be_sealed_as_new_receipt(self):
        case = self.fixture()
        record = case.prepare(text_only=True)
        record['version'] = 1
        gate.save_record(case.root, record)
        with self.assertRaisesRegex(gate.GateError, 'record policy|version'):
            gate.seal(case.root, case.ingest)

    def test_packet_preparation_api_is_not_transport_attestation(self):
        case = self.fixture(visual_fixtures.VisualEvidenceTests)
        case.prepare_pdf()
        self.assertTrue(callable(getattr(visual, 'prepare_packet', None)), 'packet preparation API absent')

    def test_quantitative_checklist_cannot_mark_unit_denominator_uncertainty_inapplicable(self):
        case = self.fixture()
        case.prepare()
        gate.seal(case.root, case.ingest)
        record = gate.load_record(case.root, case.ingest)
        for key in ('values', 'units', 'denominator', 'uncertainty'):
            report = case.report(record)
            report['risk_claim_checks']['C1']['checks'][key] = 'not-applicable'
            with self.subTest(key=key), self.assertRaisesRegex(gate.GateError, 'relevant risk checks'):
                gate.review(case.root, case.ingest, report)

    def test_native_numeric_risk_cannot_be_declared_qualitative(self):
        case = self.fixture()
        record = self.numeric_text(case)
        record['claims'][0]['risks'] = []
        gate.save_record(case.root, record)
        with self.assertRaisesRegex(gate.GateError, 'risk classification omits'):
            gate.seal(case.root, case.ingest)

    def test_independent_exclusion_review_is_not_all_page_coverage_alone(self):
        case = self.fixture()
        record = case.prepare()
        case.change_manifest(record, lambda m: m['pages'][1].update(review={
            'status': 'excluded', 'inspection': 'full-page', 'classification': ['blank'],
            'notes': 'TEST full inspection', 'exclusion_reason': 'TEST scope boundary'}))
        gate.seal(case.root, case.ingest)
        record = gate.load_record(case.root, case.ingest)
        for field, value in [('checked_excluded_pages', []), ('exclusion_checks', {})]:
            report = case.report(record); report[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(gate.GateError, 'exclusion'):
                gate.review(case.root, case.ingest, report)

    def test_exact_existing_published_v1_pin_only_not_state_or_date(self):
        # Synthetic historical fixture: no real production exemption is added.
        case = self.fixture(gate_fixtures.GateTests)
        record = case.prepare()
        record['version'] = 1
        for claim in record['claims']:
            claim.pop('risks'); claim.pop('risk_basis')
        record.pop('visual_exclusions', None)
        record['payload_sha256'] = gate.sha(gate.canonical(gate.payload(record)))
        record['review'] = case.report(record)
        for key in ('risk_classification_checked', 'checked_risk_claims', 'risk_claim_checks', 'checked_excluded_pages', 'exclusion_checks'):
            record['review'].pop(key, None)
        record['review_sha256'] = gate.sha(gate.canonical(record['review']))
        record['state'] = 'published'
        pin = gate.sha(gate.canonical(record))
        with mock.patch.object(gate, 'LEGACY_PUBLISHED_RECEIPTS', {record['id']: pin}):
            gate.validate_review(record, historical=True)
            case.legacy.write_text('The source reports a model.\n')
            gate.save_record(case.root, record)
            self.assertTrue(gate.check(case.root)['ok'])
            draft = case.root / '.ingest-work/revision/src/concepts/legacy.md'
            draft.parent.mkdir(parents=True)
            draft.write_text('No measurement is demonstrated.\n')
            revision = gate.stage(case.root, 'revision', 'synthetic-second-writer')
            claim = copy.deepcopy(record['claims'][0])
            claim.update(text='No measurement is demonstrated.', risks=[], risk_basis='TEST-only qualitative model boundary.')
            revision['claims'] = [claim]
            gate.save_record(case.root, revision)
            gate.seal(case.root, 'revision')
            with self.assertRaises(gate.GateError):
                gate.validate_review(record)
            tampered = copy.deepcopy(record); tampered['created_at'] = '2000-01-01'
            with self.assertRaises(gate.GateError):
                gate.validate_review(tampered, historical=True)
            record['state'] = 'reviewed'
            with self.assertRaises(gate.GateError):
                gate.validate_review(record, historical=True)


if __name__ == '__main__':
    unittest.main()
