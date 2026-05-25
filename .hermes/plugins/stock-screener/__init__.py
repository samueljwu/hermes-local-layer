"""stock-screener plugin: read-only Discord commands for the screener.

Commands are intentionally read-only. Mutating workflow changes should happen
through explicit #stock-screener channel requests so updates are auditable.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path("/home/hermes/stock-screener")
LIST_PATH = ROOT / "config" / "long_biased_overrides.json"
FILTERS_PATH = ROOT / "config" / "screener_filters.json"
FILTERED_UNIVERSE_PATH = ROOT / "data" / "screener" / "filtered_universe.csv"
SECTOR_SUMMARY_PATH = ROOT / "data" / "metadata" / "processed" / "sector_summary.csv"
FILTER_SUMMARY_PATH = ROOT / "data" / "screener" / "filter_summary.json"


def _read_symbols() -> list[str]:
    try:
        payload = json.loads(LIST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return sorted({str(s).strip().upper() for s in payload.get("symbols", []) if str(s).strip()})


def _handle_stockbias(raw_args: str = "") -> str:
    args = (raw_args or "").strip().lower()
    if args in {"", "list", "show"}:
        symbols = _read_symbols()
        if not symbols:
            return f"Long-biased override list is empty. Source: `{LIST_PATH}`"
        return (
            "Long-biased override symbols:\n"
            + "\n".join(f"- {s}" for s in symbols)
            + "\n\nOnly these symbols receive special long-biased chart treatment. "
              "Updates must be requested manually in #stock-screener; this slash command is read-only."
        )
    if args.startswith(("add", "remove", "delete", "set", "clear", "update")):
        return (
            "`/stockbias` is read-only. To update the long-biased override list, "
            "send an explicit manual request in #stock-screener. The agent will edit "
            f"`{LIST_PATH}` and rerun validation."
        )
    return "Usage: `/stockbias` or `/stockbias list`"


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _sector_counts_from_csv(path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                sector = (row.get("sector") or "UNKNOWN").strip() or "UNKNOWN"
                counts[sector] = counts.get(sector, 0) + 1
    except OSError:
        return {}
    return counts


def _sector_summary_counts(path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                sector = (row.get("sector") or "UNKNOWN").strip() or "UNKNOWN"
                try:
                    counts[sector] = int(float(row.get("count") or 0))
                except ValueError:
                    counts[sector] = 0
    except OSError:
        return {}
    return counts


def _fmt_counts(items: list[tuple[str, int]]) -> str:
    return "\n".join(f"- {name}: {count}" for name, count in items) if items else "- none"


def _handle_stocksectors(raw_args: str = "") -> str:
    args = (raw_args or "").strip().lower()
    if args not in {"", "list", "show", "sectors"}:
        return "Usage: `/stocksectors` or `/stocksectors list`"

    filters = _read_json(FILTERS_PATH)
    summary = _read_json(FILTER_SUMMARY_PATH)
    available_counts = _sector_summary_counts(SECTOR_SUMMARY_PATH)
    included_counts = _sector_counts_from_csv(FILTERED_UNIVERSE_PATH)

    include_sectors = [str(s).strip() for s in filters.get("include_sectors", []) if str(s).strip()]
    excluded_config = [str(s).strip() for s in filters.get("exclude_sectors", []) if str(s).strip()]
    exclude_unknown = bool(filters.get("exclude_unknown_sector"))

    included_items = sorted(included_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    excluded_items: list[tuple[str, int]] = []
    for sector in excluded_config:
        excluded_items.append((sector, available_counts.get(sector, 0)))
    if exclude_unknown:
        excluded_items.append(("UNKNOWN / missing sector", available_counts.get("UNKNOWN", 0)))

    if include_sectors:
        mode = "allowlist via include_sectors"
    else:
        mode = "all sectors except configured exclusions"

    return (
        "Stock screener sector filter\n"
        f"Mode: {mode}\n"
        f"Filtered universe rows: {summary.get('output_rows', 'unknown')}\n\n"
        "Included sectors in current filtered universe:\n"
        f"{_fmt_counts(included_items)}\n\n"
        "Excluded sectors / sector-like exclusions:\n"
        f"{_fmt_counts(excluded_items)}\n\n"
        f"Source: `{FILTERS_PATH}`"
    )


def register(ctx) -> None:
    ctx.register_command(
        "stockbias",
        handler=_handle_stockbias,
        description="Show the stock-screener long-biased override list.",
    )
    ctx.register_command(
        "stocksectors",
        handler=_handle_stocksectors,
        description="Show included and excluded stock-screener sectors.",
    )
