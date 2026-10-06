"""Offline durability contract for canonical external mutations."""
import pytest
from test_task_ops import task_ops as ops, isolated_side_effects


def test_future_only_default_matrix():
    t = ops.add_task('new', '2026-05-20')
    assert t['start_date'] == '2026-05-20'
    t = ops.amend_task(t['id'], due_date='2026-05-25')
    assert t['start_date'] == '2026-05-20'
    with pytest.raises(ValueError):
        ops.amend_task(t['id'], due_date='2026-05-19')
    t = ops.amend_task(t['id'], clear_start_date=True)
    assert t['start_date'] == '2026-05-25'
    u = ops.add_task('undated')
    assert u['start_date'] is None
    u = ops.amend_task(u['id'], due_date='2026-06-01')
    assert u['start_date'] == '2026-06-01'
    # Synthetic pre-cutover blanks, not a migration of live records.
    rows = ops.read_registry()
    for row in rows:
        row['start_date'] = None
    ops.write_registry(rows)
    ops.regenerate_notes(rows)
    assert all(r['start_date'] is None for r in ops.read_registry())
    t = ops.amend_task(t['id'], name='unrelated edit')
    assert t['start_date'] is None
    full = {f: t.get(f) for f in ops.BUSINESS_FIELDS}
    full['done'] = False
    t = ops.apply_external_change(t['id'], full, t['_revision'], 'unchanged-date-full-snapshot')
    assert t['start_date'] is None
    full = {f: t.get(f) for f in ops.BUSINESS_FIELDS}
    full['due_date'] = '2026-05-26'
    t = ops.apply_external_change(t['id'], full, t['_revision'], 'future-due-edit')
    assert t['start_date'] == t['due_date'] == '2026-05-26'
    email = ops.add_task('Email fixture', '2026-06-01')
    assert email['auto_follow_up']['start_date'] == email['auto_follow_up']['due_date']


def test_start_and_revision():
    t = ops.add_task('work', '2026-05-20', start_date='2026-05-18')
    assert t['_revision'] == 1
    assert ops.amend_task(t['id'], name='work')['_revision'] == 1
    t = ops.amend_task(t['id'], clear_start_date=True)
    assert t['start_date'] == '2026-05-20' and t['_revision'] == 2
    with pytest.raises(ValueError):
        ops.amend_task(t['id'], start_date='2026-05-21')


def test_external_replay_repair(monkeypatch):
    t = ops.add_task('work', '2026-05-20', recurrence='weekly', start_date='2026-05-18')
    original = ops.regenerate_notes
    monkeypatch.setattr(ops, 'regenerate_notes', lambda *a: (_ for _ in ()).throw(OSError('cache')))
    with pytest.raises(OSError):
        ops.apply_external_change(t['id'], {'done': True}, 1, 'complete')
    committed = ops.read_registry()[0]
    assert committed['due_date'] == '2026-05-27' and committed['start_date'] == '2026-05-25'
    monkeypatch.setattr(ops, 'regenerate_notes', original)
    result = ops.apply_external_change(t['id'], {'done': True}, 1, 'complete')
    assert result['_revision'] == 2 and result['due_date'] == '2026-05-27'
    assert ops.LOG_PATH.read_text().count('completed occurrence') == 1
    assert ops.validate_registry(ops.read_registry(), check_notes=True) == []
    with pytest.raises(ValueError):
        ops.apply_external_change(t['id'], {'status': 'cancelled'}, 1, 'complete')


@pytest.mark.parametrize('phase', ['write_registry', '_flush_external_events', 'regenerate_notes', 'regenerate_index'])
def test_interrupted_commit_and_repair(monkeypatch, phase):
    t = ops.add_task('monthly', '2026-01-31', recurrence='monthly')
    original = getattr(ops, phase)
    calls = 0
    def fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        # First event flush is before commit; second is after commit.
        if phase != '_flush_external_events' or calls == 2:
            raise OSError('interrupted')
        return original(*args, **kwargs)
    before = ops.REGISTRY_PATH.read_bytes()
    monkeypatch.setattr(ops, phase, fail)
    with pytest.raises(OSError):
        ops.apply_external_change(t['id'], {'done': True}, 1, 'op')
    if phase == 'write_registry':
        assert ops.REGISTRY_PATH.read_bytes() == before
        assert not ops.LOG_PATH.exists()
    else:
        assert ops.read_registry()[0]['due_date'] == '2026-02-28'
    monkeypatch.setattr(ops, phase, original)
    result = ops.apply_external_change(t['id'], {'done': True}, 1, 'op')
    assert result['due_date'] == result['start_date'] == '2026-02-28'
    assert result['_revision'] == 2
    assert ops.LOG_PATH.read_text().count('completed occurrence') == 1
    assert not ops.validate_registry(ops.read_registry(), check_notes=True)


