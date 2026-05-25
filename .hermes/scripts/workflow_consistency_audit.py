#!/usr/bin/env python3
"""Silent consistency audit for journal/tasks workflows.

For Hermes cron with no_agent=True: prints only if an actionable inconsistency is
found; empty stdout means healthy/silent.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    task_ops = load_module('task_ops', Path.home() / 'tasks' / '_tools' / 'task_ops.py')
    journal_ops = load_module('journal_ops', Path.home() / 'journal' / '_tools' / 'journal_ops.py')
    issues = []

    try:
        task_registry = task_ops.read_registry()
        issues.extend(f'tasks: {x}' for x in task_ops.validate_registry(task_registry))
        pending_ids = {str(t.get('id')) for t in task_registry if t.get('status', 'pending') == 'pending'}
        note_ids = {p.stem for p in (Path.home() / 'tasks').glob('*/T*.md')}
        for tid in sorted(pending_ids - note_ids):
            issues.append(f'tasks: pending {tid} missing derived note')
        for tid in sorted(note_ids - pending_ids):
            issues.append(f'tasks: stale derived note for non-pending {tid}')
    except Exception as exc:
        issues.append(f'tasks: audit failed: {exc}')

    try:
        journal_registry = journal_ops.read_registry()
        index_path = Path.home() / 'journal' / 'index.md'
        index_text = index_path.read_text(errors='replace') if index_path.exists() else ''
        issues.extend(f'journal: {x}' for x in journal_ops.validate_registry(journal_registry, index_text=index_text))
    except Exception as exc:
        issues.append(f'journal: audit failed: {exc}')

    if issues:
        print('Workflow consistency audit found issues:')
        for issue in issues:
            print(f'- {issue}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
