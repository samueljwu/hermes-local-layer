#!/usr/bin/env python3
"""Deterministic bounded two-way Notion reconciliation; canonical registry wins only
through explicit three-way decisions, never last-writer-wins. Import is inert.

Public interfaces: Core.read()/apply_external_change(...), transport.request(...),
State.read()/write(value), initialize(...), reconcile(...). Inject these for tests.
Notion offers no distributed CAS: pre/post GET narrows, not eliminates, races.
Ambiguous create is quarantined, NEVER retried without finding its stable marker.
"""
from __future__ import annotations
import argparse
from contextlib import nullcontext
from copy import deepcopy
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

# Shared schema is pure; load source-relative without importing task_ops or state.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".hermes" / "scripts"))
from task_schema import validate_project_registry
from notion_transport import (Client, State, SyncError, SyncBusy, require, SOURCE_ID, DB_ID,
    STATE_ROOT, UUID_RE, open_dir, strict_json, query_all)

FIELDS = ('name', 'project_id', 'done', 'start_date', 'due_date', 'priority', 'recurrence', 'notes')
TYPES = dict(zip(FIELDS, ('title', 'relation', 'checkbox', 'date', 'date', 'select', 'rich_text', 'rich_text')))
PROJECT_ID_RE = re.compile(r'P-[1-9][0-9]*')
PROJECT_SOURCE = '3d463936-8ded-80e7-82bd-000bb7b17b11'


class ProjectColumns(dict):
    """Per-reconciliation immutable-by-convention relation lookup; never global."""
    def __init__(self, columns, catalog):
        super().__init__(columns)
        require(isinstance(catalog, dict) and all(
            isinstance(k, str) and PROJECT_ID_RE.fullmatch(k) and isinstance(v, dict) and
            set(v) == {'name', 'page_id'} and isinstance(v['name'], str) and bool(v['name'].strip()) and
            isinstance(v['page_id'], str) and UUID_RE.fullmatch(v['page_id'])
            for k, v in catalog.items()), 'invalid-project-catalog')
        require(len({v['name'] for v in catalog.values()}) == len(catalog), 'duplicate-project-name')
        self.catalog = deepcopy(catalog)
        self.reverse = {v['page_id']: k for k, v in self.catalog.items()}
        require(len(self.reverse) == len(self.catalog), 'duplicate-project-page')
        self.other = next((k for k, v in self.catalog.items() if v['name'] == 'Other'), None)


def load_project_catalog(core, store):
    """Explicit local inputs only. Missing new bindings do not block known projects.

    Requires core.read_projects() and store.read('projects.json'); no fallback I/O.
    Pending catalog creates are deliberately tolerated, but never used as bindings.
    """
    from notion_projects import validate_state as validate_projects
    projects = core.read_projects()
    require(not validate_project_registry(projects), 'invalid-project-registry')
    state = validate_projects(store.read('projects.json'))
    names = {p['id']: p['name'] for p in projects}
    require(set(state['bindings']) <= set(names), 'unknown-bound-project')
    return {k: {'name': names[k], 'page_id': b['page_id']} for k, b in state['bindings'].items()}


def relation_catalog(columns):
    require(isinstance(columns, ProjectColumns), 'project-catalog-required')
    return columns.catalog
MARKER = 'hermes_task_key'
# Exact identities for the single approved SOURCE_ID; preserve encoded tokens.
PROPERTY_IDS = {'name': 'title', 'start_date': '%3BBh%3B', 'project_id': 'H%3BQa',
                'recurrence': 'SYBU', 'hermes_task_key': 'ZyNW',
                'priority': 'aBIM', 'due_date': 'h%5Cw%3F', 'notes': 'so%7DJ'}
KEY_RE = re.compile(r'hermes_tasks:[1-9][0-9]*')
ID_RE = re.compile(r'T(?:([1-9][0-9]*)|-[1-9][0-9]*-([1-9][0-9]*))')
PRIORITIES = {'low', 'medium', 'high', 'top', 'urgent'}

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()

def key(row):
    require(isinstance(row, dict) and isinstance(row.get('id'), str), 'invalid-task-id')
    match = ID_RE.fullmatch(row['id'])
    require(match is not None, 'invalid-task-id')
    return 'hermes_tasks:' + (match.group(1) or match.group(2))

def fields(row, *, blank=False):
    require(isinstance(row, dict) and 'task' not in row, 'invalid-record')
    out = {f: row.get(f) for f in FIELDS}
    for f in ('name', 'project_id', 'priority', 'notes'):
        require(isinstance(out[f], str), 'invalid-scalar')
    require(blank or bool(out['name'].strip()), 'blank-name')
    require(PROJECT_ID_RE.fullmatch(out['project_id']), 'invalid-project-id')
    require(type(out['done']) is bool, 'invalid-done')
    require(out['priority'] in PRIORITIES, 'invalid-vocabulary')
    for f in ('start_date', 'due_date'):
        v = out[f]
        if v is not None:
            require(isinstance(v, str) and bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}', v)), 'invalid-date')
            try:
                date.fromisoformat(v)
            except ValueError:
                raise SyncError('invalid-date') from None
    require(out['recurrence'] is None or isinstance(out['recurrence'], str), 'invalid-recurrence')
    # Canonical absence is null; Notion rich_text cannot distinguish null/empty.
    require(out['recurrence'] != '', 'empty-recurrence')
    for f in ('name', 'notes', 'recurrence'):
        require(out[f] is None or len(out[f]) <= 200000, 'text-size-cap')
    return out

def revision(row):
    value = row.get('_revision', 0)
    require(type(value) is int and value >= 0, 'invalid-revision')
    return value

def validate_rows(rows):
    require(isinstance(rows, list), 'invalid-registry')
    indexed = {}
    origins = set()
    for row in rows:
        k = key(row)
        fields(row)
        revision(row)
        require(k not in indexed, 'duplicate-local-key')
        origin = row.get('_notion_page_id')
        if origin is not None:
            require(isinstance(origin, str) and UUID_RE.fullmatch(origin) and origin not in origins, 'invalid-origin')
            origins.add(origin)
        indexed[k] = row
    return indexed

