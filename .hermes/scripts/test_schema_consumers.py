"""Offline name-only/boolean consumer contracts; no runtime calls."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
import pytest

PROJECTS = [{"id": "P-1", "name": "Research"}, {"id": "P-2", "name": "Archive"}]

def canonical(**fields):
    from datetime import date, timedelta
    row = dict(due_date=None, reminder=None, recurrence=None, priority='medium', notes='', project_id='P-1')
    row.update(fields)
    if row['due_date']:
        row['reminder'] = (date.fromisoformat(row['due_date']) - timedelta(days=1)).isoformat()
    return row

def joined(rows):
    names = {p['id']: p['name'] for p in PROJECTS}
    return [dict(row, project_name=names[row['project_id']]) for row in rows]

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

@pytest.fixture
def rows():
    return [canonical(id=f'T-{i}-{i}', name=f'Name {i}', done=done,
                 project_id='P-1' if i < 3 else 'P-2', notes='fixture', priority='medium')
            for i, done in enumerate((False, False, True, True), 1)]

def test_dashboard_four_states_and_all_record_commands(rows):
    module = load('schema_dashboard', SCRIPTS / 'update_tasks_dashboard.py')
    text = module.build_dashboard(rows, projects=PROJECTS)
    assert 'Name 1' in text and 'Name 2' in text
    assert 'Name 3' not in text and 'Name 4' not in text
    assert '/archive' in text

@pytest.mark.parametrize('bad', [None, 'pending', 'unknown', 0, 1, [], {}])
def test_display_consumers_fail_closed(bad):
    row = {'id': 'T-1-1', 'name': 'Invalid', 'project_id': 'P-1'}
    if bad is not None:
        row['done'] = bad
    dashboard = load('schema_dashboard', SCRIPTS / 'update_tasks_dashboard.py')
    tags = load('schema_tags', SCRIPTS.parent / 'plugins/tasks-tags/__init__.py')
    with pytest.raises(ValueError):
        dashboard.build_dashboard([row], projects=PROJECTS)
    with pytest.raises(ValueError):
        tags._render_tag([row], 'Research')

def test_pending_tasks_in_multiple_projects_remain_open():
    dashboard = load('schema_dashboard', SCRIPTS / 'update_tasks_dashboard.py')
    rows = [canonical(id='T-1-1', name='Research pending', done=False, project_id='P-1'),
            canonical(id='T-2-2', name='Archive pending', done=False, project_id='P-2')]
    text = dashboard.build_dashboard(rows, projects=PROJECTS)
    assert 'Research pending' in text and 'Archive pending' in text

@pytest.mark.parametrize('legacy', ['not_started', 'in_progress', 'completed', 'cancelled'])
def test_dashboard_rejects_legacy_status_even_with_valid_checkbox(legacy):
    dashboard = load('schema_dashboard', SCRIPTS / 'update_tasks_dashboard.py')
    row = canonical(id='T-1-1', name='Invalid', done=False, status=legacy)
    with pytest.raises(ValueError):
        dashboard.build_dashboard([row], projects=PROJECTS)

def test_dashboard_backfills_five_and_keeps_long_term():
    module = load('schema_dashboard', SCRIPTS / 'update_tasks_dashboard.py')
    rows = [canonical(id=f'T-{i}-{i}', name=f'Near {i}', done=False,
                 due_date='2020-01-01', project_id='P-1') for i in range(1, 7)]
    rows.insert(0, canonical(id='T-7-7', name='Long priority', done=False,
                        due_date='2999-01-01', priority='high', project_id='P-1'))
    text = module.build_dashboard(rows, projects=PROJECTS)
    near, long = text.split('**Top long-term priorities**')
    assert all(f'Near {i}' in near for i in range(1, 6))
    assert 'Near 6' not in text and 'Long priority' not in near
    assert 'Long priority - High' in long

def test_tags_single_tag_open_count_and_all_record_registration(rows):
    tags = load('schema_tags', SCRIPTS.parent / 'plugins/tasks-tags/__init__.py')
    sync = load('schema_sync', SCRIPTS / 'discord_tag_commands.py')
    with patch.object(tags, '_read_registry', return_value=joined(rows)):
        assert '2 open task(s)' in tags._handle_tags()
        assert 'Name 2' in tags._handle_tag_slug('research')
        assert 'No open tasks' in tags._handle_tag_slug('archive')
    assert sync.get_current_tags(rows, projects=PROJECTS) == {'Research', 'Archive'}
    import discord_tag_commands as shared_sync
    registered = {}
    class Context:
        def register_command(self, name, **kwargs):
            registered[name] = kwargs
            return object()
    with patch.object(tags, '_read_registry', return_value=joined(rows)), \
         patch.object(shared_sync, 'reserved_commands', return_value=set()):
        assert tags.LiveTags(Context()).refresh_handlers() == {'archive': 'Archive', 'research': 'Research'}
    assert set(registered) == {'archive', 'research'}
    assert all('open tasks' in entry['description'] for entry in registered.values())
    assert 'Name' not in tags._render_tag(joined(rows), 'Res')

@pytest.mark.parametrize('description', ["Show all pending tasks tagged 'Research'", "Show pending tasks for the Research tag.", "Show all open tasks tagged 'Research'", "Show open tasks for the Research tag."])
def test_exact_old_new_command_ownership(description):
    sync = load('schema_sync', SCRIPTS / 'discord_tag_commands.py')
    assert sync._owned(dict(name='research', type=1, description=description))
    assert not sync._owned(dict(name='other', type=1, description=description))
    assert not sync._owned(dict(name='research', type=2, description=description))
    assert not sync._owned(dict(name='research', type=1, description=description+' extra'))

def test_canonical_registry_paths(monkeypatch):
    for key in ('TASKS_ROOT', 'HERMES_ALLOW_NONCANONICAL_LOCAL_ROOTS'):
        monkeypatch.delenv(key, raising=False)
    for name, path in [('dashboard', SCRIPTS / 'update_tasks_dashboard.py'),
                       ('tags', SCRIPTS.parent / 'plugins/tasks-tags/__init__.py'),
                       ('outstanding', SCRIPTS.parent / 'plugins/tasks-outstanding/__init__.py'),
                       ('sync', SCRIPTS / 'discord_tag_commands.py')]:
        module = load('canonical_' + name, path)
        assert module.REGISTRY_PATH == Path('/home/hermes/tasks/_meta/task_registry.json')
        assert module.LOCAL_SCRIPTS == SCRIPTS


def test_remote_legacy_description_refresh_and_readback(tmp_path):
    sync = load('schema_sync', SCRIPTS / 'discord_tag_commands.py')
    fixture = load('sync_fixture', SCRIPTS.parent / 'plugins/tasks-tags/tests/test_sync.py')
    rest = fixture.FakeREST([dict(id='1', name='research', type=1, options=[],
                                  description="Show all pending tasks tagged 'Research'")])
    with patch.object(sync, 'HERMES_HOME', tmp_path), patch.object(sync, 'discord_request', rest):
        assert sync.reconcile('fixture', {'Research'}, app_id='123', reserved=set())['changed'] == 1
        assert rest.writes[0][0] == 'PATCH'
        assert rest.calls[-1][:2] == ('GET', '/applications/123/commands/1')
        assert rest.commands['1']['description'] == "Show all open tasks tagged 'Research'"
        assert sync.reconcile('fixture', {'Research'}, app_id='123', reserved=set())['changed'] == 0


def test_native_owned_description_refresh_preserves_callback():
    tags = load('schema_tags', SCRIPTS.parent / 'plugins/tasks-tags/__init__.py')
    import discord_tag_commands as sync
    existing = SimpleNamespace(description="Show all pending tasks tagged 'Research'", callback=object())
    callback = existing.callback
    tree = SimpleNamespace(get_command=lambda slug: existing)
    with patch.dict(sys.modules, {'discord': SimpleNamespace(app_commands=SimpleNamespace())}):
        live = tags.LiveTags(None)
        assert live.refresh_native(SimpleNamespace(tree=tree), None, {'research': 'Research'}) == {'research': 'Research'}
    assert existing.description == sync.build_tag_command('Research')['description']
    assert existing.callback is callback
    existing.description = 'Unrelated research command'
    with patch.dict(sys.modules, {'discord': SimpleNamespace(app_commands=SimpleNamespace())}):
        assert live.refresh_native(SimpleNamespace(tree=tree), None, {'research': 'Research'}) == {}
    assert existing.description == 'Unrelated research command'
