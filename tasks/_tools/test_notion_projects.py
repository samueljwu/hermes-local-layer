"""Offline unit/contract tests. Never imports task_ops or reads production state.
Run: python3 -m unittest discover -s /home/hermes/tasks/_tools -p test_notion_projects.py -v
"""
import contextlib
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
import uuid

import notion_projects as p
import notion_sync as s
from notion_transport import State, Client, SyncError, SyncBusy
from test_notion_sync import row, page as task_page, API as TaskAPI, SAMPLES


def uid(n):
    return str(uuid.UUID(int=n))


def rich(ident, typ, text):
    return {'id': ident, 'type': typ, typ: [{'type': 'text', 'text': {'content': text}, 'plain_text': text}] if text else []}


def project_page(tag, n=200, mark=''):
    # Server shape: display-name keys, encoded property IDs, no archived key.
    return {'object': 'page', 'id': uid(n), 'in_trash': False,
            'parent': {'type': 'data_source_id', 'data_source_id': p.PROJECT_SOURCE},
            'last_edited_time': '2026-09-07T00:00:00.000Z', 'request_id': 'volatile',
            'properties': {'name': rich('title', 'title', tag),
                           'notes': rich(p.PROJECT_NOTES, 'rich_text', mark),
                           'tasks': {'id': p.PROJECT_TASKS, 'type': 'relation', 'relation': [], 'has_more': False}}}


def schema_relation(ident, target, reverse):
    return {'id': ident, 'name': 'display name', 'type': 'relation',
            'relation': {'data_source_id': target, 'type': 'dual_property',
                         'dual_property': {'synced_property_id': reverse, 'synced_property_name': 'irrelevant'}}}


class Core:
    def __init__(self):
        self.rows = [row()]
        self.projects = [{'id': 'P-5', 'name': 'Other'}, {'id': 'P-9', 'name': 'Empty'}]
        self.calls = 0
        self.on_snapshot = None

    def snapshot(self):
        self.calls += 1
        if self.on_snapshot:
            self.on_snapshot(self)
        return deepcopy({'projects': self.projects})


class Store:
    def __init__(self, core):
        self.data = {'two-way.json': {'version': 1, 'source_id': s.SOURCE_ID, 'database_id': s.DB_ID,
            'ignored': SAMPLES, 'pending': {}, 'bindings': {
                s.key(r): {'page_id': uid(10 + i), 'baseline': s.fields(r), 'revision': s.revision(r), 'receipt': 'fixture'}
                for i, r in enumerate(core.rows)}}}
        self.writes = []

    def read(self, name='two-way.json'):
        return deepcopy(self.data.get(name))

    def write(self, value, name='two-way.json'):
        if name != 'projects.json':
            raise AssertionError('connector state mutation forbidden')
        self.data[name] = deepcopy(value)
        self.writes.append(deepcopy(value))