def rich_text(text):
    require(isinstance(text, str) and len(text) <= 200000, 'invalid-text')
    return [{'type': 'text', 'text': {'content': text[i:i+2000]}} for i in range(0, len(text), 2000)]

def plain(parts):
    require(isinstance(parts, list) and len(parts) <= 100, 'incomplete-rich-text')
    result = []
    for part in parts:
        require(isinstance(part, dict) and part.get('type', 'text') == 'text' and isinstance(part.get('text'), dict), 'invalid-rich-text')
        text = part['text'].get('content')
        require(isinstance(text, str) and len(text) <= 2000 and part.get('plain_text', text) == text, 'invalid-rich-text')
        result.append(text)
    return ''.join(result)

def properties(values, columns, marker=None):
    out = {}
    for f, value in values.items():
        require(f in FIELDS, 'unknown-property')
        typ = TYPES[f]
        if typ in {'title', 'rich_text'}:
            value = rich_text(value or '')
        elif typ == 'relation':
            catalog = relation_catalog(columns)
            require(value in catalog, 'unmapped-project-id')
            value = [{'id': catalog[value]['page_id']}]
        elif typ == 'checkbox':
            require(type(value) is bool, 'invalid-done')
        elif typ == 'select':
            value = {'name': value}
        else:
            value = None if value is None else {'start': value, 'end': None, 'time_zone': None}
        out[columns[f]] = {typ: value}
    if marker is not None:
        require(KEY_RE.fullmatch(marker), 'invalid-marker')
        out[MARKER] = {'rich_text': rich_text(marker)}
    return out

def checkbox_binding(value):
    require(isinstance(value, dict) and set(value) == {'source_id', 'done_property_id'} and
            value['source_id'] == SOURCE_ID and isinstance(value['done_property_id'], str) and
            bool(re.fullmatch(r'[A-Za-z0-9_%\\:;?@!$&*+.,~=-]{1,128}', value['done_property_id'])) and
            value['done_property_id'] not in {*PROPERTY_IDS.values(), 'US%5Cb'}, 'invalid-checkbox-binding')
    return deepcopy(value)


def schema(api, catalog=None, *, mapping=None):
    binding = checkbox_binding(mapping)
    property_ids = {**PROPERTY_IDS, 'done': binding['done_property_id']}
    source = api.request('GET', '/data_sources/' + SOURCE_ID)
    require(source.get('object') == 'data_source' and source.get('id') == SOURCE_ID and
            source.get('parent', {}).get('database_id') == DB_ID, 'wrong-container')
    props = source.get('properties')
    require(isinstance(props, dict), 'invalid-schema')
    columns = ProjectColumns({f: 'project' if f == 'project_id' else f for f in FIELDS}, catalog or {})
    require(not ('start_date' in props and 'Start date' in props), 'ambiguous-start-date')
    if 'start_date' not in props:
        columns['start_date'] = 'Start date'
    seen = set()
    for f, typ in {**TYPES, MARKER: 'rich_text'}.items():
        name = columns.get(f, f)
        prop = props.get(name, {})
        require(isinstance(prop, dict) and prop.get('type') == typ and isinstance(prop.get('id'), str) and
                prop['id'] == property_ids[f] and prop['id'] not in seen, 'schema-property-mismatch')
        seen.add(prop['id'])
        if f == 'project_id':
            relation = prop.get('relation', {})
            require(relation.get('data_source_id') == PROJECT_SOURCE and
                    relation.get('type') == 'dual_property' and
                    relation.get('dual_property', {}).get('synced_property_id') == 'oQv%5B',
                    'project-relation-schema-mismatch')
        if f == 'priority':
            options = prop.get(typ, {}).get('options')
            require(isinstance(options, list) and all(isinstance(o, dict) and isinstance(o.get('name'), str) for o in options), 'invalid-schema-options')
            required = PRIORITIES
            require(required <= {o['name'] for o in options}, 'missing-schema-options')
    return columns

def page_identity(page, expected_id=None, *, allow_trash=False):
    require(isinstance(page, dict) and page.get('object') == 'page' and isinstance(page.get('id'), str) and
            UUID_RE.fullmatch(page['id']) and (expected_id is None or page['id'] == expected_id), 'invalid-page-id')
    parent = page.get('parent', {})
    require(isinstance(parent, dict) and parent.get('type') == 'data_source_id' and parent.get('data_source_id') == SOURCE_ID, 'wrong-parent')
    require(page.get('in_trash') is False or (allow_trash and page.get('in_trash') is True), 'trashed-or-unknown')
    require(isinstance(page.get('last_edited_time'), str) and bool(page['last_edited_time']), 'missing-remote-revision')
    require(isinstance(page.get('properties'), dict), 'invalid-properties')

def marker(page):
    prop = page['properties'].get(MARKER)
    require(isinstance(prop, dict) and prop.get('type', 'rich_text') == 'rich_text' and 'rich_text' in prop, 'invalid-marker-property')
    value = plain(prop['rich_text'])
    require(not value or KEY_RE.fullmatch(value), 'unknown-marker-format')
    return value or None

def page_fields(page, columns, *, blank=False, intake=False):
    require(not intake or marker(page) is None, 'intake-must-be-unowned')
    out = {}
    for f, typ in TYPES.items():
        prop = page['properties'].get(columns[f])
        require(isinstance(prop, dict) and prop.get('type', typ) == typ and typ in prop, 'invalid-remote-property')
        value = prop[typ]
        if typ in {'title', 'rich_text'}:
            value = plain(value)
            if f == 'recurrence':
                value = value or None
        elif typ == 'relation':
            relation_catalog(columns)
            require(prop.get('has_more', False) is False and isinstance(value, list), 'incomplete-project-relation')
            if intake and not value:
                require(columns.other is not None, 'other-project-unmapped')
                value = columns.other
            else:
                require(len(value) == 1 and isinstance(value[0], dict) and
                        isinstance(value[0].get('id'), str), 'invalid-project-relation')
                require(value[0]['id'] in columns.reverse, 'unknown-project-relation')
                value = columns.reverse[value[0]['id']]
        elif typ == 'checkbox':
            require(type(value) is bool, 'invalid-done')
        elif typ == 'select':
            if intake and value is None and f == 'priority':
                value = {'name': 'medium'}
            require(isinstance(value, dict) and isinstance(value.get('name'), str), 'missing-remote-option')
            value = value['name']  # IDs, color, nullable description are server metadata.
        elif value is not None:
            require(isinstance(value, dict) and value.get('end') is None and value.get('time_zone') is None, 'unsupported-date-range')
            value = value.get('start')
            require(value is not None, 'invalid-remote-date')
        out[f] = value
    return fields(out, blank=blank)

