"""Network-blocked fixtures for the production connector and real canonical API."""
import contextlib
from copy import deepcopy
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
import uuid

import notion_sync as s
from notion_transport import State, SyncError, SyncBusy, Client, strict_json, query_all, QUERY_PATH

class BusyLockTests(unittest.TestCase):
    def test_busy_is_distinct_and_releases_failed_handles(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o700)
            with State(Path(tmp)):
                contender = State(Path(tmp))
                with self.assertRaises(SyncBusy):
                    with contender:
                        self.fail('overlapping connector entered')
                self.assertIsNone(contender.fd)
                self.assertIsNone(contender.lock)
            with State(Path(tmp)):
                pass


SAMPLES = [str(uuid.UUID(int=i)) for i in range(1, 4)]
COLS = {f: f for f in s.FIELDS}
OBSERVED_IDS = {'name': 'title', 'start_date': '%3BBh%3B', 'tag': 'Qcac',
                'recurrence': 'SYBU', 'status': 'US%5Cb', 'hermes_task_key': 'ZyNW',
                'priority': 'aBIM', 'due_date': 'h%5Cw%3F', 'notes': 'so%7DJ'}

def row(n=1, **changes):
    value = dict(id=f'T-1-{n}', name='Fixture work', tag='Other', status='not_started',
                 start_date='2026-05-18', due_date='2026-05-20', priority='medium',
                 recurrence=None, notes='', reminder=None, _revision=1)
    value.update(changes)
    return value

def page(value, n=10, mark=None):
    return {'object': 'page', 'id': str(uuid.UUID(int=n)), 'in_trash': False,
            'parent': {'type': 'data_source_id', 'data_source_id': s.SOURCE_ID},
            'last_edited_time': '2026-05-01T00:00:00.000Z',
            'properties': {**s.properties(s.fields(value), COLS),
                           s.MARKER: {'rich_text': s.rich_text(mark or '')}}}

class API:
    def __init__(self, pages=()):
        self.pages = {p['id']: deepcopy(p) for p in pages}
        self.calls = []
        self.property_ids = dict(OBSERVED_IDS)
        self.canonical_locked = lambda: bool(False)
        self.before_get = None
        self.after_patch = None
        self.after_create = None
        self.hide = set()
        self.page_size = 100
        self.counter = 100
    def request(self, method, path, body=None):
        assert not self.canonical_locked(), 'API call under canonical file lock'
        self.calls.append((method, path, deepcopy(body)))
        if method == 'GET' and path == '/databases/' + s.DB_ID:
            return {'object': 'database', 'id': s.DB_ID, 'in_trash': False}
        if method == 'GET' and path.startswith('/data_sources/'):
            props = {}
            for f, typ in {**s.TYPES, s.MARKER: 'rich_text'}.items():
                props[f] = {'id': self.property_ids[f], 'type': typ, 'name': f, typ: {}}
                if f in {'priority', 'status'}:
                    values = s.PRIORITIES if f == 'priority' else s.STATUSES
                    props[f][typ] = {'options': [{'name': v, 'id': v, 'description': None} for v in sorted(values)]}
            return {'object': 'data_source', 'id': s.SOURCE_ID, 'in_trash': False,
                    'parent': {'database_id': s.DB_ID}, 'properties': props}
        if path == QUERY_PATH:
            values = [p for pid, p in sorted(self.pages.items()) if pid not in self.hide]
            offset = int(body.get('start_cursor', '0'))
            more = offset + self.page_size < len(values)
            return {'results': deepcopy(values[offset:offset+self.page_size]), 'has_more': more,
                    'next_cursor': str(offset+self.page_size) if more else None}
        if path == '/pages' and method == 'POST':
            self.counter += 1
            p = page(row(), self.counter)
            p['properties'] = deepcopy(body['properties'])
            self.pages[p['id']] = p
            if self.after_create:
                self.after_create(p)
            return deepcopy(p)
        pid = path.split('/')[-1]
        if pid not in self.pages:
            raise SyncError('http-404')
        if method == 'GET':
            if self.before_get:
                self.before_get(pid)
            return deepcopy(self.pages[pid])
        if method == 'PATCH':
            self.pages[pid]['properties'].update(deepcopy(body['properties']))
            self.pages[pid]['last_edited_time'] += 'x'
            if self.after_patch:
                self.after_patch(pid)
            return deepcopy(self.pages[pid])
        raise AssertionError('unexpected fixture request')
    def edit(self, pid, **values):
        self.pages[pid]['properties'].update(s.properties(values, COLS))
        self.pages[pid]['last_edited_time'] += 'x'
    def mutations(self):
        return [c for c in self.calls if c[0] == 'PATCH' or c[:2] == ('POST', '/pages')]