class API(TaskAPI):
    def __init__(self, core, store):
        pages = []
        for i, r in enumerate(core.rows):
            page = task_page(r, 10 + i, s.key(r))
            for name, prop in page['properties'].items():
                field = 'project_id' if name == 'project' else name
                prop['id'] = {'done': 'fixtureDone', 'est_time': 'fixtureEst'}.get(field, s.PROPERTY_IDS.get(field))
                prop['type'] = 'rich_text' if field == s.MARKER else s.TYPES[field]
            page['properties']['project'] = {'id': p.TASK_PROJECT, 'type': 'relation', 'relation': [], 'has_more': False}
            pages.append(page)
        super().__init__(pages)
        self.store = store
        self.project_pages = {}
        self.project_page_size = 100
        self.create_failure = None
        self.schema_change = None
        self.after_link = None
        self.relation_values = []
        self.relation_page_size = 10
        self.before_request = None

    def request(self, method, path, body=None):
        if self.before_request:
            self.before_request(method, path, body)
        self.calls.append((method, path, deepcopy(body)))
        if method == 'GET' and path == '/databases/' + p.PROJECT_DB:
            return {'object': 'database', 'id': p.PROJECT_DB, 'in_trash': False}
        if method == 'GET' and path == '/data_sources/' + p.PROJECT_SOURCE:
            data = {'object': 'data_source', 'id': p.PROJECT_SOURCE, 'in_trash': False,
                    'parent': {'database_id': p.PROJECT_DB}, 'properties': {
                        'name': {'id': 'title', 'type': 'title', 'title': {}},
                        'notes': {'id': p.PROJECT_NOTES, 'type': 'rich_text', 'rich_text': {}},
                        'tasks': schema_relation(p.PROJECT_TASKS, s.SOURCE_ID, p.TASK_PROJECT)}}
            if self.schema_change:
                self.schema_change(data)
            return data
        if method == 'GET' and path == '/data_sources/' + s.SOURCE_ID:
            data = super().request(method, path, body)
            data['properties']['project'] = schema_relation(p.TASK_PROJECT, p.PROJECT_SOURCE, p.PROJECT_TASKS)
            return data
        if path == p.PROJECT_QUERY:
            values = sorted(self.project_pages.values(), key=lambda v: v['id'])
            offset = int(body.get('start_cursor', '0'))
            end = offset + self.project_page_size
            return {'results': deepcopy(values[offset:end]), 'has_more': end < len(values),
                    'next_cursor': str(end) if end < len(values) else None}
        if '/properties/' in path:
            offset = int(path.split('?start_cursor=')[-1]) if '?' in path else 0
            end = offset + self.relation_page_size
            return {'results': [{'object': 'property_item', 'id': p.TASK_PROJECT, 'type': 'relation', 'relation': {'id': v}}
                                for v in self.relation_values[offset:end]], 'has_more': end < len(self.relation_values),
                    'next_cursor': str(end) if end < len(self.relation_values) else None}
        if path == '/pages' and method == 'POST':
            tag = s.plain(body['properties']['title']['title'])
            # The journal must precede any network mutation.
            mark = s.plain(body['properties'][p.PROJECT_NOTES]['rich_text'])
            intent = next(v for v in self.store.read('projects.json')['pending'].values() if v['marker'] == mark)
            if not intent['sent']:
                raise AssertionError('create was not durable')
            if self.create_failure == 'before':
                raise SyncError('fixture-response-lost')
            self.counter += 1
            page = project_page(tag, self.counter, s.plain(body['properties'][p.PROJECT_NOTES]['rich_text']))
            self.project_pages[page['id']] = page
            if self.create_failure == 'after':
                raise SyncError('fixture-response-lost')
            return deepcopy(page)
        if method == 'GET' and path.removeprefix('/pages/') in self.project_pages:
            data = deepcopy(self.project_pages[path.removeprefix('/pages/')])
            data['request_id'] = str(len(self.calls))
            return data
        if method == 'PATCH':
            if set(body) != {'properties'} or set(body['properties']) != {p.PROJECT_TITLE}:
                raise AssertionError('task fields or schema mutation forbidden')
            page = self.project_pages[path.removeprefix('/pages/')]
            page['properties']['name']['title'] = deepcopy(body['properties'][p.PROJECT_TITLE]['title'])
            page['last_edited_time'] += 'x'
            if self.after_link:
                self.after_link(page)
            return deepcopy(page)
        self.calls.pop()  # Parent records its own request.
        return super().request(method, path, body)


class ProjectsTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.core = Core()
        self.store = Store(self.core)
        self.api = API(self.core, self.store)
        self.original_owner = self.store.read()
        self.original_rows = deepcopy(self.core.rows)

    def run_sync(self, **kw):
        return p.reconcile(self.api, self.core, self.store, **kw)

    def seed_projects(self):
        for i, item in enumerate(self.core.projects):
            page = project_page(item['name'], 200 + i)
            self.api.project_pages[page['id']] = page

    def test_dry_run_no_writes_includes_empty_default(self):
        result = self.run_sync()
        self.assertEqual(result['planned'], 2)
        self.assertEqual([a['project_id'] for a in result['actions'] if a['kind'] == 'create'], ['P-5', 'P-9'])
        self.assertEqual(self.store.writes, [])
        self.assertEqual(self.api.mutations(), [])

    def test_create_link_readback_idempotent_preserves_shared(self):
        result = self.run_sync(apply=True)
        self.assertEqual((result['applied'], result['remaining'], result['conflicts']), (2, 0, {}))
        self.assertEqual(self.run_sync()['planned'], 0)
        self.assertEqual(self.store.read(), self.original_owner)
        self.assertEqual(self.core.rows, self.original_rows)
        self.assertFalse(any(c[1] == '/pages/' + uid(10) for c in self.api.calls))
        self.assertEqual(len(self.api.mutations()), 2)
        self.assertEqual(len(self.api.project_pages), 2)

    def test_adopt_existing_exact_titles_never_touch_notes(self):
        self.seed_projects()
        self.api.project_pages[uid(200)]['properties']['notes'] = rich(p.PROJECT_NOTES, 'rich_text', 'User notes')
        baseline = deepcopy(self.api.project_pages)
        result = self.run_sync(apply=True)
        self.assertEqual(result['applied'], 0)
        self.assertEqual(result['adopted_or_recovered'], 2)
        self.assertEqual(self.api.project_pages, baseline)

    def test_budget_bounds_mutations_and_converges(self):
        for _ in range(2):
            before = len(self.api.mutations())
            self.assertEqual(self.run_sync(apply=True, max_actions=1)['applied'], 1)
            self.assertEqual(len(self.api.mutations()) - before, 1)
        self.assertEqual(self.run_sync()['planned'], 0)

    def test_zero_budget_no_remote_writes(self):
        self.assertEqual(self.run_sync(apply=True, max_actions=0)['applied'], 0)
        self.assertFalse(self.api.mutations())
        self.assertFalse(self.store.read('projects.json')['pending'])

    def test_ambiguous_create_recovers_unique_marker(self):
        self.api.create_failure = 'after'
        result = self.run_sync(apply=True)
        self.assertTrue(result['conflicts'])
        self.assertEqual(len(self.api.mutations()), 1)
        self.api.create_failure = None
        self.assertEqual(self.run_sync(apply=True)['conflicts'], {})
        self.assertEqual(self.run_sync()['planned'], 0)
        self.assertEqual(len(self.api.project_pages), 2)
        self.assertFalse(self.store.read('projects.json')['pending'])

    def test_ambiguous_create_absent_never_retries(self):
        self.api.create_failure = 'before'
        self.run_sync(apply=True)
        before = len(self.api.mutations())
        with self.assertRaisesRegex(SyncError, 'ambiguous-project-create'):
            self.run_sync(apply=True)
        self.assertEqual(len(self.api.mutations()), before)

    def test_duplicate_title_blocks_all_writes(self):
        self.api.project_pages = {uid(i): project_page('Other', i) for i in (200, 201)}
        with self.assertRaisesRegex(SyncError, 'duplicate-project-title'):
            self.run_sync(apply=True)
        self.assertFalse(self.api.mutations())

    def test_duplicate_marker_blocks_all_writes(self):
        self.api.project_pages = {uid(i): project_page('Other', i, p.project_marker('P-5')) for i in (200, 201)}
        with self.assertRaisesRegex(SyncError, 'duplicate-project-marker'):
            self.run_sync(apply=True)
        self.assertFalse(self.api.mutations())

    def test_malformed_marker_blocks_replacement(self):
        self.api.project_pages[uid(200)] = project_page('Other', mark=p.MARKER_PREFIX + 'bad')
        with self.assertRaisesRegex(SyncError, 'invalid-project-marker'):
            self.run_sync(apply=True)
        self.assertFalse(self.api.mutations())

    def test_source_and_relation_schema_pinned(self):
        for mutation in (
            lambda d: d['parent'].update(database_id=uid(999)),
            lambda d: d['properties']['tasks']['relation'].update(data_source_id=uid(999)),
            lambda d: d['properties']['tasks']['relation']['dual_property'].update(synced_property_id='H;Qa'),
            lambda d: d.update(in_trash=True),
            lambda d: d['properties']['notes'].update(id='v|]v')):
            with self.subTest(mutation=mutation):
                self.api.schema_change = mutation
                with self.assertRaises(SyncError):
                    self.run_sync(apply=True)
                self.assertFalse(self.api.mutations())



    def test_local_change_during_discovery_blocks(self):
        def change(core):
            if core.calls == 2:
                core.projects[0]['name'] = 'Changed'
        self.core.on_snapshot = change
        with self.assertRaisesRegex(SyncError, 'source-changed'):
            self.run_sync(apply=True)
        self.assertFalse(self.api.mutations())











    def test_repeated_cursor_fails_closed(self):
        class Broken:
            def request(self, *args):
                return {'results': [], 'has_more': True, 'next_cursor': 'same'}
        with self.assertRaisesRegex(SyncError, 'cursor'):
            p.paginated(Broken(), p.PROJECT_QUERY, query=True)









    def test_empty_malformed_state_never_silently_reset(self):
        for value in ({}, [], False, 0):
            with self.subTest(value=value):
                self.store.data['projects.json'] = value
                with self.assertRaisesRegex(SyncError, 'invalid-project-state'):
                    self.run_sync(apply=True)
                self.assertFalse(self.store.writes)

    def test_report_contract_and_read_only_task_get_count(self):
        result = self.run_sync()
        for field in ('planned', 'applied', 'pending'):
            self.assertIs(type(result[field]), int)
            self.assertGreaterEqual(result[field], 0)
        self.assertIsInstance(result['conflicts'], dict)
        self.assertEqual(sum(c[:2] == ('GET', '/pages/' + uid(10)) for c in self.api.calls), 0)

    def test_bad_state_rejected_not_reset(self):
        self.store.data['projects.json'] = {'version': 2}
        with self.assertRaisesRegex(SyncError, 'invalid-project-state'):
            self.run_sync(apply=True)
        self.assertFalse(self.store.writes)

    def test_invalid_cap_and_cli_plan_apply_are_inert(self):
        for cap in (-1, 101, True):
            with self.assertRaisesRegex(SyncError, 'invalid-action-cap'):
                self.run_sync(apply=True, max_actions=cap)
        with patch.object(p, 'State', side_effect=AssertionError('state opened')):
            with self.assertRaisesRegex(SyncError, 'plan-cannot-apply'):
                p.main(['plan', '--apply'])
        self.assertFalse(self.api.calls)