def get_page(api, page_id, columns, expected_marker):
    page = api.request('GET', '/pages/' + page_id)
    page_identity(page, page_id)
    require(marker(page) == expected_marker, 'ownership-changed')
    return page, page_fields(page, columns, intake=expected_marker is None)

def validate_state(state):
    require(isinstance(state, dict) and set(state) in ({'version', 'source_id', 'database_id', 'ignored', 'bindings', 'pending'},
                {'version', 'source_id', 'database_id', 'ignored', 'bindings', 'pending', 'deletion_policy'}) and
            state['version'] == 1 and state['source_id'] == SOURCE_ID and state['database_id'] == DB_ID, 'invalid-state-header')
    policy = state.get('deletion_policy')
    if policy is not None:
        require(isinstance(policy, dict) and set(policy) == {'enrolled', 'excluded', 'deleted'} and
                all(isinstance(v, dict) for v in policy.values()), 'invalid-deletion-policy')
        seen_keys, seen_pages = set(), set()
        for group in policy.values():
            for k, pid in group.items():
                require(isinstance(k, str) and KEY_RE.fullmatch(k) and isinstance(pid, str) and
                        UUID_RE.fullmatch(pid) and k not in seen_keys and pid not in seen_pages, 'invalid-deletion-enrollment')
                seen_keys.add(k)
                seen_pages.add(pid)
        require(all(k in state['bindings'] and state['bindings'][k]['page_id'] == pid
                    for group in ('enrolled', 'excluded') for k, pid in policy[group].items()), 'deletion-binding-mismatch')
        require(not (set(policy['deleted']) & set(state['bindings'])), 'deleted-binding-retained')
    ignored = state['ignored']
    require(isinstance(ignored, list) and len(ignored) == 3 and len(set(ignored)) == 3 and
            all(isinstance(i, str) and UUID_RE.fullmatch(i) for i in ignored), 'invalid-sample-ids')
    require(isinstance(state['bindings'], dict) and isinstance(state['pending'], dict), 'invalid-state-maps')
    pages = set(ignored)
    for k, b in state['bindings'].items():
        require(KEY_RE.fullmatch(k) and isinstance(b, dict) and set(b) == {'page_id', 'baseline', 'revision', 'receipt'}, 'invalid-binding')
        require(isinstance(b['page_id'], str) and UUID_RE.fullmatch(b['page_id']) and b['page_id'] not in pages, 'duplicate-binding')
        pages.add(b['page_id'])
        require(isinstance(b['baseline'], dict) and set(b['baseline']) == set(FIELDS), 'invalid-baseline')
        fields(b['baseline'])
        require(type(b['revision']) is int and b['revision'] >= 0 and isinstance(b['receipt'], str), 'invalid-binding-receipt')
    for k, op in state['pending'].items():
        require(isinstance(k, str) and (KEY_RE.fullmatch(k) or UUID_RE.fullmatch(k)), 'invalid-operation-key')
        required = {'kind', 'phase', 'operation_id', 'task_id', 'page_id', 'source_revision', 'before', 'target', 'remote_revision', 'result_revision', 'marker', 'create_sent'}
        require(isinstance(op, dict) and set(op) == required and op['kind'] in {'pull', 'push', 'create', 'intake', 'delete', 'cancel'} and
                op['phase'] in {'prepared', 'committed'} and isinstance(op['operation_id'], str) and UUID_RE.fullmatch(op['operation_id']), 'invalid-pending')
        require(op['task_id'] is None or (isinstance(op['task_id'], str) and ID_RE.fullmatch(op['task_id'])), 'invalid-pending-task')
        require(op['page_id'] is None or (isinstance(op['page_id'], str) and UUID_RE.fullmatch(op['page_id'])), 'invalid-pending-page')
        require(op['marker'] is None or (isinstance(op['marker'], str) and KEY_RE.fullmatch(op['marker'])), 'invalid-pending-marker')
        require(type(op['create_sent']) is bool, 'invalid-create-state')
        for f in ('source_revision', 'result_revision'):
            require(op[f] is None or type(op[f]) is int and op[f] >= 0, 'invalid-pending-revision')
        require(op['remote_revision'] is None or isinstance(op['remote_revision'], str), 'invalid-pending-remote-revision')
        for f in ('before', 'target'):
            require(op[f] is None and f == 'before' or isinstance(op[f], dict) and set(op[f]) == set(FIELDS), 'invalid-pending-fields')
            if op[f] is not None:
                fields(op[f])
        if op['kind'] == 'intake':
            require(op['page_id'] == k and op['before'] is not None, 'invalid-intake-state')
        else:
            require(op['task_id'] is not None and key({'id': op['task_id']}) == k and op['marker'] == k and op['source_revision'] is not None, 'invalid-owned-operation')
        require(op['kind'] == 'create' or op['page_id'] is not None, 'missing-pending-page')
        require(op['phase'] != 'committed' or (op['task_id'] is not None and op['result_revision'] is not None and op['marker'] is not None), 'incomplete-commit-state')
    return state

