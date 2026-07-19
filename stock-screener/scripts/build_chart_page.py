#!/usr/bin/env python3
"""Build the static stock chart candidate page from pattern matches.

The normal configuration renders all matched tickers, one chart per ticker, sorted
by best-match quality. If configured with a numeric sample size, it samples symbols
uniformly and still sorts the sampled charts by quality for review.
"""

from __future__ import annotations

import csv
import html
import json
import math
import random
import re
import shutil
import time
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stock_screener.price_history import fetch_symbol_with_retries, is_cache_fresh

CONFIG_PATH = ROOT / "config/chart_page.json"
SITE_DIST = (ROOT / "site" / "dist").resolve()
DATA_PATTERNS = (ROOT / "data" / "patterns").resolve()


def resolve_owned_output_path(base: Path, configured: str | Path, *, allow_file: bool = False) -> Path:
    candidate = Path(configured)
    if candidate.is_absolute():
        raise ValueError(f"absolute output path is not allowed: {candidate}")
    resolved = (ROOT / candidate).resolve()
    base_resolved = base.resolve()
    allowed = resolved == base_resolved or base_resolved in resolved.parents
    if not allowed:
        raise ValueError(f"output path escapes {base_resolved}: {resolved}")
    probe = resolved.parent if allow_file else resolved
    for parent in [probe, *probe.parents]:
        if parent == ROOT.resolve().parent:
            break
        if parent.exists() and parent.is_symlink():
            raise ValueError(f"refusing symlinked output path: {parent}")
    return resolved