class TransportAndStateTests(unittest.TestCase):
    def test_original_allowlist_unchanged(self):
        with self.assertRaisesRegex(SyncError, 'endpoint-not-allowed'):
            Client('fixture').request('GET', '/data_sources/' + p.PROJECT_SOURCE)

    def test_narrow_transport_rejects_unapproved_writes_before_network(self):
        class Opener:
            def open(self, *args, **kwargs):
                raise AssertionError('network forbidden')
        client = p.ProjectClient('fixture', opener=Opener())
        for method, path, body in [
            ('GET', '/data_sources/' + uid(9), None),
            ('PATCH', '/data_sources/' + p.PROJECT_SOURCE, {'properties': {}}),
            ('PATCH', '/pages/' + uid(10), {'properties': {'name': {'title': []}}}),
            ('PATCH', '/pages/' + uid(10), {'in_trash': True}),
            ('GET', '/pages/' + uid(10) + '/properties/H;Qa', None),
            ('POST', '/pages', {'parent': {'data_source_id': s.SOURCE_ID}, 'properties': {}, 'template': {'type': 'none'}}),
            ('POST', p.PROJECT_QUERY, {'page_size': 100, 'filter': {}})]:
            with self.subTest(path=path, method=method):
                with self.assertRaises(SyncError):
                    client.request(method, path, body)
        self.assertEqual(client.calls, 0)

    def test_allowed_transport_is_pinned_no_redirect_or_retries(self):
        class Response:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self, _):
                return b'{"object":"fixture"}'
        class Opener:
            def __init__(self):
                self.requests = []
            def open(self, req, **kwargs):
                self.requests.append(req)
                return Response()
        opener = Opener()
        client = p.ProjectClient('fixture', opener=opener)
        with patch.object(p.time, 'sleep'):
            client.request('GET', '/data_sources/' + p.PROJECT_SOURCE)
            client.request('PATCH', '/pages/' + uid(200), {'properties': {p.PROJECT_TITLE: {'title': s.rich_text('Rename')}}})
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(opener.requests[0].full_url, 'https://api.notion.com/v1/data_sources/' + p.PROJECT_SOURCE)
        self.assertEqual(opener.requests[0].get_header('Notion-version'), '2026-03-11')

    def test_private_state_separate_lock_and_symlink_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os.chmod(root, 0o700)
            with State(root) as store:
                store.write({'untouched': True})
                store.write(p.initial_state(), 'projects.json')
                self.assertEqual(store.read(), {'untouched': True})
                self.assertEqual(os.stat(root / 'projects.json').st_mode & 0o777, 0o600)
                with self.assertRaises(SyncBusy):
                    with State(root):
                        pass
            (root / 'projects.json').unlink()
            (root / 'projects.json').symlink_to(root / 'two-way.json')
            with State(root) as store:
                with self.assertRaises(OSError):
                    store.write(p.initial_state(), 'projects.json')


if __name__ == '__main__':
    unittest.main()
