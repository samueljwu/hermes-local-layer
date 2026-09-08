#!/usr/bin/env python3
"""Generate the 10-week task-completion report data and stacked bar chart."""
from __future__ import annotations

import csv
import contextlib
import fcntl
import html
import io
import json
import math
import os
import re
import stat
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import cairosvg

TASKS = Path("/home/hermes/tasks")
OUT = Path("/home/hermes/task-completion-report")
REGISTRY = TASKS / "_meta/task_registry.json"
PROJECTS = TASKS / "_meta/project_registry.json"
DELETIONS = TASKS / "_meta/task_deletions.json"
LOG = TASKS / "log.md"
FEEDBACK = TASKS / "_meta/weekly_completion_significance_feedback.json"
LOCK = Path("/home/hermes/.hermes/state/locks/task-completion-report.lock")
LEGACY_OUTPUT_NAMES = (
    "latest_report.json",
    "latest_report_tasks.csv",
    "weekly_completed_tasks_last_10_weeks.svg",
    "weekly_completed_tasks_last_10_weeks.png",
)
OUTPUT_NAMES = LEGACY_OUTPUT_NAMES + (
    "weekly_completed_estimated_hours_last_10_weeks.svg",
    "weekly_completed_estimated_hours_last_10_weeks.png",
)
GENERATIONS_DIRNAME = ".generations"
CURRENT_LINK_NAME = "current"


