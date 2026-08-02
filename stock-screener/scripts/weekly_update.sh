#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/hermes/stock-screener"
cd "$ROOT"

LOCK_PATH="/tmp/stock_screener_weekly_update.lock"
if [[ "${HERMES_STOCK_SCREENER_LOCK_HELD:-0}" != "1" ]]; then
  export HERMES_STOCK_SCREENER_LOCK_HELD=1
  exec flock -n "$LOCK_PATH" "$0" "$@"
fi

export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

started_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "Stock screener weekly update started: ${started_utc}"

# Stock-screener workflows may inspect the wiki and run deploy checks, but must
# never mutate /home/hermes/wiki, its harness, or generated artifacts. Capture
# the full git-visible wiki state before any allowed read-only wiki access and
# verify it is unchanged afterwards. This still works when the wiki already has
# unrelated dirty files: the before/after snapshots must match byte-for-byte.
wiki_state_before="$(mktemp)"
wiki_state_after="$(mktemp)"
cleanup_wiki_state() {
  rm -f "$wiki_state_before" "$wiki_state_after"
}
trap cleanup_wiki_state EXIT
# Snapshot protected wiki content (including ignored generated artifacts) before
# the permitted deploy check. The manifest excludes node_modules but covers the
# canonical source, generated site, harnesses, and build configuration.
python3 scripts/wiki_readonly_manifest.py >"$wiki_state_before"

set +e
python3 scripts/refresh_price_history.py
refresh_status=$?
set -e
PYTHONPATH=src python3 -m unittest discover -s tests -v
if [[ "$refresh_status" -ne 0 ]]; then
  echo "Price refresh reported failures; validating existing cache before continuing"
fi
python3 scripts/validate_price_history.py
python3 scripts/scan_patterns.py
python3 scripts/build_chart_page.py
python3 scripts/build_excluded_chart_page.py
python3 - <<'PY'
import csv, json, re
from pathlib import Path
root=Path('/home/hermes/stock-screener')
scan=json.loads((root/'data/patterns/latest_summary.json').read_text())
chart=json.loads((root/'site/dist/summary.json').read_text())
excluded=json.loads((root/'site/dist/excluded_summary.json').read_text())
rows=list(csv.DictReader((root/'data/patterns/all_candidate_charts.csv').open(newline='', encoding='utf-8')))
ex_rows=list(csv.DictReader((root/'data/patterns/excluded_random_50.csv').open(newline='', encoding='utf-8')))
html=(root/'site/dist/index.html').read_text(encoding='utf-8')
ex_html=(root/'site/dist/excluded.html').read_text(encoding='utf-8')
qualities=[float(r['quality']) for r in rows]
ex_scores=[float(r['exclusion_quality']) for r in ex_rows]
assert len(rows)==len({r['symbol'] for r in rows})==chart['rendered_charts']
assert qualities==sorted(qualities, reverse=True), 'candidate page shortlist is not quality-desc sorted'
assert len(ex_rows)==len({r['symbol'] for r in ex_rows})==excluded['rendered_charts']==50
assert ex_scores==sorted(ex_scores), 'excluded page sample is not exclusion-quality-asc sorted'
assert len(re.findall(r'<section class="card">', html))==chart['rendered_charts']
assert len(re.findall(r'<section class="card">', ex_html))==50
print('Stock screener weekly update complete')
print(f"Candidates: {scan['symbols_with_matches']} symbols / {scan['total_matches']} matches")
print('Pattern counts: ' + ', '.join(f"{k}={v}" for k,v in sorted(scan['pattern_counts'].items())))
print(f"Excluded review: 50 of {excluded['eligible_excluded_symbols']} eligible excluded symbols; seed {excluded['sample_seed']}")
print('Candidate page: https://hermes.tail5857b7.ts.net/stocks/')
print('Excluded review: https://hermes.tail5857b7.ts.net/stocks/excluded.html')
PY

cd /home/hermes/wiki
/home/hermes/wiki/_tools/wiki_ops.py deploy-check >/tmp/stock_screener_wiki_deploy_check.json
python3 /home/hermes/stock-screener/scripts/wiki_readonly_manifest.py >"$wiki_state_after"
if ! cmp -s "$wiki_state_before" "$wiki_state_after"; then
  echo "ERROR: stock screener workflow changed protected wiki content; wiki access must remain read-only" >&2
  diff -u "$wiki_state_before" "$wiki_state_after" >&2 || true
  exit 1
fi

echo "Wiki/stocks Tailscale route deploy-check passed; wiki read-only guard passed"
echo "Stock screener weekly update finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
