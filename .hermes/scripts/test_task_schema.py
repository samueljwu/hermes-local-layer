import pytest
from task_schema import OPEN_STATUSES, VALID_STATUSES, is_open, validate_task_shape


@pytest.mark.parametrize('field, value', [
    ('id', 1), ('name', []), ('due_date', []), ('due_date', False),
    ('recurrence', []), ('recurrence', False), ('priority', []),
    ('priority', None), ('project_id', ['School', 'Other']), ('project_id', None),
    ('notes', ['bad']), ('notes', None), ('reminder', []), ('reminder', False),
])
def test_shape_rejects_noncanonical_scalar_types(field, value):
    task = dict(id='T-1-1', name='Task', due_date=None, recurrence=None,
                priority='medium', project_id='P-5', done=False, notes='', reminder=None)
    task[field] = value
    with pytest.raises(ValueError, match=field):
        validate_task_shape(task)


def test_shape_allows_nullable_strings_and_input_date_normalization():
    task = dict(id='T-1-1', name='Task', due_date='May 20, 2026', recurrence=None,
                priority='medium', project_id='P-5', done=False, notes='', reminder=None)
    validate_task_shape(task)
    task.update(due_date=None, recurrence='every 2 weeks', reminder='2026-05-19')
    validate_task_shape(task)


@pytest.mark.parametrize('value', [True, False, -1, 10**400, float('inf'), float('-inf'), float('nan'), '1', []])
def test_est_time_rejects_non_numeric_negative_or_nonfinite_values(value):
    task = dict(id='T-1-1', name='Task', due_date=None, recurrence=None,
                priority='medium', project_id='P-5', done=False, notes='', reminder=None,
                est_time=value)
    with pytest.raises(ValueError, match='est_time'):
        validate_task_shape(task)


@pytest.mark.parametrize('value', [None, 0, 0.5, 2])
def test_est_time_accepts_optional_nonnegative_hours(value):
    task = dict(id='T-1-1', name='Task', due_date=None, recurrence=None,
                priority='medium', project_id='P-5', done=False, notes='', reminder=None,
                est_time=value)
    validate_task_shape(task)


@pytest.mark.parametrize('identity', ['T-1-0', 'T-0-1', 'T0', 'T-01-1', 'T-1-01'])
def test_shape_rejects_nonpositive_or_noncanonical_identity(identity):
    task = dict(id=identity, name='Task', due_date=None, recurrence=None,
                priority='medium', project_id='P-5', done=False, notes='', reminder=None)
    with pytest.raises(ValueError, match='id'):
        validate_task_shape(task)


@pytest.mark.parametrize('projects', [None, {}, [{'id': 'P-0', 'name': 'A'}],
    [{'id': 'P-01', 'name': 'A'}], [{'id': 'P-1', 'name': ''}],
    [{'id': 'P-1', 'name': ' A'}], [{'id': 'P-1', 'name': 'A', 'tag': 'A'}],
    [{'id': 'P-1', 'name': 'A'}, {'id': 'P-1', 'name': 'B'}],
    [{'id': 'P-1', 'name': 'A'}, {'id': 'P-2', 'name': 'A'}]])
def test_project_registry_invalid(projects):
    from task_schema import validate_project_registry
    with pytest.raises(ValueError):
        validate_project_registry(projects)


def test_project_helpers_uncached(tmp_path):
    import json
    from task_schema import validate_project_registry, load_project_registry, project_name
    validate_project_registry([])
    path = tmp_path / '_meta' / 'project_registry.json'
    path.parent.mkdir()
    for name in ('Before', 'After'):
        path.write_text(json.dumps([{'id': 'P-1', 'name': name}]))
        assert project_name({'project_id': 'P-1'}, load_project_registry(tmp_path)) == name
    for pid in ('P-0', 'P-01', 'P-2', None, ['P-1']):
        with pytest.raises(ValueError):
            project_name({'project_id': pid}, load_project_registry(tmp_path))


def test_status_sets():
    assert OPEN_STATUSES == {'not_started', 'in_progress'}
    assert VALID_STATUSES == OPEN_STATUSES | {'completed', 'cancelled'}


@pytest.mark.parametrize('done, result', [(False, True), (True, False)])
def test_open_checkbox(done, result):
    assert is_open({'name': 'Task', 'done': done}) is result


@pytest.mark.parametrize('task', [{}, {'name': 'Task'}, {'name': 'Task', 'status': 'pending'}, {'name': 'Task', 'status': 'unknown'}, {'name': 'Task', 'status': None}, {'name': 'Task', 'status': []}, {'task': 'Legacy', 'done': False}, {'name': 'New', 'task': 'Mixed', 'done': True}])
def test_invalid_and_legacy_fail_closed(task):
    with pytest.raises(ValueError):
        is_open(task)