class Memory:
    def __init__(self):
        self.value = None
        self.writes = 0
        self.crash = None
    def read(self):
        return deepcopy(self.value)
    def write(self, value):
        s.validate_state(value)
        self.value = deepcopy(value)
        self.writes += 1
        if self.crash:
            self.crash(value)

class Canonical:
    def __init__(self, rows):
        self.rows = deepcopy(rows)
        self.receipts = {}
        self.calls = []
        self.after_commit = None
    def read(self):
        return deepcopy(self.rows)
    def edit(self, **values):
        self.rows[0].update(values)
        self.rows[0]['_revision'] += 1
    def apply_external_change(self, **kw):
        self.calls.append(deepcopy(kw))
        op = kw['operation_id']
        if op in self.receipts:
            return deepcopy(self.receipts[op])
        if kw['task_id'] is None:
            r = row(len(self.rows)+1, _revision=0, _notion_page_id=kw['origin_page_id'])
            self.rows.append(r)
        else:
            r = next(r for r in self.rows if s.key(r) == s.key({'id': kw['task_id']}))
            if s.revision(r) != kw['expected_revision']:
                raise ValueError('fixture conflict with private text')
        r.update(kw['fields'])
        r['_revision'] += 1
        self.receipts[op] = deepcopy(r)
        if self.after_commit:
            self.after_commit()
        return deepcopy(r)

