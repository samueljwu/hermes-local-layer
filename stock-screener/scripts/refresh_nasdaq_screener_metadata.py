#!/usr/bin/env python3
"""Refresh no-key Nasdaq screener metadata for universe filtering.

Primary source: rreichel3/US-Stock-Symbols raw GitHub JSON files.
Fallback 1: Nasdaq screener API directly.
Fallback 2: last cached raw file, if allowed.

This gives sector, industry, market cap, country, last sale, and volume without
spending paid market-data API calls. The joined output keeps our exchange-derived
universe as the canonical universe and treats this metadata as enrichment only.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "nasdaq_screener_metadata.json"


@dataclass(frozen=True)
class MetadataRow:
    symbol: str
    metadata_exchange: str
    metadata_name: str
    sector: str
    industry: str
    country: str
    ipo_year: str
    market_cap: str
    last_sale: str
    volume: str
    url: str
    metadata_source: str


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def fetch_json(url: str) -> object:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 stock-screener-metadata-refresh/0.1"})
    try:
        with urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except HTTPError as e:
        body = e.read(500).decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {body}") from e
    except URLError as e:
        raise RuntimeError(f"Network error: {e.reason}") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON: {e}") from e


def fetch_text(url: str) -> str:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 stock-screener-metadata-refresh/0.1"})
    try:
        with urlopen(req, timeout=60) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        body = e.read(500).decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {body}") from e
    except URLError as e:
        raise RuntimeError(f"Network error: {e.reason}") from e


def github_repo_pushed_at(config: dict) -> str:
    try:
        data = fetch_json(config["github_repo_api_url"])
        return str(data.get("pushed_at") or "") if isinstance(data, dict) else ""
    except Exception:
        return ""


def source_age_hours(iso_ts: str) -> float | None:
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return (utc_now() - ts).total_seconds() / 3600
    except ValueError:
        return None


def github_raw_url(config: dict, exchange: str) -> str:
    return f"{config['github_base_url'].rstrip('/')}/{exchange}/{exchange}_full_tickers.json"


def nasdaq_api_url(config: dict, exchange: str) -> str:
    params = {
        "tableonly": "true",
        "limit": "25",
        "offset": "0",
        "exchange": exchange,
        "download": "true",
    }
    return f"{config['nasdaq_api_url']}?{urlencode(params)}"


def min_exchange_rows(config: dict, exchange: str) -> int:
    return int(config.get("minimum_exchange_rows", {}).get(exchange, 100))


def validate_exchange_rows(config: dict, exchange: str, rows: list[dict], source: str) -> None:
    minimum = min_exchange_rows(config, exchange)
    if len(rows) < minimum:
        raise RuntimeError(f"{source} returned too few {exchange} metadata rows: {len(rows)} < {minimum}")


def load_exchange_from_github(config: dict, exchange: str, raw_dir: Path) -> tuple[list[dict], str, str]:
    url = github_raw_url(config, exchange)
    text = fetch_text(url)
    rows = json.loads(text)
    if not isinstance(rows, list):
        raise RuntimeError(f"GitHub source did not return a list for {exchange}")
    validate_exchange_rows(config, exchange, rows, "github_raw")
    raw_path = raw_dir / f"{exchange}_github_full_tickers.json"
    raw_path.write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return rows, "github_raw", url


def load_exchange_from_nasdaq_api(config: dict, exchange: str, raw_dir: Path) -> tuple[list[dict], str, str]:
    url = nasdaq_api_url(config, exchange)
    payload = fetch_json(url)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Nasdaq API source did not return an object for {exchange}")
    rows = payload.get("data", {}).get("rows", [])
    if not isinstance(rows, list):
        raise RuntimeError(f"Nasdaq API source did not return rows list for {exchange}")
    validate_exchange_rows(config, exchange, rows, "nasdaq_api")
    raw_path = raw_dir / f"{exchange}_nasdaq_api_full_tickers.json"
    raw_path.write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return rows, "nasdaq_api", url


def load_exchange_from_cache(exchange: str, raw_dir: Path) -> tuple[list[dict], str, str]:
    candidates = [
        raw_dir / f"{exchange}_github_full_tickers.json",
        raw_dir / f"{exchange}_nasdaq_api_full_tickers.json",
    ]
    existing = [p for p in candidates if p.exists()]
    if not existing:
        raise RuntimeError(f"No cached raw file for {exchange}")
    path = max(existing, key=lambda p: p.stat().st_mtime)
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise RuntimeError(f"Cached source did not contain a list: {path}")
    return rows, "stale_cache", str(path)


def load_exchange(config: dict, exchange: str, raw_dir: Path) -> tuple[list[dict], dict]:
    warnings: list[str] = []
    attempts: list[dict[str, str]] = []

    for loader_name, loader in [
        ("github_raw", load_exchange_from_github),
        ("nasdaq_api", load_exchange_from_nasdaq_api),
    ]:
        try:
            rows, source, url = loader(config, exchange, raw_dir)
            return rows, {"exchange": exchange, "source": source, "url": url, "rows": len(rows), "warnings": warnings, "attempts": attempts}
        except Exception as e:
            attempts.append({"source": loader_name, "error": str(e)})
            warnings.append(f"{exchange} {loader_name} failed: {e}")

    if config.get("allow_stale_cache_on_failure", True):
        try:
            rows, source, url = load_exchange_from_cache(exchange, raw_dir)
            warnings.append(f"{exchange} using stale cached metadata: {url}")
            return rows, {"exchange": exchange, "source": source, "url": url, "rows": len(rows), "warnings": warnings, "attempts": attempts}
        except Exception as e:
            attempts.append({"source": "stale_cache", "error": str(e)})
            warnings.append(f"{exchange} stale_cache failed: {e}")

    raise RuntimeError(f"All metadata sources failed for {exchange}: {warnings}")


def clean_money(value: str) -> str:
    text = str(value or "").strip().replace("$", "").replace(",", "")
    return text


def normalize_row(raw: dict, exchange: str, source: str) -> MetadataRow | None:
    symbol = str(raw.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    return MetadataRow(
        symbol=symbol,
        metadata_exchange=exchange.upper(),
        metadata_name=str(raw.get("name") or "").strip(),
        sector=str(raw.get("sector") or "").strip() or "UNKNOWN",
        industry=str(raw.get("industry") or "").strip() or "UNKNOWN",
        country=str(raw.get("country") or "").strip() or "UNKNOWN",
        ipo_year=str(raw.get("ipoyear") or "").strip(),
        market_cap=clean_money(str(raw.get("marketCap") or "")),
        last_sale=clean_money(str(raw.get("lastsale") or "")),
        volume=clean_money(str(raw.get("volume") or "")),
        url=str(raw.get("url") or "").strip(),
        metadata_source=source,
    )


def write_csv(path: Path, rows: Iterable[object]) -> int:
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    if not rows:
        tmp.write_text("", encoding="utf-8")
        tmp.replace(path)
        return 0
    fieldnames = list(asdict(rows[0]).keys()) if hasattr(rows[0], "__dataclass_fields__") else list(rows[0].keys())
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row) if hasattr(row, "__dataclass_fields__") else row)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)
    return len(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def join_to_active_universe(active_universe_path: Path, metadata_rows: list[MetadataRow]) -> list[dict[str, str]]:
    active = read_csv(active_universe_path)
    metadata_by_key = {(row.metadata_exchange, row.symbol): row for row in metadata_rows}
    symbol_counts = Counter(row.symbol for row in metadata_rows)
    metadata_by_symbol = {row.symbol: row for row in metadata_rows if symbol_counts[row.symbol] == 1}

    joined: list[dict[str, str]] = []
    for row in active:
        exchange = row.get("exchange", "").upper()
        symbol = row.get("symbol", "").upper()
        meta = metadata_by_key.get((exchange, symbol)) or metadata_by_symbol.get(symbol)
        joined_row = dict(row)
        if meta:
            joined_row.update({
                "metadata_name": meta.metadata_name,
                "sector": meta.sector,
                "industry": meta.industry,
                "country": meta.country,
                "ipo_year": meta.ipo_year,
                "market_cap": meta.market_cap,
                "last_sale": meta.last_sale,
                "metadata_volume": meta.volume,
                "metadata_exchange": meta.metadata_exchange,
                "metadata_source": meta.metadata_source,
            })
        else:
            joined_row.update({
                "metadata_name": "",
                "sector": "UNKNOWN",
                "industry": "UNKNOWN",
                "country": "UNKNOWN",
                "ipo_year": "",
                "market_cap": "",
                "last_sale": "",
                "metadata_volume": "",
                "metadata_exchange": "",
                "metadata_source": "missing",
            })
        joined.append(joined_row)
    return joined


def summary_rows(rows: list[dict[str, str]], field: str) -> list[dict[str, str | int]]:
    counts = Counter((row.get(field) or "UNKNOWN") for row in rows)
    return [{field: key, "count": count} for key, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def main() -> int:
    config = load_config()
    raw_dir = ROOT / config["raw_dir"]
    raw_dir.mkdir(parents=True, exist_ok=True)

    pushed_at = github_repo_pushed_at(config)
    age_hours = source_age_hours(pushed_at)
    global_warnings: list[str] = []
    if age_hours is not None and age_hours > float(config.get("max_source_age_hours", 72)):
        global_warnings.append(f"GitHub source appears stale: pushed_at={pushed_at}, age_hours={age_hours:.1f}")

    all_rows: list[MetadataRow] = []
    source_reports: list[dict] = []
    for exchange in config["exchanges"]:
        raw_rows, report = load_exchange(config, exchange, raw_dir)
        source_reports.append(report)
        global_warnings.extend(report.get("warnings", []))
        for raw in raw_rows:
            row = normalize_row(raw, exchange, report["source"])
            if row:
                all_rows.append(row)

    all_rows.sort(key=lambda r: (r.metadata_exchange, r.symbol))
    if len(all_rows) < int(config.get("minimum_total_metadata_rows", 500)):
        raise RuntimeError(f"Refusing to promote sparse metadata refresh: metadata_rows={len(all_rows)}")
    joined = join_to_active_universe(ROOT / config["active_universe_path"], all_rows)
    joined_count = len(joined)
    missing_count = sum(1 for row in joined if row.get("metadata_source") == "missing")
    max_missing_fraction = float(config.get("max_joined_missing_metadata_fraction", 0.95))
    missing_fraction = (missing_count / joined_count) if joined_count else 1.0
    if joined_count == 0 or missing_fraction > max_missing_fraction:
        raise RuntimeError(
            f"Refusing to promote metadata refresh with missing metadata fraction {missing_fraction:.3f} > {max_missing_fraction:.3f}"
        )

    metadata_count = write_csv(ROOT / config["processed_metadata_path"], all_rows)
    joined_count = write_csv(ROOT / config["joined_universe_path"], joined)
    write_csv(ROOT / config["sector_summary_path"], summary_rows(joined, "sector"))
    write_csv(ROOT / config["industry_summary_path"], summary_rows(joined, "industry"))
    write_csv(ROOT / config["country_summary_path"], summary_rows(joined, "country"))

    metadata = {
        "refreshed_at_utc": utc_now().isoformat(),
        "github_repo_pushed_at": pushed_at,
        "github_repo_age_hours": age_hours,
        "warnings": global_warnings,
        "sources": source_reports,
        "metadata_rows": metadata_count,
        "joined_universe_rows": joined_count,
        "joined_missing_metadata_rows": missing_count,
        "outputs": {
            "processed_metadata_path": config["processed_metadata_path"],
            "joined_universe_path": config["joined_universe_path"],
            "sector_summary_path": config["sector_summary_path"],
            "industry_summary_path": config["industry_summary_path"],
            "country_summary_path": config["country_summary_path"],
            "metadata_path": config["metadata_path"],
        },
    }
    metadata_path = ROOT / config["metadata_path"]
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_metadata_path = metadata_path.with_name(f".{metadata_path.name}.tmp")
    tmp_metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_metadata_path.replace(metadata_path)
    print(json.dumps(metadata, indent=2, sort_keys=True))
    return 0 if missing_count < joined_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
