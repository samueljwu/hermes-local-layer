"""Synthetic scratch packets and fail-closed transport observations, not receipts."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import os

import pymupdf
from PIL import Image, ImageChops
import visual_evidence as ve
import visual_packet as packet


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='packet-test-', dir=os.environ['TMPDIR'])
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.original = 'src/raw/assets/packet-fixture/original.pdf'
        source = self.root / self.original
        source.parent.mkdir(parents=True)
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=612, height=792)
            page.insert_text((30, 30), 'SYNTHETIC TEST: Dose (mV) | Response (%)', fontsize=9)
            for n in range(45):
                page.insert_text((30, 70 + n * 12), f'{n + 1} | 25', fontsize=7)
            page.insert_text((30, 760), 'Footnote: synthetic denominator is 100; no source approval.', fontsize=8)
            page.insert_text((30, 700), 'Small label', fontsize=2)
            page.insert_text((30, 70), 'Hidden bad text layer: response 99', fontsize=7, render_mode=3)
            pdf.new_page(width=612, height=792)
            source.write_bytes(pdf.tobytes())
        self.manifest = self.root / 'src/raw/assets/packet-fixture/visual/manifest.json'
        ve.prepare(self.root, self.original, 'src/raw/assets/packet-fixture/visual', dpi=300)
        self.before = self.snapshot()
        self.region = {'page': 1, 'bbox': [100, 200, 1800, 2600],
                       'header': [100, 80, 1200, 150], 'footnotes': [100, 3080, 1500, 3200],
                       'min_label_pixels': 25, 'context_basis': 'TEST-only dose/response header and denominator footnote repeated; full-page context retained.'}

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): ve.sha(p.read_bytes())
                for p in self.root.rglob('*') if p.is_file() and 'packet' not in p.parts[-2:-1]}

    def prepare(self, **kwargs):
        return packet.prepare_packet(self.manifest, self.root / 'packet', [self.region], **kwargs)

    def observation(self, data, batch=0):
        ids = data['batches'][batch]
        indexed = {i['id']: i for i in data['items']}
        return {'packet_sha256': ve.sha((self.root / 'packet/packet.json').read_bytes()),
            'route': 'TEST-only observed route', 'observation_point': 'model-input-post-transform',
            'embed_usage': 100, 'embed_budget': 1000,
            'route_limits': copy.deepcopy(data['limits']),
            'items': [{'id': ident, 'sha256': indexed[ident]['sha256'],
                'dimensions': indexed[ident]['intended_dimensions'], 'transformations': [],
                'readable': True} for ident in ids]}

    def test_context_exact_tiles_overlap_and_repeated_headers_footnotes(self):
        result = self.prepare()
        data = packet.validate_packet(result['packet'])
        self.assertEqual(result['status'], 'prepared-unobserved')
        self.assertEqual([i['page'] for i in data['items'] if i['role'] == 'full-page-context-only'], [1, 2])
        details = [i for i in data['items'] if i['role'] == 'detail']
        self.assertGreater(len(details), 1)
        stored_path = self.root / details[0]['mappings'][0]['source']['path']
        with Image.open(stored_path) as stored:
            for item in details:
                self.assertLessEqual(max(item['intended_dimensions']), 1568)
                self.assertEqual([m['component'] for m in item['mappings']], ['header', 'body', 'footnotes'])
                with Image.open(self.root / 'packet' / item['path']) as payload:
                    for mapping in item['mappings']:
                        with stored.crop(mapping['source_bbox']) as expected, payload.crop(mapping['payload_bbox']) as actual:
                            self.assertIsNone(ImageChops.difference(expected, actual).getbbox())
                        self.assertEqual(mapping['scale'], [1, 1])
        bodies = [i['mappings'][1]['source_bbox'] for i in details]
        self.assertTrue(any(a[0] == b[0] and 0 < a[3] - b[1] <= 64 for a, b in zip(bodies, bodies[1:])))
        for rel, digest in self.before.items():
            self.assertEqual(ve.sha((self.root / rel).read_bytes()), digest)
        self.assertIn('response 99', (self.manifest.parent / 'extraction.txt').read_text())
        # Hidden native text never becomes a visible packet label. Pixel crops
        # are checked above against the original-derived raster, not the text.

    def test_source_small_labels_cannot_be_upsampled_or_claimed_readable(self):
        self.region['min_label_pixels'] = 8
        with self.assertRaisesRegex(ve.VisualError, '12 pixels'):
            self.prepare()
        self.assertFalse((self.root / 'packet').exists())

    def test_low_dpi_rejected_and_header_context_too_wide_rejected(self):
        self.region['header'] = [0, 0, 2000, 100]
        with self.assertRaisesRegex(ve.VisualError, 'readable width'):
            self.prepare()
        self.region['header'] = [100, 80, 1200, 150]
        path = self.root / 'low/src/raw/assets/packet-fixture/original.pdf'
        path.parent.mkdir(parents=True)
        path.write_bytes((self.root / self.original).read_bytes())
        low = path.parents[4]
        ve.prepare(low, self.original, 'src/raw/assets/packet-fixture/visual', dpi=150)
        with self.assertRaisesRegex(ve.VisualError, '300 DPI'):
            packet.prepare_packet(low / 'src/raw/assets/packet-fixture/visual/manifest.json', self.root / 'low-packet', [self.region])

    def test_all_batches_require_observed_pixels_readability_and_embed_budget(self):
        result = self.prepare(max_images=3)
        data = packet.validate_packet(result['packet'])
        for index in range(len(data['batches'])):
            observation = self.observation(data, index)
            read = lambda ident: (self.root / 'packet' / (ident + '.png')).read_bytes()
            self.assertTrue(packet.verify_delivery(result['packet'], index, observation, read)['verified'])
            for field, value in [('embed_usage', 1001), ('observation_point', 'stored-on-disk'), ('items', [])]:
                bad = copy.deepcopy(observation); bad[field] = value
                with self.subTest(field=field), self.assertRaises(ve.VisualError):
                    packet.verify_delivery(result['packet'], index, bad, read)
            for field in ('max_edge', 'max_image_bytes', 'max_batch_bytes', 'max_images'):
                bad = copy.deepcopy(observation); bad['route_limits'][field] = 1
                with self.subTest(route_limit=field), self.assertRaises(ve.VisualError):
                    packet.verify_delivery(result['packet'], index, bad, read)
            for item in observation['items']:
                indexed = {i['id']: i for i in data['items']}
                if indexed[item['id']]['role'] == 'detail':
                    item['readable'] = False
                    with self.assertRaisesRegex(ve.VisualError, 'unreadable'):
                        packet.verify_delivery(result['packet'], index, observation, read)
                    item['readable'] = True

    def test_downsized_reencoded_or_substituted_delivery_rejected(self):
        result = self.prepare()
        data = packet.validate_packet(result['packet'])
        observation = self.observation(data)
        def altered(ident):
            with Image.open(self.root / 'packet' / (ident + '.png')) as image:
                stream = io.BytesIO()
                image.resize((image.width // 2, image.height // 2)).save(stream, format='PNG')
                return stream.getvalue()
        with self.assertRaisesRegex(ve.VisualError, 'transformed/substituted'):
            packet.verify_delivery(result['packet'], 0, observation, altered)

    def test_budget_pending_manifest_original_hash_and_canonical_output_boundaries(self):
        with self.assertRaisesRegex(ve.VisualError, 'embed byte budget'):
            self.prepare(max_image_bytes=10, max_batch_bytes=10)
        self.assertFalse((self.root / 'packet').exists())
        marker = self.root / '_meta/ingestion/baseline.json'
        marker.parent.mkdir(parents=True)
        marker.write_text('TEST-only canonical marker')
        with self.assertRaisesRegex(ve.VisualError, 'canonical wiki'):
            self.prepare()

    def test_packet_drift_and_batch_duplicate_are_rejected(self):
        result = self.prepare()
        path = Path(result['packet'])
        data = json.loads(path.read_bytes())
        data['batches'][0].append(data['batches'][0][0])
        path.write_text(json.dumps(data))
        with self.assertRaises(ve.VisualError):
            packet.validate_packet(path)


if __name__ == '__main__':
    unittest.main()
