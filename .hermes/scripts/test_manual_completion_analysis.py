"""Characterize the unchanged manual chart's historical-output contract."""
import csv
from datetime import datetime
import importlib.util
import json
from pathlib import Path


def test_manual_chart_joins_name_only_registry_by_stable_suffix(tmp_path, monkeypatch, capsys):
    source = Path(__file__).resolve().parents[2] / 'task-completion-analysis/generate_weekly_chart.py'
    spec = importlib.util.spec_from_file_location('manual_chart_schema_test', source)
    assert spec and spec.loader
    chart = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(chart)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 7, 12, tzinfo=tz)

    registry = tmp_path / '_meta/task_registry.json'
    registry.parent.mkdir()
    projects = registry.with_name('project_registry.json')
    projects.write_text(json.dumps([{'id': 'P-1', 'name': 'School'}, {'id': 'P-2', 'name': 'Admin'}]))
    log = tmp_path / 'log.md'
    out = tmp_path / 'charts'
    registry.write_text(json.dumps([
        {'id': 'T-1-7', 'name': 'Renamed active task', 'project_id': 'P-1', "done": False, 'due_date': None, 'reminder': None, 'recurrence': None, 'priority': 'medium', 'notes': ''},
        {'id': 'T8', 'name': 'Current closed title', 'project_id': 'P-2', "done": True, 'due_date': None, 'reminder': None, 'recurrence': None, 'priority': 'medium', 'notes': ''},
    ]))
    log.write_text('## 2026-09-06\n\n- **T-9-7** — Historical occurrence title — completed occurrence\n- **T8** — Historical closed title — completed\n- **T9** — Cancelled title — cancelled\n')
    before = (registry.read_bytes(), projects.read_bytes(), log.read_bytes())
    for name, value in {'REGISTRY': registry, 'LOG': log, 'OUT': out, 'datetime': FixedDatetime}.items():
        monkeypatch.setattr(chart, name, value)
    chart.main()
    capsys.readouterr()
    summary = json.loads((out / 'summary.json').read_text())
    with (out / 'completed_tasks_rolling_two_months.csv').open() as handle:
        records = list(csv.DictReader(handle))
    assert summary['completed_tasks'] == 2
    assert summary['completed_occurrences'] == 1
    assert summary['by_tag'] == {'Admin': 1, 'School': 1}
    assert {r['task'] for r in records} == {'Historical occurrence title', 'Historical closed title'}
    assert {r['status'] for r in records} == {'completed', 'completed occurrence'}
    assert 'Renamed active task' not in (out / 'completed_tasks_rolling_two_months.csv').read_text()
    assert before == (registry.read_bytes(), projects.read_bytes(), log.read_bytes())
    assert (out / 'weekly_completed_tasks_by_tag.svg').read_text().endswith('</svg>\n')
