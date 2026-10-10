"""Wiki-local strong-only routing and bounded authentic Codex transport.

No global configuration/auth writes, provider fallback, agent tools or admission.
Candidate discovery does not activate a route. Remote internal image processing
is not observable: traces cover final HTTP request bytes only.
"""
from __future__ import annotations
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

import ingestion_orchestrator as orchestration
import ingestion_gate as gate

VERSION = 'wiki-routing-v1'
PROVIDER = 'openai-codex'
STRONG = 'gpt-6.1-sol'
MAX_CALLS = 20
WIRE_LIMITS = {'max_edge': 1568, 'max_image_bytes': 2*1024*1024,
               'max_batch_bytes': 8*1024*1024, 'max_images': 8}
DEFECTS = {'pixel-native-disagreement', 'missing-units-headers', 'broken-reading-order',
           'unreadable-labels', 'ambiguous-date-speaker', 'conflicting-values'}
KINDS = {'prose', 'mixed', 'table', 'equation', 'diagram', 'scan', 'raster', 'unknown'}
DETERMINISTIC = {'hash', 'extract', 'render', 'validate', 'search'}


def adapter_version(runtime_root):
    """Persist cache identity for the actual installed conversion/stream code."""
    root = Path(runtime_root)
    paths = [Path(__file__), *(root / name for name in (
        'agent/auxiliary_client.py', 'agent/codex_responses_adapter.py',
        'agent/codex_runtime.py', 'agent/sdk_transform_bypass.py'))]
    pins = {str(path): gate.sha(path.read_bytes()) for path in paths}
    return VERSION + '-' + gate.sha(gate.canonical(pins))[:24]


def detect(observations):
    """Apply explicit strict inspection findings; do not invent pixel judgments.

    Upstream extractor/inspector supplies these observable defect booleans.
    Confidence scores and model-generated flags are not accepted evidence.
    Pixel/native comparison itself needs separately inspected original pixels.
    """
    gate.need(isinstance(observations, dict) and set(observations) == DEFECTS
              and all(type(value) is bool for value in observations.values()),
              'complete strict trusted inspection findings required')
    return sorted(key for key, value in observations.items() if value)


def choose(stage, *, kinds=(), defects=(), inspection_complete=False, permission=False):
    """Trusted local observations, never a model's confidence or source instructions.

    Uninspected/unknown evidence stays pending for human/strong pixel inspection.
    Every synthesis and independent-review route stays strong; economical
    activation is deliberately absent until separately reviewed measurements.
    """
    gate.need(stage in DETERMINISTIC | orchestration.STAGES, 'unknown route stage')
    gate.need(type(permission) is bool and type(inspection_complete) is bool, 'strict route flags required')
    gate.need(set(kinds) <= KINDS and set(defects) <= DEFECTS, 'unknown trusted evidence signal')
    if stage in DETERMINISTIC:
        return {'status': 'local', 'model': None, 'reason': 'deterministic-no-llm'}
    if not permission:
        return {'status': 'pending', 'model': None, 'reason': 'permission-required'}
    if not inspection_complete or not kinds or 'unknown' in kinds:
        return {'status': 'pending', 'model': STRONG, 'reason': 'inspection-required'}
    repair = set(defects) & {'unreadable-labels', 'missing-units-headers', 'broken-reading-order'}
    if repair:
        return {'status': 'pending', 'model': STRONG, 'reason': sorted(repair),
                'approval_authority': False, 'repair_required': True}
    return {'status': 'strong', 'model': STRONG,
            'reason': sorted(set(defects)) or ['economical-unmeasured-or-rejected'],
            'approval_authority': False}


