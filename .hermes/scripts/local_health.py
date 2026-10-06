#!/usr/bin/env python3
"""Read-only metadata health inventory. No scheduler imports or transcript reads."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import stat


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def stamp(value):
    parsed = timestamp(value)
    return parsed.isoformat() if parsed else None


def ledger(path, query, warnings):
    # Immutable reads cannot create SQLite sidecars or change locks. Do not ignore
    # an active WAL: decline that source rather than present an incomplete snapshot.
    try:
        if not path.is_file():
            warnings.append(f"{path.name}: unavailable")
            return []
        wal = Path(str(path) + "-wal")
        if wal.exists() and wal.stat().st_size:
            warnings.append(f"{path.name}: active WAL; receipt status unknown (retry later)")
            return []
        before = path.stat()
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(row) for row in conn.execute(query)]
        after = path.stat()
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size) or (wal.exists() and wal.stat().st_size):
            warnings.append(f"{path.name}: changed during read; receipt status unknown")
            return []
        return rows
    except (OSError, sqlite3.Error):
        warnings.append(f"{path.name}: unreadable or unsupported schema")
        return []


def inventory(path):
    result = {"files": 0, "bytes": 0, "oldest_mtime": None, "newest_mtime": None,
              "unreadable": 0, "symlinks_skipped": 0, "present": path.exists()}
    low = high = None

    def visit(item):
        nonlocal low, high
        try:
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode):
                result["symlinks_skipped"] += 1
            elif stat.S_ISDIR(info.st_mode):
                with os.scandir(item) as children:
                    for child in children:
                        visit(Path(child.path))
            elif stat.S_ISREG(info.st_mode):
                result["files"] += 1
                result["bytes"] += info.st_size
                low = info.st_mtime if low is None else min(low, info.st_mtime)
                high = info.st_mtime if high is None else max(high, info.st_mtime)
        except OSError:
            result["unreadable"] += 1
    if path.exists() or path.is_symlink():
        visit(path)
    for key, value in (("oldest_mtime", low), ("newest_mtime", high)):
        if value is not None:
            result[key] = datetime.fromtimestamp(value, timezone.utc).isoformat()
    return result


def report(home, now=None, stale_seconds=3600, overdue_seconds=300):
    now = now or datetime.now(timezone.utc)
    warnings = []
    try:
        data = json.loads((home / "cron/jobs.json").read_text())
        jobs = data["jobs"]
        if not isinstance(jobs, list) or not all(isinstance(j, dict) for j in jobs):
            raise ValueError("invalid jobs")
    except (OSError, ValueError, KeyError, TypeError):
        jobs = []
        warnings.append("jobs.json: unavailable or invalid; job inventory unknown")
    executions = ledger(home / "cron/executions.db",
                        "SELECT id,job_id,status,claimed_at,started_at,finished_at FROM executions", warnings)
    deliveries = ledger(home / "cron/deliveries.db",
                        "SELECT execution_id,status,finished_at FROM deliveries UNION ALL "
                        "SELECT execution_id,terminal_status AS status,finished_at FROM delivery_tombstones", warnings)
    delivery_by_id = {d["execution_id"]: d for d in deliveries}
    by_job = {}
    for execution in executions:
        by_job.setdefault(execution["job_id"], []).append(execution)
    rows = []
    floor = datetime.min.replace(tzinfo=timezone.utc)
    for job in jobs:
        receipts = by_job.get(job.get("id"), [])
        latest = max(receipts, key=lambda r: timestamp(r["claimed_at"]) or floor, default=None)
        status = job.get("last_status")
        action = {"ok": "success", "delivery_failed": "success", "error": "failed",
                  "blocked_config": "blocked_config", "interrupted": "interrupted"}.get(status, "unknown")
        flags = []
        if "interrupt" in str(job.get("last_error") or "").lower():
            flags.append("interrupted")
        last_run = stamp(job.get("last_run_at"))
        successes = [stamp(r["finished_at"]) for r in receipts if r["status"] == "completed"]
        if action == "success":
            successes.append(last_run)
        action_success = max((s for s in successes if s), default=None)
        # Latest jobs metadata can be newer than a retained receipt; never attach
        # an older delivery to that newer run. Small stamp differences are real.
        current = latest if latest and (timestamp(latest["finished_at"]) or timestamp(latest["claimed_at"]) or floor) >= (timestamp(last_run) or floor) else None
        if current:
            action = {"completed": "success", "failed": "failed", "unknown": "unknown",
                      "claimed": "claimed", "running": "running"}.get(current["status"], "unknown")
        delivery = "unknown"
        if job.get("last_delivery_error"):
            delivery = "failed"
        elif job.get("last_delivery_unverified"):
            delivery = "unverified"
        elif job.get("deliver") == "local":
            delivery = "not_configured"
        receipt = delivery_by_id.get(current["id"]) if current else None
        if receipt and not job.get("last_delivery_error") and not job.get("last_delivery_unverified"):
            delivery = {"delivered": "acknowledged", "failed": "failed", "unknown": "unknown",
                        "pending": "pending", "delivering": "delivering"}.get(receipt["status"], "unknown")
        # Queue completion records ACKs, including unverified ones. Mutable latest-job
        # metadata cannot establish verification for a retained row or tombstone.
        acknowledged = [stamp(delivery_by_id[r["id"]]["finished_at"]) for r in receipts
                        if r["id"] in delivery_by_id and delivery_by_id[r["id"]]["status"] == "delivered"]
        if job.get("enabled", True) and job.get("state") not in {"paused", "completed"}:
            due = timestamp(job.get("next_run_at"))
            if due and (now - due).total_seconds() > overdue_seconds:
                flags.append("overdue")
            if not due:
                flags.append("next_run_unknown")
        claim = job.get("fire_claim") or {}
        started = timestamp(claim.get("at")) if isinstance(claim, dict) else None
        if current and current["status"] in {"claimed", "running"}:
            started = timestamp(current["started_at"]) or timestamp(current["claimed_at"]) or started
        if started and (now - started).total_seconds() > stale_seconds:
            flags.append("stale_claim_or_run")
        rows.append({"id": job.get("id"), "name": job.get("name"), "enabled": job.get("enabled", True),
                     "action": action, "delivery": delivery, "last_run_at": last_run,
                     "last_action_success_at": action_success,
                     "last_delivery_acknowledged_at": max((s for s in acknowledged if s), default=None),
                     "last_delivery_success_at": None,
                     "next_run_at": stamp(job.get("next_run_at")), "flags": flags})
    return {"checked_at": now.isoformat(), "job_count": len(rows), "jobs": rows, "warnings": warnings,
            "storage": {p: inventory(home / p) for p in
                        ("sessions", "state.db", "state.db-wal", "state.db-shm", "logs", "checkpoints", "cron/output", "cron/executions.db", "cron/deliveries.db")},
            "retention": "This command never prunes. It does not inspect profile pruning settings. Preserve searchable sessions; file mtimes are not session dates.",
            "limits": "Metadata snapshot, not process liveness or downstream correctness. Action success timestamps are scheduler terminal-record times, not exact action-end times or proof of downstream effects. Delivery acknowledgment timestamps are queue ACK times, may describe failure notifications, and do not prove verified delivery. Retained delivery rows and tombstones have no per-delivery verification evidence; last_delivery_success_at is unknown. Historical evidence is limited to retained receipts. Unknown is not success."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")), help="Hermes state directory (explicit for another profile)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--stale-seconds", type=int, default=3600)
    parser.add_argument("--overdue-seconds", type=int, default=300)
    args = parser.parse_args()
    if args.stale_seconds < 0 or args.overdue_seconds < 0:
        parser.error("thresholds must be nonnegative")
    result = report(args.home, stale_seconds=args.stale_seconds, overdue_seconds=args.overdue_seconds)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=True))
    else:
        print(f"Local health: {result['job_count']} jobs at {result['checked_at']}")
        for row in result["jobs"]:
            label = json.dumps(row['name'], ensure_ascii=True)
            print(f"{row['id']} {label}: action={row['action']} delivery={row['delivery']} flags={','.join(row['flags']) or '-'}")
            print(f"  last action success={row['last_action_success_at'] or 'unknown'}; last delivery acknowledgment={row['last_delivery_acknowledged_at'] or 'unknown'}; last verified delivery success={row['last_delivery_success_at'] or 'unknown'}")
        for name, size in result["storage"].items():
            print(f"{name}: {size['files']} files / {size['bytes']} bytes; unreadable={size['unreadable']}; present={size['present']}")
        for warning in result["warnings"]:
            print("WARNING: " + warning)
        print(result["retention"])
        print(result["limits"])
    return 0 if result["job_count"] and not result["warnings"] and all(not r["flags"] and r["action"] not in {"failed", "unknown", "blocked_config", "interrupted"} and r["delivery"] not in {"failed", "unverified"} for r in result["jobs"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
