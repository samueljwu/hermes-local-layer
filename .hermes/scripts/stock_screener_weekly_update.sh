#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/hermes/stock-screener"
LOG_DIR="$ROOT/out/cron_logs"
mkdir -p "$LOG_DIR"
LOG_PATH="$LOG_DIR/weekly_update_$(date -u +%Y%m%dT%H%M%SZ).log"
LOCK_PATH="/tmp/stock_screener_weekly_update.lock"

set +e
HERMES_STOCK_SCREENER_LOCK_HELD=1 flock -n "$LOCK_PATH" "$ROOT/scripts/weekly_update.sh" >"$LOG_PATH" 2>&1
status=$?
set -e

if [[ "$status" -ne 0 ]]; then
  echo "Stock screener weekly update failed."
  echo "Exit code: $status"
  echo "Log: $LOG_PATH"
  echo "Last log lines:"
  tail -80 "$LOG_PATH" || true
  exit "$status"
fi

python3 - "$LOG_PATH" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

root = Path('/home/hermes/stock-screener')
log_path = Path(sys.argv[1])

def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}

scan = load_json(root / 'data/patterns/latest_summary.json')
chart = load_json(root / 'site/dist/summary.json')
excluded = load_json(root / 'site/dist/excluded_summary.json')
refresh = load_json(root / 'data/prices/yahoo_weekly_metadata.json')
validation = load_json(root / 'data/prices/yahoo_weekly_validation.json')

patterns = scan.get('pattern_counts') or {}
pattern_text = ', '.join(f'{k}={v}' for k, v in sorted(patterns.items())) or 'none'
refresh_bits = []
for key in ['fetched', 'fetched_short', 'fetched_short_preserved_cache', 'skipped_fresh', 'failed']:
    if key in refresh:
        refresh_bits.append(f'{key}={refresh[key]}')
if refresh.get('stopped_early_reason'):
    refresh_bits.append(f"stopped={refresh['stopped_early_reason']}")
refresh_text = ', '.join(refresh_bits) or 'metadata unavailable'

print('Stock screener weekly update succeeded.')
print(f"Candidates: {scan.get('symbols_with_matches', chart.get('rendered_charts', '?'))} symbols / {scan.get('total_matches', '?')} matches.")
print(f'Patterns: {pattern_text}.')
print(f"Excluded review: {excluded.get('rendered_charts', '?')} sampled from {excluded.get('eligible_excluded_symbols', '?')} eligible non-candidates.")
print(f"Price refresh: {refresh_text}.")
print(f"Cache validation: {validation.get('covered_symbols', '?')}/{validation.get('expected_symbols', '?')} symbols covered; invalid={validation.get('invalid_count', '?')}; missing={validation.get('missing_count', '?')}.")
print('Pages: <https://hermes.tail5857b7.ts.net/stocks/> and <https://hermes.tail5857b7.ts.net/stocks/excluded.html>')
print(f'Full log: {log_path}')
PY