class Core:
    """Source-relative canonical API adapter. Full registry is read under its lock."""
    def __init__(self, module=None):
        if module is None:
            import task_ops
            module = task_ops
        self.module = module
    def read(self, *, locked=False):
        m = self.module
        with nullcontext() if locked else m.file_lock():
            p = m.REGISTRY_PATH
            directory = open_dir(p.parent)
            try:
                fd = os.open(p.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            finally:
                os.close(directory)
            with os.fdopen(fd, 'rb') as f:
                info = os.fstat(f.fileno())
                require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'unsafe-registry')
                raw = f.read(16 * 1024 * 1024 + 1)
            require(len(raw) <= 16 * 1024 * 1024, 'registry-size-cap')
            rows = strict_json(raw)
            require(isinstance(rows, list) and not m.validate_registry(rows, check_notes=False), 'invalid-full-registry')
            validate_rows(rows)
            return rows
    def read_projects(self):
        with self.module.file_lock():
            return self.module.read_projects()
    def apply_external_change(self, **kwargs):
        return self.module.apply_external_change(**kwargs)
    def delete_external_task(self, **kwargs):
        return self.module.delete_external_task(**kwargs)
    def cancellation_receipt(self, mark, page_id):
        with self.module.file_lock():
            receipt = self.module.read_deletions().get(mark.split(':')[1])
            return validate_cancellation(receipt, mark, page_id) if receipt is not None else None
    def deletion_receipt(self, op):
        with self.module.file_lock():
            receipt = self.module.read_deletions().get(op['marker'].split(':')[1])
            return bool(receipt and receipt['operation_id'] == op['operation_id'] and
                        receipt['page_id'] == op['page_id'] and receipt['revision'] == op['source_revision'])

def validate_cancellation(receipt, mark, page_id):
    require(isinstance(receipt, dict) and receipt.get('reason') == 'cancelled' and
            isinstance(receipt.get('archived_at'), str) and bool(receipt['archived_at']) and
            isinstance(receipt.get('operation_id'), str) and bool(receipt['operation_id']) and
            receipt.get('page_id') in (None, page_id) and
            key({'id': receipt.get('task_id')}) == mark and
            isinstance(receipt.get('snapshot'), dict), 'invalid-cancellation-receipt')
    require(set(receipt) == {'operation_id', 'page_id', 'revision', 'task_id', 'prior_operations',
                            'log_entry', 'snapshot', 'reason', 'archived_at'} and
            isinstance(receipt['prior_operations'], list) and
            all(isinstance(v, str) for v in receipt['prior_operations']) and
            isinstance(receipt['log_entry'], str), 'invalid-cancellation-receipt')
    try:
        require(datetime.fromisoformat(receipt['archived_at']).tzinfo is not None, 'invalid-cancellation-time')
    except ValueError:
        raise SyncError('invalid-cancellation-time') from None
    snapshot = receipt['snapshot']
    require(key(snapshot) == mark and snapshot['id'] == receipt['task_id'] and
            type(receipt.get('revision')) is int and revision(snapshot) == receipt['revision'] and
            snapshot.get('_notion_page_id') in (None, page_id), 'cancellation-snapshot-mismatch')
    fields(snapshot)
    return deepcopy(receipt)


def cancellation(core, mark, page_id):
    reader = getattr(core, 'cancellation_receipt', None)
    receipt = reader(mark, page_id) if reader else None
    return validate_cancellation(receipt, mark, page_id) if receipt is not None else None


def discover(api, columns, local, state, cancellations=None):
    cancellations = cancellations or {}
    pages, by_marker, unowned = {}, {}, {}
    policy = state.get('deletion_policy', {})
    for page in query_all(api):
        page_identity(page, allow_trash=bool(policy or cancellations))
        pid = page['id']
        require(pid not in pages, 'duplicate-page')
        pages[pid] = page
        mark = marker(page)
        if pid in state['ignored']:
            require(mark is None, 'sample-became-owned')
            continue
        if mark in policy.get('deleted', {}):
            require(policy['deleted'][mark] == pid, 'deleted-marker-moved')
            continue  # Restoring a deleted page never resurrects canonical work.
        if page['in_trash'] and (mark not in state['bindings'] or state['bindings'][mark]['page_id'] != pid):
            continue
        if mark is not None:
            require(mark in local or mark in state['pending'] or mark in state['bindings'], 'unknown-owned-marker')
            require(mark not in by_marker, 'duplicate-owned-marker')
            by_marker[mark] = pid
        else:
            unowned[pid] = page
    for k, binding in state['bindings'].items():
        pending_delete = state['pending'].get(k, {}).get('kind') == 'delete'
        require(k in local or pending_delete or k in cancellations, 'missing-canonical-binding')
        pid = binding['page_id']
        require(k not in by_marker or by_marker[k] == pid, 'binding-marker-mismatch')
        # Query absence is NOT a deletion signal. GET every bound page, including absent ones.
        page = api.request('GET', '/pages/' + pid)
        page_identity(page, pid, allow_trash=bool(policy or cancellations))
        require(marker(page) == k, 'ownership-changed')
        page_fields(page, columns)
        require(pid not in unowned, 'owned-marker-cleared')
        pages[pid] = page
        by_marker[k] = pid
    for k, pid in list(by_marker.items()):
        if k in state['bindings']:
            continue
        page, _ = get_page(api, pid, columns, k)
        pages[pid] = page
    return pages, by_marker, unowned

def initialize(api, core, store, *, ignored, apply=False, verified_baselines=None):
    """No divergent adoption. Optional parent-verified baselines map key -> eight
    fields; intended for an independently verified old pilot receipt migration.
    A supplied baseline must equal CURRENT canonical fields; it is not authority.
    """
    require(store.read() is None, 'already-initialized')
    state = {'version': 1, 'source_id': SOURCE_ID, 'database_id': DB_ID, 'ignored': sorted(ignored), 'bindings': {}, 'pending': {}}
    validate_state(state)
    local = validate_rows(core.read())
    columns = schema(api, load_project_catalog(core, store), mapping=store.read('checkbox-schema.json'))
    pages, owned, _ = discover(api, columns, local, state)
    conflicts = {}
    seeds = verified_baselines or {}
    require(isinstance(seeds, dict) and all(k in local and fields(v) == fields(local[k]) for k, v in seeds.items()), 'invalid-verified-baseline')
    for k, pid in sorted(owned.items()):
        value = fields(local[k])
        remote = page_fields(pages[pid], columns)
        if remote != value and k not in seeds:
            conflicts[k] = 'init-divergence'
            continue
        state['bindings'][k] = {'page_id': pid, 'baseline': value, 'revision': revision(local[k]), 'receipt': 'init-verified' if k in seeds else 'init-equal'}
    require(validate_rows(core.read()) == local, 'source-changed-during-init')
    if apply and not conflicts:
        store.write(state)
    return {'planned': len(owned), 'applied': len(owned) if apply and not conflicts else 0, 'conflicts': conflicts}

