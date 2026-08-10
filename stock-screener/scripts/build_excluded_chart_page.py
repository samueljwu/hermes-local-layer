#!/usr/bin/env python3
"""Build a static page of random excluded symbols for false-negative review.

Excluded means: present in the configured universe, has enough weekly price history
for the scanner, and is not in the current one-row-per-symbol candidate output.
The sample is random and generic; no tickers are hardcoded.
"""

from __future__ import annotations

import csv
import html
import json
import random
import sys
import time
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from build_chart_page import (
    DATA_PATTERNS,
    SITE_DIST,
    chart_asset_name,
    ensure_daily_price_file,
    ensure_owned_directory,
    publish_html_with_assets,
    read_csv,
    resolve_owned_output_path,
    svg_chart,
    write_text_atomic,
)  # noqa: E402
from stock_screener.patterns import read_price_csv, sma  # noqa: E402
from stock_screener.atomic_io import atomic_text_writer  # noqa: E402
from stock_screener.locking import run_locked  # noqa: E402
from stock_screener.symbols import normalize_symbol, safe_symbol_path  # noqa: E402

PATTERN_CONFIG_PATH = ROOT / "config" / "patterns.json"
CHART_CONFIG_PATH = ROOT / "config" / "chart_page.json"
OUTPUT_HTML = resolve_owned_output_path(SITE_DIST, "site/dist/excluded.html", allow_file=True)
OUTPUT_SUMMARY = resolve_owned_output_path(SITE_DIST, "site/dist/excluded_summary.json", allow_file=True)
OUTPUT_SAMPLE = resolve_owned_output_path(DATA_PATTERNS, "data/patterns/excluded_random_50.csv", allow_file=True)


