"""Scratch-only routing regressions. Mock transport is NOT live delivery proof."""
import base64
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ingestion_gate as g
import ingestion_orchestrator as o
import model_routing as r
from PIL import Image


def png(size=(40, 30), mode='RGB'):
    buf = io.BytesIO()
    Image.new(mode, size, 'white').save(buf, 'PNG')
    return buf.getvalue()


def payload(images):
    return {'input':[{'role':'user','content':[{'type':'input_image',
            'detail':'high',
            'image_url':'data:image/png;base64,'+base64.b64encode(p).decode()} for p in images]}]}


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = self.root/'runtime'
        (self.runtime/'agent').mkdir(parents=True)
        for name in ['auxiliary_client.py','codex_responses_adapter.py','codex_runtime.py','sdk_transform_bypass.py']:
            (self.runtime/'agent'/name).write_text('# TEST synthetic runtime pin only\n')

    def test_adapter_and_runtime_code_cache_identity(self):
        first=r.adapter_version(self.runtime)
        (self.runtime/'agent/auxiliary_client.py').write_text('# changed TEST runtime\n')
        self.assertNotEqual(first,r.adapter_version(self.runtime))

    def test_strict_policy_stages_and_six_defects(self):
        self.assertEqual(r.detect({key: False for key in r.DEFECTS}), [])
        for key in r.DEFECTS:
            self.assertEqual(r.detect({k: k == key for k in r.DEFECTS}), [key])
        for findings in [{}, {key: 1 for key in r.DEFECTS}, {'confidence': 1.0}]:
            with self.assertRaises(g.GateError):r.detect(findings)
        for stage in r.DETERMINISTIC:
            self.assertIsNone(r.choose(stage)['model'])
        for stage in o.STAGES:
            for kind in r.KINDS-{'unknown'}:
                result=r.choose(stage,kinds=[kind],inspection_complete=True,permission=True)
                self.assertEqual(result['model'], r.STRONG)
                self.assertFalse(result['approval_authority'])
            for defect in r.DEFECTS:
                result=r.choose(stage,kinds=['prose'],defects=[defect],inspection_complete=True,permission=True)
                self.assertEqual(result['model'], r.STRONG)
                self.assertIn(defect,result['reason'])
            self.assertEqual(r.choose(stage,kinds=['prose'])['status'],'pending')
            self.assertEqual(r.choose(stage,kinds=['unknown'],permission=True,inspection_complete=True)['status'],'pending')
        for kwargs in [{'permission':1},{'inspection_complete':'yes'},{'defects':['high-confidence']},{'kinds':['unknown-model-kind']}]:
            with self.assertRaises(g.GateError): r.choose('triage',**kwargs)
        with self.assertRaises(g.GateError):r.choose('approve')

    def test_budget_durable_cap_and_invalid_entries(self):
        b=r.Budget(self.root/'budget')
        for count in range(1,21):
            self.assertEqual(b.reserve(r.STRONG,g.sha(str(count).encode())),count)
        with self.assertRaises(g.GateError):r.Budget(self.root/'budget').reserve(r.STRONG,g.sha(b'21'))
        with self.assertRaises(g.GateError):b.reserve('not-confirmed',g.sha(b'x'))
        p=self.root/'budget/budget.json'; p.write_text('[{"number":0}]')
        with self.assertRaises(g.GateError):b.reserve(r.STRONG,g.sha(b'x'))

    def test_actual_wire_pixel_decoding_and_limits(self):
        data=png(); rows, decoded=r.image_trace(payload([data]))
        self.assertEqual(decoded,[data]); self.assertEqual(rows[0]['sha256'],g.sha(data));self.assertEqual(rows[0]['dimensions'],[40,30])
        with self.assertRaises(g.GateError):r.image_trace(payload([png((1569,2))]))
        with self.assertRaises(g.GateError):r.image_trace(payload([png(mode='RGBA')]))
        with self.assertRaises(g.GateError):r.image_trace(payload([data]*9))
        with self.assertRaises(g.GateError):r.image_trace({'input':[{'content':[{'type':'input_image','image_url':'https://unapproved/image.png'}]}]})

    def test_no_economical_production_adapter(self):
        adapter=object.__new__(r.CodexTransport)
        with self.assertRaises(g.GateError):
            adapter({'provider':r.PROVIDER,'adapter_version':r.VERSION,'model':'gpt-6-luna','stage':'synthesis'},lambda _:b'')

    def test_catalog_requires_live_strong_and_route(self):
        for catalog in [{},{'live_catalog':True},{'live_catalog':True,'strong_available':True,'route_sha256':'not-hash'}]:
            with self.assertRaises(g.GateError):r.CodexTransport(catalog=catalog,budget=self.root/'budget',trace_dir=self.root/'trace',runtime_root=self.root)

    def test_actual_packet_observations_and_resume_with_mock(self):
        import test_ingestion_orchestrator as fixtures
        f=fixtures.VisualOrchestrationTests();f.setUp();self.addCleanup(f.doCleanups)
        adapter=object.__new__(r.CodexTransport)
        adapter.runtime_root=self.runtime
        snap=o.Store(f.store).get_json(f.snapshot)
        adapter.readable_hashes=tuple(meta['sha256'] for v in snap['visuals'] for meta in o.Store(f.store).get_json(v['packet_sha256'])['items'])
        calls=[]
        def fake(**kw):
            calls.append(kw)
            rows,decoded=r.image_trace(payload(kw['images']))
            return {'model':r.STRONG,'provider':r.PROVIDER,'route':'codex-test','text':'SYNTHETIC MOCK only',
                    'usage':{'input_tokens':None,'output_tokens':None},'trace':{'images':rows},'delivered_bytes':decoded}
        adapter.call=fake
        cfg={**fixtures.CFG,'provider':r.PROVIDER,'model':r.STRONG,'route':'codex-test','adapter_version':r.adapter_version(self.runtime)}
        first=o.run(f.store,f.snapshot,cfg,adapter)
        self.assertEqual(first['status'],'complete')
        self.assertTrue(o.run(f.store,f.snapshot,cfg,adapter)['cache_hit'])
        self.assertEqual(len(calls),1)
        self.assertTrue(all('complete_text_assets' in c['messages'][1]['content'] for c in calls))
        saved=o.Store(f.store).get_json(first['result_sha256'])
        self.assertTrue(all(d['verification']['verified'] for d in saved['deliveries']))
        self.assertEqual(len(saved['deliveries']),2)
        self.assertEqual(g.load_record(f.f.root,f.f.ingest)['state'],'sealed')


if __name__=='__main__':unittest.main()
