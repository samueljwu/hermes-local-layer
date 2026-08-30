#!/usr/bin/env python3
"""Apply adjustable local filters to the metadata-enriched universe."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from stock_screener.owned_paths import resolve_owned_path
from stock_screener.atomic_io import atomic_write, atomic_write_text
from stock_screener.locking import run_locked

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "screener_filters.json"


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, str]]) -> int:
    def write_temp(temp: Path) -> None:
        if not rows:
            temp.write_text("", encoding="utf-8")
            return
        with temp.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    atomic_write(path, write_temp)
    return len(rows)


def parse_float(value: str) -> float | None:
    text = str(value or "").strip().replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def norm_list(values: list[str]) -> set[str]:
    return {str(value).strip().upper() for value in values if str(value).strip()}


def reject(row: dict[str, str], config: dict[str, Any]) -> str | None:
    symbol = row.get("symbol", "").strip().upper()
    exchange = row.get("exchange", "").strip().upper()
    sector = row.get("sector", "UNKNOWN").strip()
    industry = row.get("industry", "UNKNOWN").strip()
    country = row.get("country", "UNKNOWN").strip()

    include_symbols = norm_list(config.get("include_symbols", []))
    if include_symbols and symbol not in include_symbols:
        return "not in include_symbols"

    exclude_symbols = norm_list(config.get("exclude_symbols", []))
    if symbol in exclude_symbols:
        return "in exclude_symbols"

    include_exchanges = norm_list(config.get("include_exchanges", []))
    if include_exchanges and exchange not in include_exchanges:
        return "exchange not included"

    if config.get("exclude_missing_metadata") and row.get("metadata_source") == "missing":
        return "missing metadata"

    if config.get("exclude_unknown_sector") and sector.upper() == "UNKNOWN":
        return "unknown sector"
    if config.get("exclude_unknown_industry") and industry.upper() == "UNKNOWN":
        return "unknown industry"
    if config.get("exclude_unknown_country") and country.upper() == "UNKNOWN":
        return "unknown country"

    include_sectors = norm_list(config.get("include_sectors", []))
    exclude_sectors = norm_list(config.get("exclude_sectors", []))
    if include_sectors and sector.upper() not in include_sectors:
        return "sector not included"
    if sector.upper() in exclude_sectors:
        return "sector excluded"

    include_industries = norm_list(config.get("include_industries", []))
    exclude_industries = norm_list(config.get("exclude_industries", []))
    if include_industries and industry.upper() not in include_industries:
        return "industry not included"
    if industry.upper() in exclude_industries:
        return "industry excluded"

    include_countries = norm_list(config.get("include_countries", []))
    exclude_countries = norm_list(config.get("exclude_countries", []))
    if include_countries and country.upper() not in include_countries:
        return "country not included"
    if country.upper() in exclude_countries:
        return "country excluded"

    numeric_checks = [
        ("market_cap", "min_market_cap", "max_market_cap"),
        ("last_sale", "min_price", "max_price"),
        ("metadata_volume", "min_volume", "max_volume"),
    ]
    for field, min_key, max_key in numeric_checks:
        value = parse_float(row.get(field, ""))
        min_value = config.get(min_key)
        max_value = config.get(max_key)
        if min_value is not None and (value is None or value < float(min_value)):
            return f"{field} below minimum"
        if max_value is not None and (value is None or value > float(max_value)):
            return f"{field} above maximum"

    return None


def main() -> int:
    config = load_config()
    rows = read_csv(ROOT / config["input_path"])
    kept: list[dict[str, str]] = []
    rejected = Counter()

    for row in rows:
        reason = reject(row, config)
        if reason:
            rejected[reason] += 1
        else:
            kept.append(row)

    output_count = write_csv(resolve_owned_path(ROOT, config["output_path"], label="output_path"), kept)
    summary = {
        "input_rows": len(rows),
        "output_rows": output_count,
        "rejected_rows": len(rows) - output_count,
        "rejection_reasons": dict(sorted(rejected.items())),
        "output_path": config["output_path"],
        "config_path": str(CONFIG_PATH.relative_to(ROOT)),
    }
    summary_path = resolve_owned_path(ROOT, config["summary_path"], label="summary_path")
    atomic_write_text(summary_path, json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_locked(main))
