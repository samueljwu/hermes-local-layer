from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPT = Path(__file__).with_name("workflow_consistency_audit.py")
SPEC = importlib.util.spec_from_file_location("workflow_consistency_audit_tested", SCRIPT)
assert SPEC and SPEC.loader
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def test_audit_requests_full_generated_content_checks(tmp_path, capsys):
    tasks_root = tmp_path / "tasks"
    journal_root = tmp_path / "journal"
    journal_root.mkdir()
    index = journal_root / "index.md"
    index.write_text("index", encoding="utf-8")
    calls = []

    task_ops = SimpleNamespace(
        TASKS_ROOT=tasks_root,
        read_registry=lambda: [],
        validate_registry=lambda registry, **kwargs: calls.append(("tasks", kwargs)) or [],
    )
    journal_ops = SimpleNamespace(
        JOURNAL_ROOT=journal_root,
        INDEX_PATH=index,
        read_registry=lambda: [],
        validate_registry=lambda registry, **kwargs: calls.append(("journal", kwargs)) or [],
    )

    with mock.patch.object(audit, "load_module", side_effect=[task_ops, journal_ops]):
        assert audit.main() == 0

    assert capsys.readouterr().out == ""
    assert calls[0] == ("tasks", {"check_notes": True, "root": tasks_root})
    assert calls[1][0] == "journal"
    assert calls[1][1]["check_entries"] is True
    assert calls[1][1]["root"] == journal_root
    assert calls[1][1]["index_text"] == "index"
