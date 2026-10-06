"""Standalone offline /tasksout rendering contracts."""
import importlib.util
from pathlib import Path
from unittest.mock import patch
import pytest

spec = importlib.util.spec_from_file_location('outstanding_rendering', Path(__file__).resolve().parents[1] / '__init__.py')
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

def test_four_states_name_only_sort_and_priority():
    rows = [dict(id=f'T-{i}-{i}', name=f'Fixture {i}.', done=done, project_id='P-1', project_name='Research',
                 priority='high', due_date='2026-01-01')
            for i, done in enumerate((False, False, True, True), 1)]
    with patch.object(plugin, '_read_registry', return_value=list(reversed(rows))):
        text = plugin._handle_outstanding('ignored')
    assert text.splitlines() == [
        'T-1-1 - Thu Jan 1 - Research - Fixture 1 - High',
        'T-2-2 - Thu Jan 1 - Research - Fixture 2 - High']

@pytest.mark.parametrize('row', [{'name': 'missing'}, {'name': 'bad', 'status': 'pending'}])
def test_invalid_status_raises(row):
    with patch.object(plugin, '_read_registry', return_value=[row]), pytest.raises(ValueError):
        plugin._handle_outstanding()

def test_empty_and_argumentless_registration():
    with patch.object(plugin, '_read_registry', return_value=[]):
        assert plugin._handle_outstanding() == 'No outstanding tasks found.'
    calls = []
    class Context:
        def register_command(self, *args, **kwargs):
            calls.append((args, kwargs))
    plugin.register(Context())
    assert calls[0][0] == ('tasksout',)
    assert 'args_hint' not in calls[0][1]
