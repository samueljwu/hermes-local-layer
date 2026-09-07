"""Task signals keep the last 35 records, not only open work."""
import importlib.util
from pathlib import Path
from unittest.mock import patch
import json
import pytest

spec = importlib.util.spec_from_file_location('feed_task_signals', Path(__file__).resolve().parents[1] / '_tools/feed_ops.py')
feed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(feed)

def test_name_only_last35_all_statuses_without_external_reads(tmp_path):
    states = (False, True)
    rows = [dict(id=f'T-{i+1}-{i+1}', name=f'Fixture{i}', done=states[i % 2], project_id='P-1', notes=f'Note{i}', due_date=None, reminder=None, recurrence=None, priority='medium') for i in range(40)]
    # Open ranks are contiguous even when closed records interleave.
    rank = 0
    for row in rows:
        if not row['done']:
            rank += 1
            row['id'] = f"T-{rank}-{row['id'].split('-')[-1]}"
    registry = Path('/home/hermes/tasks/_meta/task_registry.json')
    projects = [{'id': 'P-1', 'name': 'Research'}]
    real_join = feed.join_projects
    with patch.object(Path, 'exists', lambda p: p == registry), \
         patch.object(feed, 'load_json', return_value=rows) as read, \
         patch.object(feed, 'join_projects', side_effect=lambda tasks, root: real_join(tasks, tmp_path, projects)), \
         patch.object(feed, 'tail_text', return_value=''), \
         patch.object(feed, 'promoted_feedback_signals', return_value=[]):
        result = feed.collect_signals()
    read.assert_called_once_with(registry, [])
    signal = next(s for s in result['signals'] if s['kind'] == 'recent_registry')
    assert signal['weight'] == 0.45
    assert signal['text'].splitlines() == [f"{r['name']} Research {'Done' if r['done'] else 'Pending'} {r['notes']}" for r in rows[-35:]]
    assert all(status in signal['text'] for status in ('Done', 'Pending'))

def test_schema_status_tokens_are_not_interest_terms():
    assert feed.tokenize('not_started in_progress completed cancelled Done Pending') == []
