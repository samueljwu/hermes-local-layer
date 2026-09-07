"""Task signals keep the last 35 records, not only open work."""
import importlib.util
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('feed_task_signals', Path(__file__).resolve().parents[1] / '_tools/feed_ops.py')
feed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(feed)

def test_name_only_last35_all_statuses_without_external_reads():
    statuses = ('not_started', 'in_progress', 'completed', 'cancelled')
    rows = [dict(name=f'Fixture{i}', status=statuses[i % 4], tag='Research', notes=f'Note{i}') for i in range(40)]
    registry = Path('/home/hermes/tasks/_meta/task_registry.json')
    with patch.object(Path, 'exists', lambda p: p == registry), \
         patch.object(feed, 'load_json', return_value=rows) as read, \
         patch.object(feed, 'tail_text', return_value=''), \
         patch.object(feed, 'promoted_feedback_signals', return_value=[]):
        result = feed.collect_signals()
    read.assert_called_once_with(registry, [])
    signal = next(s for s in result['signals'] if s['kind'] == 'recent_registry')
    assert signal['weight'] == 0.45
    assert signal['text'].splitlines() == [f"{r['name']} {r['tag']} {r['status']} {r['notes']}" for r in rows[-35:]]
    assert all(status in signal['text'] for status in statuses)

def test_schema_status_tokens_are_not_interest_terms():
    assert feed.tokenize('not_started in_progress completed cancelled') == []