class Budget:
    """Durable counted sends, including unknown outcomes; no implicit retries.

    Reserve under flock BEFORE transmission. A crash consumes the slot rather
    than resetting it. All experiments share one private ledger.
    """
    def __init__(self, path):
        self.root = orchestration.private_root(path)

    def reserve(self, model, request_sha256):
        gate.need(model in {STRONG, 'gpt-6-luna', 'gpt-5.6-luna'}, 'candidate allowlist exceeded')
        gate.need(gate.is_hash(request_sha256), 'request hash required')
        with orchestration.Store(self.root).lock():
            p = gate.safe_path(self.root, 'budget.json', missing=True)
            raw = p.read_bytes() if p.exists() else None
            rows = json.loads(raw, object_pairs_hook=gate.unique_object) if raw is not None else []
            gate.need(isinstance(rows, list) and len(rows) < MAX_CALLS, 'initial 20-call budget exhausted')
            for number, row in enumerate(rows, 1):
                gate.need(set(row) == {'number', 'model', 'request_sha256'} and row['number'] == number
                          and row['model'] in {STRONG, 'gpt-6-luna', 'gpt-5.6-luna'}
                          and gate.is_hash(row['request_sha256']), 'budget journal corrupt')
            rows.append({'number': len(rows)+1, 'model': model, 'request_sha256': request_sha256})
            gate.atomic_write(p, gate.canonical(rows), expected=gate.sha(raw) if raw is not None else None)
            p.chmod(0o600)
            return len(rows)


def image_trace(payload):
    """Decode images from final serialized HTTP body; no intended-payload fiction."""
    from PIL import Image
    rows, pixels = [], []
    for entry in payload.get('input', []):
        for part in entry.get('content', []):
            if part.get('type') != 'input_image':
                continue
            gate.need(part.get('detail') == 'high', 'detail fidelity hint changed/absent')
            url = part.get('image_url', '')
            gate.need(url.startswith('data:image/png;base64,'), 'only pinned inline PNG allowed')
            data = base64.b64decode(url.split(',', 1)[1], validate=True)
            gate.need(len(data) <= WIRE_LIMITS['max_image_bytes'], 'image byte budget exceeded')
            with Image.open(io.BytesIO(data)) as im:
                gate.need(im.format == 'PNG' and im.mode == 'RGB' and max(im.size) <= 1568,
                          'unreviewed delivered image format/dimensions')
                size = list(im.size)
            rows.append({'sha256': gate.sha(data), 'dimensions': size, 'transformations': []})
            pixels.append(data)
    gate.need(len(rows) <= WIRE_LIMITS['max_images'] and sum(map(len, pixels)) <= WIRE_LIMITS['max_batch_bytes'],
              'wire image count/byte budget exceeded')
    return rows, pixels