def test_log_write_completed_then_raised_and_prior_flush(monkeypatch):
    t = ops.add_task('weekly', '2026-05-20', recurrence='weekly')
    original = ops.atomic_write_text
    def fail_after_log(path, text):
        original(path, text)
        if path == ops.LOG_PATH:
            raise OSError('lost acknowledgement')
    monkeypatch.setattr(ops, 'atomic_write_text', fail_after_log)
    with pytest.raises(OSError):
        ops.apply_external_change(t['id'], {'done': True}, 1, 'first')
    monkeypatch.setattr(ops, 'atomic_write_text', original)
    # Simulate missing derived history, then publish a newer operation.
    ops.LOG_PATH.unlink()
    updated = ops.apply_external_change(t['id'], {'notes': 'new'}, 2, 'second')
    assert ops.LOG_PATH.read_text().count('completed occurrence') == 1
    replay = ops.apply_external_change(t['id'], {'done': True}, 1, 'first')
    assert replay['_revision'] == updated['_revision'] == 3
    assert replay['due_date'] == '2026-05-27'
    assert len(replay['_external_receipt']['operations']) == 2


def test_all_fields_clears_and_due_only_order():
    first = ops.add_task('first', '2026-05-25', start_date='2026-01-01')
    t = ops.add_task('second', '2026-05-20')
    assert t['id'].startswith('T-1-')
    fields = dict(name='renamed', done=False, start_date='2026-05-18',
                  due_date='2026-05-21', priority='high', project_id='P-7', notes='edited',
                  recurrence='daily', est_time=1.25)
    t = ops.apply_external_change(t['id'], fields, 1, 'all')
    assert all(t[k] == v for k, v in fields.items())
    assert t['reminder'] == '2026-05-20'
    assert ops.resolve_task_identity(ops.read_registry(), first['id'])['_revision'] == 1
    t = ops.apply_external_change(t['id'], dict(start_date=None, due_date=None, recurrence=None, est_time=None), 2, 'clear')
    assert t['start_date'] is t['due_date'] is t['recurrence'] is t['reminder'] is t['est_time'] is None
    assert ops.apply_external_change(t['id'], {}, 3, 'noop')['_revision'] == 3


def test_checkbox_completion_and_deliberate_reopen():
    t = ops.add_task('one', '2026-05-20')
    t = ops.apply_external_change(t['id'], {'done': True}, 1, 'close')
    assert t['done'] is True and t['_revision'] == 2
    assert ops.apply_external_change(t['id'], {'done': True}, 2, 'noop')['_revision'] == 2
    t = ops.apply_external_change(t['id'], {'done': False}, 2, 'reopen')
    assert t['done'] is False and t['_revision'] == 3
    assert ops.task_note_path(t).exists()
    assert ops.apply_external_change(t['id'], {'done': True}, 1, 'close')['done'] is False
    assert ops.LOG_PATH.read_text().count('— completed') == 1


@pytest.mark.parametrize('bad', [True, -1, 1.0, None, '0'])
def test_strict_revision_even_closed(bad):
    t = ops.add_task('one')
    t['done'] = True
    t['_revision'] = bad
    ops.write_registry([t])
    with pytest.raises(ValueError):
        ops.apply_external_change(t['id'], {}, 1, 'bad')


@pytest.mark.parametrize('field,value', [('start_date', '2026-02-30'), ('start_date', False),
                                        ('start_date', '2026-05-21'), ('priority', 'bogus'),
                                        ('status', 'pending'), ('name', None), ('notes', []),
                                        ('est_time', True), ('est_time', -1), ('est_time', float('inf')),
                                        ('_revision', 99), ('_external_receipt', {})])
def test_invalid_external_before_commit(field, value):
    t = ops.add_task('one', '2026-05-20')
    before = ops.REGISTRY_PATH.read_bytes()
    with pytest.raises(ValueError):
        ops.apply_external_change(t['id'], {field: value}, 1, 'invalid')
    assert ops.REGISTRY_PATH.read_bytes() == before