class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.local = Canonical([row()])
        self.pid = str(uuid.UUID(int=10))
        self.api = API([page(row(), mark=s.key(row()))])
        self.store = Memory()
        s.initialize(self.api, self.local, self.store, ignored=SAMPLES, apply=True)
        self.api.calls.clear()
    def sync(self, **kw):
        return s.reconcile(self.api, self.local, self.store, **kw)
    def test_replacement_same_name_column_rejected_before_writes(self):
        self.api.edit(self.pid, start_date=None, due_date=None, recurrence=None, notes='')
        before = self.store.read()
        writes = self.store.writes
        for field in OBSERVED_IDS:
            with self.subTest(field=field):
                self.api.property_ids = dict(OBSERVED_IDS, **{field: 'replacement-id'})
                with self.assertRaisesRegex(SyncError, 'schema-property-mismatch'):
                    self.sync(apply=True)
                fresh = Memory()
                with self.assertRaisesRegex(SyncError, 'schema-property-mismatch'):
                    s.initialize(self.api, self.local, fresh, ignored=SAMPLES, apply=True)
                self.assertIsNone(fresh.read())
        self.assertEqual(self.store.read(), before)
        self.assertEqual(self.store.writes, writes)
        self.assertFalse(self.api.mutations())
        self.assertFalse(self.local.calls)

    def test_dry_run_never_writes(self):
        self.local.edit(notes='local')
        before = self.store.writes
        result = self.sync()
        self.assertEqual(result['planned'], 1)
        self.assertEqual(self.store.writes, before)
        self.assertEqual(self.api.mutations(), [])
        self.assertEqual(self.local.calls, [])
    def test_local_remote_and_equal_whole_record(self):
        self.local.edit(notes='local', start_date=None)
        self.assertEqual(self.sync(apply=True)['applied'], 1)
        payload = self.api.mutations()[-1][2]['properties']
        self.assertEqual(set(payload), {'notes', 'start_date'})
        self.api.edit(self.pid, name='remote', tag='Admin', priority='high', due_date=None, start_date=None, status='in_progress', recurrence=None, notes='remote')
        self.assertEqual(self.sync(apply=True)['applied'], 1)
        self.assertEqual(self.local.rows[0]['name'], 'remote')
        self.assertIsNone(self.local.rows[0]['due_date'])
        self.assertEqual(self.sync(apply=True)['planned'], 0)
        self.local.edit(notes='same')
        self.api.edit(self.pid, notes='same')
        count = len(self.api.mutations())
        self.assertEqual(self.sync(apply=True)['applied'], 1)
        self.assertEqual(len(self.api.mutations()), count)
    def test_both_different_disjoint_fields_conflict(self):
        self.local.edit(notes='private local')
        self.api.edit(self.pid, priority='high')
        result = self.sync(apply=True)
        self.assertEqual(result['conflicts'], {s.key(row()): 'both-changed'})
        self.assertNotIn('private local', json.dumps(result))
        self.assertFalse(self.api.mutations())
    def test_init_divergence_requires_verified_seed(self):
        self.api.edit(self.pid, notes='remote')
        store = Memory()
        result = s.initialize(self.api, self.local, store, ignored=SAMPLES, apply=True)
        self.assertTrue(result['conflicts'])
        self.assertIsNone(store.value)
        s.initialize(self.api, self.local, store, ignored=SAMPLES, apply=True,
                     verified_baselines={s.key(row()): s.fields(row())})
        self.assertIsNotNone(store.value)
    def test_crash_after_canonical_commit_replays_same_operation(self):
        self.api.edit(self.pid, notes='remote')
        self.local.after_commit = lambda: (_ for _ in ()).throw(OSError('crash'))
        with self.assertRaises(OSError):
            self.sync(apply=True)
        original = self.store.value['pending'][s.key(row())]['operation_id']
        self.local.after_commit = None
        self.assertEqual(self.sync(apply=True)['applied'], 1)
        self.assertEqual(len(self.local.receipts), 1)
        self.assertEqual(self.local.calls[-1]['operation_id'], original)
        self.assertEqual(self.local.rows[0]['_revision'], 2)
    def test_patch_lost_ack_replay_without_second_patch(self):
        self.local.edit(notes='edit')
        self.api.after_patch = lambda _: (_ for _ in ()).throw(OSError('lost ack'))
        with self.assertRaises(OSError):
            self.sync(apply=True)
        self.api.after_patch = None
        self.assertEqual(self.sync(apply=True)['applied'], 1)
        self.assertEqual(len(self.api.mutations()), 1)
    def test_create_lost_ack_recovers_marker(self):
        self.local.rows.append(row(2))
        self.api.after_create = lambda _: (_ for _ in ()).throw(OSError('lost ack'))
        with self.assertRaises(OSError):
            self.sync(apply=True)
        self.api.after_create = None
        self.assertEqual(self.sync(apply=True)['applied'], 1)
        self.assertEqual(len(self.api.mutations()), 1)
        self.assertEqual(len(self.store.value['bindings']), 2)
    def test_ambiguous_create_absent_never_retries(self):
        self.local.rows.append(row(2))
        def fail(p):
            self.api.hide.add(p['id'])
            raise OSError('lost')
        self.api.after_create = fail
        with self.assertRaises(OSError):
            self.sync(apply=True)
        result = self.sync(apply=True)
        self.assertEqual(result['conflicts'][s.key(row(2))], 'ambiguous-create-needs-operator')
        self.assertEqual(len(self.api.mutations()), 1)
    def test_intake_blank_closed_samples_caps_and_replay(self):
        for n in range(20, 24):
            p = page(row(n), n)
            self.api.pages[p['id']] = p
        blank = page(row(), 30)
        blank['properties'] = {'name': {'title': []}, s.MARKER: {'rich_text': []}}
        self.api.pages[blank['id']] = blank
        closed = page(row(status='completed'), 31)
        self.api.pages[closed['id']] = closed
        sample = page(row(), 1)
        self.api.pages[sample['id']] = sample
        result = self.sync(apply=True, max_actions=1, max_new_intakes=1)
        self.assertEqual(result['applied'], 1)
        self.assertEqual(len(self.local.rows), 2)
        self.assertEqual(len({r.get('_notion_page_id') for r in self.local.rows if r.get('_notion_page_id')}), 1)
        self.assertEqual(self.sync(apply=True, max_new_intakes=0)['applied'], 0)
    def test_remote_concurrency_before_patch(self):
        self.local.edit(notes='local')
        calls = 0
        def change(pid):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.api.edit(pid, priority='high')
        self.api.before_get = change
        result = self.sync(apply=True)
        self.assertEqual(result['conflicts'][s.key(row())], 'remote-concurrency-conflict')
        self.assertFalse(self.api.mutations())
        self.assertEqual(self.store.value['bindings'][s.key(row())]['baseline'], s.fields(row()))
    def test_remote_concurrency_after_patch_keeps_baseline(self):
        self.local.edit(notes='local')
        self.api.after_patch = lambda pid: self.api.edit(pid, priority='urgent')
        result = self.sync(apply=True)
        self.assertEqual(result['conflicts'][s.key(row())], 'remote-readback-mismatch')
        self.assertEqual(self.store.value['bindings'][s.key(row())]['baseline'], s.fields(row()))
    def test_query_missing_owned_gets_not_recreates(self):
        self.api.hide.add(self.pid)
        self.assertEqual(self.sync(apply=True)['planned'], 0)
        self.assertTrue(any(c[:2] == ('GET', '/pages/' + self.pid) for c in self.api.calls))
        self.assertFalse(self.api.mutations())
    def test_remote_danger_blocks(self):
        changes = [lambda p: p.update(in_trash=True), lambda p: p.update(in_trash=None),
                   lambda p: p['parent'].update(data_source_id=SAMPLES[0]),
                   lambda p: p['properties'].update({s.MARKER: {'rich_text': s.rich_text('hermes_tasks:999')}}),
                   lambda p: p['properties'].pop('notes')]
        for change in changes:
            with self.subTest(change=change):
                original = deepcopy(self.api.pages[self.pid])
                change(self.api.pages[self.pid])
                with self.assertRaises(SyncError):
                    self.sync(apply=True)
                self.api.pages[self.pid] = original
        duplicate = deepcopy(self.api.pages[self.pid])
        duplicate['id'] = str(uuid.UUID(int=99))
        self.api.pages[duplicate['id']] = duplicate
        with self.assertRaises(SyncError):
            self.sync(apply=True)
        self.assertFalse(self.api.mutations())
    def test_pagination_complete_and_cycle_fails(self):
        self.api.page_size = 1
        for n in range(20, 23):
            p = page(row(), n)
            self.api.pages[p['id']] = p
        self.assertEqual(len(query_all(self.api)), 4)
        class Bad:
            def request(self, *args):
                return {'results': [], 'has_more': True, 'next_cursor': 'same'}
        with self.assertRaises(SyncError):
            query_all(Bad())
    def test_state_corruption_and_full_registry_validation(self):
        for mutation in (lambda st: st.update(version=2), lambda st: st['bindings'][s.key(row())]['baseline'].pop('notes'),
                         lambda st: st['ignored'].pop(), lambda st: st['pending'].update({'bad': {}})):
            original = deepcopy(self.store.value)
            mutation(self.store.value)
            with self.assertRaises(SyncError):
                self.sync(apply=True)
            self.store.value = original
        self.local.rows.append(row(2, status='completed', due_date='broken'))
        with self.assertRaises(SyncError):
            self.sync(apply=True)
    def test_richtext_chunks_clear_dates_server_metadata(self):
        value = row(notes='x' * 4001, start_date=None, due_date=None)
        p = page(value)
        p['properties']['tag']['select']['description'] = None
        self.assertEqual(len(p['properties']['notes']['rich_text']), 3)
        self.assertEqual(s.page_fields(p, COLS), s.fields(value))
        p['properties']['due_date']['date'] = {'start': '2026-05-20', 'end': '2026-05-21'}
        with self.assertRaises(SyncError):
            s.page_fields(p, COLS)
    def test_closed_reopen_blocked(self):
        self.local.rows[0]['status'] = 'completed'
        self.api.edit(self.pid, status='completed')
        self.store = Memory()
        s.initialize(self.api, self.local, self.store, ignored=SAMPLES, apply=True)
        self.api.edit(self.pid, status='not_started')
        self.assertEqual(self.sync(apply=True)['conflicts'][s.key(row())], 'reopen-blocked')

