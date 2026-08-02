#!/usr/bin/env python3
"""Refresh normalized weekly OHLCV price history for the filtered universe.

Provider: Yahoo chart endpoint via Python stdlib. This script is intentionally
cache-first, resumable, and conservative. It writes provider-independent CSVs
that later pattern scanners can consume without knowing where the data came from.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_screener.price_history import (  # noqa: E402
    FetchResult,
    fetch_symbol_with_retries,
    is_cache_fresh,
    read_symbols_from_filtered_universe,
    sleep_between_requests,
)
from stock_screener.owned_paths import resolve_owned_path  # noqa: E402

CONFIG_PATH = ROOT / "config" / "price_history.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def write_failures(path: Path, failures: list[FetchResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["symbol", "yahoo_symbol", "status", "rows", "path", "attempts", "error"]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for failure in failures:
            writer.writerow(asdict(failure))


def append_progress(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh weekly OHLCV price history for filtered universe")
    parser.add_argument("--max-symbols", type=int, default=None, help="Limit number of symbols for smoke tests")
    parser.add_argument("--force-refresh", action="store_true", help="Refetch even when cached files are fresh")
    parser.add_argument("--symbol", action="append", help="Fetch only specific symbol(s); can be passed multiple times")
    parser.add_argument("--min-delay", type=float, default=None, help="Override minimum delay seconds")
    parser.add_argument("--max-delay", type=float, default=None, help="Override maximum delay seconds")
    parser.add_argument("--max-elapsed-seconds", type=float, default=None, help="Stop starting new fetches after this many seconds; existing cache remains usable")
    args = parser.parse_args()

    config = load_config()
    output_dir = resolve_owned_path(ROOT, config["output_dir"], label="output_dir")
    metadata_path = resolve_owned_path(ROOT, config["metadata_path"], label="metadata_path")
    failures_path = resolve_owned_path(ROOT, config["failures_path"], label="failures_path")
    progress_path = resolve_owned_path(ROOT, config["progress_path"], label="progress_path")
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.symbol:
        symbols = sorted(dict.fromkeys(s.strip().upper() for s in args.symbol if s.strip()))
    else:
        symbols = read_symbols_from_filtered_universe(ROOT / config["input_path"])
    if args.max_symbols is not None:
        symbols = symbols[: args.max_symbols]

    min_delay = float(args.min_delay if args.min_delay is not None else config["min_delay_seconds"])
    max_delay = float(args.max_delay if args.max_delay is not None else config["max_delay_seconds"])
    max_elapsed_seconds = args.max_elapsed_seconds
    if max_elapsed_seconds is None:
        configured_max_elapsed = config.get("max_elapsed_seconds")
        max_elapsed_seconds = float(configured_max_elapsed) if configured_max_elapsed is not None else None

    started = time.perf_counter()
    fetched = 0
    fetched_short = 0
    fetched_short_preserved_cache = 0
    skipped_fresh = 0
    failed = 0
    rate_limited = 0
    failures: list[FetchResult] = []
    rate_limit_errors_seen = 0
    stopped_early_reason = ""

    append_progress(progress_path, {"event": "start", "at": utc_now_iso(), "symbols": len(symbols), "force_refresh": args.force_refresh, "max_elapsed_seconds": max_elapsed_seconds})

    for index, symbol in enumerate(symbols, start=1):
        elapsed_before_symbol = time.perf_counter() - started
        if max_elapsed_seconds is not None and elapsed_before_symbol >= max_elapsed_seconds:
            stopped_early_reason = "max_elapsed_seconds"
            append_progress(
                progress_path,
                {
                    "event": "stop_time_budget",
                    "at": utc_now_iso(),
                    "index": index,
                    "total": len(symbols),
                    "elapsed_seconds": round(elapsed_before_symbol, 3),
                    "max_elapsed_seconds": max_elapsed_seconds,
                },
            )
            break
        out_path = output_dir / f"{symbol}.csv"
        if not args.force_refresh and is_cache_fresh(
            out_path,
            freshness_days=int(config["freshness_days"]),
            min_rows=int(config["min_expected_rows"]),
        ):
            rows = 0
            result = FetchResult(symbol, symbol, "skipped_fresh", rows, str(out_path), 0)
            skipped_fresh += 1
        else:
            result = fetch_symbol_with_retries(
                symbol=symbol,
                output_dir=output_dir,
                history_years=int(config["history_years"]),
                interval=str(config["interval"]),
                timeout_seconds=int(config["request_timeout_seconds"]),
                max_retries=int(config["max_retries"]),
                backoff_seconds=list(config["backoff_seconds"]),
                min_expected_rows=int(config["min_expected_rows"]),
            )
            if result.status == "fetched":
                fetched += 1
            elif result.status == "fetched_short":
                fetched_short += 1
            elif result.status == "fetched_short_preserved_cache":
                fetched_short_preserved_cache += 1
            elif result.status == "rate_limited":
                rate_limited += 1
                failed += 1
                failures.append(result)
                rate_limit_errors_seen += 1
            else:
                failed += 1
                failures.append(result)

            sleep_between_requests(min_delay, max_delay)

        append_progress(progress_path, {"event": "symbol", "at": utc_now_iso(), "index": index, "total": len(symbols), **asdict(result)})

        if rate_limit_errors_seen >= int(config["stop_after_rate_limit_errors"]):
            append_progress(progress_path, {"event": "stop_rate_limit", "at": utc_now_iso(), "rate_limit_errors_seen": rate_limit_errors_seen})
            break

    elapsed = time.perf_counter() - started
    write_failures(failures_path, failures)

    metadata = {
        "started_or_last_run_at_utc": utc_now_iso(),
        "provider": config["provider"],
        "interval": config["interval"],
        "history_years": config["history_years"],
        "input_path": config["input_path"],
        "output_dir": config["output_dir"],
        "symbols_requested": len(symbols),
        "fetched": fetched,
        "fetched_short": fetched_short,
        "fetched_short_preserved_cache": fetched_short_preserved_cache,
        "skipped_fresh": skipped_fresh,
        "failed": failed,
        "rate_limited": rate_limited,
        "elapsed_seconds": round(elapsed, 3),
        "force_refresh": args.force_refresh,
        "max_symbols": args.max_symbols,
        "min_delay_seconds": min_delay,
        "max_delay_seconds": max_delay,
        "failures_path": config["failures_path"],
        "progress_path": config["progress_path"],
        "stopped_early_reason": stopped_early_reason,
    }
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(metadata, indent=2, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