def operation(kind, row, page, before, target, mark):
    return {'kind': kind, 'phase': 'prepared', 'operation_id': str(uuid.uuid4()),
            'task_id': row['id'] if row else None, 'page_id': page['id'] if page else None,
            'source_revision': revision(row) if row else None, 'before': before,
            'target': target, 'remote_revision': page['last_edited_time'] if page else None,
            'result_revision': None, 'marker': mark, 'create_sent': False}

def _current(core, op):
    local = validate_rows(core.read())
    k = key({'id': op['task_id']})
    require(k in local, 'source-disappeared')
    row = local[k]
    require(fields(row) == op['target'] and revision(row) == op['result_revision'], 'source-changed-during-operation')
    return row

def execute_operation(api, core, store, state, slot, columns, owned):
    """Resume one journaled operation; every outgoing action follows durable intent.
    Canonical dedupe owns replay semantics (especially recurrence/email side effects).
    """
    op = state['pending'][slot]
    if op['kind'] == 'cancel':
        execute_cancel(api, core, store, state, slot, columns)
        return
    if op['kind'] == 'delete':
        execute_delete(api, core, store, state, slot, columns)
        return
    if op['phase'] == 'prepared':
        if op['kind'] in {'pull', 'intake'}:
            # On replay canonical dedupe may already have committed. Its receipt is
            # authoritative; remote input must still equal the originally observed edit.
            page, remote = get_page(api, op['page_id'], columns, op['marker'])
            require(remote == op['before'] and page['last_edited_time'] == op['remote_revision'], 'remote-changed-before-commit')
            result = core.apply_external_change(task_id=op['task_id'], fields=deepcopy(op['target']),
                expected_revision=op['source_revision'], operation_id=op['operation_id'],
                origin_page_id=op['page_id'] if op['kind'] == 'intake' else None)
            fields(result)
            op['task_id'] = result['id']
            op['marker'] = key(result)
            op['target'] = fields(result)
            op['result_revision'] = revision(result)
        else:
            op['result_revision'] = op['source_revision']
        op['phase'] = 'committed'
        store.write(state)
    row = _current(core, op)
    mark = op['marker']
    pid = op['page_id']
    if op['kind'] == 'create' and pid is None:
        if mark in owned:
            pid = owned[mark]
            _, remote = get_page(api, pid, columns, mark)
            require(remote == op['target'], 'create-recovery-mismatch')
        else:
            require(not op['create_sent'], 'ambiguous-create-needs-operator')
            op['create_sent'] = True
            store.write(state)
            result = api.request('POST', '/pages', {'parent': {'type': 'data_source_id', 'data_source_id': SOURCE_ID},
                'properties': properties(op['target'], columns, mark), 'template': {'type': 'none'}})
            page_identity(result)
            pid = result['id']
        op['page_id'] = pid
        store.write(state)
    expected_marker = None if op['kind'] == 'intake' else mark
    page = api.request('GET', '/pages/' + pid)
    page_identity(page, pid)
    observed_marker = marker(page)
    require(observed_marker in {expected_marker, mark}, 'ownership-changed')
    remote = page_fields(page, columns, intake=op['kind'] == 'intake' and observed_marker is None)
    if remote != op['target'] or observed_marker != mark:
        require(remote == op['before'] and observed_marker == expected_marker and
                page['last_edited_time'] == op['remote_revision'], 'remote-concurrency-conflict')
        _current(core, op)
        changed = {f: op['target'][f] for f in FIELDS
                   if remote[f] != op['target'][f] or op['kind'] == 'intake' and observed_marker is None}
        payload = properties(changed, columns, mark if observed_marker != mark else None)
        # Intent already includes before/target and stable operation ID; repeating a
        # PATCH is safe only after a fresh GET still equals the exact original input.
        api.request('PATCH', '/pages/' + pid, {'properties': payload})
    _, readback = get_page(api, pid, columns, mark)
    require(readback == op['target'], 'remote-readback-mismatch')
    row = _current(core, op)
    state['bindings'][mark] = {'page_id': pid, 'baseline': fields(row), 'revision': revision(row), 'receipt': op['operation_id']}
    if 'deletion_policy' in state:
        state['deletion_policy']['excluded'].pop(mark, None)
        state['deletion_policy']['enrolled'][mark] = pid
    del state['pending'][slot]
    store.write(state)

def active_container(api):
    """Never interpret inherited database/data-source trash as individual deletion."""
    database = api.request('GET', '/databases/' + DB_ID)
    require(database.get('object') == 'database' and database.get('id') == DB_ID and
            database.get('in_trash') is False, 'database-not-active')
    source = api.request('GET', '/data_sources/' + SOURCE_ID)
    require(source.get('object') == 'data_source' and source.get('id') == SOURCE_ID and
            source.get('parent', {}).get('database_id') == DB_ID and
            source.get('in_trash') is False, 'data-source-not-active')