class PrivateAndTransportTests(unittest.TestCase):
    def test_private_state_lock_symlink_and_corrupt_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with State(root) as st:
                with self.assertRaises(SyncBusy):
                    with State(root):
                        pass
                st.write({'fixture': True})
                self.assertEqual(st.read(), {'fixture': True})
                self.assertEqual((root / 'two-way.json').stat().st_mode & 0o777, 0o600)
            (root / 'two-way.json').unlink()
            (root / 'two-way.json').symlink_to(root / 'outbound.lock')
            with State(root) as st:
                with self.assertRaises(OSError):
                    st.read()
            (root / 'two-way.json').unlink()
            (root / 'two-way.json').write_text('{', encoding='utf8')
            (root / 'two-way.json').chmod(0o600)
            with State(root) as st:
                with self.assertRaises(SyncError):
                    st.read()
            alias = root / 'alias'
            alias.symlink_to(root, target_is_directory=True)
            with self.assertRaises(OSError):
                with State(alias):
                    pass
    def test_duplicate_json_and_nonfinite_rejected(self):
        for raw in ('{"a":1,"a":2}', '{"a":NaN}'):
            with self.assertRaises(SyncError):
                strict_json(raw)
    def test_http403_no_retry_no_leak_and_version(self):
        import urllib.error
        class Opener:
            calls = 0
            def open(self, request, timeout):
                self.calls += 1
                self.request = request
                self.timeout = timeout
                raise urllib.error.HTTPError(request.full_url, 403, 'SECRET TASK TOKEN', {}, None)
        opener = Opener()
        client = Client('fixture-token', opener=opener)
        with self.assertRaisesRegex(SyncError, '^http-403$'):
            client.request('GET', '/data_sources/' + s.SOURCE_ID)
        self.assertEqual(opener.calls, 1)
        self.assertEqual(opener.request.get_header('Notion-version'), '2026-03-11')
        self.assertLessEqual(opener.timeout, 20)
        with self.assertRaises(SyncError):
            client.request('DELETE', '/pages/' + SAMPLES[0])

class RealCanonicalTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(tmp) / 'tasks'
        (self.root / '_meta').mkdir(parents=True)
        self.stack.enter_context(patch.dict(os.environ, {'TASKS_ROOT': str(self.root), 'HERMES_ALLOW_NONCANONICAL_LOCAL_ROOTS': '1', 'TASKS_DASHBOARD_AUTO_UPDATE': '0', 'TASKS_CALENDAR_AUTO_SYNC': '0'}))
        spec = importlib.util.spec_from_file_location('_notion_fixture_core', Path(__file__).with_name('task_ops.py'))
        self.ops = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.ops)
        self.stack.enter_context(patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch.object(self.ops.subprocess, 'run', side_effect=AssertionError('subprocess forbidden')))
        for name in ('refresh_tasks_dashboard', 'refresh_google_calendar'):
            self.stack.enter_context(patch.object(self.ops, name, return_value=None))
        self.stack.enter_context(patch.object(self.ops, 'list_cron_jobs', return_value=[]))
        self.stack.enter_context(patch.object(self.ops, 'remove_enabled_reminder_crons_for_task', return_value={'removed': [], 'errors': []}))
        self.core = s.Core(self.ops)
        self.ops.write_registry([])
        self.api, self.store = API(), Memory()
        self.canonical_locked = False
        original_lock = self.ops.file_lock
        @contextlib.contextmanager
        def tracked_lock(*args, **kwargs):
            with original_lock(*args, **kwargs):
                self.canonical_locked = True
                try:
                    yield
                finally:
                    self.canonical_locked = False
        self.stack.enter_context(patch.object(self.ops, 'file_lock', tracked_lock))
        self.api.canonical_locked = lambda: self.canonical_locked
    def seed(self, **changes):
        r = self.ops.add_task('Fixture work', '2026-05-20', start_date='2026-05-18', **changes)
        p = page(r, mark=s.key(r))
        self.pid = p['id']
        self.api.pages[p['id']] = p
        s.initialize(self.api, self.core, self.store, ignored=SAMPLES, apply=True)
        return r
    def test_real_recurrence_completion_crash_repair_one_advance(self):
        self.seed(recurrence='weekly')
        self.api.edit(self.pid, status='completed')
        with patch.object(self.ops, 'regenerate_notes', side_effect=OSError('crash after canonical commit')):
            with self.assertRaises(OSError):
                s.reconcile(self.api, self.core, self.store, apply=True)
        result = s.reconcile(self.api, self.core, self.store, apply=True)
        self.assertEqual(result['applied'], 1)
        r = self.core.read()[0]
        self.assertEqual((r['due_date'], r['start_date'], r['status'], r['_revision']),
                         ('2026-05-27', '2026-05-25', 'not_started', 2))
        self.assertEqual(s.page_fields(self.api.pages[self.pid], COLS), s.fields(r))
        self.assertEqual(self.ops.LOG_PATH.read_text().count('completed occurrence'), 1)
        self.assertEqual(s.reconcile(self.api, self.core, self.store, apply=True)['planned'], 0)
    def test_real_email_intake_creates_single_follow_up(self):
        p = page(row(name='Email fixture team'), 50)
        self.api.pages[p['id']] = p
        s.initialize(self.api, self.core, self.store, ignored=SAMPLES, apply=True)
        self.api.after_patch = lambda _: (_ for _ in ()).throw(OSError('lost ack'))
        with self.assertRaises(OSError):
            s.reconcile(self.api, self.core, self.store, apply=True)
        self.api.after_patch = None
        result = s.reconcile(self.api, self.core, self.store, apply=True)
        self.assertGreaterEqual(result['applied'], 1)
        records = self.core.read()
        self.assertEqual(len(records), 2)
        self.assertEqual(sum(r.get('_notion_page_id') == p['id'] for r in records), 1)
        self.assertEqual(sum('follow' in r['name'].lower() for r in records), 1)
        self.assertEqual(s.reconcile(self.api, self.core, self.store, apply=True)['planned'], 0)
    def test_resolve_equal_postcommit_local_edit(self):
        r = self.seed()
        self.api.edit(self.pid, notes='remote')
        def crash(state):
            if any(op['phase'] == 'committed' for op in state['pending'].values()):
                raise OSError('lost commit ack')
        self.store.crash = crash
        with self.assertRaises(OSError):
            s.reconcile(self.api, self.core, self.store, apply=True)
        self.store.crash = None
        # A later legitimate local edit blocks completion of durable committed intent.
        records = self.core.read()
        records[0]['notes'] = 'later'
        records[0]['_revision'] += 1
        self.ops.write_registry(records)
        self.assertTrue(s.reconcile(self.api, self.core, self.store, apply=True)['conflicts'])
        k = s.key(r)
        op = deepcopy(self.store.value['pending'][k])
        self.api.edit(self.pid, notes='later')
        before = deepcopy(self.store.value)
        source = self.ops.REGISTRY_PATH.read_bytes()
        self.api.calls.clear()
        kw = dict(key_value=k, expected_operation_id=op['operation_id'], expected_revision=records[0]['_revision'])
        self.assertEqual(s.resolve_equal(self.api, self.core, self.store, **kw)['applied'], 0)
        self.assertEqual(self.store.value, before)
        for bad in (dict(expected_revision=999), dict(expected_operation_id=str(uuid.uuid4())), dict(key_value='hermes_tasks:999')):
            with self.assertRaises(SyncError):
                s.resolve_equal(self.api, self.core, self.store, **(kw | bad), apply=True)
        for f in s.FIELDS:
            original = deepcopy(self.api.pages[self.pid])
            value = None if f in ('start_date', 'due_date') else ('monthly' if f == 'recurrence' else {'status': 'cancelled', 'priority': 'high', 'tag': 'Admin'}.get(f, 'different'))
            self.api.edit(self.pid, **{f: value})
            with self.assertRaises(SyncError):
                s.resolve_equal(self.api, self.core, self.store, **kw, apply=True)
            self.api.pages[self.pid] = original
        for change in (lambda: self.api.pages[self.pid]['properties'].update({s.MARKER: {'rich_text': []}}),
                       lambda: self.store.value['pending'][k].update(kind='create'),
                       lambda: self.store.value['pending'][k].update(phase='prepared')):
            original = deepcopy(self.api.pages[self.pid])
            change()
            with self.assertRaises(SyncError):
                s.resolve_equal(self.api, self.core, self.store, **kw, apply=True)
            self.api.pages[self.pid] = original
            self.store.value = deepcopy(before)
        # Schema replacements also block operator acknowledgement.
        for field in OBSERVED_IDS:
            self.api.property_ids = dict(OBSERVED_IDS, **{field: 'replacement-id'})
            with self.assertRaisesRegex(SyncError, 'schema-property-mismatch'):
                s.resolve_equal(self.api, self.core, self.store, **kw, apply=True)
            self.assertEqual(self.store.read(), before)
        self.api.property_ids = dict(OBSERVED_IDS)
        # Changes during unlocked remote reads must fail the final local CAS.
        for target in ('source', 'state'):
            def concurrent_change(pid):
                self.api.before_get = None
                if target == 'source':
                    changed = deepcopy(records)
                    changed[0]['_revision'] += 1
                    self.ops.write_registry(changed)
                else:
                    self.store.value['bindings'][k]['receipt'] = 'concurrent'
            self.api.before_get = concurrent_change
            with self.assertRaisesRegex(SyncError, 'resolution-source-or-state-changed'):
                s.resolve_equal(self.api, self.core, self.store, **kw, apply=True)
            self.ops.write_registry(records)
            self.store.value = deepcopy(before)
        def assert_ack_locked(state):
            self.assertTrue(self.canonical_locked)
        self.store.crash = assert_ack_locked
        self.assertEqual(s.resolve_equal(self.api, self.core, self.store, **kw, apply=True)['applied'], 1)
        self.store.crash = None
        self.assertEqual(self.store.value['pending'], {})
        self.assertEqual(self.store.value['bindings'][k]['receipt'], op['operation_id'])
        self.assertEqual(self.ops.REGISTRY_PATH.read_bytes(), source)
        self.assertFalse(self.api.mutations())
        self.assertEqual(s.reconcile(self.api, self.core, self.store, apply=True)['planned'], 0)

    def test_real_null_intake_normalizes_and_lost_ack_replays(self):
        p = page(row(start_date=None, due_date=None), 60)
        for f in ('tag', 'priority'):
            p['properties'][f]['select'] = None
        self.api.pages[p['id']] = p
        blank = page(row(), 61)
        blank['properties'] = {'name': {'title': []}, s.MARKER: {'rich_text': []}}
        self.api.pages[blank['id']] = blank
        s.initialize(self.api, self.core, self.store, ignored=SAMPLES, apply=True)
        self.api.after_patch = lambda _: (_ for _ in ()).throw(OSError('lost ack'))
        with self.assertRaises(OSError):
            s.reconcile(self.api, self.core, self.store, apply=True)
        self.api.after_patch = None
        self.assertEqual(len(self.core.read()), 1)
        r = self.core.read()[0]
        self.assertEqual((r['tag'], r['priority'], r['start_date'], r['due_date']), ('Other', 'medium', None, None))
        self.assertEqual(s.page_fields(self.api.pages[p['id']], COLS), s.fields(r))
        self.assertEqual(s.marker(self.api.pages[p['id']]), s.key(r))
        self.assertEqual(s.reconcile(self.api, self.core, self.store, apply=True)['applied'], 1)
        self.assertEqual(s.reconcile(self.api, self.core, self.store, apply=True)['planned'], 0)
        self.assertEqual(len(self.core.read()), 1)
        self.assertEqual(len(self.api.mutations()), 1)
        self.assertEqual(set(self.api.mutations()[0][2]['properties']), set(s.FIELDS) | {s.MARKER})
        self.assertFalse(any(c[1] == '/pages/' + pid for c in self.api.calls for pid in SAMPLES))
        for f in ('tag', 'priority'):
            owned = deepcopy(self.api.pages[p['id']])
            owned['properties'][f]['select'] = None
            with self.assertRaises(SyncError):
                s.page_fields(owned, COLS)

    def test_real_eight_field_edit_and_clear_dates(self):
        self.seed()
        values = dict(name='Renamed fixture', tag='Admin', status='in_progress', start_date='2026-05-19',
                      due_date='2026-05-21', priority='high', recurrence='weekly', notes='Changed fixture')
        self.api.edit(self.pid, **values)
        self.assertEqual(s.reconcile(self.api, self.core, self.store, apply=True)['applied'], 1)
        self.assertEqual(s.fields(self.core.read()[0]), values)
        self.api.edit(self.pid, start_date=None, due_date=None, recurrence=None)
        self.assertEqual(s.reconcile(self.api, self.core, self.store, apply=True)['applied'], 1)
        self.assertIsNone(self.core.read()[0]['due_date'])
        self.assertIsNone(self.core.read()[0]['start_date'])

if __name__ == '__main__':
    unittest.main()
