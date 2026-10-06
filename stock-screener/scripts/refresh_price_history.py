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
from stock_screener.atomic_io import append_text, atomic_write, atomic_write_text  # noqa: E402
from stock_screener.price_validation import validate_price_cache  # noqa: E402
from stock_screener.locking import run_locked  # noqa: E402
from stock_screener.symbols import normalize_symbol, safe_symbol_path  # noqa: E402

CONFIG_PATH = ROOT / "config" / "price_history.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def write_failures(path: Path, failures: list[FetchResult]) -> None:
    def write_temp(temp: Path) -> None:
        fields = ["symbol", "yahoo_symbol", "status", "rows", "path", "attempts", "error"]
        with temp.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for failure in failures:
                writer.writerow(asdict(failure))
    atomic_write(path, write_temp)


def append_progress(path: Path, event: dict) -> None:
    append_text(path, json.dumps(event, sort_keys=True) + "\n")


def ordered_attempts(symbols: list[str], state: dict[str, str]) -> list[str]:
    """Oldest actual attempt first; failed symbols cannot monopolize a budget."""
    return sorted(symbols, key=lambda symbol: (state.get(symbol, ""), symbol))


def load_attempt_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("last_attempt_utc"), dict):
        raise ValueError("invalid attempt state")
    state = payload["last_attempt_utc"]
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in state.items()):
        raise ValueError("invalid attempt state values")
    return state


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
    state_path = resolve_owned_path(ROOT, config["attempt_state_path"], label="attempt_state_path")
    benchmark_config = config.get("benchmark", {})
    benchmark_enabled = bool(benchmark_config.get("enabled", False))
    benchmark_symbol = ""
    benchmark_dir = output_dir
    if benchmark_enabled:
        benchmark_symbol = normalize_symbol(benchmark_config["symbol"])
        benchmark_dir = resolve_owned_path(ROOT, benchmark_config["output_dir"], label="benchmark_output_dir")
        if benchmark_dir == output_dir:
            raise ValueError("benchmark cache must be separate from equity cache")

    if args.symbol:
        symbols = sorted(dict.fromkeys(s.strip().upper() for s in args.symbol if s.strip()))
    else:
        symbols = read_symbols_from_filtered_universe(ROOT / config["input_path"])
    if args.max_symbols is not None:
        symbols = symbols[: args.max_symbols]
    attempt_state = load_attempt_state(state_path)
    symbols = ordered_attempts(symbols, attempt_state)

    min_delay = float(args.min_delay if args.min_delay is not None else config["min_delay_seconds"])
    max_delay = float(args.max_delay if args.max_delay is not None else config["max_delay_seconds"])
    max_elapsed_seconds = args.max_elapsed_seconds
    if max_elapsed_seconds is None:
        configured_max_elapsed = config.get("max_elapsed_seconds")
        max_elapsed_seconds = float(configured_max_elapsed) if configured_max_elapsed is not None else None

    started = time.perf_counter()
    deadline = started + max_elapsed_seconds if max_elapsed_seconds is not None else None
    fetched = 0
    fetched_short = 0
    fetched_short_preserved_cache = 0
    skipped_fresh = 0
    failed = 0
    rate_limited = 0
    budget_exhausted = 0
    failures: list[FetchResult] = []
    rate_limit_errors_seen = 0
    stopped_early_reason = ""
    processed = 0

    append_progress(progress_path, {"event": "start", "at": utc_now_iso(), "symbols": len(symbols), "force_refresh": args.force_refresh, "max_elapsed_seconds": max_elapsed_seconds})

    for index, symbol in enumerate(symbols, start=1):
        elapsed_before_symbol = time.perf_counter() - started
        if deadline is not None and time.perf_counter() >= deadline:
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
            interval="1wk",
        ):
            rows = 0
            result = FetchResult(symbol, symbol, "skipped_fresh", rows, str(out_path), 0)
            skipped_fresh += 1
        else:
            # Persist before the request so a crash or rate limit also advances
            # fairness. The lock serializes simultaneous weekly invocations.
            previous_attempt = attempt_state.get(symbol)
            attempt_state[symbol] = utc_now_iso()
            atomic_write_text(state_path, json.dumps({"last_attempt_utc": attempt_state}, indent=2, sort_keys=True) + "\n")
            result = fetch_symbol_with_retries(
                symbol=symbol,
                output_dir=output_dir,
                history_years=int(config["history_years"]),
                interval=str(config["interval"]),
                timeout_seconds=int(config["request_timeout_seconds"]),
                max_retries=int(config["max_retries"]),
                backoff_seconds=list(config["backoff_seconds"]),
                min_expected_rows=int(config["min_expected_rows"]),
                deadline=deadline,
            )
            if result.attempts == 0:
                # A deadline can expire between the loop check and the fetch;
                # do not record an attempt that never reached the provider.
                if previous_attempt is None:
                    attempt_state.pop(symbol, None)
                else:
                    attempt_state[symbol] = previous_attempt
                atomic_write_text(state_path, json.dumps({"last_attempt_utc": attempt_state}, indent=2, sort_keys=True) + "\n")
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
            elif result.status == "budget_exhausted":
                budget_exhausted += 1
                stopped_early_reason = "max_elapsed_seconds"
            else:
                failed += 1
                failures.append(result)

            if deadline is None:
                sleep_between_requests(min_delay, max_delay)
            elif result.status != "budget_exhausted":
                remaining = max(0.0, deadline - time.perf_counter())
                if remaining > 0:
                    sleep_between_requests(min(min_delay, remaining), min(max_delay, remaining))

        append_progress(progress_path, {"event": "symbol", "at": utc_now_iso(), "index": index, "total": len(symbols), **asdict(result)})
        if result.status != "budget_exhausted" or result.attempts:
            processed += 1

        if result.status == "budget_exhausted":
            append_progress(progress_path, {"event": "stop_time_budget", "at": utc_now_iso(), "index": index,
                                             "total": len(symbols), "elapsed_seconds": round(time.perf_counter() - started, 3),
                                             "max_elapsed_seconds": max_elapsed_seconds})
            break

        if rate_limit_errors_seen >= int(config["stop_after_rate_limit_errors"]):
            stopped_early_reason = "rate_limit"
            append_progress(progress_path, {"event": "stop_rate_limit", "at": utc_now_iso(), "rate_limit_errors_seen": rate_limit_errors_seen})
            break

    # Independent cache and budget: the benchmark can be fetched separately even
    # when its ticker also belongs to the ordinary filtered equity universe.
    benchmark_status = "disabled"
    benchmark_error = ""
    if benchmark_enabled:
        benchmark_path = safe_symbol_path(benchmark_dir, benchmark_symbol, ".csv")
        benchmark_budget = float(benchmark_config.get("max_elapsed_seconds", 60))
        if benchmark_budget <= 0:
            raise ValueError("benchmark max_elapsed_seconds must be positive")
        benchmark_deadline = time.perf_counter() + benchmark_budget
        if not args.force_refresh and is_cache_fresh(benchmark_path, int(config["freshness_days"]),
                                                     int(config["min_expected_rows"]), interval="1wk"):
            benchmark_status = "skipped_fresh"
        elif stopped_early_reason == "rate_limit":
            benchmark_status = "rate_limit_deferred"
        elif time.perf_counter() >= benchmark_deadline:
            benchmark_status = "budget_exhausted"
        else:
            try:
                benchmark_result = fetch_symbol_with_retries(
                    symbol=benchmark_symbol, output_dir=benchmark_dir,
                    history_years=int(config["history_years"]), interval="1wk",
                    timeout_seconds=int(config["request_timeout_seconds"]),
                    max_retries=int(config["max_retries"]), backoff_seconds=list(config["backoff_seconds"]),
                    min_expected_rows=int(config["min_expected_rows"]), deadline=benchmark_deadline)
                benchmark_status = benchmark_result.status
                benchmark_error = benchmark_result.error or ""
            except Exception as exc:
                benchmark_status = "error"
                benchmark_error = f"{type(exc).__name__}: {exc}"
        append_progress(progress_path, {"event": "benchmark", "at": utc_now_iso(), "symbol": benchmark_symbol,
                                        "status": benchmark_status, "error": benchmark_error})

    elapsed = time.perf_counter() - started
    write_failures(failures_path, failures)
    coverage = validate_price_cache(symbols, output_dir, min_rows=int(config["min_expected_rows"]))
    benchmark_coverage = (validate_price_cache([benchmark_symbol], benchmark_dir,
                          min_rows=int(config["min_expected_rows"])) if benchmark_enabled else None)

    metadata = {
        "started_or_last_run_at_utc": utc_now_iso(),
        "provider": config["provider"],
        "benchmark": {"symbol": benchmark_symbol, "status": benchmark_status, "error": benchmark_error,
                      "coverage": benchmark_coverage} if benchmark_enabled else {"status": "disabled"},
        "interval": config["interval"],
        "history_years": config["history_years"],
        "input_path": config["input_path"],
        "output_dir": config["output_dir"],
        "symbols_requested": len(symbols),
        "processed": processed,
        "remaining": len(symbols) - processed,
        "as_of_week": coverage["as_of_week"],
        "fresh": coverage["fresh_count"],
        "stale": coverage["stale_count"],
        "incomplete": coverage["incomplete_count"],
        "invalid": coverage["invalid_count"],
        "missing": coverage["missing_count"],
        "fetched": fetched,
        "fetched_short": fetched_short,
        "fetched_short_preserved_cache": fetched_short_preserved_cache,
        "skipped_fresh": skipped_fresh,
        "failed": failed,
        "rate_limited": rate_limited,
        "budget_exhausted": budget_exhausted,
        "incomplete_tail": coverage["incomplete_tail_count"],
        "elapsed_seconds": round(elapsed, 3),
        "force_refresh": args.force_refresh,
        "max_symbols": args.max_symbols,
        "min_delay_seconds": min_delay,
        "max_delay_seconds": max_delay,
        "failures_path": config["failures_path"],
        "progress_path": config["progress_path"],
        "stopped_early_reason": stopped_early_reason,
    }
    atomic_write_text(metadata_path, json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    print(json.dumps(metadata, indent=2, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run_locked(main))
