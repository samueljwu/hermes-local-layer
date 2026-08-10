#!/usr/bin/env python3
"""Refresh FMP company profiles for the active stock universe.

Reads the API key from FMP_API_KEY. The key is never written to disk.

Outputs:
- data/fmp/processed/company_profiles.csv
- data/fmp/processed/sector_summary.csv
- data/fmp/processed/industry_summary.csv
- data/fmp/processed/profile_metadata.json

Raw per-symbol FMP responses are cached under data/fmp/raw/company_profile/.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from stock_screener.symbols import normalize_symbol, safe_symbol_path
from stock_screener.owned_paths import resolve_owned_path
from stock_screener.atomic_io import atomic_write, atomic_write_text
from stock_screener.locking import run_locked

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "fmp_profile_refresh.json"


@dataclass(frozen=True)
class ProfileRow:
    symbol: str
    name: str
    exchange: str
    mic: str
    fmp_symbol: str
    company_name: str
    sector: str
    industry: str
    country: str
    exchange_short_name: str
    market_cap: str
    price: str
    beta: str
    volume: str
    avg_volume: str
    is_etf: str
    is_actively_trading: str
    error: str


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def read_universe(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def fmp_symbol(symbol: str) -> str:
    """Convert local ticker style to FMP URL ticker style if needed.

    FMP generally accepts normal tickers directly. Keep this as a single place
    to adjust later if preferred-share/class-share translation is needed.
    """
    return normalize_symbol(symbol)


def fetch_profile(base_url: str, symbol: str, api_key: str) -> list[dict]:
    base = base_url.rstrip("/")
    if base.endswith("/stable"):
        url = f"{base}/profile?{urlencode({'symbol': symbol, 'apikey': api_key})}"
    else:
        url = f"{base}/profile/{symbol}?{urlencode({'apikey': api_key})}"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 stock-screener-fmp-profile-refresh/0.1"})
    try:
        with urlopen(req, timeout=60) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
            data = json.loads(payload)
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and "Error Message" in data:
                raise RuntimeError(data["Error Message"])
            return []
    except HTTPError as e:
        body = e.read(500).decode("utf-8", errors="replace")
        if e.code == 429:
            raise RuntimeError(f"HTTP 429 rate/plan limit reached: {body}") from e
        raise RuntimeError(f"HTTP {e.code}: {e.reason}: {body}") from e
    except URLError as e:
        raise RuntimeError(f"Network error: {e.reason}") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON: {e}") from e


def load_or_fetch_profile(
    *,
    base_url: str,
    symbol: str,
    api_key: str,
    raw_dir: Path,
    force_refresh: bool,
    delay_seconds: float,
) -> tuple[list[dict], bool, str]:
    raw_path = safe_symbol_path(raw_dir, symbol, ".json")
    if raw_path.exists() and not force_refresh:
        try:
            cached = json.loads(raw_path.read_text(encoding="utf-8"))
            if isinstance(cached, list) and cached and isinstance(cached[0], dict):
                return cached, False, ""
        except (json.JSONDecodeError, OSError):
            pass

    try:
        data = fetch_profile(base_url, symbol, api_key)
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise RuntimeError("empty or malformed FMP profile response; preserved existing raw cache")
        raw_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(raw_path, json.dumps(data, indent=2, sort_keys=True) + "\n")
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        return data, True, ""
    except Exception as e:  # keep partial refresh usable
        return [], True, str(e)


def scalar(value) -> str:
    if value is None:
        return ""
    return str(value)


def build_profile_row(universe_row: dict[str, str], profile_payload: list[dict], error: str) -> ProfileRow:
    profile = profile_payload[0] if profile_payload else {}
    local_symbol = universe_row["symbol"]
    return ProfileRow(
        symbol=local_symbol,
        name=universe_row.get("name", ""),
        exchange=universe_row.get("exchange", ""),
        mic=universe_row.get("mic", ""),
        fmp_symbol=scalar(profile.get("symbol") or fmp_symbol(local_symbol)),
        company_name=scalar(profile.get("companyName")),
        sector=scalar(profile.get("sector")),
        industry=scalar(profile.get("industry")),
        country=scalar(profile.get("country")),
        exchange_short_name=scalar(profile.get("exchangeShortName") or profile.get("exchange")),
        market_cap=scalar(profile.get("mktCap") or profile.get("marketCap")),
        price=scalar(profile.get("price")),
        beta=scalar(profile.get("beta")),
        volume=scalar(profile.get("volume")),
        avg_volume=scalar(profile.get("volAvg") or profile.get("averageVolume")),
        is_etf=scalar(profile.get("isEtf")),
        is_actively_trading=scalar(profile.get("isActivelyTrading")),
        error=error,
    )


def write_csv(path: Path, rows: Iterable[object]) -> int:
    rows = list(rows)
    def write_temp(tmp_path: Path) -> None:
        if not rows:
            tmp_path.write_text("", encoding="utf-8")
            return
        fieldnames = list(asdict(rows[0]).keys()) if hasattr(rows[0], "__dataclass_fields__") else list(rows[0].keys())
        with tmp_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(asdict(row) if hasattr(row, "__dataclass_fields__") else row)
            f.flush()
            os.fsync(f.fileno())
    atomic_write(path, write_temp)
    return len(rows)


def summary_rows(rows: list[ProfileRow], field: str) -> list[dict[str, str | int]]:
    counter = Counter(getattr(row, field) or "UNKNOWN" for row in rows if not row.error)
    return [{field: key, "count": count} for key, count in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))]


def should_promote_processed_outputs(
    *,
    rows: list[ProfileRow],
    error_count: int,
    max_error_rate: float,
    existing_output_paths: Iterable[Path],
) -> tuple[bool, str]:
    """Decide whether a refresh attempt may replace processed profile outputs.

    Provider/API outages should not destructively replace a useful previous
    processed dataset with partial rows. If there is no previous processed
    dataset, promotion is allowed so first-run smoke tests can still emit
    inspectable outputs.
    """
    if not rows:
        return False, "no profile rows produced"
    existing_outputs = list(existing_output_paths)
    has_existing_outputs = all(path.exists() for path in existing_outputs)
    error_rate = error_count / len(rows)
    if error_rate <= max_error_rate:
        return True, f"error_rate {error_rate:.4f} <= max_error_rate {max_error_rate:.4f}"
    if not has_existing_outputs:
        return True, "no complete existing processed outputs to preserve"
    return False, f"error_rate {error_rate:.4f} exceeds max_error_rate {max_error_rate:.4f}; preserved existing processed outputs"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh FMP company profiles and summarize sectors/industries.")
    parser.add_argument("--max-symbols", type=int, default=None, help="Limit number of symbols for testing.")
    parser.add_argument("--force-refresh", action="store_true", help="Ignore cached raw profile files.")
    parser.add_argument("--delay", type=float, default=None, help="Delay between API calls in seconds.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config()
    api_key = os.environ.get("FMP_API_KEY", "").strip()
    if not api_key:
        print("Missing FMP_API_KEY. Run: export FMP_API_KEY='your_key_here'", flush=True)
        return 2

    universe_path = ROOT / config["universe_path"]
    raw_dir = resolve_owned_path(ROOT, config["raw_profile_dir"], label="raw_profile_dir")
    processed_profiles_path = resolve_owned_path(ROOT, config["processed_profiles_path"], label="processed_profiles_path")
    sector_summary_path = resolve_owned_path(ROOT, config["sector_summary_path"], label="sector_summary_path")
    industry_summary_path = resolve_owned_path(ROOT, config["industry_summary_path"], label="industry_summary_path")
    metadata_path = resolve_owned_path(ROOT, config["metadata_path"], label="metadata_path")

    universe = read_universe(universe_path)
    max_symbols = args.max_symbols if args.max_symbols is not None else config.get("max_symbols")
    if max_symbols:
        universe = universe[:max_symbols]

    force_refresh = args.force_refresh or bool(config.get("force_refresh"))
    delay_seconds = args.delay if args.delay is not None else float(config.get("request_delay_seconds", 0.25))

    rows: list[ProfileRow] = []
    fetched_count = 0
    cached_count = 0
    error_count = 0

    for idx, universe_row in enumerate(universe, start=1):
        try:
            symbol = fmp_symbol(universe_row["symbol"])
        except ValueError as e:
            error_count += 1
            rows.append(build_profile_row({**universe_row, "symbol": str(universe_row.get("symbol", ""))}, [], str(e)))
            continue
        data, fetched, error = load_or_fetch_profile(
            base_url=config["base_url"],
            symbol=symbol,
            api_key=api_key,
            raw_dir=raw_dir,
            force_refresh=force_refresh,
            delay_seconds=delay_seconds,
        )
        fetched_count += int(fetched)
        cached_count += int(not fetched)
        error_count += int(bool(error))
        rows.append(build_profile_row(universe_row, data, error))
        if idx % 100 == 0:
            print(f"processed {idx}/{len(universe)} symbols", flush=True)

    max_error_rate = float(config.get("max_error_rate", 0.0))
    output_paths = [processed_profiles_path, sector_summary_path, industry_summary_path]
    promote_outputs, promotion_reason = should_promote_processed_outputs(
        rows=rows,
        error_count=error_count,
        max_error_rate=max_error_rate,
        existing_output_paths=output_paths,
    )
    if promote_outputs:
        write_csv(processed_profiles_path, rows)
        write_csv(sector_summary_path, summary_rows(rows, "sector"))
        write_csv(industry_summary_path, summary_rows(rows, "industry"))
    else:
        print(f"Preserving existing processed FMP outputs: {promotion_reason}", flush=True)

    metadata = {
        "refreshed_at_utc": datetime.now(timezone.utc).isoformat(),
        "universe_path": config["universe_path"],
        "profile_rows": len(rows),
        "fetched_count": fetched_count,
        "cached_count": cached_count,
        "error_count": error_count,
        "force_refresh": force_refresh,
        "delay_seconds": delay_seconds,
        "max_symbols": max_symbols,
        "max_error_rate": max_error_rate,
        "processed_outputs_promoted": promote_outputs,
        "processed_output_promotion_reason": promotion_reason,
    }
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(metadata_path, json.dumps(metadata, indent=2, sort_keys=True) + "\n")

    print(json.dumps(metadata, indent=2, sort_keys=True))
    return 0 if error_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run_locked(main))