def enable_deletions(api, core, store, *, apply=False):
    """One-time technical enrollment. GET only; never canonical or remote writes.

    Caller holds connector State lock. Old trash is permanently excluded; only
    equal active bindings are enrolled. No replacing a previously saved cutover.
    """
    state = validate_state(deepcopy(store.read()))
    require('deletion_policy' not in state, 'deletion-policy-already-enabled')
    require(not state['pending'], 'pending-operations-block-cutover')
    local = validate_rows(core.read())
    columns = schema(api, load_project_catalog(core, store), mapping=store.read('checkbox-schema.json'))
    active_container(api)
    policy = {'enrolled': {}, 'excluded': {}, 'deleted': {}}
    observations, conflicts = {}, {}
    for k, binding in sorted(state['bindings'].items()):
        require(k in local, 'missing-canonical-binding')
        page = api.request('GET', '/pages/' + binding['page_id'])
        page_identity(page, binding['page_id'], allow_trash=True)
        require(marker(page) == k, 'ownership-changed')
        observations[k] = page
        if page['in_trash']:
            policy['excluded'][k] = page['id']
        elif (page_fields(page, columns) != binding['baseline'] or
              fields(local[k]) != binding['baseline'] or revision(local[k]) != binding['revision']):
            conflicts[k] = 'cutover-baseline-conflict'
        else:
            policy['enrolled'][k] = page['id']
    for k, before in observations.items():
        after = api.request('GET', '/pages/' + before['id'])
        page_identity(after, before['id'], allow_trash=True)
        # request_id and other response-envelope metadata vary on every GET.
        # Compare validated task identity/state, never the entire HTTP response.
        require(marker(after) == k and after['in_trash'] == before['in_trash'] and
                after['last_edited_time'] == before['last_edited_time'] and
                page_fields(after, columns) == page_fields(before, columns), 'remote-changed-during-cutover')
    active_container(api)
    # Source lock binds the technical publication to the observed revisions.
    with core.module.file_lock():
        require(validate_rows(core.read(locked=True)) == local and store.read() == state,
                'source-or-state-changed-during-cutover')
        if apply and not conflicts:
            state['deletion_policy'] = policy
            validate_state(state)
            store.write(state)
    return {'planned': len(policy['enrolled']), 'excluded': len(policy['excluded']),
            'applied': int(apply and not conflicts), 'conflicts': conflicts}


def execute_cancel(api, core, store, state, slot, columns):
    op = state['pending'][slot]
    binding = state['bindings'][slot]
    require(binding['page_id'] == op['page_id'], 'cancellation-binding-changed')
    receipt = cancellation(core, slot, op['page_id'])
    require(receipt is not None and op['operation_id'] == str(uuid.uuid5(uuid.NAMESPACE_URL, digest(receipt))) and
            fields(receipt['snapshot']) == op['target'] and
            receipt['revision'] == op['source_revision'] and receipt['task_id'] == op['task_id'],
            'cancellation-receipt-changed')
    require(slot not in validate_rows(core.read()), 'cancelled-task-reappeared')
    active_container(api)
    # Revalidate pinned schema immediately before the destructive (recoverable) PATCH.
    schema(api, columns.catalog, mapping=store.read('checkbox-schema.json'))
    page = api.request('GET', '/pages/' + op['page_id'])
    page_identity(page, op['page_id'], allow_trash=True)
    require(marker(page) == slot and page_fields(page, columns) == op['before'], 'cancellation-remote-changed')
    if not page['in_trash']:
        require(page['last_edited_time'] == op['remote_revision'], 'cancellation-remote-changed')
        require(cancellation(core, slot, op['page_id']) == receipt and
                slot not in validate_rows(core.read()), 'cancellation-source-changed')
        api.request('PATCH', '/pages/' + op['page_id'], {'in_trash': True})
    after = api.request('GET', '/pages/' + op['page_id'])
    page_identity(after, op['page_id'], allow_trash=True)
    require(after['in_trash'] is True and marker(after) == slot and
            page_fields(after, columns) == op['before'], 'cancellation-readback-failed')
    active_container(api)
    schema(api, columns.catalog, mapping=store.read('checkbox-schema.json'))
    require(cancellation(core, slot, op['page_id']) == receipt and
            slot not in validate_rows(core.read()), 'cancellation-source-changed')
    policy = state.setdefault('deletion_policy', {'enrolled': {}, 'excluded': {}, 'deleted': {}})
    policy['enrolled'].pop(slot, None)
    policy['excluded'].pop(slot, None)
    policy['deleted'][slot] = op['page_id']
    del state['bindings'][slot]
    del state['pending'][slot]
    store.write(state)


def execute_delete(api, core, store, state, slot, columns):
    op = state['pending'][slot]
    policy = state.get('deletion_policy', {})
    require(policy.get('enrolled', {}).get(slot) == op['page_id'] and
            state['bindings'][slot]['baseline'] == op['target'] and
            state['bindings'][slot]['revision'] == op['source_revision'], 'deletion-enrollment-changed')
    # A canonical receipt is irrevocable; restoration after commit only repairs
    # local derivatives/ack. Before the first receipt, require fresh exact trash.
    if not core.deletion_receipt(op):
        active_container(api)
        page = api.request('GET', '/pages/' + op['page_id'])
        page_identity(page, op['page_id'], allow_trash=True)
        require(marker(page) == slot and page['in_trash'] is True and
                page['last_edited_time'] == op['remote_revision'] and
                page_fields(page, columns) == op['before'] == op['target'], 'deletion-remote-changed')
    core.delete_external_task(task_id=op['task_id'], expected_revision=op['source_revision'],
                             operation_id=op['operation_id'], page_id=op['page_id'])
    require(slot not in validate_rows(core.read()) and core.deletion_receipt(op), 'deletion-readback-failed')
    policy['deleted'][slot] = policy['enrolled'].pop(slot)
    del state['bindings'][slot]
    del state['pending'][slot]
    store.write(state)


