#!/usr/bin/env python3
"""Bounded one-way canonical project catalog -> Notion Projects projection.

Run independently after notion_sync with the SAME locked State. Creates, adopts exact-title
unbound pages, and renames by stable project_id; never edits Tasks or schemas.
projects.json v2 preserves permanent page IDs and legacy notes markers. Empty
projects are retained. Ambiguous creates require marker recovery, never retries.
All remote writes are read back. No API calls hold the canonical registry lock.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
import re
import time
import urllib.error
import urllib.request

import notion_sync as tasks
from notion_transport import (Client, State, SyncError, SyncBusy, require, strict_json,
                              UUID_RE, STATE_ROOT, SOURCE_ID, DB_ID, API_VERSION)

PROJECT_DB = '3d463936-8ded-807a-a136-f834f5a89824'
PROJECT_SOURCE = '3d463936-8ded-80e7-82bd-000bb7b17b11'
PROJECT_TITLE = 'title'
PROJECT_TASKS = 'oQv%5B'
TASK_PROJECT = 'H%3BQa'
PROJECT_NOTES = 'v%7C%5Dv'
PROJECT_QUERY = '/data_sources/' + PROJECT_SOURCE + '/query'
STATE_NAME = 'projects.json'
MARKER_PREFIX = 'hermes_tag_project:v1:'


def valid_id(value):
    return isinstance(value, str) and UUID_RE.fullmatch(value) is not None


def valid_tag(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 100 and ',' not in value


def valid_project_name(value):
    """Canonical names are not legacy comma-free tags; retain rich-text bounds."""
    return (isinstance(value, str) and bool(value.strip()) and value == value.strip()
            and len(value) <= 200000)


def valid_project_id(value):
    return isinstance(value, str) and tasks.PROJECT_ID_RE.fullmatch(value) is not None


def valid_marker(value):
    return isinstance(value, str) and (value == '' or re.fullmatch(
        r'hermes_(?:tag_project:v1|project:v2):[0-9a-f]{64}', value) is not None)


def project_marker(project_id):
    require(valid_project_id(project_id), 'invalid-project-id')
    return 'hermes_project:v2:' + tasks.digest(project_id)


class ProjectClient(Client):
    """Separate, narrower writer; original Client allowlist is unchanged.

    No arbitrary source/schema/block endpoints, redirects, retries, templates,
    shared-field PATCHes or project notes updates. Page identity is gated by
    reconcile immediately before every PATCH (page IDs alone do not prove scope).
    """
    def request(self, method, path, body=None):
        page = re.fullmatch('/pages/' + UUID_RE.pattern, path) is not None
        prop = re.fullmatch('/pages/' + UUID_RE.pattern + '/properties/' +
                            re.escape(TASK_PROJECT) + r'(?:\?start_cursor=[A-Za-z0-9%_.~-]+)?', path) is not None
        allowed_get = {'/databases/' + DB_ID, '/databases/' + PROJECT_DB,
                       '/data_sources/' + SOURCE_ID, '/data_sources/' + PROJECT_SOURCE}
        require((method == 'GET' and (page or prop or path in allowed_get) and body is None) or
                (method == 'POST' and path in {PROJECT_QUERY, '/pages'}) or
                (method == 'PATCH' and page), 'endpoint-not-allowed')
        if method == 'PATCH':
            require(isinstance(body, dict) and set(body) == {'properties'} and
                    isinstance(body['properties'], dict) and set(body['properties']) == {PROJECT_TITLE},
                    'project-title-only')
            value = body['properties'][PROJECT_TITLE]
            require(isinstance(value, dict) and set(value) == {'title'} and
                    valid_project_name(tasks.plain(value['title'])), 'invalid-project-title')
        if method == 'POST' and path == '/pages':
            require(isinstance(body, dict) and set(body) == {'parent', 'properties', 'template'} and
                    body['parent'] == {'type': 'data_source_id', 'data_source_id': PROJECT_SOURCE} and
                    body['template'] == {'type': 'none'} and isinstance(body['properties'], dict) and
                    set(body['properties']) == {PROJECT_TITLE, PROJECT_NOTES}, 'unsafe-project-create')
            title, notes = body['properties'][PROJECT_TITLE], body['properties'][PROJECT_NOTES]
            require(isinstance(title, dict) and set(title) == {'title'} and
                    isinstance(notes, dict) and set(notes) == {'rich_text'}, 'unsafe-project-create')
            tag = tasks.plain(title['title'])
            require(valid_project_name(tag) and re.fullmatch(r'hermes_project:v2:[0-9a-f]{64}',
                    tasks.plain(notes['rich_text'])), 'invalid-create-marker')
        if method == 'POST' and path == PROJECT_QUERY:
            require(isinstance(body, dict) and set(body) <= {'page_size', 'start_cursor'} and
                    body.get('page_size') == 100 and (body.get('start_cursor') is None or
                    isinstance(body['start_cursor'], str) and 0 < len(body['start_cursor']) <= 256), 'invalid-query-body')
        self.calls += 1
        require(self.calls <= 1000, 'request-cap')
        raw = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        require(raw is None or len(raw) <= 500000, 'payload-size-cap')
        time.sleep(max(0, 0.4 - (time.monotonic() - self._last)))
        req = urllib.request.Request('https://api.notion.com/v1' + path, data=raw, method=method,
            headers={'Authorization': 'Bearer ' + self._token, 'Notion-Version': API_VERSION,
                     'Content-Type': 'application/json'})
        self._last = time.monotonic()
        require(self._last < self._deadline, 'run-time-budget')
        try:
            with self._opener.open(req, timeout=min(20, max(0.1, self._deadline - time.monotonic()))) as response:
                require(200 <= response.status < 300, 'http-status')
                data = response.read(16 * 1024 * 1024 + 1)
                require(len(data) <= 16 * 1024 * 1024, 'response-size-cap')
                result = strict_json(data)
                require(isinstance(result, dict), 'invalid-response')
                return result
        except urllib.error.HTTPError as exc:
            raise SyncError('http-' + str(exc.code)) from None
        except (OSError, ValueError):
            raise SyncError('transport-failure') from None


class ProjectCore(tasks.Core):
    def snapshot(self):
        with self.module.file_lock():
            return {'projects': self.module.read_projects()}


def snapshot(core):
    value = core.snapshot()
    from task_schema import validate_project_registry
    require(isinstance(value, dict) and set(value) == {'projects'}, 'invalid-project-snapshot')
    require(not validate_project_registry(value['projects']), 'invalid-project-registry')
    require(all(valid_project_name(p['name']) for p in value['projects']), 'invalid-project-title')
    return value


def initial_state():
    return {'version': 2, 'database_id': PROJECT_DB, 'source_id': PROJECT_SOURCE,
            'task_source_id': SOURCE_ID, 'bindings': {}, 'pending': {}}


def validate_state(value):
    require(isinstance(value, dict) and set(value) == set(initial_state()) and
            type(value['version']) is int and value['version'] == 2 and
            value['database_id'] == PROJECT_DB and value['source_id'] == PROJECT_SOURCE and
            value['task_source_id'] == SOURCE_ID and isinstance(value['bindings'], dict) and
            isinstance(value['pending'], dict), 'invalid-project-state')
    ids = set()
    for tag, binding in value['bindings'].items():
        require(valid_project_id(tag) and isinstance(binding, dict) and set(binding) == {'page_id', 'marker'} and
                valid_id(binding['page_id']) and binding['page_id'] not in ids and
                valid_marker(binding['marker']), 'invalid-project-binding')
        ids.add(binding['page_id'])
    for tag, intent in value['pending'].items():
        require(valid_project_id(tag) and tag not in value['bindings'] and isinstance(intent, dict) and
                set(intent) == {'marker', 'sent', 'page_id'} and intent['marker'] == project_marker(tag) and
                type(intent['sent']) is bool and (intent['page_id'] is None or
                valid_id(intent['page_id']) and intent['sent'] and intent['page_id'] not in ids), 'invalid-project-intent')
        if intent['page_id']:
            ids.add(intent['page_id'])
    return value


def by_id(properties, ident, typ):
    require(isinstance(properties, dict), 'invalid-properties')
    matches = [p for p in properties.values() if isinstance(p, dict) and p.get('id') == ident]
    require(len(matches) == 1 and matches[0].get('type') == typ and typ in matches[0], 'project-property-mismatch')
    return matches[0]


def schemas(api):
    # Validate only owned identities. Unrelated formulas/rollups (including progress)
    # remain parent-managed and are neither interpreted nor projected by this pass.
    sources = {}
    for db, source in ((DB_ID, SOURCE_ID), (PROJECT_DB, PROJECT_SOURCE)):
        database = api.request('GET', '/databases/' + db)
        require(database.get('object') == 'database' and database.get('id') == db and
                database.get('in_trash') is False, 'project-database-not-active')
        data = api.request('GET', '/data_sources/' + source)
        require(data.get('object') == 'data_source' and data.get('id') == source and
                data.get('in_trash') is False and data.get('parent', {}).get('database_id') == db,
                'project-source-binding-mismatch')
        sources[source] = data
    props = sources[PROJECT_SOURCE]['properties']
    by_id(props, PROJECT_TITLE, 'title')
    by_id(props, PROJECT_NOTES, 'rich_text')
    for props, ident, target, reverse in (
            (props, PROJECT_TASKS, SOURCE_ID, TASK_PROJECT),
            (sources[SOURCE_ID]['properties'], TASK_PROJECT, PROJECT_SOURCE, PROJECT_TASKS)):
        relation = by_id(props, ident, 'relation')['relation']
        require(isinstance(relation, dict) and relation.get('data_source_id') == target and
                relation.get('type') == 'dual_property' and
                relation.get('dual_property', {}).get('synced_property_id') == reverse,
                'project-relation-schema-mismatch')
    return None


def paginated(api, path, *, query=False):
    from urllib.parse import quote
    result_rows, seen, cursor = [], set(), None
    for _ in range(100):
        if query:
            body = {'page_size': 100}
            if cursor is not None:
                body['start_cursor'] = cursor
            result = api.request('POST', path, body)
        else:
            result = api.request('GET', path + (('?start_cursor=' + quote(cursor, safe='')) if cursor else ''))
        require(isinstance(result, dict) and isinstance(result.get('results'), list) and
                len(result['results']) <= 100 and type(result.get('has_more')) is bool, 'invalid-project-pagination')
        result_rows.extend(result['results'])
        if not result['has_more']:
            require(result.get('next_cursor') is None, 'invalid-project-continuation')
            return result_rows
        cursor = result.get('next_cursor')
        require(isinstance(cursor, str) and 0 < len(cursor) <= 256 and cursor not in seen, 'invalid-project-cursor')
        seen.add(cursor)
    raise SyncError('incomplete-project-pagination')


def project(page, expected_id=None):
    require(isinstance(page, dict) and page.get('object') == 'page' and valid_id(page.get('id')) and
            (expected_id is None or page['id'] == expected_id) and page.get('in_trash') is False and
            page.get('parent', {}).get('type') == 'data_source_id' and
            page['parent'].get('data_source_id') == PROJECT_SOURCE and
            isinstance(page.get('last_edited_time'), str) and bool(page['last_edited_time']), 'invalid-project-page')
    props = page.get('properties')
    title = tasks.plain(by_id(props, PROJECT_TITLE, 'title')['title'])
    notes = tasks.plain(by_id(props, PROJECT_NOTES, 'rich_text')['rich_text'])
    mark = notes if notes.startswith((MARKER_PREFIX, 'hermes_project:v2:')) else ''
    require(valid_project_name(title) and valid_marker(mark), 'invalid-project-marker')
    return title, mark


def reconcile(api, core, store, *, apply=False, max_actions=10):
    """Catalog-only projection, under caller's State lock, independent of task sync.

    No task state is read and no task page is fetched or written. Bindings are
    permanent IDs; local renames update only the title, leaving all notes intact.
    Creates journal before send and ambiguous creates never blindly retry.
    """
    require(type(max_actions) is int and 0 <= max_actions <= 100, 'invalid-action-cap')
    source = snapshot(core)
    names = {p['id']: p['name'] for p in source['projects']}
    stored = store.read(STATE_NAME)
    state = validate_state(initial_state() if stored is None else deepcopy(stored))
    require(set(state['bindings']) | set(state['pending']) <= set(names), 'missing-canonical-project')
    schemas(api)
    pages, titles, markers = {}, {}, {}
    for page in paginated(api, PROJECT_QUERY, query=True):
        title, mark = project(page)
        pid = page['id']
        require(pid not in pages, 'duplicate-project-page')
        pages[pid] = page
        titles.setdefault(title, []).append(pid)
        if mark:
            require(mark not in markers, 'duplicate-project-marker')
            markers[mark] = pid
    resolved = deepcopy(state['bindings'])
    creates, renames, recoveries = [], [], []
    occupied = {b['page_id'] for b in resolved.values()}
    for ident, binding in state['bindings'].items():
        page = api.request('GET', '/pages/' + binding['page_id'])
        title, mark = project(page, binding['page_id'])
        require(mark == binding['marker'], 'project-binding-changed')
        if title != names[ident]:
            renames.append({'project_id': ident, 'page_id': binding['page_id'],
                            'before': page, 'marker': mark})
    for ident, name in sorted(names.items()):
        if ident in resolved:
            continue
        intent = state['pending'].get(ident)
        matches = titles.get(name, [])
        require(len(matches) <= 1, 'duplicate-project-title')
        if intent and intent['page_id']:
            pid = intent['page_id']
        elif intent and intent['sent']:
            pid = markers.get(intent['marker'])
            require(pid is not None, 'ambiguous-project-create-needs-operator')
        else:
            pid = matches[0] if matches else None
        if pid:
            require(pid not in occupied, 'project-page-already-bound')
            page = api.request('GET', '/pages/' + pid)
            title, mark = project(page, pid)
            require((intent and mark == intent['marker']) or (not intent and title == name and
                    mark in ('', project_marker(ident), MARKER_PREFIX + tasks.digest(name))),
                    'create-recovery-mismatch')
            resolved[ident] = {'page_id': pid, 'marker': mark}
            occupied.add(pid)
            recoveries.append(ident)
            if title != name:
                renames.append({'project_id': ident, 'page_id': pid, 'before': page, 'marker': mark})
        else:
            creates.append(ident)
    require(snapshot(core) == source and store.read(STATE_NAME) == stored, 'project-source-changed')
    actions = ([{'kind': 'create', 'project_id': ident} for ident in creates] +
               [{'kind': 'rename', 'project_id': op['project_id'], 'page_id': op['page_id']} for op in renames])
    result = {'planned': len(actions), 'actions': actions, 'applied': 0, 'remaining': len(actions),
              'pending': len(state['pending']), 'adopted_or_recovered': len(recoveries), 'conflicts': {}}
    if not apply:
        return result
    def fresh():
        require(snapshot(core) == source, 'project-source-changed')
    def save():
        validate_state(state)
        store.write(state, STATE_NAME)
    fresh()
    for ident in recoveries:
        state['bindings'][ident] = resolved[ident]
        state['pending'].pop(ident, None)
    if recoveries or stored is None:
        save()
    try:
        for ident in creates:
            if result['applied'] >= max_actions:
                break
            fresh()
            intent = state['pending'].setdefault(ident, {'marker': project_marker(ident), 'sent': False, 'page_id': None})
            require(not intent['sent'], 'ambiguous-project-create-needs-operator')
            intent['sent'] = True
            save()
            page = api.request('POST', '/pages', {
                'parent': {'type': 'data_source_id', 'data_source_id': PROJECT_SOURCE},
                'properties': {PROJECT_TITLE: {'title': tasks.rich_text(names[ident])},
                               PROJECT_NOTES: {'rich_text': tasks.rich_text(intent['marker'])}},
                'template': {'type': 'none'}})
            require(project(page) == (names[ident], intent['marker']), 'project-create-response-mismatch')
            require(page['id'] not in occupied, 'project-page-already-bound')
            intent['page_id'] = page['id']
            save()
            readback = api.request('GET', '/pages/' + page['id'])
            require(project(readback, page['id']) == (names[ident], intent['marker']), 'project-create-readback-mismatch')
            fresh()
            state['bindings'][ident] = {'page_id': page['id'], 'marker': intent['marker']}
            occupied.add(page['id'])
            del state['pending'][ident]
            save()
            result['applied'] += 1
        for op in renames:
            if result['applied'] >= max_actions:
                break
            fresh()
            pid, ident = op['page_id'], op['project_id']
            page = api.request('GET', '/pages/' + pid)
            project(page, pid)
            require(page['properties'] == op['before']['properties'] and
                    page['last_edited_time'] == op['before']['last_edited_time'], 'project-concurrency-conflict')
            api.request('PATCH', '/pages/' + pid, {
                'properties': {PROJECT_TITLE: {'title': tasks.rich_text(names[ident])}}})
            readback = api.request('GET', '/pages/' + pid)
            require(project(readback, pid) == (names[ident], op['marker']), 'project-rename-readback-mismatch')
            require(by_id(readback['properties'], PROJECT_NOTES, 'rich_text') ==
                    by_id(page['properties'], PROJECT_NOTES, 'rich_text'), 'project-notes-changed')
            fresh()
            result['applied'] += 1
    except (SyncError, OSError, ValueError, RuntimeError) as exc:
        result['conflicts']['run'] = str(exc) if isinstance(exc, SyncError) else 'project-operation-failed'
    result['remaining'] = result['planned'] - result['applied']
    result['pending'] = len(state['pending'])
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'sync'))
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--max-actions', type=int, default=10)
    args = parser.parse_args(argv)
    require(not (args.command == 'plan' and args.apply), 'plan-cannot-apply')
    require(type(args.max_actions) is int and 0 <= args.max_actions <= 100, 'invalid-action-cap')
    require(not os.environ.get('TASKS_ROOT') or os.environ['TASKS_ROOT'] == '/home/hermes/tasks', 'noncanonical-project-root')
    require(os.environ.get('HERMES_ALLOW_NONCANONICAL_LOCAL_ROOTS') != '1', 'test-root-override-forbidden')
    with State(STATE_ROOT) as store:
        token = store.read_bytes('token').decode('ascii')
        require(0 < len(token) <= 4096 and not any(c.isspace() for c in token), 'invalid-token')
        result = reconcile(ProjectClient(token), ProjectCore(), store, apply=args.apply, max_actions=args.max_actions)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2 if result['conflicts'] else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SyncBusy:
        print('{"busy":true}')
        raise SystemExit(75)
    except (SyncError, OSError, ValueError, RuntimeError):
        print('{"error":"projects-sync-stopped"}')
        raise SystemExit(2)
