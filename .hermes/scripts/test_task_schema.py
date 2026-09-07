import pytest
from task_schema import OPEN_STATUSES, VALID_STATUSES, is_open, validate_task_shape


@pytest.mark.parametrize('field, value', [
    ('id', 1), ('name', []), ('due_date', []), ('due_date', False),
    ('recurrence', []), ('recurrence', False), ('priority', []),
    ('priority', None), ('tag', ['School', 'Other']), ('tag', None),
    ('notes', ['bad']), ('notes', None), ('reminder', []), ('reminder', False),
])
def test_shape_rejects_noncanonical_scalar_types(field, value):
    task = dict(id='T-1-1', name='Task', due_date=None, recurrence=None,
                priority='medium', tag='Other', status='not_started', notes='', reminder=None)
    task[field] = value
    with pytest.raises(ValueError, match=field):
        validate_task_shape(task)


def test_shape_allows_nullable_strings_and_input_date_normalization():
    task = dict(id='T-1-1', name='Task', due_date='May 20, 2026', recurrence=None,
                priority='medium', tag='Other', status='not_started', notes='', reminder=None)
    validate_task_shape(task)
    task.update(due_date=None, recurrence='every 2 weeks', reminder='2026-05-19')
    validate_task_shape(task)


@pytest.mark.parametrize('identity', ['T-1-0', 'T-0-1', 'T0', 'T-01-1', 'T-1-01'])
def test_shape_rejects_nonpositive_or_noncanonical_identity(identity):
    task = dict(id=identity, name='Task', due_date=None, recurrence=None,
                priority='medium', tag='Other', status='not_started', notes='', reminder=None)
    with pytest.raises(ValueError, match='id'):
        validate_task_shape(task)


def test_status_sets():
    assert OPEN_STATUSES == {'not_started', 'in_progress'}
    assert VALID_STATUSES == OPEN_STATUSES | {'completed', 'cancelled'}


@pytest.mark.parametrize('status, result', [('not_started', True), ('in_progress', True), ('completed', False), ('cancelled', False)])
def test_open_statuses(status, result):
    assert is_open({'name': 'Task', 'status': status}) is result


@pytest.mark.parametrize('task', [{}, {'name': 'Task'}, {'name': 'Task', 'status': 'pending'}, {'name': 'Task', 'status': 'unknown'}, {'name': 'Task', 'status': None}, {'name': 'Task', 'status': []}, {'task': 'Legacy', 'status': 'not_started'}, {'name': 'New', 'task': 'Mixed', 'status': 'completed'}])
def test_invalid_and_legacy_fail_closed(task):
    with pytest.raises(ValueError):
        is_open(task)