def ensure_owned_directory(path: Path) -> Path:
    if path.exists() and path.is_symlink():
        raise ValueError(f"refusing symlinked output directory: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f"refusing symlinked output directory: {path}")
    return path


def write_text_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def promote_directory(staging_dir: Path, live_dir: Path) -> None:
    """Replace a generated asset directory after a caller has finished staging."""
    backup_dir = live_dir.with_name(f".{live_dir.name}.old")
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    if live_dir.exists():
        live_dir.rename(backup_dir)
    try:
        staging_dir.rename(live_dir)
    except Exception:
        if backup_dir.exists() and not live_dir.exists():
            backup_dir.rename(live_dir)
        raise
    if backup_dir.exists():
        shutil.rmtree(backup_dir)


def publish_html_with_assets(staging_dir: Path, live_dir: Path, html_path: Path, html_text: str) -> None:
    """Atomically publish assets + HTML, rolling assets back if HTML promotion fails."""
    backup_dir = live_dir.with_name(f".{live_dir.name}.old")
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    if live_dir.exists():
        live_dir.rename(backup_dir)
    try:
        staging_dir.rename(live_dir)
        write_text_atomic(html_path, html_text)
    except Exception:
        if live_dir.exists():
            shutil.rmtree(live_dir)
        if backup_dir.exists():
            backup_dir.rename(live_dir)
        raise
    else:
        if backup_dir.exists():
            shutil.rmtree(backup_dir)


def chart_asset_name(index: int, symbol: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", symbol).strip(".-") or "chart"
    return f"{index:03d}-{safe}.svg"


def read_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


def f(row: dict, key: str) -> float:
    return float(row[key])


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def best_match(rows: list[dict[str, str]]) -> dict[str, str]:
    return max(rows, key=lambda r: float(r.get("quality") or 0.0))


def sample_matches(matches: list[dict[str, str]], size: int | str, seed: int | None) -> tuple[list[dict[str, str]], int]:
    """Return one chart per ticker.

    For the current all-interesting page, order by best match quality descending.
    For ad-hoc random review pages, sample symbols uniformly and then sort the
    sampled charts by quality descending.
    """
    actual_seed = seed if seed is not None else time.time_ns()
    rng = random.Random(actual_seed)
    by_symbol: dict[str, list[dict[str, str]]] = {}
    for row in matches:
        by_symbol.setdefault(row["symbol"], []).append(row)
    best_by_symbol = {symbol: best_match(rows) for symbol, rows in by_symbol.items()}
    if isinstance(size, str) and size.lower() == "all":
        selected = list(best_by_symbol.values())
    else:
        symbols = list(best_by_symbol)
        rng.shuffle(symbols)
        selected = [best_by_symbol[symbol] for symbol in symbols[:int(size)]]
    selected.sort(key=lambda r: (-float(r.get("quality") or 0.0), r["symbol"]))
    return selected, actual_seed


def write_shortlist(path: Path, rows: list[dict[str, str]], seed: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fieldnames = ["sample_seed"] + list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            out = {"sample_seed": seed}
            out.update(row)
            writer.writerow(out)


def scale_y(value: float, lo: float, hi: float, top: float, height: float) -> float:
    if hi <= lo:
        return top + height / 2
    return top + (hi - value) / (hi - lo) * height


def moving_average(values: list[float], window: int) -> list[float | None]:
    out: list[float | None] = []
    running = 0.0
    for i, val in enumerate(values):
        running += val
        if i >= window:
            running -= values[i - window]
        if i + 1 >= window:
            out.append(running / window)
        else:
            out.append(None)
    return out


def path_for_series(series: list[float | None], lo: float, hi: float, left: float, top: float, width: float, height: float) -> str:
    pts = []
    n = len(series)
    for i, val in enumerate(series):
        if val is None:
            continue
        x = left + (i / max(1, n - 1)) * width
        y = scale_y(val, lo, hi, top, height)
        pts.append((x, y))
    if not pts:
        return ""
    return "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)


def svg_chart(symbol: str, rows: list[dict[str, str]], pattern: str, evidence: dict, lookback: int) -> str:
    rows = rows[-lookback:]
    if not rows:
        raise ValueError(f"no price rows for {symbol}")
    width, height = 560, 330
    left, right, top, bottom = 46, 12, 18, 52
    cw = width - left - right
    ch = height - top - bottom
    lows = [f(r, "low") for r in rows]
    highs = [f(r, "high") for r in rows]
    closes = [f(r, "close") for r in rows]
    volumes = [f(r, "volume") for r in rows]
    lo = min(lows)
    hi = max(highs)
    pad = (hi - lo) * 0.06 if hi > lo else max(1.0, hi * 0.05)
    lo -= pad
    hi += pad
    n = len(rows)
    candle_w = max(2.0, min(8.0, cw / max(1, n) * 0.58))
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(symbol)} chart">'
    ]
    parts.append('<rect width="100%" height="100%" rx="12" fill="#0f172a"/>')
    # grid and labels
    for k in range(5):
        y = top + k * ch / 4
        price = hi - k * (hi - lo) / 4
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#233044" stroke-width="1"/>')
        parts.append(f'<text x="8" y="{y+4:.1f}" fill="#94a3b8" font-size="10">{price:.0f}</text>')
    max_vol = max(volumes) if volumes else 1
    vol_top = height - bottom + 6
    vol_h = 34
    for i, r in enumerate(rows):
        x = left + (i / max(1, n - 1)) * cw
        o, h, l, c = f(r, "open"), f(r, "high"), f(r, "low"), f(r, "close")
        color = "#22c55e" if c >= o else "#ef4444"
        y_h = scale_y(h, lo, hi, top, ch)
        y_l = scale_y(l, lo, hi, top, ch)
        y_o = scale_y(o, lo, hi, top, ch)
        y_c = scale_y(c, lo, hi, top, ch)
        body_top = min(y_o, y_c)
        body_h = max(1.5, abs(y_o - y_c))
        parts.append(f'<line x1="{x:.1f}" y1="{y_h:.1f}" x2="{x:.1f}" y2="{y_l:.1f}" stroke="{color}" stroke-width="1"/>')
        parts.append(f'<rect x="{x-candle_w/2:.1f}" y="{body_top:.1f}" width="{candle_w:.1f}" height="{body_h:.1f}" fill="{color}" opacity="0.88"/>')
        vh = (f(r, "volume") / max_vol) * vol_h if max_vol else 0
        parts.append(f'<rect x="{x-candle_w/2:.1f}" y="{vol_top+vol_h-vh:.1f}" width="{candle_w:.1f}" height="{vh:.1f}" fill="{color}" opacity="0.30"/>')
    ma20 = moving_average(closes, 20)
    ma50 = moving_average(closes, 50)
    ma200 = moving_average(closes, 200)
    p20 = path_for_series(ma20, lo, hi, left, top, cw, ch)
    p50 = path_for_series(ma50, lo, hi, left, top, cw, ch)
    p200 = path_for_series(ma200, lo, hi, left, top, cw, ch)
    if p20:
        parts.append(f'<path d="{p20}" fill="none" stroke="#facc15" stroke-width="1.8" opacity="0.95"/>')
    if p50:
        parts.append(f'<path d="{p50}" fill="none" stroke="#38bdf8" stroke-width="1.8" opacity="0.95"/>')
    if p200:
        parts.append(f'<path d="{p200}" fill="none" stroke="#f97316" stroke-width="2.0" opacity="0.95"/>')
    # pattern guide lines
    for key, color in [("resistance_level", "#a78bfa"), ("neckline", "#fb7185"), ("support_level", "#34d399")]:
        if key in evidence:
            val = float(evidence[key])
            if lo <= val <= hi:
                y = scale_y(val, lo, hi, top, ch)
                parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="{color}" stroke-width="2" stroke-dasharray="6 5"/>')
                parts.append(f'<text x="{left+4}" y="{y-5:.1f}" fill="{color}" font-size="10">{key.replace("_", " ")}: {val:.2f}</text>')
    latest = rows[-1]
    latest_close = float(latest["close"])
    latest_y = scale_y(latest_close, lo, hi, top, ch)
    parts.append(f'<line x1="{left}" y1="{latest_y:.1f}" x2="{width-right}" y2="{latest_y:.1f}" stroke="#e2e8f0" stroke-width="1.4" stroke-dasharray="2 5" opacity="0.9"/>')
    parts.append(f'<text x="{width-right-74}" y="{latest_y-5:.1f}" fill="#e2e8f0" font-size="10">last {latest_close:.2f}</text>')
    subtitle = f"{pattern.replace('_', ' ')} · {rows[0]['date']} → {latest['date']} · close {latest_close:.2f}"
    parts.append(f'<text x="{left}" y="{height-8}" fill="#cbd5e1" font-size="11">{html.escape(subtitle)}</text>')
    parts.append('</svg>')
    return "".join(parts)


def ensure_daily_price_file(symbol: str, path: Path, config: dict) -> tuple[Path | None, str]:
    if is_cache_fresh(path, int(config.get("daily_cache_fresh_days", 5)), 200):
        return path, "daily_cached"
    if config.get("fetch_missing_daily", False):
        result = fetch_symbol_with_retries(
            symbol=symbol,
            output_dir=path.parent,
            history_years=int(config.get("daily_history_years", 2)),
            interval="1d",
            timeout_seconds=20,
            max_retries=2,
            backoff_seconds=[2, 5],
            min_expected_rows=120,
        )
        if result.status in {"fetched", "fetched_short"} and path.exists():
            return path, result.status
    fallback = config.get("fallback_price_dir")
    if fallback:
        fallback_path = ROOT / fallback / f"{symbol}.csv"
        if fallback_path.exists():
            return fallback_path, "weekly_fallback"
    return None, "missing"


def build_page(config: dict, sampled: list[dict[str, str]], seed: int) -> dict:
    output_dir = resolve_owned_output_path(SITE_DIST, config["output_dir"])
    ensure_owned_directory(output_dir)
    chart_asset_dir = resolve_owned_output_path(SITE_DIST, Path(config["output_dir"]) / "charts")
    chart_staging_dir = resolve_owned_output_path(SITE_DIST, Path(config["output_dir"]) / ".charts.tmp")
    if chart_staging_dir.exists():
        shutil.rmtree(chart_staging_dir)
    ensure_owned_directory(chart_staging_dir)
    price_dir = ROOT / config["price_dir"]
    cards = []
    skipped = []
    price_sources = {}
    for idx, match in enumerate(sampled, start=1):
        symbol = match["symbol"]
        try:
            path = price_dir / f"{symbol}.csv"
            usable_path, source_status = ensure_daily_price_file(symbol, path, config)
            if usable_path is None:
                skipped.append({"symbol": symbol, "reason": "missing_price_file"})
                continue
            price_sources[source_status] = price_sources.get(source_status, 0) + 1
            rows = read_csv(usable_path)
            evidence = json.loads(match.get("evidence_json") or "{}")
            lookback = min(int(config.get("lookback_days", config.get("lookback_weeks", 260))), len(rows))
            svg = svg_chart(symbol, rows, match["pattern"], evidence, lookback)
            svg_name = chart_asset_name(idx, symbol)
            write_text_atomic(chart_staging_dir / svg_name, svg)
            chart_url = f"/stocks/charts/{svg_name}"
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as e:
            skipped.append({"symbol": symbol, "reason": f"{type(e).__name__}: {e}"})
            continue
        cards.append({"index": idx, "match": match, "chart_url": chart_url})
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
.pattern { color: #c4b5fd; font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }
.details { color: #94a3b8; font-size: 12px; padding: 0 14px 10px; min-height: 18px; }
svg, .chart-img { display: block; width: 100%; height: auto; }
a { color: #93c5fd; }
@media (max-width: 640px) { body { padding-top: 56px; } .topbar-shell, .topbar, .brand, .nav, .nav a { height: 56px; } .topbar { gap: 14px; padding: 0 14px; } .brand { font-size: 15px; } .nav { gap: 14px; overflow-x: auto; scrollbar-width: none; } .nav::-webkit-scrollbar { display: none; } .nav a { font-size: 12px; flex: 0 0 auto; } .hero { padding: 12px 10px 10px; } .grid { gap: 12px; padding: 10px; } .card-head { align-items: flex-start; flex-direction: column; } }

"""
    html_cards = []
    for card in cards:
        m = card["match"]
        try:
            ev = json.loads(m.get("evidence_json") or "{}")
        except json.JSONDecodeError:
            ev = {}
        evidence_bits = []
        for key in ["lookback_weeks", "breakout_buffer_pct", "touch_count", "volume_confirmation", "bounce_from_low_pct"]:
            if key in ev:
                evidence_bits.append(f"{key.replace('_', ' ')}: {ev[key]}")
        detail = " · ".join(evidence_bits)
        html_cards.append(f"""
<section class="card">
  <div class="card-head"><div class="symbol">#{card['index']:02d} {html.escape(m['symbol'])}</div><div class="pattern">{html.escape(m['pattern'].replace('_', ' '))}</div></div>
  <div class="details">quality {html.escape(m['quality'])} · {html.escape(detail)}</div>
  <img class="chart-img" src="{html.escape(card['chart_url'])}" width="560" height="330" loading="lazy" decoding="async" alt="{html.escape(m['symbol'])} candlestick chart">
</section>""")
    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(config.get('title', 'Stock Screener'))}</title>
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
    <nav class="subnav" aria-label="Stock Screener pages"><span class="active">Interesting</span><a href="/stocks/excluded.html">Meh</a></nav>
    <div class="meta">Generated: {generated}. Seed/order: {seed}. Source: {html.escape(config['matches_path'])}.</div>
    <div class="meta">Candles: fresh daily OHLCV when available; otherwise validated weekly fallback. SMA windows follow the rendered bar interval. White dotted line: latest close. Dashed colored line: detected pattern reference level.</div>
  </section>
</header>
<main class="grid">
{''.join(html_cards)}
</main>
</body>
</html>
"""
    publish_html_with_assets(chart_staging_dir, chart_asset_dir, output_dir / "index.html", doc)
    return {"generated_at_utc": generated, "sample_seed": seed, "sampled_matches": len(sampled), "rendered_charts": len(cards), "price_sources": price_sources, "skipped_missing_prices": skipped, "output_path": str(output_dir / "index.html")}


def main() -> int:
    config = read_config()
    matches = read_csv(ROOT / config["matches_path"])
    sampled, seed = sample_matches(matches, config.get("sample_size", 50), config.get("random_seed"))
    shortlist_path = resolve_owned_output_path(DATA_PATTERNS, config["shortlist_path"], allow_file=True)
    write_shortlist(shortlist_path, sampled, seed)
    summary = build_page(config, sampled, seed)
    summary["shortlist_path"] = config["shortlist_path"]
    summary["matches_available"] = len(matches)
    summary_path = resolve_owned_output_path(SITE_DIST, Path(config["output_dir"]) / "summary.json", allow_file=True)
    write_text_atomic(summary_path, json.dumps(summary, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
