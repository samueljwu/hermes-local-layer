#!/usr/bin/env python3
"""Scan local weekly OHLCV cache for initial rule-based chart patterns."""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_screener.patterns import (  # noqa: E402
    PatternMatch,
    detect_double_bottom_breakout,
    detect_long_biased_override,
    detect_resistance_breakout,
    detect_support_bounce_uptrend,
    is_downtrend,
    read_price_csv,
    sma,
)
from stock_screener.price_history import fetch_symbol_with_retries, is_cache_fresh  # noqa: E402
from stock_screener.symbols import normalize_symbol, safe_symbol_path  # noqa: E402

CONFIG_PATH = ROOT / "config" / "patterns.json"


def read_universe(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return {normalize_symbol(row["symbol"]): row for row in csv.DictReader(fh) if row.get("symbol")}


def read_long_biased_symbols(path: Path) -> set[str]:
    if not path.exists():
        return set()
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {normalize_symbol(sym) for sym in payload.get("symbols", []) if str(sym).strip()}


def pattern_config(config: dict, key: str) -> dict:
    """Attach shared Weinstein Stage-2 mechanics to an ordinary pattern config."""
    out = dict(config[key])
    out["weinstein_stage2"] = config.get("weinstein_stage2", {})
    return out


def write_matches(path: Path, matches: list[PatternMatch], universe: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "symbol", "name", "exchange", "sector", "industry", "market_cap",
        "pattern", "date", "close", "quality", "evidence_json",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for match in sorted(matches, key=lambda m: (-m.quality, m.pattern, m.symbol)):
            meta = universe.get(match.symbol, {})
            writer.writerow({
                "symbol": match.symbol,
                "name": meta.get("name") or meta.get("metadata_name") or "",
                "exchange": meta.get("exchange", ""),
                "sector": meta.get("sector", ""),
                "industry": meta.get("industry", ""),
                "market_cap": meta.get("market_cap", ""),
                "pattern": match.pattern,
                "date": match.date,
                "close": round(match.close, 4),
                "quality": round(match.quality, 2),
                "evidence_json": json.dumps(match.evidence, sort_keys=True),
            })


def write_symbol_summary(path: Path, matches: list[PatternMatch], universe: dict[str, dict[str, str]]) -> None:
    by_symbol: dict[str, list[PatternMatch]] = {}
    for match in matches:
        by_symbol.setdefault(match.symbol, []).append(match)
    fields = ["symbol", "name", "exchange", "sector", "industry", "market_cap", "patterns", "best_quality", "latest_close"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for symbol, symbol_matches in sorted(by_symbol.items(), key=lambda kv: (-max(m.quality for m in kv[1]), kv[0])):
            meta = universe.get(symbol, {})
            best = max(symbol_matches, key=lambda m: m.quality)
            writer.writerow({
                "symbol": symbol,
                "name": meta.get("name") or meta.get("metadata_name") or "",
                "exchange": meta.get("exchange", ""),
                "sector": meta.get("sector", ""),
                "industry": meta.get("industry", ""),
                "market_cap": meta.get("market_cap", ""),
                "patterns": ";".join(sorted({m.pattern for m in symbol_matches})),
                "best_quality": round(best.quality, 2),
                "latest_close": round(best.close, 4),
            })


def daily_support_confirmation(symbol: str, support_config: dict) -> tuple[bool, dict[str, float | str | bool]]:
    """Confirm support-bounce candidates against daily 20d SMA if enabled.

    This uses only price-chart data. It is intentionally applied only after a weekly
    support-bounce candidate exists, to catch charts that visibly lost short-term
    support on the daily view.
    """
    if not support_config.get("daily_confirmation_enabled", False):
        return True, {"daily_confirmation": False}
    daily_dir = ROOT / support_config.get("daily_price_dir", "data/prices/yahoo_daily")
    path = safe_symbol_path(daily_dir, symbol, ".csv")
    if not is_cache_fresh(path, int(support_config.get("daily_cache_fresh_days", 5)), 50):
        fetch_symbol_with_retries(
            symbol=symbol,
            output_dir=daily_dir,
            history_years=int(support_config.get("daily_history_years", 2)),
            interval="1d",
            timeout_seconds=20,
            max_retries=2,
            backoff_seconds=[2, 5],
            min_expected_rows=50,
        )
    if not path.exists():
        # Do not exclude solely because the daily confirmation cache is unavailable.
        return True, {"daily_confirmation": "missing"}
    rows = read_price_csv(path)
    closes = [float(r["close"]) for r in rows]
    sma20 = sma(closes, 20)
    if sma20 is None:
        return True, {"daily_confirmation": "insufficient_history"}
    latest = closes[-1]
    daily_close_vs_sma20 = (latest - sma20) / sma20 * 100 if sma20 else 0.0
    ok = daily_close_vs_sma20 >= float(support_config.get("min_daily_close_vs_sma20_pct", 0.0))
    return ok, {
        "daily_confirmation": True,
        "daily_close": round(latest, 4),
        "daily_sma20": round(sma20, 4),
        "daily_close_vs_sma20_pct": round(daily_close_vs_sma20, 2),
    }


def main() -> int:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    universe = read_universe(ROOT / config["input_universe_path"])
    long_biased_symbols = read_long_biased_symbols(ROOT / config.get("long_biased_overrides_path", "config/long_biased_overrides.json"))
    price_dir = ROOT / config["price_dir"]
    matches: list[PatternMatch] = []
    skipped_missing = 0
    skipped_too_short = 0
    skipped_invalid_prices = 0
    invalid_price_samples: list[dict[str, str]] = []
    skipped_downtrend = 0
    scanned = 0

    for symbol in sorted(universe):
        path = safe_symbol_path(price_dir, symbol, ".csv")
        if not path.exists():
            skipped_missing += 1
            continue
        try:
            rows = read_price_csv(path)
        except (OSError, ValueError, KeyError) as e:
            skipped_invalid_prices += 1
            if len(invalid_price_samples) < 25:
                invalid_price_samples.append({"symbol": symbol, "path": str(path.relative_to(ROOT)), "error": f"{type(e).__name__}: {e}"})
            continue
        if len(rows) < int(config["min_history_weeks"]):
            skipped_too_short += 1
            # include short histories generally, but <20 weeks is too little for these patterns
            continue
        scanned += 1
        is_long_biased = symbol in long_biased_symbols
        resistance_config = pattern_config(config, "resistance_breakout")
        double_bottom_config = pattern_config(config, "double_bottom_breakout")
        support_bounce_config = pattern_config(config, "support_bounce_uptrend")
        if is_long_biased and config.get("long_biased_override", {}).get("enabled", True):
            m = detect_long_biased_override(symbol, rows, config["long_biased_override"])
            if m:
                matches.append(m)
        if config.get("exclude_downtrend", True) and is_downtrend(rows, config["downtrend"]):
            if not is_long_biased:
                skipped_downtrend += 1
                continue
        if resistance_config.get("enabled", True):
            m = detect_resistance_breakout(symbol, rows, resistance_config)
            if m:
                matches.append(m)
        if double_bottom_config.get("enabled", True):
            m = detect_double_bottom_breakout(symbol, rows, double_bottom_config)
            if m:
                matches.append(m)
        if support_bounce_config.get("enabled", True):
            m = detect_support_bounce_uptrend(symbol, rows, support_bounce_config)
            if m:
                try:
                    daily_ok, daily_evidence = daily_support_confirmation(symbol, support_bounce_config)
                except (OSError, ValueError, KeyError) as e:
                    skipped_invalid_prices += 1
                    if len(invalid_price_samples) < 25:
                        invalid_price_samples.append({"symbol": symbol, "path": "daily_confirmation", "error": f"{type(e).__name__}: {e}"})
                    daily_ok, daily_evidence = True, {"daily_confirmation": "invalid", "daily_confirmation_error": f"{type(e).__name__}: {e}"}
                if daily_ok:
                    m.evidence.update(daily_evidence)
                    matches.append(m)

    output_matches = ROOT / config["output_matches_path"]
    write_matches(output_matches, matches, universe)
    output_symbols = ROOT / config["output_symbols_path"]
    write_symbol_summary(output_symbols, matches, universe)
    counts = Counter(m.pattern for m in matches)
    symbols_with_matches = len(set(m.symbol for m in matches))
    summary = {
        "scanned_at_utc": datetime.now(timezone.utc).isoformat(),
        "universe_symbols": len(universe),
        "scanned_symbols": scanned,
        "skipped_missing_prices": skipped_missing,
        "skipped_invalid_prices": skipped_invalid_prices,
        "invalid_price_samples": invalid_price_samples,
        "skipped_too_short_lt_min_history": skipped_too_short,
        "skipped_obvious_downtrend": skipped_downtrend,
        "total_matches": len(matches),
        "symbols_with_matches": symbols_with_matches,
        "pattern_counts": dict(sorted(counts.items())),
        "long_biased_override_symbols": sorted(long_biased_symbols),
        "long_biased_matches": sum(1 for m in matches if m.evidence.get("manual_long_biased_override")),
        "output_matches_path": config["output_matches_path"],
        "output_symbols_path": config["output_symbols_path"],
        "config_path": str(CONFIG_PATH.relative_to(ROOT)),
        "note": "Pattern detection uses only local OHLCV price/volume data. Metadata is appended only after matching for labels/filter review. Weinstein Stage 2 concepts are integrated inside ordinary pattern rules as capped quality components and, when strongly supportive, conservative near-miss gate widening; they are not a separate overlay.",
    }
    output_summary = ROOT / config["output_summary_path"]
    output_summary.parent.mkdir(parents=True, exist_ok=True)
    output_summary.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
