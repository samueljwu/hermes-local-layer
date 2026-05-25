"""Outstanding tasks slash command for the Discord #tasks workflow.

Registers /tasksout. The command reads the canonical registry directly instead of
searching session history, so it stays aligned with the dashboard and per-tag
commands.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import re

REGISTRY_PATH = Path.home() / "tasks" / "_meta" / "task_registry.json"
LOCAL_TZ = ZoneInfo("Asia/Hong_Kong")
LOCAL_TZ_LABEL = "HKT"


TASK_ID_RE = re.compile(r"^T(?:-(?P<rank>\d+)-(?P<created>\d+)|(?P<legacy>\d+))$")


def _creation_order(task: dict) -> int:
    m = TASK_ID_RE.fullmatch(str(task.get("id", "")))
    if not m:
        return 10**9
    return int(m.group("created") or m.group("legacy"))


def _parse_due(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = str(value).replace("Z", "+00:00")
    for parser in (
        lambda s: datetime.fromisoformat(s),
        lambda s: datetime.strptime(s, "%B %d, %Y"),
        lambda s: datetime.strptime(s, "%B %d %Y"),
    ):
        try:
            dt = parser(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=LOCAL_TZ)
            return dt.astimezone(LOCAL_TZ)
        except Exception:
            continue
    return None


def _format_due(value: str | None) -> str:
    dt = _parse_due(value)
    if not dt:
        return "No date" if not value else str(value)
    return f"{dt.strftime('%a %b')} {dt.day}"


def _read_registry() -> list[dict]:
    if not REGISTRY_PATH.exists():
        return []
    data = json.loads(REGISTRY_PATH.read_text())
    if not isinstance(data, list):
        raise ValueError("Task registry must be a flat JSON array, not an object")
    return data


def _sort_key(task: dict) -> tuple[datetime, int, str]:
    due = _parse_due(task.get("due_date"))
    return (due or datetime.max.replace(tzinfo=LOCAL_TZ), _creation_order(task), str(task.get("task", "")))


def _handle_outstanding(raw_args: str = "") -> str:
    # /tasksout intentionally takes no Discord slash-command arguments. Keep the
    # handler signature tolerant because gateway text dispatch may still pass a
    # raw_args string, but ignore it so native Discord registration can stay
    # argument-free.
    tasks = [t for t in _read_registry() if t.get("status", "pending") == "pending"]

    if not tasks:
        return "No outstanding tasks found."

    lines: list[str] = []
    for t in sorted(tasks, key=_sort_key):
        due = _format_due(t.get("due_date"))
        tag = str(t.get("tag") or "Other").strip()
        desc = str(t.get("task", "")).strip().rstrip(".")
        priority = str(t.get("priority", "medium")).lower()
        high = " - High" if priority in {"high", "top", "urgent"} else ""
        lines.append(f"{t.get('id', '?')} - {due} - {tag} - {desc}{high}")
    return "\n".join(lines)


def register(ctx) -> None:
    ctx.register_command(
        "tasksout",
        handler=_handle_outstanding,
        description="Show outstanding #tasks items from the canonical registry.",
    )
