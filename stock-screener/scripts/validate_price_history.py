#!/usr/bin/env python3
"""Validate local normalized OHLCV price-history cache."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_screener.price_validation import validate_price_cache  # noqa: E402

CONFIG_PATH = ROOT / "config" / "price_history.json"
DEFAULT_OUTPUT = ROOT / "data" / "prices" / "yahoo_weekly_validation.json"


def read_symbols(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8") as f:
        return [row["symbol"].strip().upper() for row in csv.DictReader(f) if row.get("symbol")]


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate price history cache")
    parser.add_argument("--min-rows", type=int, default=None, help="Minimum rows to consider history full length")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Validation JSON output path")
    args = parser.parse_args()

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    min_rows = int(args.min_rows if args.min_rows is not None else config["min_expected_rows"])
    symbols = read_symbols(ROOT / config["input_path"])
    summary = validate_price_cache(symbols, ROOT / config["output_dir"], min_rows=min_rows)
    summary.update({
        "validated_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_path": config["input_path"],
        "price_dir": config["output_dir"],
        "min_expected_rows": min_rows,
    })
    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if summary["missing_count"] or summary["invalid_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
