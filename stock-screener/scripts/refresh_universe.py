#!/usr/bin/env python3
"""Refresh the stock universe from official NASDAQ and NYSE web sources.

This script intentionally uses only Python standard-library modules so universe
maintenance has no dependency setup step.
"""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "universe_sources.json"
RAW_DIR = ROOT / "data" / "universe" / "raw"
PROCESSED_DIR = ROOT / "data" / "universe" / "processed"


@dataclass(frozen=True)
class UniverseRow:
    symbol: str
    name: str
    exchange: str
    mic: str
    source: str
    source_symbol: str
    is_etf: str
    test_issue: str
    raw_url: str


@dataclass(frozen=True)
class ExcludedRow:
    symbol: str
    name: str
    exchange: str
    source: str
    reason: str


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def fetch_url(url: str, *, data: bytes | None = None, content_type: str | None = None) -> bytes:
    headers = {"User-Agent": "Mozilla/5.0 stock-screener-universe-refresh/0.1"}
    if content_type:
        headers["Content-Type"] = content_type
    req = Request(url, data=data, headers=headers)
    try:
        with urlopen(req, timeout=60) as resp:
            return resp.read()
    except HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} fetching {url}: {e.reason}") from e
    except URLError as e:
        raise RuntimeError(f"Network error fetching {url}: {e.reason}") from e


def download_sources(config: dict) -> dict[str, Path]:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    sources = config["sources"]

    nasdaq_path = ROOT / sources["nasdaq"]["raw_path"]
    nasdaq_path.write_bytes(fetch_url(sources["nasdaq"]["url"]))

    nyse_payload = {
        "instrumentType": "EQUITY",
        "pageNumber": 1,
        "sortColumn": "NORMALIZED_TICKER",
        "sortOrder": "ASC",
        "maxResultsPerPage": 10000,
        "filterToken": "",
    }
    nyse_path = ROOT / sources["nyse"]["raw_path"]
    nyse_path.write_bytes(
        fetch_url(
            sources["nyse"]["url"],
            data=json.dumps(nyse_payload).encode("utf-8"),
            content_type="application/json",
        )
    )

    return {"nasdaq": nasdaq_path, "nyse": nyse_path}


def read_pipe_file(path: Path) -> Iterable[dict[str, str]]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    data_lines = [line for line in lines if line and not line.startswith("File Creation Time:")]
    yield from csv.DictReader(data_lines, delimiter="|")


def normalize_nasdaq(path: Path, source_url: str) -> list[UniverseRow]:
    rows: list[UniverseRow] = []
    for row in read_pipe_file(path):
        symbol = (row.get("Symbol") or "").strip()
        if not symbol:
            continue
        rows.append(
            UniverseRow(
                symbol=symbol,
                name=(row.get("Security Name") or "").strip(),
                exchange="NASDAQ",
                mic="XNAS",
                source="nasdaqtrader_nasdaqlisted",
                source_symbol=symbol,
                is_etf=(row.get("ETF") or "").strip(),
                test_issue=(row.get("Test Issue") or "").strip(),
                raw_url=source_url,
            )
        )
    return rows


def nyse_mic(row: dict) -> str:
    """Extract the market identifier from a NYSE quote URL.

    The NYSE quote filter endpoint returns multiple US markets. The URL embeds
    the actual MIC, e.g. https://www.nyse.com/quote/XNYS:IBM or XNGS:AAPL.
    """
    match = re.search(r"/quote/([^:]+):", row.get("url", ""))
    return match.group(1) if match else ""


def normalize_nyse(path: Path, source_url: str) -> list[UniverseRow]:
    payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    rows: list[UniverseRow] = []
    for row in payload:
        mic = nyse_mic(row)
        if mic != "XNYS":
            continue
        symbol = (row.get("normalizedTicker") or row.get("symbolExchangeTicker") or "").strip()
        name = (row.get("instrumentName") or "").strip()
        if not symbol:
            continue
        rows.append(
            UniverseRow(
                symbol=symbol,
                name=name,
                exchange="NYSE",
                mic=mic,
                source="nyse_quotes_filter",
                source_symbol=(row.get("symbolExchangeTicker") or symbol).strip(),
                is_etf="",
                test_issue="",
                raw_url=source_url,
            )
        )
    return rows


def exclusion_reason(row: UniverseRow, config: dict) -> str | None:
    filters = config["filters"]
    upper_name = f" {row.name.upper()} "
    upper_symbol = row.symbol.upper()

    if filters.get("exclude_etfs") and row.is_etf.upper() == "Y":
        return "ETF flag"

    if filters.get("exclude_test_issues") and row.test_issue.upper() == "Y":
        return "test issue flag"

    for needle in filters.get("exclude_symbol_contains", []):
        if needle.upper() in upper_symbol:
            return f"symbol contains {needle}"

    for needle in filters.get("exclude_name_contains", []):
        if needle.upper() in upper_name:
            return f"name contains {needle.strip()}"

    return None


def split_active_excluded(rows: Iterable[UniverseRow], config: dict) -> tuple[list[UniverseRow], list[ExcludedRow]]:
    active: list[UniverseRow] = []
    excluded: list[ExcludedRow] = []
    seen: set[tuple[str, str]] = set()

    for row in rows:
        key = (row.exchange, row.symbol)
        if key in seen:
            continue
        seen.add(key)
        reason = exclusion_reason(row, config)
        if reason:
            excluded.append(
                ExcludedRow(
                    symbol=row.symbol,
                    name=row.name,
                    exchange=row.exchange,
                    source=row.source,
                    reason=reason,
                )
            )
        else:
            active.append(row)

    active.sort(key=lambda r: (r.exchange, r.symbol))
    excluded.sort(key=lambda r: (r.exchange, r.symbol))
    return active, excluded


def write_csv(path: Path, rows: Iterable[object]) -> int:
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return 0
    fieldnames = list(asdict(rows[0]).keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
    return len(rows)


def main() -> int:
    config = load_config()
    paths = download_sources(config)

    payload = json.loads(paths["nyse"].read_text(encoding="utf-8", errors="replace"))

    nasdaq_rows = normalize_nasdaq(paths["nasdaq"], config["sources"]["nasdaq"]["url"])
    nyse_rows = normalize_nyse(paths["nyse"], config["sources"]["nyse"]["url"])

    active, excluded = split_active_excluded([*nasdaq_rows, *nyse_rows], config)
    active_nasdaq = [row for row in active if row.exchange == "NASDAQ"]
    active_nyse = [row for row in active if row.exchange == "NYSE"]

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    counts = {
        "nasdaq_raw": len(nasdaq_rows),
        "nyse_raw_all_markets": len(payload),
        "nyse_raw_xnys": len(nyse_rows),
        "nasdaq_active": write_csv(PROCESSED_DIR / "nasdaq_universe.csv", active_nasdaq),
        "nyse_active": write_csv(PROCESSED_DIR / "nyse_universe.csv", active_nyse),
        "combined_active": write_csv(PROCESSED_DIR / "active_universe.csv", active),
        "excluded": write_csv(PROCESSED_DIR / "excluded_universe.csv", excluded),
    }

    metadata = {
        "refreshed_at_utc": datetime.now(timezone.utc).isoformat(),
        "sources": config["sources"],
        "filters": config["filters"],
        "counts": counts,
    }
    (PROCESSED_DIR / "universe_metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(counts, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
