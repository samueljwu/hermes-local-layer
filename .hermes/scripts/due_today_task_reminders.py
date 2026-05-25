#!/usr/bin/env python3
"""Emit #tasks reminders for tasks due today.

Designed for Hermes cron with no_agent=True. Non-empty stdout is delivered to
Discord #tasks; empty stdout stays silent.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TASK_OPS_PATH = Path.home() / "tasks" / "_tools" / "task_ops.py"
LOCAL_TZ = ZoneInfo("Asia/Hong_Kong")

spec = importlib.util.spec_from_file_location("task_ops", TASK_OPS_PATH)
task_ops = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(task_ops)


def main() -> int:
    registry = task_ops.read_registry()
    today = datetime.now(LOCAL_TZ).date()
    lines = task_ops.render_due_today_reminders(registry, today=today)
    if lines:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