def reconcile(api, core, store, *, apply=False, max_actions=10, max_new_intakes=3, max_deletes=3):
    """Return only counts and key->fixed conflict codes. max_actions counts complete
    logical operations (one may perform a canonical commit plus one remote PATCH).
    No default mutation; pending operations consume the same budget on resume.
    """
    require(type(max_actions) is int and 0 <= max_actions <= 100 and type(max_new_intakes) is int and
            0 <= max_new_intakes <= 100, 'invalid-action-cap')
    require(type(max_deletes) is int and 0 <= max_deletes <= 3, 'invalid-delete-cap')
    state = validate_state(deepcopy(store.read()))
    # Irrevocable local receipts repair before ANY remote access/discovery. This
    # recovery-only pass cannot be blocked by subsequent remote loss of access.
    repairs = [(k, op) for k, op in sorted(state['pending'].items())
               if op['kind'] == 'delete' and core.deletion_receipt(op)]
    if repairs:
        selected = repairs[:min(max_actions, max_deletes)]
        applied, conflicts = 0, {}
        if apply:
            for slot, op in selected:
                try:
                    execute_delete(api, core, store, state, slot, {})
                    applied += 1
                except (SyncError, ValueError, RuntimeError):
                    conflicts[slot] = 'deletion-repair-failed'
        return {'planned': len(repairs), 'selected': len(selected), 'attempted': len(selected) if apply else 0,
                'applied': applied, 'pending': len(state['pending']), 'conflicts': conflicts}
    local = validate_rows(core.read())
    columns = schema(api, load_project_catalog(core, store), mapping=store.read('checkbox-schema.json'))
    if 'deletion_policy' in state:
        active_container(api)
    cancellations = {}
    for k, binding in state['bindings'].items():
        if k not in local and state['pending'].get(k, {}).get('kind') != 'delete':
            receipt = cancellation(core, k, binding['page_id'])
            require(receipt is not None, 'missing-canonical-binding')
            cancellations[k] = receipt
    pages, owned, unowned = discover(api, columns, local, state, cancellations)
    conflicts, candidates = {}, []
    for k, receipt in sorted(cancellations.items()):
        if k in state['pending']:
            continue
        binding = state['bindings'][k]
        page = pages[binding['page_id']]
        remote = page_fields(page, columns)
        if remote != binding['baseline']:
            conflicts[k] = 'cancellation-baseline-conflict'
            continue
        op = operation('cancel', receipt['snapshot'], page, remote, fields(receipt['snapshot']), k)
        # Pin the complete original archive (including metadata) using the existing
        # journal identity, without adding a second receipt store or pending shape.
        op['operation_id'] = str(uuid.uuid5(uuid.NAMESPACE_URL, digest(receipt)))
        candidates.append((k, op))
    pending_keys = {op['marker'] for op in state['pending'].values()}
    for slot, op in sorted(state['pending'].items()):
        candidates.append((slot, op))
    for k, row in sorted(local.items()):
        if k in pending_keys or k in state['pending']:
            continue
        current = fields(row)
        if current['project_id'] not in columns.catalog:
            conflicts[k] = 'unmapped-project-id'
            continue  # Catalog pass can create it; never journal an unsendable task.
        binding = state['bindings'].get(k)
        if binding is None:
            if k in owned:
                conflicts[k] = 'uninitialized-owned-page'
            elif row.get('_notion_page_id'):
                conflicts[k] = 'unbound-origin-page'
            elif row['done'] is False:
                candidates.append((k, operation('create', row, None, None, current, k)))
            continue
        page = pages[binding['page_id']]
        remote = page_fields(page, columns)
        baseline = binding['baseline']
        policy = state.get('deletion_policy', {})
        if k in policy.get('excluded', {}):
            if not page['in_trash'] and remote == current == baseline and revision(row) == binding['revision']:
                # Restoration is a future event. Only technical acknowledgement
                # of equal data enrolls this formerly quarantined page.
                candidates.append((k, operation('push', row, page, remote, current, k)))
            else:
                conflicts[k] = 'pre-cutover-trash-excluded'
            continue
        if page['in_trash']:
            if policy.get('enrolled', {}).get(k) != page['id']:
                conflicts[k] = 'deletion-not-enrolled'
            elif current != baseline or revision(row) != binding['revision'] or remote != baseline:
                conflicts[k] = 'deletion-baseline-conflict'
            else:
                candidates.append((k, operation('delete', row, page, remote, current, k)))
            continue
        if current == remote == baseline:
            continue
        if current == remote:
            candidates.append((k, operation('push', row, page, remote, current, k)))
        elif current == baseline:
            candidates.append((k, operation('pull', row, page, remote, remote, k)))
        elif remote == baseline:
            candidates.append((k, operation('push', row, page, remote, current, k)))
        else:
            conflicts[k] = 'both-changed'
    pending_pages = {o['page_id'] for o in state['pending'].values()}
    for pid, page in sorted(unowned.items()):
        if pid in pending_pages:
            continue
        title = page['properties'].get(columns['name'], {})
        require(isinstance(title, dict) and 'title' in title, 'invalid-draft-title')
        if not plain(title['title']).strip():
            continue  # Draft rows legitimately lack every other required option.
        value = page_fields(page, columns, intake=True)
        if value['done']:
            continue  # Never import historical closed records.
        if any(r.get('_notion_page_id') == pid for r in local.values()):
            conflicts[pid] = 'unbound-existing-intake'
            continue
        candidates.append((pid, operation('intake', None, page, value, value, None)))
    applied = attempted = intakes = deletes = 0
    selected = []
    for slot, op in candidates:
        if len(selected) >= max_actions:
            break
        if op['kind'] in {'delete', 'cancel'}:
            if deletes >= max_deletes:
                continue
            deletes += 1
        if op['kind'] == 'intake':
            if intakes >= max_new_intakes:
                continue
            intakes += 1
        selected.append((slot, op))
    if apply:
        for slot, op in selected:
            attempted += 1
            if slot not in state['pending']:
                state['pending'][slot] = op
                store.write(state)
            try:
                execute_operation(api, core, store, state, slot, columns, owned)
                applied += 1
            except (SyncError, ValueError, RuntimeError) as exc:
                # Canonical exceptions may contain task text: never echo them.
                conflicts[slot] = str(exc) if isinstance(exc, SyncError) else 'canonical-operation-failed'
                # Fail closed for this key, preserving the durable pending intent.
    return {'planned': len(candidates), 'selected': len(selected), 'attempted': attempted,
            'applied': applied, 'pending': len(state['pending']), 'conflicts': conflicts}

