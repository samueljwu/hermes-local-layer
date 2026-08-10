#!/usr/bin/env python3
"""Emit #tasks reminders from the canonical registry.

Designed for Hermes cron with no_agent=True:
- non-empty stdout is delivered to Discord #tasks;
- empty stdout stays silent.

Modes:
- --mode tomorrow: reminders for tasks due tomorrow.
- --mode today: reminders for tasks due today.

Reads the canonical task registry fresh. Never reminds for completed/cancelled tasks.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

LOCAL_SCRIPTS = Path("/home/hermes/.hermes/scripts")
if str(LOCAL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(LOCAL_SCRIPTS))
from local_ops import resolve_tasks_root  # noqa: E402

TASK_OPS_PATH = resolve_tasks_root() / "_tools" / "task_ops.py"
LOCAL_TZ = ZoneInfo("Asia/Hong_Kong")

spec = importlib.util.spec_from_file_location("task_ops", TASK_OPS_PATH)
task_ops = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(task_ops)


def main() -> int:
    parser = argparse.ArgumentParser(description="Emit #tasks reminders for due dates")
    parser.add_argument("--mode", choices=["tomorrow", "today"], default="tomorrow")
    args = parser.parse_args()

    registry = task_ops.read_registry()
    today = datetime.now(LOCAL_TZ).date()
    if args.mode == "today":
        lines = task_ops.render_due_today_reminders(registry, today=today)
    else:
        lines = task_ops.render_due_tomorrow_reminders(registry, today=today)
    if lines:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
