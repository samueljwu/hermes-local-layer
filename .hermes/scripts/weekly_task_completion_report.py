#!/usr/bin/env python3
"""Generate the 10-week task-completion report data and stacked bar chart."""
from __future__ import annotations

import csv
import contextlib
import fcntl
import html
import io
import json
import os
import re
import shutil
import stat
import tempfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import cairosvg

TASKS = Path("/home/hermes/tasks")
OUT = Path("/home/hermes/task-completion-report")
REGISTRY = TASKS / "_meta/task_registry.json"
LOG = TASKS / "log.md"
FEEDBACK = TASKS / "_meta/weekly_completion_significance_feedback.json"
LOCK = Path("/home/hermes/.hermes/state/locks/task-completion-report.lock")
OUTPUT_NAMES = (
    "latest_report.json",
    "latest_report_tasks.csv",
    "weekly_completed_tasks_last_10_weeks.svg",
    "weekly_completed_tasks_last_10_weeks.png",
)
GENERATIONS_DIRNAME = ".generations"
CURRENT_LINK_NAME = "current"


@contextlib.contextmanager
def report_lock():
    LOCK.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(LOCK.parent, 0o700)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(LOCK, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError(f"refusing non-regular report lock: {LOCK}")
        handle = os.fdopen(fd, "r+", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise
    with handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def ensure_output_root() -> None:
    if OUT.is_symlink():
        raise RuntimeError(f"refusing symlinked task-completion report root: {OUT}")
    if OUT.parent.resolve(strict=True) != Path("/home/hermes"):
        raise RuntimeError(f"refusing non-canonical task-completion report parent: {OUT.parent}")
    OUT.mkdir(parents=False, exist_ok=True)


def _replace_symlink(path: Path, target: str) -> None:
    temporary = path.with_name(f".{path.name}.link.{os.getpid()}.{uuid4().hex}")
    os.symlink(target, temporary)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fixed_links_are_current() -> bool:
    return all(
        (OUT / name).is_symlink() and os.readlink(OUT / name) == f"{CURRENT_LINK_NAME}/{name}"
        for name in OUTPUT_NAMES
    )


def _initialize_generation_layout(generations: Path) -> None:
    """Migrate a complete regular-file bundle without changing visible content."""
    current = OUT / CURRENT_LINK_NAME
    if current.is_symlink() and _fixed_links_are_current():
        target = os.readlink(current)
        prefix = f"{GENERATIONS_DIRNAME}/"
        if target.startswith(prefix) and "/" not in target[len(prefix):] and ".." not in target:
            return
        raise RuntimeError(f"refusing unsafe report current pointer: {target}")
    if current.exists() or current.is_symlink():
        raise RuntimeError(f"refusing unexpected report current pointer: {current}")

    existing = [OUT / name for name in OUTPUT_NAMES if (OUT / name).exists() or (OUT / name).is_symlink()]
    if existing and len(existing) != len(OUTPUT_NAMES):
        raise RuntimeError("refusing incomplete pre-existing task report bundle")
    if any(path.is_symlink() or not path.is_file() for path in existing):
        raise RuntimeError("refusing unexpected pre-existing task report artifact")

    if existing:
        legacy_id = f"legacy-{uuid4().hex}"
        legacy_stage = Path(tempfile.mkdtemp(prefix=".legacy.", dir=generations))
        try:
            for path in existing:
                shutil.copy2(path, legacy_stage / path.name)
            os.replace(legacy_stage, generations / legacy_id)
        finally:
            if legacy_stage.exists():
                shutil.rmtree(legacy_stage)
        _replace_symlink(current, f"{GENERATIONS_DIRNAME}/{legacy_id}")
        for name in OUTPUT_NAMES:
            _replace_symlink(OUT / name, f"{CURRENT_LINK_NAME}/{name}")


def _switch_generation(generation_id: str) -> None:
    _replace_symlink(OUT / CURRENT_LINK_NAME, f"{GENERATIONS_DIRNAME}/{generation_id}")


def publish_report_files(payloads: dict[str, str | bytes]) -> None:
    """Publish a complete artifact generation through one atomic pointer switch."""
    ensure_output_root()
    generations = OUT / GENERATIONS_DIRNAME
    generations.mkdir(mode=0o700, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".weekly-report.", dir=OUT))
    try:
        for name in OUTPUT_NAMES:
            payload = payloads[name]
            if isinstance(payload, bytes):
                with (staging / name).open("wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
            else:
                with (staging / name).open("w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())

        with report_lock():
            _initialize_generation_layout(generations)
            generation_id = f"report-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex}"
            generation_dir = generations / generation_id
            os.replace(staging, generation_dir)
            if not _fixed_links_are_current():
                for name in OUTPUT_NAMES:
                    _replace_symlink(OUT / name, f"{CURRENT_LINK_NAME}/{name}")
            _switch_generation(generation_id)
            _fsync_directory(OUT)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def creation_number(task_id: str) -> int:
    nums = re.findall(r"\d+", task_id)
    if not nums:
        raise ValueError(f"Task ID has no number: {task_id}")
    return int(nums[-1])


def parse_completed_log(text: str, start: date, end: date) -> list[dict]:
    active_date = None
    records = []
    date_re = re.compile(r"^## (\d{4}-\d{2}-\d{2})$")
    entry_re = re.compile(
        r"^- \*\*(?P<id>[^*]+)\*\* — (?P<task>.*?) — "
        r"(?P<status>completed(?: occurrence)?)$"
    )
    for raw in text.splitlines():
        line = raw.strip()
        dm = date_re.match(line)
        if dm:
            active_date = date.fromisoformat(dm.group(1))
            continue
        em = entry_re.match(line)
        if em and active_date and start <= active_date <= end:
            records.append({
                "date": active_date,
                "id": em.group("id"),
                "task": em.group("task"),
                "status": em.group("status"),
            })
    return records


def build_completion_records(log_text: str, registry: list[dict], start: date, end: date) -> list[dict]:
    """Build report rows, preserving the completion date recorded in the log."""
    by_creation = {creation_number(task["id"]): task for task in registry}
    records = parse_completed_log(log_text, start, end)
    for record in records:
        source = by_creation.get(creation_number(record["id"]))
        if source is None:
            raise RuntimeError(f"No canonical registry record for {record['id']}")
        record["tag"] = source["tag"]
        record["notes"] = source.get("notes", "")
    return records


def completion_week_start(completed_on: date) -> date:
    """Return the Monday of the week containing the completion date."""
    return completed_on - timedelta(days=completed_on.weekday())


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def main() -> None:
    ensure_output_root()
    today = datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
    # Scheduled reports run Sunday at 21:00 HKT and include records available
    # through that run time. Manual runs select the latest Sunday boundary.
    days_since_sunday = (today.weekday() + 1) % 7
    report_end = today - timedelta(days=days_since_sunday)
    report_start = report_end - timedelta(days=6)
    chart_start = report_start - timedelta(weeks=9)
    mtd_start = report_end.replace(day=1)

    registry = json.loads(REGISTRY.read_text())
    records = build_completion_records(LOG.read_text(), registry, chart_start, report_end)

    weeks = [chart_start + timedelta(weeks=i) for i in range(10)]
    tags = sorted({r["tag"] for r in records})
    counts = {week: Counter() for week in weeks}
    for record in records:
        week = completion_week_start(record["date"])
        counts[week][record["tag"]] += 1

    report_week_records = [r for r in records if report_start <= r["date"] <= report_end]
    month_records = [r for r in records if mtd_start <= r["date"] <= report_end]
    totals_by_tag = Counter(r["tag"] for r in records)
    weekly = []
    for week in weeks:
        weekly.append({
            "week_start": week.isoformat(),
            "week_end": (week + timedelta(days=6)).isoformat(),
            "label": f"{week.strftime('%b %-d')}–{(week + timedelta(days=6)).strftime('%b %-d')}",
            "total": sum(counts[week].values()),
            "by_tag": {tag: counts[week][tag] for tag in tags},
        })

    feedback = []
    if FEEDBACK.exists():
        loaded = json.loads(FEEDBACK.read_text())
        feedback = loaded if isinstance(loaded, list) else []

    summary = {
        "timezone": "Asia/Hong_Kong",
        "generated_at": datetime.now(ZoneInfo("Asia/Hong_Kong")).isoformat(timespec="seconds"),
        "report_week": {"start": report_start.isoformat(), "end": report_end.isoformat()},
        "chart_range": {"start": chart_start.isoformat(), "end": report_end.isoformat()},
        "month_to_date": {"start": mtd_start.isoformat(), "end": report_end.isoformat()},
        "last_10_weeks_completed": len(records),
        "last_10_weeks_by_tag": dict(sorted(totals_by_tag.items())),
        "weekly": weekly,
        "report_week_completed": len(report_week_records),
        "report_week_by_tag": dict(sorted(Counter(r["tag"] for r in report_week_records).items())),
        "report_week_tasks": [
            {**r, "date": r["date"].isoformat()} for r in sorted(report_week_records, key=lambda x: (x["date"], x["id"]))
        ],
        "month_to_date_completed": len(month_records),
        "month_to_date_by_tag": dict(sorted(Counter(r["tag"] for r in month_records).items())),
        "month_to_date_tasks": [
            {**r, "date": r["date"].isoformat()} for r in sorted(month_records, key=lambda x: (x["date"], x["id"]))
        ],
        "significance_feedback": feedback,
        "chart_png": str(OUT / "weekly_completed_tasks_last_10_weeks.png"),
        "chart_svg": str(OUT / "weekly_completed_tasks_last_10_weeks.svg"),
    }
    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=["completed_date", "week_start", "task_id", "tag", "task", "status"])
    writer.writeheader()
    for r in sorted(records, key=lambda x: (x["date"], x["id"])):
        writer.writerow({
            "completed_date": r["date"].isoformat(),
            "week_start": completion_week_start(r["date"]).isoformat(),
            "task_id": r["id"], "tag": r["tag"], "task": r["task"], "status": r["status"],
        })

    W, H = 1500, 900
    left, right, top, bottom = 105, 60, 185, 150
    plot_w, plot_h = W - left - right, H - top - bottom
    max_total = max([w["total"] for w in weekly] + [1])
    y_max = max(5, ((max_total + 4) // 5) * 5)
    palette = ["#2563EB", "#F59E0B", "#10B981", "#8B5CF6", "#EF4444", "#06B6D4", "#64748B"]
    colors = {tag: palette[i % len(palette)] for i, tag in enumerate(tags)}
    slot = plot_w / 10
    bar_w = min(86, slot * 0.62)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
        '<rect width="100%" height="100%" fill="#F8FAFC"/>',
        '<style>text{font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif;fill:#0F172A}.muted{fill:#64748B}.grid{stroke:#CBD5E1;stroke-width:1}.axis{stroke:#475569;stroke-width:1.5}</style>',
        f'<text x="{left}" y="58" font-size="31" font-weight="700">Weekly completed tasks · last 10 weeks</text>',
        f'<text x="{left}" y="94" font-size="18" class="muted">{esc(chart_start.strftime("%b %-d, %Y"))}–{esc(report_end.strftime("%b %-d, %Y"))} · Asia/Hong_Kong · {len(records)} completed</text>',
    ]
    lx, ly = left, 132
    for tag in tags:
        label = f"{tag} ({totals_by_tag[tag]})"
        parts.extend([
            f'<rect x="{lx}" y="{ly-15}" width="17" height="17" rx="3" fill="{colors[tag]}"/>',
            f'<text x="{lx+25}" y="{ly}" font-size="16">{esc(label)}</text>',
        ])
        lx += 42 + len(label) * 9
    tick_step = 5 if y_max >= 10 else 1
    for value in range(0, y_max + 1, tick_step):
        y = top + plot_h - (value / y_max) * plot_h
        parts.extend([
            f'<line x1="{left}" y1="{y:.1f}" x2="{left+plot_w}" y2="{y:.1f}" class="grid"/>',
            f'<text x="{left-18}" y="{y+6:.1f}" text-anchor="end" font-size="15" class="muted">{value}</text>',
        ])
    parts.extend([
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top+plot_h}" class="axis"/>',
        f'<line x1="{left}" y1="{top+plot_h}" x2="{left+plot_w}" y2="{top+plot_h}" class="axis"/>',
        f'<text x="30" y="{top+plot_h/2}" transform="rotate(-90 30 {top+plot_h/2})" text-anchor="middle" font-size="17" class="muted">Tasks completed</text>',
    ])
    for i, row in enumerate(weekly):
        x = left + slot * (i + 0.5) - bar_w / 2
        y_cursor = top + plot_h
        for tag in tags:
            value = row["by_tag"][tag]
            if not value:
                continue
            height = (value / y_max) * plot_h
            y_cursor -= height
            parts.append(f'<rect x="{x:.1f}" y="{y_cursor:.1f}" width="{bar_w:.1f}" height="{height:.1f}" fill="{colors[tag]}"/>')
            if height >= 27:
                parts.append(f'<text x="{x+bar_w/2:.1f}" y="{y_cursor+height/2+6:.1f}" text-anchor="middle" font-size="15" font-weight="700" style="fill:white">{value}</text>')
        total_y = top + plot_h - (row["total"] / y_max) * plot_h - 10
        parts.extend([
            f'<text x="{x+bar_w/2:.1f}" y="{total_y:.1f}" text-anchor="middle" font-size="16" font-weight="700">{row["total"]}</text>',
            f'<text x="{x+bar_w/2:.1f}" y="{top+plot_h+34}" text-anchor="middle" font-size="14" class="muted">{esc(row["label"])}</text>',
        ])
    parts.extend([
        f'<text x="{left}" y="{H-45}" font-size="14" class="muted">Source: canonical task registry + completion log. Cancelled tasks excluded; recurring completed occurrences included.</text>',
        '</svg>',
    ])
    svg_text = "\n".join(parts) + "\n"
    png_bytes = cairosvg.svg2png(bytestring=svg_text.encode("utf-8"), output_width=1950, output_height=1170)
    publish_report_files({
        "latest_report.json": json.dumps(summary, indent=2) + "\n",
        "latest_report_tasks.csv": csv_buffer.getvalue(),
        "weekly_completed_tasks_last_10_weeks.svg": svg_text,
        "weekly_completed_tasks_last_10_weeks.png": png_bytes,
    })
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