class CodexTransport:
    """Supported installed CodexAuxiliaryClient, read-only existing credentials.

    Constructing a credential-bound SDK client avoids runtime auto-refresh writes.
    Account-scoped catalog pins exact model names; adapter echoes are NOT proof
    of the actual returned model, so capture backend SSE model stamps separately.
    """
    def __init__(self, *, catalog, budget, trace_dir, runtime_root):
        self.catalog = catalog
        self.budget = Budget(budget)
        self.trace_dir = orchestration.private_root(trace_dir)
        self.runtime_root = Path(runtime_root)
        self.readable_hashes = ()
        gate.need(catalog.get('live_catalog') is True and catalog.get('strong_available') is True,
                  'verified live strong catalog required')
        gate.need(gate.is_hash(catalog.get('route_sha256')), 'verified existing route required')

    def call(self, *, model, messages, images=(), readable_hashes=()):
        """One SDK call, one counted wire send; unknown outcomes never replay here."""
        import sys
        if str(self.runtime_root) not in sys.path:
            sys.path.insert(0, str(self.runtime_root))
        import httpx
        from openai import OpenAI
        from hermes_cli.auth import resolve_codex_runtime_credentials
        from agent.codex_headers import codex_account_headers
        from agent.auxiliary_client import CodexAuxiliaryClient
        gate.need(model in self.catalog['models'] and model in {STRONG, 'gpt-6-luna', 'gpt-5.6-luna'},
                  'model not confirmed in bounded live catalog')
        gate.need(len(messages) == 2 and [m['role'] for m in messages] == ['system', 'user']
                  and all(isinstance(m['content'], str) for m in messages), 'instruction/data separation required')
        gate.need(len(images) <= 8, 'bounded image count exceeded')
        expected = [gate.sha(data) for data in images]
        gate.need(set(expected) <= set(readable_hashes), 'independent delivered-pixel inspection pins required')
        creds = resolve_codex_runtime_credentials(read_only=True)
        base = creds['base_url'].rstrip('/')
        gate.need(gate.sha(base.encode()) == self.catalog['route_sha256'], 'existing provider route changed')
        origin = urlsplit(base)
        gate.need(origin.hostname and not origin.username and not origin.password and not origin.query
                  and not origin.fragment, 'unsafe credential-bound route')
        trace = {'version': VERSION, 'provider': PROVIDER, 'requested_model': model,
                 'adapter_version': adapter_version(self.runtime_root),
                 'route_sha256': self.catalog['route_sha256'], 'observation_point': 'final-http-request-body',
                 'server_internal_transforms': 'unobservable', 'sdk_retries': 0, 'wire_sends': 0,
                 'images': [], 'actual_models': [], 'status': 'unknown'}
        delivered = []
        wire_bytes = b''
        class ObservedStream(httpx.SyncByteStream):
            def __init__(self, stream):
                self.stream, self.pending = stream, b''
            def __iter__(self):
                for chunk in self.stream:
                    self.pending += chunk
                    while b'\n' in self.pending:
                        line, self.pending = self.pending.split(b'\n', 1)
                        if line.startswith(b'data: '):
                            try:
                                event = json.loads(line[6:])
                                stamp = event.get('response', {}).get('model')
                                if isinstance(stamp, str) and stamp not in trace['actual_models']:
                                    trace['actual_models'].append(stamp)
                            except (ValueError, TypeError, AttributeError):
                                pass
                    gate.need(len(self.pending) <= 8*1024*1024, 'SSE line budget exceeded')
                    yield chunk
            def close(self):
                self.stream.close()
        def before(req):
            nonlocal wire_bytes, delivered
            # Do not persist headers/auth URLs or source text.
            target = urlsplit(str(req.url))
            gate.need((target.scheme, target.netloc) == (origin.scheme, origin.netloc)
                      and target.path == origin.path.rstrip('/')+'/responses' and not target.query,
                      'cross-origin/endpoint credential transmission refused')
            gate.need(trace['wire_sends'] == 0, 'implicit retransmission refused')
            wire_bytes = req.content
            payload = json.loads(wire_bytes)
            gate.need(payload.get('model') == model and payload.get('store') is False
                      and not payload.get('tools'), 'wire route/privacy/tools mismatch')
            gate.need(payload.get('instructions') == messages[0]['content'], 'trusted instructions changed')
            wire_input = payload.get('input')
            gate.need(isinstance(wire_input, list) and len(wire_input) == 1
                      and wire_input[0].get('role') == 'user', 'source role boundary changed')
            parts = wire_input[0].get('content', [])
            gate.need(parts and parts[0].get('type') == 'input_text'
                      and parts[0].get('text') == messages[1]['content']
                      and all(p.get('type') == 'input_image' for p in parts[1:]),
                      'source text transformed or unexpected wire content')
            rows, delivered = image_trace(payload)
            gate.need([r['sha256'] for r in rows] == expected, 'actual image substitution/transform/drop')
            trace['images'] = rows
            trace['body_sha256'] = gate.sha(wire_bytes)
            trace['call_number'] = self.budget.reserve(model, trace['body_sha256'])
            trace['wire_sends'] += 1
        def after(response):
            trace['http_status'] = response.status_code
            response.stream = ObservedStream(response.stream)
        client = httpx.Client(event_hooks={'request':[before], 'response':[after]},
                              follow_redirects=False, timeout=120, trust_env=False)
        raw = OpenAI(api_key=creds['api_key'], base_url=base, max_retries=0,
                     default_headers=codex_account_headers(creds['api_key']), http_client=client)
        adapter = CodexAuxiliaryClient(raw, model)
        content = [{'type':'text', 'text':messages[1]['content']}]
        content.extend({'type':'image_url', 'image_url':{'url':'data:image/png;base64,'+
                       base64.b64encode(data).decode(), 'detail':'high'}} for data in images)
        started = time.monotonic()
        try:
            result = adapter.chat.completions.create(model=model, messages=[messages[0], {'role':'user','content':content}],
                         timeout=120, extra_body={'reasoning':{'effort':'medium'}})
            gate.need(trace['actual_models'] == [model], 'backend model stamp absent/mismatched')
            gate.need(not result.choices[0].message.tool_calls, 'unexpected model tool calls')
            trace['status'] = 'response-received'
            usage = getattr(result, 'usage', None)
            usage_data = {'input_tokens': getattr(usage, 'prompt_tokens', None),
                          'output_tokens': getattr(usage, 'completion_tokens', None)}
            trace['usage'] = usage_data
            return {'text':result.choices[0].message.content, 'model':model, 'provider':PROVIDER,
                    'route':'codex-'+self.catalog['route_sha256'][:16], 'usage':usage_data,
                    'trace':trace, 'delivered_bytes':delivered}
        except Exception as exc:
            trace['error_type'] = type(exc).__name__
            if not trace['wire_sends']:
                raise orchestration.AdapterNotSent('local transport preflight refused') from None
            raise
        finally:
            trace['elapsed_seconds'] = time.monotonic()-started
            adapter.close()
            name = 'send-'+str(trace.get('call_number','unsent'))+'-'+str(time.time_ns())+'.json'
            gate.atomic_write(self.trace_dir/name, gate.canonical(trace), expected=None)

    def __call__(self, request, read_blob):
        gate.need(request['model'] == STRONG and request['stage'] in orchestration.STAGES,
                  'economical route is not activated for orchestration')
        gate.need(request['provider'] == PROVIDER
                  and request['adapter_version'] == adapter_version(self.runtime_root),
                  'request provider/adapter pin mismatch')
        data = json.loads(request['messages'][1]['content'])
        images, planned = [], []
        for visual in data['visuals']:
            packet = json.loads(read_blob(visual['packet_sha256']))
            for batch, ids in enumerate(packet['batches']):
                indexed = {i['id']: i for i in packet['items']}
                for ident in ids:
                    item = indexed[ident]
                    planned.append((visual, packet, batch, item))
                    images.append(read_blob(item['sha256']))
        # Caller must pin genuine prior independent inspection; never infer it
        # from source/model assertions, dimensions or hashes.
        pins = getattr(self, 'readable_hashes', ())
        extra = {}
        for path, meta in data['assets'].items():
            if path.endswith(('.md', '.txt')):
                extra[path] = {'sha256':meta['sha256'], 'text':read_blob(meta['sha256']).decode('utf-8')}
        for path, meta in data.get('base_assets', {}).items():
            if meta['sha256'] and path.endswith(('.md', '.txt')):
                extra['base:'+path] = {'sha256':meta['sha256'], 'text':read_blob(meta['sha256']).decode('utf-8')}
        augmented = [request['messages'][0], {'role':'user', 'content':gate.canonical({**data,'complete_text_assets':extra}).decode()}]
        response = self.call(model=request['model'], messages=augmented, images=images, readable_hashes=pins)
        gate.need(response['route'] == request['route'], 'actual requested route mismatch')
        deliveries, delivered = [], {}
        cursor = 0
        for visual in data['visuals']:
            packet = json.loads(read_blob(visual['packet_sha256']))
            for batch, ids in enumerate(packet['batches']):
                rows = []
                for ident in ids:
                    observed = response['trace']['images'][cursor]
                    delivered[(visual['packet_sha256'], batch, ident)] = response['delivered_bytes'][cursor]
                    rows.append({**observed, 'id':ident, 'readable':observed['sha256'] in pins})
                    cursor += 1
                deliveries.append({'packet_sha256':visual['packet_sha256'], 'batch':batch,
                    'route':response['route'], 'observation_point':'model-input-post-transform',
                    'observation_scope':'Final serialized HTTP input; server internal processing unobservable.',
                    'embed_usage':len(images), 'embed_budget':8, 'route_limits':dict(WIRE_LIMITS),
                    'route_limits_basis':'Conservative wiki-local accepted-request envelope, not a discovered provider maximum.', 'items':rows})
        return {'status':'complete', 'provider':PROVIDER, 'model':response['model'], 'route':response['route'],
                'result':{'text':response['text'], 'actual_transport_scope':'final-http-request-body'},
                'usage':response['usage'], 'deliveries':deliveries, 'delivered_bytes':delivered}
