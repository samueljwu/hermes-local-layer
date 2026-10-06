#!/usr/bin/env python3
"""One-time guarded deployment of the optional est_time hours field."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from notion_transport import API_VERSION, Client, DB_ID, NoRedirect, SOURCE_ID, STATE_ROOT, State, SyncError, require

FIELD = 'est_time'
BINDING_FILE = 'est-time-schema.json'
ATTEMPT_FILE = 'est-time-schema-attempt.json'
RECEIPT_FILE = 'est-time-schema-receipt.json'
OLD_FIELDS = {'name', 'project_id', 'done', 'start_date', 'due_date', 'priority', 'recurrence', 'notes'}
NEW_FIELDS = OLD_FIELDS | {FIELD}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def migrate_state(value):
    """Add a null estimate to every durable shared-field snapshot, preserving all else."""
    result = deepcopy(value)
    require(isinstance(result, dict) and result.get('source_id') == SOURCE_ID and
            result.get('database_id') == DB_ID and result.get('version') == 1 and
            isinstance(result.get('bindings'), dict) and
            isinstance(result.get('pending'), dict), 'invalid-state')
    shapes = set()
    for binding in result['bindings'].values():
        require(isinstance(binding, dict), 'invalid-old-binding')
        baseline = binding.get('baseline')
        require(isinstance(baseline, dict) and set(baseline) in (OLD_FIELDS, NEW_FIELDS),
                'invalid-old-baseline')
        shapes.add(frozenset(baseline))
        baseline.setdefault(FIELD, None)
    for operation in result['pending'].values():
        require(isinstance(operation, dict), 'invalid-old-operation')
        for key in ('before', 'target'):
            fields = operation.get(key)
            if fields is None:
                continue
            require(isinstance(fields, dict) and set(fields) in (OLD_FIELDS, NEW_FIELDS),
                    'invalid-old-pending-fields')
            shapes.add(frozenset(fields))
            fields.setdefault(FIELD, None)
    require(len(shapes) <= 1, 'mixed-state-schemas')
    return result


def validated_migration(value):
    """Build and strictly validate connector state before any schema mutation."""
    migrated = migrate_state(value)
    from notion_sync import validate_state
    validate_state(migrated)
    return migrated


def validate_completed_receipt(receipt, attempt, binding):
    require(isinstance(receipt, dict) and set(receipt) == {
        'source_id', 'field', 'before_schema_hash', 'after_schema_hash',
        'before_state_hash', 'after_state_hash', 'binding'}, 'invalid-migration-receipt')
    require(receipt['source_id'] == SOURCE_ID and receipt['field'] == FIELD and
            receipt['binding'] == binding and
            receipt['before_schema_hash'] == digest(attempt['before']) and
            receipt['before_state_hash'] == attempt['before_state_hash'] and
            all(isinstance(receipt[name], str) and len(receipt[name]) == 64 and
                all(c in '0123456789abcdef' for c in receipt[name])
                for name in ('before_schema_hash', 'after_schema_hash',
                             'before_state_hash', 'after_state_hash')),
            'migration-receipt-mismatch')
    return receipt


def validate_attempt(attempt):
    require(isinstance(attempt, dict) and set(attempt) == {
        'source_id', 'field', 'before', 'before_state_hash', 'expected'} and
        attempt['source_id'] == SOURCE_ID and attempt['field'] == FIELD and
        attempt['expected'] == {'number': {'format': 'number'}} and
        isinstance(attempt['before'], dict) and
        isinstance(attempt['before_state_hash'], str) and
        len(attempt['before_state_hash']) == 64 and
        all(c in '0123456789abcdef' for c in attempt['before_state_hash']),
        'invalid-schema-attempt')
    return attempt


def validate_schema_change(before, after):
    require(isinstance(before, dict) and isinstance(after, dict), 'invalid-schema')
    for value in (before, after):
        require(value.get('object') == 'data_source' and value.get('id') == SOURCE_ID and
                value.get('parent', {}).get('database_id') == DB_ID and
                isinstance(value.get('properties'), dict), 'wrong-container')
    before_props, after_props = before['properties'], after['properties']
    require(FIELD not in before_props and set(after_props) == set(before_props) | {FIELD},
            'unexpected-schema-change')
    require(all(after_props[name] == prop for name, prop in before_props.items()),
            'existing-schema-changed')
    prop = after_props[FIELD]
    require(isinstance(prop, dict) and prop.get('type') == 'number' and
            prop.get('number') == {'format': 'number'} and isinstance(prop.get('id'), str) and
            bool(prop['id']), 'invalid-est-time-property')
    return {'source_id': SOURCE_ID, 'est_time_property_id': prop['id']}


def validate_existing_schema(schema):
    require(isinstance(schema, dict) and schema.get('object') == 'data_source' and
            schema.get('id') == SOURCE_ID and schema.get('parent', {}).get('database_id') == DB_ID,
            'wrong-container')
    prop = schema.get('properties', {}).get(FIELD)
    require(isinstance(prop, dict) and prop.get('type') == 'number' and
            prop.get('number') == {'format': 'number'} and isinstance(prop.get('id'), str) and
            bool(prop['id']), 'invalid-est-time-property')
    return {'source_id': SOURCE_ID, 'est_time_property_id': prop['id']}


def patch_schema(token):
    body = json.dumps({'properties': {FIELD: {'number': {'format': 'number'}}}},
                      separators=(',', ':')).encode()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request('https://api.notion.com/v1/data_sources/' + SOURCE_ID,
        data=body, method='PATCH', headers={'Authorization': 'Bearer ' + token,
        'Notion-Version': API_VERSION, 'Content-Type': 'application/json'})
    try:
        with opener.open(request, timeout=20) as response:
            require(200 <= response.status < 300, 'schema-http-status')
            raw = response.read(16 * 1024 * 1024 + 1)
            require(len(raw) <= 16 * 1024 * 1024, 'schema-response-size-cap')
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        raise SyncError('schema-http-' + str(exc.code)) from None
    except (OSError, ValueError):
        raise SyncError('schema-transport-failure') from None


def run(*, apply=False):
    with State(STATE_ROOT) as store:
        token = store.read_bytes('token').decode('ascii')
        require(token and not any(c.isspace() for c in token), 'invalid-token')
        api = Client(token)
        current = api.request('GET', '/data_sources/' + SOURCE_ID)
        state = store.read()
        if not isinstance(state, dict):
            raise SyncError('invalid-state')
        migrated = validated_migration(state)
        require(not state['pending'], 'pending-operations-block-cutover')
        receipt = store.read(RECEIPT_FILE)
        if FIELD in current.get('properties', {}):
            binding = validate_existing_schema(current)
            attempt = validate_attempt(store.read(ATTEMPT_FILE))
            validate_schema_change(attempt['before'], current)
            if receipt is not None:
                validate_completed_receipt(receipt, attempt, binding)
                require(store.read(BINDING_FILE) == binding and migrated == state,
                        'completed-migration-state-drift')
                return {'planned': 0, 'applied': 0, 'replayed': 1,
                        'migrated_bindings': len(state['bindings']),
                        'migrated_pending': len(state['pending'])}
        else:
            require(receipt is None, 'completed-migration-schema-missing')
            binding = None
            attempt = {'source_id': SOURCE_ID, 'field': FIELD, 'before': current,
                       'before_state_hash': digest(state),
                       'expected': {'number': {'format': 'number'}}}
        if not apply:
            return {'planned': 1,
                    'state_snapshots': sum(1 for _ in state['bindings']) +
                                       sum(sum(op.get(k) is not None for k in ('before', 'target'))
                                           for op in state['pending'].values()),
                    'applied': 0}
        if binding is None:
            store.write(attempt, ATTEMPT_FILE)
            patched = patch_schema(token)
            binding = validate_schema_change(current, patched)
            fresh = Client(token).request('GET', '/data_sources/' + SOURCE_ID)
            require(validate_existing_schema(fresh) == binding and
                    all(fresh['properties'][name] == prop for name, prop in current['properties'].items()),
                    'schema-readback-mismatch')
        store.write(binding, BINDING_FILE)
        store.write(migrated)
        receipt = {'source_id': SOURCE_ID, 'field': FIELD,
                   'before_schema_hash': digest(attempt['before']),
                   'after_schema_hash': digest(Client(token).request('GET', '/data_sources/' + SOURCE_ID)),
                   'before_state_hash': attempt['before_state_hash'],
                   'after_state_hash': digest(migrated), 'binding': binding}
        store.write(receipt, RECEIPT_FILE)
        return {'planned': 1, 'applied': 1, 'migrated_bindings': len(migrated['bindings']),
                'migrated_pending': len(migrated['pending'])}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    print(json.dumps(run(apply=args.apply), sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (SyncError, OSError, ValueError, RuntimeError):
        print('{"error":"est-time-migration-stopped"}')
        raise SystemExit(2)