@contextlib.contextmanager
def report_lock():
    parent_fd = _open_directory_nofollow(LOCK.parent, create=True, mode=0o700)
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
    try:
        fd = os.open(LOCK.name, flags, 0o600, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
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


def _open_directory_nofollow(path: Path, *, create: bool = False, mode: int = 0o755) -> int:
    """Bind a complete directory hierarchy without following symlinks."""
    directory = Path(path)
    fd = os.open("/" if directory.is_absolute() else ".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = directory.parts[1:] if directory.is_absolute() else directory.parts
        for component in parts:
            if component in {"", "."}:
                continue
            if component == "..":
                raise RuntimeError(f"refusing parent traversal in report path: {directory}")
            try:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode, dir_fd=fd)
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def ensure_output_root() -> None:
    if OUT.parent.resolve(strict=True) != Path("/home/hermes"):
        raise RuntimeError(f"refusing non-canonical task-completion report parent: {OUT.parent}")
    parent_fd = _open_directory_nofollow(OUT.parent)
    try:
        try:
            os.mkdir(OUT.name, 0o755, dir_fd=parent_fd)
        except FileExistsError:
            pass
        output_fd = os.open(OUT.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        os.close(output_fd)
    finally:
        os.close(parent_fd)


def _replace_symlink(dir_fd: int, name: str, target: str) -> None:
    temporary = f".{name}.link.{os.getpid()}.{uuid4().hex}"
    os.symlink(target, temporary, dir_fd=dir_fd)
    try:
        os.replace(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=dir_fd)
        except FileNotFoundError:
            pass


def _fixed_links_are_current(out_fd: int) -> bool:
    for name in OUTPUT_NAMES:
        try:
            info = os.stat(name, dir_fd=out_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISLNK(info.st_mode):
            return False
        if os.readlink(name, dir_fd=out_fd) != f"{CURRENT_LINK_NAME}/{name}":
            return False
    return True


def _entries_are_missing(dir_fd: int, names: tuple[str, ...]) -> bool:
    for name in names:
        try:
            os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        return False
    return True


def _current_generation_has_files(
    out_fd: int, generations_fd: int, names: tuple[str, ...]
) -> bool:
    target = os.readlink(CURRENT_LINK_NAME, dir_fd=out_fd)
    match = re.fullmatch(r"\.generations/((?:legacy|report)-[A-Za-z0-9._-]+)", target)
    if match is None:
        raise RuntimeError(f"refusing unsafe report current pointer: {target}")
    generation_fd = os.open(
        match.group(1), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=generations_fd
    )
    try:
        for name in names:
            info = os.stat(name, dir_fd=generation_fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                return False
        return True
    except FileNotFoundError:
        return False
    finally:
        os.close(generation_fd)


def _initialize_generation_layout(out_fd: int, generations_fd: int) -> None:
    """Migrate a complete regular-file bundle without changing visible content."""
    try:
        current_info = os.stat(CURRENT_LINK_NAME, dir_fd=out_fd, follow_symlinks=False)
    except FileNotFoundError:
        current_info = None
    if current_info is not None and stat.S_ISLNK(current_info.st_mode):
        complete = _current_generation_has_files(out_fd, generations_fd, OUTPUT_NAMES)
        legacy_upgrade = (
            not complete
            and _current_generation_has_files(out_fd, generations_fd, LEGACY_OUTPUT_NAMES)
            and _entries_are_missing(out_fd, OUTPUT_NAMES[len(LEGACY_OUTPUT_NAMES):])
        )
        if not (complete or legacy_upgrade):
            raise RuntimeError("refusing incomplete report current generation")
        # A prior process may have switched ``current`` and stopped while
        # repairing the stable links. Replacing every fixed link is idempotent
        # and restores a valid layout before publishing the next generation.
        if not _fixed_links_are_current(out_fd):
            for name in OUTPUT_NAMES:
                _replace_symlink(out_fd, name, f"{CURRENT_LINK_NAME}/{name}")
            os.fsync(out_fd)
        return
    if current_info is not None:
        raise RuntimeError(f"refusing unexpected report current pointer: {OUT / CURRENT_LINK_NAME}")

    existing: list[str] = []
    for name in OUTPUT_NAMES:
        try:
            info = os.stat(name, dir_fd=out_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError("refusing unexpected pre-existing task report artifact")
        existing.append(name)
    existing_names = set(existing)
    valid_existing = existing_names == set(LEGACY_OUTPUT_NAMES) or existing_names == set(OUTPUT_NAMES)
    if existing and not valid_existing:
        raise RuntimeError("refusing incomplete pre-existing task report bundle")

    if existing:
        legacy_id = f"legacy-{uuid4().hex}"
        legacy_stage = f".legacy.{uuid4().hex}"
        os.mkdir(legacy_stage, 0o700, dir_fd=generations_fd)
        stage_fd = os.open(legacy_stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=generations_fd)
        try:
            for name in existing:
                source_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=out_fd)
                destination_fd = os.open(
                    name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=stage_fd
                )
                try:
                    while chunk := os.read(source_fd, 1024 * 1024):
                        view = memoryview(chunk)
                        while view:
                            view = view[os.write(destination_fd, view):]
                    os.fsync(destination_fd)
                finally:
                    os.close(source_fd)
                    os.close(destination_fd)
            os.fsync(stage_fd)
        finally:
            os.close(stage_fd)
        os.replace(legacy_stage, legacy_id, src_dir_fd=generations_fd, dst_dir_fd=generations_fd)
        _replace_symlink(out_fd, CURRENT_LINK_NAME, f"{GENERATIONS_DIRNAME}/{legacy_id}")
        for name in OUTPUT_NAMES:
            _replace_symlink(out_fd, name, f"{CURRENT_LINK_NAME}/{name}")


def _switch_generation(generation_id: str, *, out_fd: int) -> None:
    _replace_symlink(out_fd, CURRENT_LINK_NAME, f"{GENERATIONS_DIRNAME}/{generation_id}")


def publish_report_files(payloads: dict[str, str | bytes]) -> None:
    """Publish a complete artifact generation through one atomic pointer switch."""
    ensure_output_root()
    out_fd = _open_directory_nofollow(OUT)
    try:
        try:
            os.mkdir(GENERATIONS_DIRNAME, 0o700, dir_fd=out_fd)
        except FileExistsError:
            pass
        generations_fd = os.open(
            GENERATIONS_DIRNAME, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=out_fd
        )
    except BaseException:
        os.close(out_fd)
        raise
    staging = f".weekly-report.{uuid4().hex}"
    os.mkdir(staging, 0o700, dir_fd=out_fd)
    staging_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=out_fd)
    try:
        for name in OUTPUT_NAMES:
            payload = payloads[name]
            raw = payload if isinstance(payload, bytes) else payload.encode("utf-8")
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=staging_fd)
            try:
                view = memoryview(raw)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
        os.fsync(staging_fd)

        with report_lock():
            _initialize_generation_layout(out_fd, generations_fd)
            generation_id = f"report-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex}"
            os.replace(staging, generation_id, src_dir_fd=out_fd, dst_dir_fd=generations_fd)
            staging = ""
            if not _fixed_links_are_current(out_fd):
                for name in OUTPUT_NAMES:
                    _replace_symlink(out_fd, name, f"{CURRENT_LINK_NAME}/{name}")
            _switch_generation(generation_id, out_fd=out_fd)
            os.fsync(generations_fd)
            os.fsync(out_fd)
    finally:
        if staging:
            for name in OUTPUT_NAMES:
                try:
                    os.unlink(name, dir_fd=staging_fd)
                except FileNotFoundError:
                    pass
        os.close(staging_fd)
        if staging:
            try:
                os.rmdir(staging, dir_fd=out_fd)
            except FileNotFoundError:
                pass
        os.close(generations_fd)
        os.close(out_fd)


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


def build_completion_records(
    log_text: str,
    registry: list[dict],
    start: date,
    end: date,
    projects=None,
    deletions=None,
) -> list[dict]:
    """Build report rows, preserving historical log text and output column names."""
    from task_schema import (
        project_name, validate_est_time, validate_project_registry, validate_task_shape,
    )

    use_live_inputs = projects is None
    project_catalog = projects if projects is not None else json.loads(PROJECTS.read_text())
    validate_project_registry(project_catalog)
    joined_registry = []
    for task in registry:
        validate_task_shape(task)
        joined_registry.append({**task, "project_name": project_name(task, project_catalog)})
    by_creation = {creation_number(task["id"]): task for task in joined_registry}
    project_names = {project["id"]: project["name"] for project in project_catalog}
    if deletions is None:
        deletions = json.loads(DELETIONS.read_text()) if use_live_inputs and DELETIONS.exists() else {}
    if not isinstance(deletions, dict):
        raise RuntimeError("Invalid task deletion ledger")
    for suffix, receipt in deletions.items():
        if not isinstance(receipt, dict) or "snapshot" not in receipt:
            continue  # Older technical receipts have no recoverable business snapshot.
        snapshot = receipt["snapshot"]
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("id"), str):
            raise RuntimeError(f"Invalid archived task snapshot for suffix {suffix}")
        number = creation_number(snapshot["id"])
        if str(number) != str(suffix) or creation_number(str(receipt.get("task_id", ""))) != number:
            raise RuntimeError(f"Archived task identity mismatch for suffix {suffix}")
        if number in by_creation:
            raise RuntimeError(f"Task identity exists in registry and deletion ledger: {number}")
        notes = snapshot.get("notes", "")
        if not isinstance(notes, str):
            raise RuntimeError(f"Invalid archived task notes for suffix {suffix}")
        try:
            validate_est_time(snapshot.get("est_time"))
        except ValueError as exc:
            raise RuntimeError(f"Invalid archived task estimate for suffix {suffix}") from exc
        if "project_id" in snapshot:
            if "tag" in snapshot or "status" in snapshot or type(snapshot.get("done")) is not bool:
                raise RuntimeError(f"Invalid canonical archived task for suffix {suffix}")
            project_name = project_names.get(snapshot["project_id"])
            if project_name is None:
                raise RuntimeError(f"Unknown archived task project for suffix {suffix}")
        else:
            project_name = snapshot.get("tag")
            if not isinstance(project_name, str) or not project_name:
                raise RuntimeError(f"Invalid legacy archived task project for suffix {suffix}")
        by_creation[number] = {
            "project_name": project_name,
            "notes": notes,
            "est_time": snapshot.get("est_time"),
        }
    records = parse_completed_log(log_text, start, end)
    for record in records:
        source = by_creation.get(creation_number(record["id"]))
        if source is None:
            raise RuntimeError(f"No canonical registry record for {record['id']}")
        record["tag"] = source["project_name"]
        record["notes"] = source.get("notes", "")
        record["est_time"] = 1 if source.get("est_time") is None else source["est_time"]
    return records


def completion_week_start(completed_on: date) -> date:
    """Return the Monday of the week containing the completion date."""
    return completed_on - timedelta(days=completed_on.weekday())


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def display_number(value: int | float) -> str:
    return f"{value:g}"


def render_stacked_chart(
    *, weekly: list[dict], tags: list[str], totals_by_tag: Counter,
    chart_start: date, report_end: date, title: str, subtitle: str,
    y_label: str, total_key: str, by_tag_key: str, footer: str,
) -> tuple[str, bytes]:
    """Render one project-stacked ten-week chart from pre-aggregated rows."""
    W, H = 1500, 900
    left, right, top, bottom = 105, 60, 185, 150
    plot_w, plot_h = W - left - right, H - top - bottom
    max_total = max([row[total_key] for row in weekly] + [1])
    y_max = max(5, math.ceil(max_total / 5) * 5)
    palette = ["#2563EB", "#F59E0B", "#10B981", "#8B5CF6", "#EF4444", "#06B6D4", "#64748B"]
    colors = {tag: palette[i % len(palette)] for i, tag in enumerate(tags)}
    slot = plot_w / 10
    bar_w = min(86, slot * 0.62)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
        '<rect width="100%" height="100%" fill="#F8FAFC"/>',
        '<style>text{font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif;fill:#0F172A}.muted{fill:#64748B}.grid{stroke:#CBD5E1;stroke-width:1}.axis{stroke:#475569;stroke-width:1.5}</style>',
        f'<text x="{left}" y="58" font-size="31" font-weight="700">{esc(title)}</text>',
        f'<text x="{left}" y="94" font-size="18" class="muted">{esc(chart_start.strftime("%b %-d, %Y"))}–{esc(report_end.strftime("%b %-d, %Y"))} · Asia/Hong_Kong · {esc(subtitle)}</text>',
    ]
    lx, ly = left, 132
    for tag in tags:
        label = f"{tag} ({display_number(totals_by_tag[tag])})"
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
        f'<text x="30" y="{top+plot_h/2}" transform="rotate(-90 30 {top+plot_h/2})" text-anchor="middle" font-size="17" class="muted">{esc(y_label)}</text>',
    ])
    for i, row in enumerate(weekly):
        x = left + slot * (i + 0.5) - bar_w / 2
        y_cursor = top + plot_h
        for tag in tags:
            value = row[by_tag_key][tag]
            if not value:
                continue
            height = (value / y_max) * plot_h
            y_cursor -= height
            parts.append(f'<rect x="{x:.1f}" y="{y_cursor:.1f}" width="{bar_w:.1f}" height="{height:.1f}" fill="{colors[tag]}"/>')
            if height >= 27:
                parts.append(f'<text x="{x+bar_w/2:.1f}" y="{y_cursor+height/2+6:.1f}" text-anchor="middle" font-size="15" font-weight="700" style="fill:white">{display_number(value)}</text>')
        total_y = top + plot_h - (row[total_key] / y_max) * plot_h - 10
        parts.extend([
            f'<text x="{x+bar_w/2:.1f}" y="{total_y:.1f}" text-anchor="middle" font-size="16" font-weight="700">{display_number(row[total_key])}</text>',
            f'<text x="{x+bar_w/2:.1f}" y="{top+plot_h+34}" text-anchor="middle" font-size="14" class="muted">{esc(row["label"])}</text>',
        ])
    parts.extend([
        f'<text x="{left}" y="{H-45}" font-size="14" class="muted">{esc(footer)}</text>',
        '</svg>',
    ])
    svg_text = "\n".join(parts) + "\n"
    png_bytes = cairosvg.svg2png(bytestring=svg_text.encode("utf-8"), output_width=1950, output_height=1170)
    return svg_text, png_bytes


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
    projects = json.loads(PROJECTS.read_text())
    deletions = json.loads(DELETIONS.read_text()) if DELETIONS.exists() else {}
    records = build_completion_records(
        LOG.read_text(), registry, chart_start, report_end, projects=projects, deletions=deletions
    )

    weeks = [chart_start + timedelta(weeks=i) for i in range(10)]
    tags = sorted({r["tag"] for r in records})
    counts = {week: Counter() for week in weeks}
    estimated_hours = {week: Counter() for week in weeks}
    for record in records:
        week = completion_week_start(record["date"])
        counts[week][record["tag"]] += 1
        estimated_hours[week][record["tag"]] += record["est_time"]

    report_week_records = [r for r in records if report_start <= r["date"] <= report_end]
    month_records = [r for r in records if mtd_start <= r["date"] <= report_end]
    totals_by_tag = Counter(r["tag"] for r in records)
    estimated_hours_by_tag = Counter()
    for record in records:
        estimated_hours_by_tag[record["tag"]] += record["est_time"]
    weekly = []
    for week in weeks:
        weekly.append({
            "week_start": week.isoformat(),
            "week_end": (week + timedelta(days=6)).isoformat(),
            "label": f"{week.strftime('%b %-d')}–{(week + timedelta(days=6)).strftime('%b %-d')}",
            "total": sum(counts[week].values()),
            "by_tag": {tag: counts[week][tag] for tag in tags},
            "total_estimated_hours": sum(estimated_hours[week].values()),
            "estimated_hours_by_tag": {tag: estimated_hours[week][tag] for tag in tags},
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
        "last_10_weeks_estimated_hours": sum(r["est_time"] for r in records),
        "last_10_weeks_estimated_hours_by_tag": dict(sorted(estimated_hours_by_tag.items())),
        "weekly": weekly,
        "report_week_completed": len(report_week_records),
        "report_week_estimated_hours": sum(r["est_time"] for r in report_week_records),
        "report_week_by_tag": dict(sorted(Counter(r["tag"] for r in report_week_records).items())),
        "report_week_tasks": [
            {**r, "date": r["date"].isoformat()} for r in sorted(report_week_records, key=lambda x: (x["date"], x["id"]))
        ],
        "month_to_date_completed": len(month_records),
        "month_to_date_estimated_hours": sum(r["est_time"] for r in month_records),
        "month_to_date_by_tag": dict(sorted(Counter(r["tag"] for r in month_records).items())),
        "month_to_date_tasks": [
            {**r, "date": r["date"].isoformat()} for r in sorted(month_records, key=lambda x: (x["date"], x["id"]))
        ],
        "significance_feedback": feedback,
        "chart_png": str(OUT / "weekly_completed_tasks_last_10_weeks.png"),
        "chart_svg": str(OUT / "weekly_completed_tasks_last_10_weeks.svg"),
        "estimated_hours_chart_png": str(OUT / "weekly_completed_estimated_hours_last_10_weeks.png"),
        "estimated_hours_chart_svg": str(OUT / "weekly_completed_estimated_hours_last_10_weeks.svg"),
    }
    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=["completed_date", "week_start", "task_id", "tag", "task", "status", "est_time"])
    writer.writeheader()
    for r in sorted(records, key=lambda x: (x["date"], x["id"])):
        writer.writerow({
            "completed_date": r["date"].isoformat(),
            "week_start": completion_week_start(r["date"]).isoformat(),
            "task_id": r["id"], "tag": r["tag"], "task": r["task"], "status": r["status"],
            "est_time": r["est_time"],
        })

    footer = (
        "Source: canonical task registry + completion log. Cancelled tasks excluded; "
        "recurring completed occurrences included."
    )
    svg_text, png_bytes = render_stacked_chart(
        weekly=weekly, tags=tags, totals_by_tag=totals_by_tag,
        chart_start=chart_start, report_end=report_end,
        title="Weekly completed tasks · last 10 weeks",
        subtitle=f"{len(records)} completed", y_label="Tasks completed",
        total_key="total", by_tag_key="by_tag", footer=footer,
    )
    estimated_hours_svg, estimated_hours_png = render_stacked_chart(
        weekly=weekly, tags=tags, totals_by_tag=estimated_hours_by_tag,
        chart_start=chart_start, report_end=report_end,
        title="Weekly completed work · estimated hours",
        subtitle=f"{display_number(sum(r['est_time'] for r in records))} estimated hours",
        y_label="Estimated hours completed", total_key="total_estimated_hours",
        by_tag_key="estimated_hours_by_tag",
        footer=footer + " Tasks without an estimate count as 1 hour.",
    )
    publish_report_files({
        "latest_report.json": json.dumps(summary, indent=2) + "\n",
        "latest_report_tasks.csv": csv_buffer.getvalue(),
        "weekly_completed_tasks_last_10_weeks.svg": svg_text,
        "weekly_completed_tasks_last_10_weeks.png": png_bytes,
        "weekly_completed_estimated_hours_last_10_weeks.svg": estimated_hours_svg,
        "weekly_completed_estimated_hours_last_10_weeks.png": estimated_hours_png,
    })
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
