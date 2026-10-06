from copy import deepcopy
import uuid

import pytest

import notion_est_time_migration as m
from notion_transport import DB_ID, SOURCE_ID, SyncError


def old_fields():
    return dict(name='Task', project_id='P-1', done=True, start_date=None,
                due_date=None, priority='medium', recurrence=None, notes='')


def state(with_pending=True):
    pending = {'hermes_tasks:1': {
        'kind': 'push', 'phase': 'prepared', 'operation_id': str(uuid.UUID(int=20)),
        'task_id': 'T1', 'page_id': str(uuid.UUID(int=10)), 'source_revision': 0,
        'before': old_fields(), 'target': old_fields(), 'remote_revision': 'old-edit',
        'result_revision': None, 'marker': 'hermes_tasks:1', 'create_sent': False,
    }} if with_pending else {}
    return {'version': 1, 'source_id': SOURCE_ID, 'database_id': DB_ID,
            'ignored': [str(uuid.UUID(int=i)) for i in range(1, 4)],
            'bindings': {'hermes_tasks:1': {'page_id': str(uuid.UUID(int=10)),
                'baseline': old_fields(), 'revision': 0, 'receipt': 'old'}},
            'pending': pending}


def schema(with_estimate=False):
    properties = {'name': {'id': 'title', 'name': 'name', 'type': 'title', 'title': {}}}
    if with_estimate:
        properties['est_time'] = {'id': 'estimate', 'name': 'est_time', 'type': 'number',
                                  'number': {'format': 'number'}}
    return {'object': 'data_source', 'id': SOURCE_ID,
            'parent': {'database_id': DB_ID}, 'properties': properties}


def test_state_migration_preserves_input_and_adds_only_null_estimates():
    before = state()
    original = deepcopy(before)
    after = m.migrate_state(before)
    assert before == original
    assert after['bindings']['hermes_tasks:1']['baseline']['est_time'] is None
    assert after['pending']['hermes_tasks:1']['before']['est_time'] is None
    assert after['pending']['hermes_tasks:1']['target']['est_time'] is None
    after_again = m.migrate_state(after)
    assert after_again == after
    assert m.validated_migration(before) == after


def test_state_migration_rejects_malformed_and_mixed_state():
    malformed = state(False)
    malformed['bindings']['hermes_tasks:1']['revision'] = 'zero'
    with pytest.raises(SyncError):
        m.validated_migration(malformed)
    mixed = state()
    mixed['bindings']['hermes_tasks:1']['baseline']['est_time'] = None
    with pytest.raises(SyncError, match='mixed-state-schemas'):
        m.migrate_state(mixed)


def test_schema_change_accepts_exact_number_addition_only():
    before, after = schema(), schema(True)
    assert m.validate_schema_change(before, after) == {
        'source_id': SOURCE_ID, 'est_time_property_id': 'estimate'}
    changed = deepcopy(after)
    changed['properties']['name']['name'] = 'renamed'
    with pytest.raises(SyncError, match='existing-schema-changed'):
        m.validate_schema_change(before, changed)
    bad = deepcopy(after)
    bad['properties']['est_time']['number']['format'] = 'percent'
    with pytest.raises(SyncError, match='invalid-est-time-property'):
        m.validate_schema_change(before, bad)


def test_existing_schema_requires_number_hours_column():
    assert m.validate_existing_schema(schema(True))['est_time_property_id'] == 'estimate'
    with pytest.raises(SyncError):
        m.validate_existing_schema(schema())


def test_completed_run_is_an_idempotent_read_only_replay(monkeypatch):
    before = state(False)
    current_state = m.validated_migration(before)
    attempt = {'source_id': SOURCE_ID, 'field': m.FIELD, 'before': schema(),
               'before_state_hash': m.digest(before),
               'expected': {'number': {'format': 'number'}}}
    binding = {'source_id': SOURCE_ID, 'est_time_property_id': 'estimate'}
    receipt = {'source_id': SOURCE_ID, 'field': m.FIELD,
               'before_schema_hash': m.digest(attempt['before']),
               'after_schema_hash': 'a' * 64,
               'before_state_hash': attempt['before_state_hash'],
               'after_state_hash': 'b' * 64, 'binding': binding}

    class FakeState:
        writes = []
        def __init__(self, _root): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def read_bytes(self, _name): return b'token'
        def read(self, name='two-way.json'):
            return {m.ATTEMPT_FILE: attempt, m.RECEIPT_FILE: receipt,
                    m.BINDING_FILE: binding}.get(name, current_state)
        def write(self, value, name='two-way.json'):
            self.writes.append((name, value))

    class FakeClient:
        def __init__(self, _token): pass
        def request(self, method, path):
            assert (method, path) == ('GET', '/data_sources/' + SOURCE_ID)
            return schema(True)

    monkeypatch.setattr(m, 'State', FakeState)
    monkeypatch.setattr(m, 'Client', FakeClient)
    result = m.run(apply=True)
    assert result['planned'] == result['applied'] == 0
    assert result['replayed'] == 1
    assert FakeState.writes == []
