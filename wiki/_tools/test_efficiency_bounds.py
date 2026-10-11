"""Synthetic operation-local bounds; mocks are not delivery or review evidence."""
from collections import Counter
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import ingestion_gate as g
import ingestion_orchestrator as o
import model_routing as r
import visual_evidence as ve
import visual_packet as vp
import test_visual_packet as fixtures


class PixelBoundsTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.PacketTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_fidelity_reads_one_page_per_consecutive_detail_group(self):
        result = self.f.prepare()
        packet = json.loads(Path(result['packet']).read_bytes())
        manifest = json.loads(self.f.manifest.read_bytes())
        reads = Counter()
        original = ve._read
        def read(root, path, *args):
            reads[path] += 1
            return original(root, path, *args)
        with patch.object(ve, '_read', side_effect=read):
            vp._validate_fidelity(packet, Path(result['packet']).parent, self.f.root, manifest)
        for page in manifest['pages']:
            self.assertEqual(reads[page['render']['path']], 2 if page['page'] == 1 else 1)
        self.assertGreater(len([i for i in packet['items'] if i['role'] == 'detail']), 1)

    def test_page_reader_closes_before_switch_and_after_exception(self):
        manifest = json.loads(self.f.manifest.read_bytes())
        first = second = None
        with self.assertRaisesRegex(RuntimeError, 'TEST failure'):
            with vp._page_reader(self.f.root, manifest) as read:
                first = read(1)
                self.assertIs(read(1), first)
                second = read(2)
                with self.assertRaises(ValueError):
                    first.getpixel((0, 0))
                raise RuntimeError('TEST failure')
        with self.assertRaises(ValueError):
            second.getpixel((0, 0))

    def test_region_order_switches_preserved_without_global_reuse(self):
        manifest = json.loads(self.f.manifest.read_bytes())
        regions = [self.f.region, {**self.f.region, 'page': 2}, copy.deepcopy(self.f.region)]
        result = vp.prepare_packet(self.f.manifest, self.f.root / 'ordered', regions)
        packet = vp.validate_packet(result['packet'])
        pages = [i['page'] for i in packet['items'] if i['role'] == 'detail']
        self.assertEqual(pages, [row[0] for row in vp._detail_plan(manifest, regions, 1568, 64)])
        data = (self.f.root / manifest['pages'][0]['render']['path']).read_bytes()
        with vp._page_reader(self.f.root, manifest) as read:
            a = read(1)
        with vp._page_reader(self.f.root, manifest) as read:
            b = read(1)
            self.assertIsNot(a, b)
        self.assertEqual((self.f.root / manifest['pages'][0]['render']['path']).read_bytes(), data)


class AdapterBoundsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = self.root / 'runtime'
        (self.runtime / 'agent').mkdir(parents=True)
        for name in ('auxiliary_client.py', 'codex_responses_adapter.py', 'codex_runtime.py', 'sdk_transform_bypass.py'):
            (self.runtime / 'agent' / name).write_text('# TEST-only runtime pin\n')
        self.adapter = object.__new__(r.CodexTransport)
        self.adapter.runtime_root = self.runtime
        self.adapter.readable_hashes = ()
        self.calls = []
        self.adapter.call = lambda **kw: self.calls.append(kw)

    def request(self, count, image_bytes=100):
        items = [{'id': f'item-{i:06d}', 'sha256': g.sha(str(i).encode()), 'encoded_bytes': image_bytes,
                  'intended_dimensions': [20, 20]} for i in range(count)]
        packet = {'items': items, 'batches': [[i['id'] for i in items[j:j+2]] for j in range(0, count, 2)]}
        data = {'visuals': [{'packet_sha256': g.sha(b'packet')}], 'assets': {}, 'base_assets': {}}
        request = {'model': r.STRONG, 'provider': r.PROVIDER, 'stage': 'independent-review',
                   'route': 'TEST-only', 'adapter_version': r.adapter_version(self.runtime),
                   'messages': [{'role': 'system', 'content': o.INSTRUCTIONS},
                                {'role': 'user', 'content': json.dumps(data)}]}
        return request, packet

    def test_over_image_envelope_refuses_before_any_image_blob_or_send(self):
        request, packet = self.request(30)
        reads = []
        def read(pin):
            reads.append(pin)
            if pin == g.sha(b'packet'):
                return g.canonical(packet)
            self.fail('oversized request must not load image blobs')
        with self.assertRaises(o.AdapterNotSent):
            self.adapter(request, read)
        self.assertEqual(reads, [g.sha(b'packet')])
        self.assertEqual(self.calls, [])

    def test_byte_and_dimension_budgets_refuse_before_image_loading(self):
        for count, size, dimensions in [(8, 2*1024*1024, [20, 20]), (1, 2*1024*1024+1, [20, 20]),
                                        (1, 100, [1569, 20])]:
            with self.subTest(count=count, size=size):
                request, packet = self.request(count, size)
                for item in packet['items']:
                    item['intended_dimensions'] = dimensions
                def read(pin):
                    self.assertEqual(pin, g.sha(b'packet'))
                    return g.canonical(packet)
                with self.assertRaises(o.AdapterNotSent):
                    self.adapter(request, read)
        self.assertEqual(self.calls, [])

    def test_incomplete_or_reordered_plan_refuses_without_truncation(self):
        for change in ('missing', 'duplicate', 'reorder'):
            with self.subTest(change=change):
                request, packet = self.request(4)
                if change == 'missing':
                    packet['batches'].pop()
                elif change == 'duplicate':
                    packet['batches'][0].append(packet['batches'][0][0])
                else:
                    packet['batches'].reverse()
                def read(pin):
                    self.assertEqual(pin, g.sha(b'packet'))
                    return g.canonical(packet)
                with self.assertRaises(o.AdapterNotSent):
                    self.adapter(request, read)
        self.assertEqual(self.calls, [])


class ReviewBoundaryTests(unittest.TestCase):
    def test_real_prepared_request_over_envelope_stays_pending_not_sent(self):
        import test_ingestion_orchestrator as fixtures
        fixture = fixtures.VisualOrchestrationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        adapter = object.__new__(r.CodexTransport)
        adapter.runtime_root = Path('/home/hermes/.hermes/hermes-agent')
        calls = []
        adapter.call = lambda **kw: calls.append(kw)
        cfg = {**fixtures.CFG, 'provider': r.PROVIDER, 'model': r.STRONG,
               'adapter_version': r.adapter_version(adapter.runtime_root), 'stage': 'independent-review'}
        with patch.dict(r.WIRE_LIMITS, max_images=3):
            result = o.run(fixture.store, fixture.snapshot, cfg, adapter)
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(result['remote_outcome'], 'not-sent')
        self.assertEqual(calls, [])
        self.assertEqual(g.load_record(fixture.f.root, fixture.f.ingest)['state'], 'sealed')

    def test_policy_drift_invalidates_snapshot_without_rewriting_inputs(self):
        import test_ingestion_orchestrator as fixtures
        fixture = fixtures.OrchestrationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        policy = Path(o.__file__).resolve().parent.parent / 'INGESTION.md'
        original = Path.read_bytes
        def changed(path):
            data = original(path)
            return data + b'\nTEST-only policy drift\n' if path == policy else data
        self.assertEqual(o.toolchain_pins()['INGESTION.md'], g.sha(original(policy)))
        with patch.object(Path, 'read_bytes', changed):
            with self.assertRaisesRegex(g.GateError, 'toolchain'):
                o.validate_snapshot(o.Store(fixture.store), fixture.digest)

    def test_cross_section_pass_is_trusted_instruction_not_generated_attestation(self):
        for required in ('cross-section completeness, chronology', 'contradiction pass',
                         'definitions, qualifications and unresolved issues',
                         'complete originals and final/base files', 'stays pending'):
            self.assertIn(required, o.INSTRUCTIONS)
        self.assertNotIn('checked', o.INSTRUCTIONS)


if __name__ == '__main__':
    unittest.main()