def test_cli_start_and_legacy_missing_revision(capsys):
    assert ops.main(['add', 'cli', '--start-date', '2026-05-18', '--due-date', '2026-05-20']) == 0
    t = ops.read_registry()[0]
    assert ops.main(['amend', t['id'], '--clear-start-date']) == 0
    t = ops.read_registry()[0]
    assert t['start_date'] == '2026-05-20'
    del t['start_date'], t['_revision']
    ops.write_registry([t])
    assert not ops.validate_registry(ops.read_registry())
    t = ops.apply_external_change(t['id'], {'notes': 'legacy'}, 0, 'legacy')
    assert t['_revision'] == 1


def test_local_close_shifts_and_increments():
    t = ops.add_task('month', '2026-01-31', recurrence='monthly', start_date='2026-01-29')
    t = ops.close_task(t['id'])
    assert t['_revision'] == 2 and t['start_date'] == '2026-02-26'
    assert t['due_date'] == '2026-02-28'


def test_atomic_rename_failure_and_lost_commit_ack(monkeypatch):
    t = ops.add_task('weekly', '2026-05-20', recurrence='weekly')
    replace = ops.os.replace
    before = ops.REGISTRY_PATH.read_bytes()
    def deny(*a, **kw):
        raise OSError('before rename')
    monkeypatch.setattr(ops.os, 'replace', deny)
    with pytest.raises(OSError):
        ops.apply_external_change(t['id'], {'done': True}, 1, 'rename')
    assert ops.REGISTRY_PATH.read_bytes() == before
    monkeypatch.setattr(ops.os, 'replace', replace)
    write = ops.write_registry
    def lost_ack(registry):
        write(registry)
        raise OSError('after durable registry publication')
    monkeypatch.setattr(ops, 'write_registry', lost_ack)
    with pytest.raises(OSError):
        ops.apply_external_change(t['id'], {'done': True}, 1, 'rename')
    monkeypatch.setattr(ops, 'write_registry', write)
    t = ops.apply_external_change(t['id'], {'done': True}, 1, 'rename')
    assert t['due_date'] == '2026-05-27' and t['_revision'] == 2
    assert ops.LOG_PATH.read_text().count('completed occurrence') == 1


def test_registry_parent_fsync_failure_replay_and_current_state(monkeypatch):
    from test_task_ops import managed_bytes

    t = ops.add_task('weekly', '2026-05-20', recurrence='weekly', start_date='2026-05-18')
    before = managed_bytes(ops.TASKS_ROOT)
    parent = ops.REGISTRY_PATH.parent.stat()
    real_fsync = ops.os.fsync
    attempts = 0
    failing = True

    def parent_fsync(fd):
        nonlocal attempts
        info = ops.os.fstat(fd)
        if (info.st_dev, info.st_ino) == (parent.st_dev, parent.st_ino):
            attempts += 1
            if failing:
                raise OSError('canonical parent fsync failed')
        return real_fsync(fd)

    monkeypatch.setattr(ops.os, 'fsync', parent_fsync)
    for name in ('refresh_tasks_dashboard', 'refresh_google_calendar', 'remove_enabled_reminder_crons_for_task'):
        monkeypatch.setattr(ops, name, pytest.fail)
    for expected_attempts in (1, 2, 3):
        with pytest.raises(OSError, match='canonical parent fsync failed'):
            ops.apply_external_change(t['id'], {'done': True}, 1, 'dir-fsync')
        assert attempts == expected_attempts
        current = ops.read_registry()[0]
        assert current['_revision'] == 2
        assert current['due_date'] == '2026-05-27'
        assert current['start_date'] == '2026-05-25'
        assert len(current['_external_receipt']['operations']) == 1
        after = managed_bytes(ops.TASKS_ROOT)
        assert {k: v for k, v in after.items() if k != '_meta/task_registry.json'} == {
            k: v for k, v in before.items() if k != '_meta/task_registry.json'}

    failing = False
    monkeypatch.setattr(ops, 'refresh_tasks_dashboard', lambda: None)
    monkeypatch.setattr(ops, 'refresh_google_calendar', lambda: None)
    # A subsequent local edit must survive replay of the old receipt.
    ops.amend_task(t['id'], notes='later local edit')
    later = ops.read_registry()[0]
    before_replay = ops.REGISTRY_PATH.read_bytes()
    # Even after later edits, another failed replay must not restore old state.
    failing = True
    with pytest.raises(OSError, match='canonical parent fsync failed'):
        ops.apply_external_change(t['id'], {'done': True}, 1, 'dir-fsync')
    assert ops.REGISTRY_PATH.read_bytes() == before_replay
    assert not ops.LOG_PATH.exists()
    failing = False
    attempts_before_replay = attempts
    result = ops.apply_external_change(t['id'], {'done': True}, 1, 'dir-fsync')
    assert attempts > attempts_before_replay
    assert result == later
    assert result['_revision'] == 3 and result['notes'] == 'later local edit'
    assert ops.REGISTRY_PATH.read_bytes() == before_replay
    assert ops.LOG_PATH.read_text().count('completed occurrence') == 1
    assert not ops.validate_registry(ops.read_registry(), check_notes=True)
    assert ops.apply_external_change(t['id'], {'done': True}, 1, 'dir-fsync') == result
    assert ops.LOG_PATH.read_text().count('completed occurrence') == 1