def resolve_equal(api, core, store, *, key_value, expected_operation_id, expected_revision, apply=False):
    """Caller holds State lock throughout; source lock protects snapshots and ack.
    Remote reads run outside the source lock, then validated snapshots are compared.
    Never replay a business operation or write Notion; reject ambiguous creates.
    """
    require(isinstance(key_value, str) and KEY_RE.fullmatch(key_value), 'invalid-resolution-key')
    require(isinstance(expected_operation_id, str) and UUID_RE.fullmatch(expected_operation_id), 'invalid-resolution-operation')
    require(type(expected_revision) is int and expected_revision >= 0, 'invalid-resolution-revision')
    with core.module.file_lock():
        state = validate_state(deepcopy(store.read()))
        local = validate_rows(core.read(locked=True))
        matches = [(slot, op) for slot, op in state['pending'].items() if op['marker'] == key_value]
        require(len(matches) == 1 and key_value in local, 'resolution-no-unique-operation')
        slot, op = matches[0]
        uncommitted_delete = op['kind'] == 'delete' and op['phase'] == 'prepared'
        require(op['operation_id'] == expected_operation_id and
                (uncommitted_delete or (op['phase'] == 'committed' and
                 op['kind'] in {'push', 'pull', 'intake'})) and op['page_id'] not in state['ignored'] and
                key({'id': op['task_id']}) == key_value, 'resolution-operation-mismatch')
        require(sum(p['page_id'] == op['page_id'] for p in state['pending'].values()) == 1,
                'resolution-duplicate-page')
        binding = state['bindings'].get(key_value)
        require(binding is None or binding['page_id'] == op['page_id'], 'resolution-binding-mismatch')
        require(all(k == key_value or b['page_id'] != op['page_id'] for k, b in state['bindings'].items()),
                'resolution-binding-mismatch')
        row = local[key_value]
        if uncommitted_delete:
            # Any receipt for this permanent identity is irreversible, even if
            # registry removal has not happened yet. Already under source lock.
            require(key_value.split(':')[1] not in core.module.read_deletions(),
                    'resolution-deletion-receipt-present')
        if op['kind'] in {'pull', 'intake'}:
            require(expected_operation_id in row.get('_external_receipt', {}).get('operations', {}),
                    'resolution-canonical-receipt-missing')
        require(revision(row) == expected_revision, 'resolution-revision-mismatch')
        require(op['kind'] != 'intake' or row.get('_notion_page_id') == op['page_id'], 'resolution-origin-mismatch')
    if uncommitted_delete:
        active_container(api)
    columns = schema(api, load_project_catalog(core, store), mapping=store.read('checkbox-schema.json'))
    _, owned, _ = discover(api, columns, local, state)
    require(owned.get(key_value) == op['page_id'], 'resolution-ownership-mismatch')
    page, remote = get_page(api, op['page_id'], columns, key_value)
    require(remote == fields(row), 'resolution-fields-mismatch')
    again, values = get_page(api, op['page_id'], columns, key_value)
    require(again['last_edited_time'] == page['last_edited_time'] and values == remote,
            'resolution-remote-changed')
    with core.module.file_lock():
        current = validate_rows(core.read(locked=True))
        latest_state = validate_state(deepcopy(store.read()))
        if uncommitted_delete:
            require(key_value.split(':')[1] not in core.module.read_deletions(),
                    'resolution-deletion-receipt-present')
        require(current == local and latest_state == state and
                revision(current[key_value]) == expected_revision and
                fields(current[key_value]) == remote, 'resolution-source-or-state-changed')
        if apply:
            state['bindings'][key_value] = {'page_id': op['page_id'], 'baseline': remote,
                'revision': expected_revision, 'receipt': op['operation_id']}
            del state['pending'][slot]
            validate_state(state)
            store.write(state)
        return {'planned': 1, 'applied': int(apply), 'conflicts': {}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('init', 'plan', 'sync', 'resolve-equal', 'enable-deletions'))
    parser.add_argument('--key')
    parser.add_argument('--expected-operation-id')
    parser.add_argument('--expected-revision', type=int)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--max-actions', type=int, default=10)
    parser.add_argument('--max-new-intakes', type=int, default=3)
    parser.add_argument('--max-deletes', type=int, default=3)
    parser.add_argument('--ignore-page-id', action='append', default=[])
    parser.add_argument('--verified-baselines-file', help='Private JSON in fixed config directory; independently verified by operator')
    args = parser.parse_args(argv)
    resolution_args = (args.key, args.expected_operation_id, args.expected_revision)
    require(all(v is not None for v in resolution_args) if args.command == 'resolve-equal'
            else all(v is None for v in resolution_args), 'resolution-options-only-and-required')
    require(not (args.command == 'plan' and args.apply), 'plan-cannot-apply')
    require(args.command == 'init' or not (args.ignore_page_id or args.verified_baselines_file), 'init-options-only')
    with State(STATE_ROOT) as store:
        token = store.read_bytes('token').decode('ascii')
        require(0 < len(token) <= 4096 and not any(c.isspace() for c in token), 'invalid-token')
        api, core = Client(token), Core()
        if args.command == 'init':
            seeds = store.read(args.verified_baselines_file) if args.verified_baselines_file else None
            result = initialize(api, core, store, ignored=args.ignore_page_id, apply=args.apply, verified_baselines=seeds)
        elif args.command == 'enable-deletions':
            result = enable_deletions(api, core, store, apply=args.apply)
        elif args.command == 'resolve-equal':
            result = resolve_equal(api, core, store, key_value=args.key,
                expected_operation_id=args.expected_operation_id, expected_revision=args.expected_revision, apply=args.apply)
        else:
            result = reconcile(api, core, store, apply=args.apply, max_actions=args.max_actions, max_new_intakes=args.max_new_intakes, max_deletes=args.max_deletes)
        print(json.dumps(result, sort_keys=True))
        return 2 if result['conflicts'] else 0

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SyncBusy:
        print('{"busy":true}')
        raise SystemExit(75)
    except (SyncError, OSError, ValueError, RuntimeError):
        print('{"error":"sync-stopped"}')
        raise SystemExit(2)
