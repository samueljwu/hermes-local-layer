#!/usr/bin/env python3
"""Move the device-roadmap task to today if its daily occurrence was missed."""
from __future__ import annotations

import argparse
import importlib.util
import json
from datetime import date, datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

TASK_OPS_PATH = Path(__file__).resolve().parent / "task_ops.py"
LOCAL_TZ = ZoneInfo("Asia/Hong_Kong")
TASK_SUFFIX = "309"
EXPECTED_NAME = "work on device design roadmap"
EXPECTED_PROJECT_ID = "P-3"
EXPECTED_RECURRENCE = "daily"
EXPECTED_EST_TIME = 1.5


def load_task_ops():
    spec = importlib.util.spec_from_file_location("task_ops_roll_forward", TASK_OPS_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load canonical task operations")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def roll_forward(today: date, *, dry_run: bool = False) -> dict:
    ops = load_task_ops()
    with ops.file_lock():
        registry = ops.read_registry()
        ops.require_valid_registry(registry)
        task = ops.resolve_task_identity(registry, TASK_SUFFIX)
        if not ops.is_open(task):
            return {"action": "none", "task_id": task["id"], "reason": "task is closed"}
        expected = {
            "name": EXPECTED_NAME,
            "project_id": EXPECTED_PROJECT_ID,
            "recurrence": EXPECTED_RECURRENCE,
            "est_time": EXPECTED_EST_TIME,
            "done": False,
        }
        ops.check_expected(task, expected)
        due = ops.parse_due(task.get("due_date"))
        if due is None:
            raise ValueError("device-roadmap task has no valid due date")
        if due >= today:
            return {"action": "none", "task_id": task["id"], "due_date": task["due_date"]}
        if dry_run:
            return {"action": "roll_forward", "task_id": task["id"], "from": task["due_date"], "to": today.isoformat()}
        current_due = task["due_date"]

    # The expected snapshot is checked again inside amend_task's mutation lock,
    # so a concurrent completion or edit cannot be overwritten.
    updated = ops.amend_task(
        TASK_SUFFIX,
        due_date=today.isoformat(),
        expected={**expected, "due_date": current_due},
    )
    return {"action": "rolled_forward", "task_id": updated["id"], "from": current_due, "to": updated["due_date"]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--today", help="test-only ISO date; defaults to current HKT date")
    args = parser.parse_args()
    today = date.fromisoformat(args.today) if args.today else datetime.now(LOCAL_TZ).date()
    try:
        result = roll_forward(today, dry_run=args.dry_run)
        if result["action"] != "none":
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"device-roadmap auto-roll failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