def read_universe(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return {normalize_symbol(row["symbol"]): row for row in csv.DictReader(fh) if row.get("symbol")}


def read_candidate_symbols(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8") as fh:
        return {normalize_symbol(row["symbol"]) for row in csv.DictReader(fh) if row.get("symbol")}


def excluded_symbols(pattern_config: dict) -> tuple[list[str], dict[str, int]]:
    universe = read_universe(ROOT / pattern_config["input_universe_path"])
    candidate_symbols = read_candidate_symbols(ROOT / pattern_config["output_symbols_path"])
    price_dir = ROOT / pattern_config["price_dir"]
    min_history = int(pattern_config["min_history_weeks"])
    excluded: list[str] = []
    stats = {
        "universe_symbols": len(universe),
        "candidate_symbols": len(candidate_symbols),
        "missing_weekly_prices": 0,
        "too_short_weekly_history": 0,
        "invalid_weekly_prices": 0,
    }
    for symbol in sorted(universe):
        if symbol in candidate_symbols:
            continue
        path = safe_symbol_path(price_dir, symbol, ".csv")
        if not path.exists():
            stats["missing_weekly_prices"] += 1
            continue
        try:
            rows = read_price_csv(path)
        except (OSError, ValueError, KeyError) as e:
            stats["invalid_weekly_prices"] += 1
            continue
        if len(rows) < min_history:
            stats["too_short_weekly_history"] += 1
            continue
        excluded.append(symbol)
    stats["eligible_excluded_symbols"] = len(excluded)
    return excluded, stats


def pct(a: float, b: float) -> float:
    return (a - b) / b * 100.0 if b else 0.0


def exclusion_quality_score(symbol: str, pattern_config: dict) -> float:
    """Generic price-only score for ordering excluded charts worst-to-best.

    This is intentionally not a candidate detector. It just gives the random
    excluded sample an "opposite" ordering from interesting: lower/uglier chart
    structures first, stronger/closer-to-interesting charts later.
    """
    path = safe_symbol_path(ROOT / pattern_config["price_dir"], symbol, ".csv")
    rows = read_price_csv(path)
    closes = [float(r["close"]) for r in rows]
    highs = [float(r["high"]) for r in rows]
    lows = [float(r["low"]) for r in rows]
    close = closes[-1]
    sma20 = sma(closes, min(20, len(closes))) or close
    sma50 = sma(closes, min(50, len(closes))) or close
    ret13 = pct(close, closes[-min(13, len(closes))])
    high104 = max(highs[-min(104, len(highs)):])
    low104 = min(lows[-min(104, len(lows)):])
    range_pos = ((close - low104) / (high104 - low104) * 100.0) if high104 > low104 else 50.0
    drawdown = pct(close, high104)
    score = 50.0
    score += max(-30.0, min(30.0, ret13)) * 0.45
    score += max(-25.0, min(25.0, pct(close, sma20))) * 0.75
    score += max(-25.0, min(25.0, pct(sma20, sma50))) * 0.70
    score += (range_pos - 50.0) * 0.25
    score += max(-60.0, min(0.0, drawdown)) * 0.20
    return round(score, 2)


def sample_symbols(symbols: list[str], size: int, pattern_config: dict, seed: int | None = None) -> tuple[list[tuple[str, float]], int]:
    actual_seed = seed if seed is not None else time.time_ns()
    rng = random.Random(actual_seed)
    symbols = symbols[:]
    rng.shuffle(symbols)
    sampled = symbols[:size]
    scored = []
    for symbol in sampled:
        try:
            scored.append((symbol, exclusion_quality_score(symbol, pattern_config)))
        except (OSError, ValueError, KeyError):
            continue
    scored.sort(key=lambda item: (item[1], item[0]))
    return scored, actual_seed


def write_sample(path: Path, scored_symbols: list[tuple[str, float]], seed: int, universe: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["sample_seed", "symbol", "exclusion_quality", "name", "exchange", "sector", "industry", "market_cap"]
    with atomic_text_writer(path, newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for symbol, score in scored_symbols:
            meta = universe.get(symbol, {})
            writer.writerow({
                "sample_seed": seed,
                "symbol": symbol,
                "exclusion_quality": score,
                "name": meta.get("name") or meta.get("metadata_name") or "",
                "exchange": meta.get("exchange", ""),
                "sector": meta.get("sector", ""),
                "industry": meta.get("industry", ""),
                "market_cap": meta.get("market_cap", ""),
            })


def render_page(scored_symbols: list[tuple[str, float]], seed: int, pattern_config: dict, chart_config: dict) -> dict:
    output_dir = resolve_owned_output_path(SITE_DIST, chart_config["output_dir"])
    ensure_owned_directory(output_dir)
    chart_asset_dir = resolve_owned_output_path(SITE_DIST, Path(chart_config["output_dir"]) / "excluded-charts")
    chart_staging_dir = Path(tempfile.mkdtemp(dir=output_dir, prefix=".excluded-charts."))
    price_dir = ROOT / chart_config["price_dir"]
    cards = []
    skipped = []
    price_sources: dict[str, int] = {}
    for idx, (symbol, score) in enumerate(scored_symbols, start=1):
        try:
            path = safe_symbol_path(price_dir, symbol, ".csv")
            usable_path, source_status = ensure_daily_price_file(symbol, path, chart_config)
            if usable_path is None:
                skipped.append({"symbol": symbol, "reason": "missing_price_file"})
                continue
            price_sources[source_status] = price_sources.get(source_status, 0) + 1
            rows = read_csv(usable_path)
            lookback = min(int(chart_config.get("lookback_days", 260)), len(rows))
            svg = svg_chart(symbol, rows, "excluded_non_candidate", {}, lookback)
            svg_name = chart_asset_name(idx, symbol)
            write_text_atomic(chart_staging_dir / svg_name, svg)
            chart_url = f"/stocks/excluded-charts/{svg_name}"
        except (OSError, ValueError, KeyError) as e:
            skipped.append({"symbol": symbol, "reason": f"{type(e).__name__}: {e}"})
            continue
        cards.append({"index": idx, "symbol": symbol, "score": score, "chart_url": chart_url})

    generated = datetime.now(timezone.utc).isoformat()
    style = """
:root { color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f1117; color: #e6e7eb; --bg:#0f1117; --panel:#151821; --panel-2:#1a1e29; --text:#e6e7eb; --muted:#9aa3b2; --line:#2a2f3a; --accent:#8ea0ff; }
* { box-sizing: border-box; }
body { margin: 0; padding-top: 64px; background: var(--bg); color: var(--text); }
.site-header { background: var(--bg); }
.topbar-shell { position: fixed; top: 0; left: 0; right: 0; z-index: 50; height: 64px; border-bottom: 1px solid var(--line); background: var(--bg); }
.topbar { display: flex; align-items: center; justify-content: space-between; gap: 24px; width: 100%; height: 64px; margin: 0 auto; padding: 0 32px; }
.brand { display: inline-flex; align-items: center; height: 64px; color: var(--text); text-decoration: none; font-size: 16px; font-weight: 600; letter-spacing: -.01em; }
.nav { display: flex; align-items: center; justify-content: flex-end; gap: 24px; height: 64px; }
.nav a { display: inline-flex; align-items: center; height: 64px; padding: 0; color: var(--muted); text-decoration: none; font-size: 13px; font-weight: 500; white-space: nowrap; }
.nav a:hover, .nav a:focus-visible, .nav a.active { color: var(--accent); outline: none; }
.hero { max-width: 1240px; margin: 0 auto; padding: 14px 16px 12px; }
h1 { margin: 0 0 8px; font-size: clamp(22px, 3.4vw, 32px); line-height: 1.1; letter-spacing: -.04em; }
.subnav { display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0 10px; }
.subnav a, .subnav span { display: inline-flex; align-items: center; min-height: 28px; padding: 4px 10px; border: 1px solid var(--line); border-radius: 999px; color: var(--muted); text-decoration: none; font-size: 13px; }
.subnav .active { color: var(--text); background: var(--panel-2); }
.meta { max-width: 1000px; color: var(--muted); font-size: 12px; line-height: 1.45; }
.meta + .meta { margin-top: 3px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 430px), 1fr)); gap: 18px; max-width: 1680px; margin: 0 auto; padding: 18px; }
.card { border: 1px solid #1e293b; border-radius: 16px; overflow: hidden; background: #0f172a; box-shadow: 0 18px 40px rgba(0,0,0,.22); content-visibility: auto; contain-intrinsic-size: 420px; }
.card-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; padding: 12px 14px 6px; }
.symbol { font-size: 18px; font-weight: 800; letter-spacing: .04em; }
.pattern { color: #fbbf24; font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }
.details { color: #94a3b8; font-size: 12px; padding: 0 14px 10px; min-height: 18px; }
svg, .chart-img { display: block; width: 100%; height: auto; }
a { color: #93c5fd; }
@media (max-width: 640px) { body { padding-top: 56px; } .topbar-shell, .topbar, .brand, .nav, .nav a { height: 56px; } .topbar { gap: 14px; padding: 0 14px; } .brand { font-size: 15px; } .nav { gap: 14px; overflow-x: auto; scrollbar-width: none; } .nav::-webkit-scrollbar { display: none; } .nav a { font-size: 12px; flex: 0 0 auto; } .hero { padding: 12px 10px 10px; } .grid { gap: 12px; padding: 10px; } .card-head { align-items: flex-start; flex-direction: column; } }

"""
    html_cards = []
    for card in cards:
        html_cards.append(f"""
<section class="card">
  <div class="card-head"><div class="symbol">#{card['index']:02d} {html.escape(card['symbol'])}</div><div class="pattern">excluded · q {card['score']}</div></div>
  <div class="details">Randomly sampled from scanned symbols that did not pass current pattern rules. Lower exclusion-quality charts are shown first.</div>
  <img class="chart-img" src="{html.escape(card['chart_url'])}" width="560" height="330" loading="lazy" decoding="async" alt="{html.escape(card['symbol'])} candlestick chart">
</section>""")
    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stock Screener · Meh</title>
<style>{style}</style>
</head>
<body>
<header class="site-header">
  <div class="topbar-shell">
    <div class="topbar">
      <a class="brand" href="/">Hermes</a>
      <nav class="nav" aria-label="Main sections"><a href="/">Home</a><a href="/wiki/">Wiki</a><a class="active" href="/stocks/">Stocks</a><a href="/feed/">Feed</a></nav>
    </div>
  </div>
  <section class="hero">
    <h1>Stock Screener</h1>
    <nav class="subnav" aria-label="Stock Screener pages"><a href="/stocks/">Interesting</a><span class="active">Meh</span></nav>
    <div class="meta">Meh sample. Seed: {seed}. Generated: {generated}.</div>
    <div class="meta">Candles: fresh daily OHLCV when available; otherwise validated weekly fallback. SMA windows follow the rendered bar interval. White dotted line: latest close.</div>
  </section>
</header>
<main class="grid">
{''.join(html_cards)}
</main>
</body>
</html>
"""
    publish_html_with_assets(chart_staging_dir, chart_asset_dir, OUTPUT_HTML, doc)
    return {
        "generated_at_utc": generated,
        "sample_seed": seed,
        "sampled_symbols": len(scored_symbols),
        "rendered_charts": len(cards),
        "price_sources": price_sources,
        "skipped_missing_prices": skipped,
        "output_path": str(OUTPUT_HTML),
        "sample_path": str(OUTPUT_SAMPLE),
    }


def main() -> int:
    pattern_config = json.loads(PATTERN_CONFIG_PATH.read_text(encoding="utf-8"))
    chart_config = json.loads(CHART_CONFIG_PATH.read_text(encoding="utf-8"))
    universe = read_universe(ROOT / pattern_config["input_universe_path"])
    excluded, stats = excluded_symbols(pattern_config)
    sampled, seed = sample_symbols(excluded, 50, pattern_config, None)
    write_sample(OUTPUT_SAMPLE, sampled, seed, universe)
    summary = render_page(sampled, seed, pattern_config, chart_config)
    summary.update(stats)
    write_text_atomic(OUTPUT_SUMMARY, json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_locked(main))
