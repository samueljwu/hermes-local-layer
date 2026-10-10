"""R1/R2 scratch-only adversarial regressions, not authentic source approval."""
import copy
import json
import unittest
from pathlib import Path

import ingestion_gate as gate
import visual_evidence as ve
import visual_packet as packet
import test_visual_ingestion_gate as fixtures
import test_visual_packet as packets


class InheritedRiskTests(unittest.TestCase):
    def reuse(self, risk='quantitative', excluded=False):
        case = fixtures.VisualAdmissionTests('runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)
        record = case.prepare()
        if excluded:
            case.change_manifest(record, lambda m: m['pages'][1].update(review={
                'status': 'excluded', 'inspection': 'full-page', 'classification': ['blank'],
                'notes': 'TEST-only blank inspected', 'exclusion_reason': 'TEST-only scope'}))
        gate.seal(case.root, case.ingest)
        record = gate.load_record(case.root, case.ingest)
        gate.review(case.root, case.ingest, case.report(record))
        gate.apply(case.root, case.ingest)
        manifest = gate.read_json(case.root / case.manifest_path)
        target = case.root / '.ingest-work/revision/src/concepts/test.md'
        target.parent.mkdir(parents=True)
        text = 'The fixture response remains 25.' if risk == 'quantitative' else 'The fixture has a source-bound interpretation.'
        target.write_text(text + '\n')
        revision = gate.stage(case.root, 'revision', 'synthetic-second-writer')
        pin = manifest['crops'][0]['render']
        revision['claims'] = [{'id': 'C1', 'text': text, 'kind': 'source-statement',
            'destinations': ['src/concepts/test.md'], 'qualifications': 'TEST only; no source approval.',
            'risks': [risk], 'risk_basis': 'TEST-only inherited risk interpretation.',
            'evidence': [{'path': gate.native_extraction_path(manifest),
                'sha256': gate.sha((case.root / gate.native_extraction_path(manifest)).read_bytes()),
                'locator': '## PDF page 1', 'excerpt': '25',
                'details': [{'page': 1, 'crop': manifest['crops'][0]['id'], **pin}]}]}]
        return case, revision, manifest

    def test_inherited_quantitative_equation_topology_seal_review_apply(self):
        for risk in ('quantitative', 'equation', 'topology'):
            with self.subTest(risk=risk):
                case, revision, manifest = self.reuse(risk)
                before = {p: gate.sha((case.root / p).read_bytes()) for p in gate.inventory(case.root) if p.startswith('src/raw/')}
                self.assertEqual(revision['visuals'], [])
                gate.save_record(case.root, revision)
                gate.seal(case.root, 'revision')
                sealed = gate.load_record(case.root, 'revision')
                detail = revision['claims'][0]['evidence'][0]['details'][0]['path']
                self.assertIn(detail, gate.evidence_paths(sealed))
                self.assertEqual(len(sealed['visuals']), 1)
                report = case.report(sealed)
                for mode in ('detail', 'pages', 'pixels', 'risk'):
                    bad = copy.deepcopy(report)
                    if mode == 'detail': bad['checked_evidence'].remove(detail)
                    if mode == 'pages': bad['checked_visual_pages'] = []
                    if mode == 'pixels': bad['visuals_checked'] = False
                    if mode == 'risk': bad['risk_claim_checks']['C1']['pixel_detail_checked'] = False
                    with self.subTest(mode=mode), self.assertRaises(gate.GateError):
                        gate.review(case.root, 'revision', bad)
                gate.review(case.root, 'revision', report)
                gate.apply(case.root, 'revision')
                self.assertTrue(gate.check(case.root)['ok'])
                self.assertEqual(before, {p: gate.sha((case.root / p).read_bytes()) for p in before})

    def test_inherited_invalid_details_fail_closed(self):
        case, base, manifest = self.reuse(excluded=True)
        for mode in ('missing', 'stale', 'low-dpi', 'wrong-page', 'excluded', 'unknown-crop', 'invalid-manifest'):
            revision = copy.deepcopy(base)
            evidence = revision['claims'][0]['evidence'][0]
            detail = evidence['details'][0]
            if mode == 'missing': evidence['details'] = []
            if mode == 'stale': detail['sha256'] = '0' * 64
            if mode == 'low-dpi': detail.clear(); detail.update(page=1, **manifest['pages'][0]['render'])
            if mode == 'wrong-page': detail['page'] = 2
            if mode == 'excluded':
                evidence.update(locator='## PDF page 2', excerpt='No native text extracted', pages=[2])
                detail.clear(); detail.update(page=2, **manifest['pages'][1]['render'])
            if mode == 'unknown-crop': detail['crop'] = 'absent'
            if mode == 'invalid-manifest': evidence['manifest'] = {'path': 'bad', 'sha256': '0' * 64}
            gate.save_record(case.root, revision)
            with self.subTest(mode=mode), self.assertRaises(gate.GateError):
                gate.seal(case.root, 'revision')


    def test_inherited_payload_and_source_pins_remain_required_after_seal(self):
        case, revision, manifest = self.reuse()
        gate.save_record(case.root, revision)
        gate.seal(case.root, 'revision')
        sealed = gate.load_record(case.root, 'revision')
        for mode in ('visual-ref', 'manifest-pin', 'detail', 'page-assets', 'source-hash'):
            bad = copy.deepcopy(sealed)
            item = bad['claims'][0]['evidence'][0]
            if mode == 'visual-ref': bad['visuals'] = []
            if mode == 'manifest-pin': item['manifest']['sha256'] = '0' * 64
            if mode == 'detail': item['details'][0]['sha256'] = '0' * 64
            if mode == 'page-assets': item['page_assets'] = []
            if mode == 'source-hash': item['sha256'] = '0' * 64
            with self.subTest(mode=mode), self.assertRaises(gate.GateError):
                gate.validate_payload(case.root, bad, lambda p: gate.candidate_bytes(case.root, bad, p))
        path = case.root / case.manifest_path
        before = path.read_bytes()
        path.write_bytes(before + b'\n')
        with self.assertRaises(gate.GateError):
            gate.validate_payload(case.root, sealed, lambda p: gate.candidate_bytes(case.root, sealed, p))


class PacketFidelityTests(unittest.TestCase):
    def fixture(self):
        case = packets.PacketTests('runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)
        result = case.prepare()
        path = Path(result['packet'])
        return case, path, json.loads(path.read_bytes())

    def test_missing_details_shifted_coordinates_and_plan_mutations(self):
        case, path, base = self.fixture()
        for mode in ('all-details', 'gap', 'shift', 'overlap', 'order', 'component', 'header', 'footnotes',
                     'region-bounds', 'region-page', 'region-schema', 'region-label', 'region-basis',
                     'region-shift', 'original-pin', 'manifest-pin', 'extraction-pin', 'rotation',
                     'source-dimensions', 'overlap-setting', 'payload-offset', 'extra-component', 'empty-regions'):
            data = copy.deepcopy(base)
            details = [i for i in data['items'] if i['role'] == 'detail']
            body = next(m for m in details[0]['mappings'] if m['component'] == 'body')
            if mode == 'all-details': data['items'] = [i for i in data['items'] if i['role'] != 'detail']
            elif mode == 'gap': data['items'].remove(details[0])
            elif mode == 'shift':
                body['source_bbox'][1] += 1; body['source_bbox'][3] += 1
            elif mode == 'overlap':
                later = next(m for m in details[1]['mappings'] if m['component'] == 'body')
                later['source_bbox'][1] += 1; later['source_bbox'][3] += 1
            elif mode == 'order': data['items'][-1], data['items'][-2] = data['items'][-2], data['items'][-1]
            elif mode == 'component': details[0]['mappings'][0]['component'] = 'body'
            elif mode in ('header', 'footnotes'): details[0]['mappings'] = [m for m in details[0]['mappings'] if m['component'] != mode]
            elif mode == 'region-bounds': data['regions'][0]['bbox'][2] = 99999
            elif mode == 'region-page': data['regions'][0]['page'] = 99
            elif mode == 'region-schema': data['regions'][0].pop('header')
            elif mode == 'region-label': data['regions'][0]['min_label_pixels'] = 1
            elif mode == 'region-basis': data['regions'][0]['context_basis'] = ''
            elif mode == 'region-shift': data['regions'][0]['bbox'][1] += 1
            elif mode == 'original-pin': data['original']['sha256'] = '0' * 64
            elif mode == 'manifest-pin': data['manifest']['sha256'] = '0' * 64
            elif mode == 'extraction-pin': data['native_extraction_sha256'] = '0' * 64
            elif mode == 'rotation': data['source_pages'][0]['rotation'] = 90
            elif mode == 'source-dimensions': data['source_pages'][0]['stored_dimensions'][0] += 1
            elif mode == 'overlap-setting': data['overlap'] += 1
            elif mode == 'payload-offset': body['payload_bbox'][0] += 1
            elif mode == 'extra-component': details[0]['mappings'].append(copy.deepcopy(details[0]['mappings'][0]))
            elif mode == 'empty-regions': data['regions'] = []
            data['batches'] = [[i['id'] for i in data['items']]]
            path.write_text(json.dumps(data))
            with self.subTest(mode=mode), self.assertRaises(ve.VisualError):
                packet.validate_packet(path)
        path.write_text(json.dumps(base))
        self.assertTrue(packet.validate_packet(path))

    def test_context_only_synthetic_observation_cannot_verify(self):
        case, path, data = self.fixture()
        data['items'] = [i for i in data['items'] if i['role'] != 'detail']
        data['batches'] = [[i['id'] for i in data['items']]]
        path.write_text(json.dumps(data))
        observation = case.observation(data)
        for item in observation['items']: item['readable'] = False
        with self.assertRaises(ve.VisualError):
            packet.verify_delivery(path, 0, observation, lambda ident: (path.parent / (ident + '.png')).read_bytes())

    def test_rehashed_payload_pixel_substitution_is_rejected(self):
        from PIL import Image
        case, path, data = self.fixture()
        item = next(i for i in data['items'] if i['role'] == 'detail')
        asset = path.parent / item['path']
        with Image.open(asset) as image:
            image.putpixel((0, 0), (255, 0, 0))
            image.save(asset)
        item['sha256'] = ve.sha(asset.read_bytes())
        item['encoded_bytes'] = len(asset.read_bytes())
        path.write_text(json.dumps(data))
        with self.assertRaises(ve.VisualError): packet.validate_packet(path)

    def test_source_drift_after_preparation_rejected(self):
        case, path, data = self.fixture()
        original = case.root / case.original
        original.write_bytes(original.read_bytes() + b'TEST-only drift')
        with self.assertRaises(ve.VisualError): packet.validate_packet(path)


    def test_trusted_preparation_pins_and_location_reject_coordinated_drift(self):
        case, path, data = self.fixture()
        pin = ve.sha(path.read_bytes())
        packet.validate_packet(path, manifest_path=case.manifest, expected_sha256=pin)
        data['transport_note'] += ' TEST-only alteration'
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ve.VisualError, 'trusted preparation packet pin'):
            packet.validate_packet(path, manifest_path=case.manifest, expected_sha256=pin)
        with self.assertRaisesRegex(ve.VisualError, 'trusted preparation manifest location'):
            packet.validate_packet(path, manifest_path=case.root / 'absent/manifest.json')

    def test_multiple_regions_without_strips_and_nondefault_overlap(self):
        case = packets.PacketTests('runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)
        second = copy.deepcopy(case.region)
        second.update(page=2, bbox=[0, 0, 600, 600], header=None, footnotes=None)
        result = packet.prepare_packet(case.manifest, case.root / 'packet', [case.region, second], overlap=32, max_images=2)
        data = packet.validate_packet(result['packet'], manifest_path=case.manifest, expected_sha256=result['sha256'])
        self.assertEqual(data['status'], 'prepared-unobserved')
        self.assertEqual(data['items'][-1]['page'], 2)
        self.assertEqual([m['component'] for m in data['items'][-1]['mappings']], ['body'])


if __name__ == '__main__':
    unittest.main()