@pytest.mark.parametrize('field', ['due_date', 'start_date'])
@pytest.mark.parametrize('value', ['', ' ', 'null', 'None', 'none', 'NULL', 'nil', 'false',
                                 False, 0, [], {}, '2026-02-30'])
@pytest.mark.parametrize('intake', [False, True])
def test_external_date_tokens_rejected_without_effects(monkeypatch, field, value, intake):
    from test_task_ops import managed_bytes

    t = ops.add_task('dated', '2026-05-20', start_date='2026-05-18')
    before = managed_bytes(ops.TASKS_ROOT)
    for name in ('write_registry', '_flush_external_events', 'regenerate_notes',
                 'regenerate_index', 'refresh_tasks_dashboard', 'refresh_google_calendar',
                 'remove_enabled_reminder_crons_for_task'):
        monkeypatch.setattr(ops, name, lambda *a, **kw: pytest.fail('invalid date caused a side effect'))
    with pytest.raises(ValueError):
        ops.apply_external_change(None if intake else t['id'],
                                  dict(name='dated', **{field: value}),
                                  None if intake else 1, 'invalid-date',
                                  'intake-page' if intake else None)
    assert managed_bytes(ops.TASKS_ROOT) == before


def test_local_empty_due_normalization_unchanged():
    t = ops.add_task('local', '')
    assert t['due_date'] is None and t['reminder'] is None


def test_revision_read_is_under_lock(monkeypatch):
    import contextlib
    t = ops.add_task('one')
    actual_lock = ops.file_lock
    @contextlib.contextmanager
    def concurrent_edit_before_acquire():
        # Emulate the preceding writer completing before our acquisition.
        registry = ops.read_registry()
        registry[0]['_revision'] += 1
        ops.write_registry(registry)
        with actual_lock():
            yield
    monkeypatch.setattr(ops, 'file_lock', concurrent_edit_before_acquire)
    with pytest.raises(ValueError, match='revision conflict'):
        ops.apply_external_change(t['id'], {'notes': 'stale'}, 1, 'racing')
    assert ops.read_registry()[0]['notes'] == ''


@pytest.mark.parametrize('corrupt', [{'start_date': 'bad'}, {'_external_receipt': None},
                                   {'_external_receipt': {'operations': {'x': {'payload_hash': '0'*64, 'log_entry': '', 'cleanup_id': None}}}}])
def test_malformed_closed_record_blocks_replay(corrupt):
    t = ops.apply_external_change(None, {'name': 'one'}, None, 'new', 'page')
    t['done'] = True
    t.update(corrupt)
    ops.write_registry([t])
    before = ops.REGISTRY_PATH.read_bytes()
    with pytest.raises(ValueError):
        ops.apply_external_change(None, {'name': 'one'}, None, 'new', 'page')
    assert ops.REGISTRY_PATH.read_bytes() == before


def test_intake_and_aba():
    t = ops.apply_external_change(None, {'name': 'email Pat', 'due_date': '2026-05-20'}, None, 'new', 'page')
    assert len(ops.read_registry()) == 2
    ops.amend_task(t['id'], notes='away')
    ops.amend_task(t['id'], notes='')
    with pytest.raises(ValueError):
        ops.apply_external_change(t['id'], {'notes': 'stale'}, 1, 'stale')
    again = ops.apply_external_change(None, {'name': 'email Pat', 'due_date': '2026-05-20'}, None, 'new', 'page')
    assert again['_revision'] == 3 and len(ops.read_registry()) == 2
    duplicate = ops.apply_external_change(None, {'name': 'duplicate'}, None, 'newer', 'page')
    assert duplicate['name'] == 'email Pat' and duplicate['_revision'] == 3
    assert len(ops.read_registry()) == 2
