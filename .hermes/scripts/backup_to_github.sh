#!/usr/bin/env bash
set -euo pipefail

cd /home/hermes

HARNESS=/home/hermes/.hermes/scripts/backup_security_harness.py
DOC_GUARD=/home/hermes/.hermes/scripts/backup_documentation_guard.py
LOCK=/tmp/hermes-knowledge-backup.lock
MODE=manual

usage() {
  cat <<'EOF'
Usage: backup_to_github.sh [--mode manual|scheduled]

Commit durable /home/hermes knowledge state to GitHub after documentation and
security checks. Manual backups commit as "Manual backup <UTC timestamp>";
scheduled backups commit as "Scheduled backup <UTC timestamp>".
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      MODE="${2:-}"
      shift 2
      ;;
    --manual)
      MODE=manual
      shift
      ;;
    --scheduled)
      MODE=scheduled
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$MODE" in
  manual|scheduled) ;;
  *)
    echo "Invalid backup mode: $MODE (expected manual or scheduled)" >&2
    exit 2
    ;;
esac

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "Another Hermes knowledge backup is already running; exiting."
  exit 0
fi

if [[ ! -x "$HARNESS" ]]; then
  echo "Security harness is missing or not executable: $HARNESS" >&2
  exit 1
fi
if [[ ! -x "$DOC_GUARD" ]]; then
  echo "Documentation guard is missing or not executable: $DOC_GUARD" >&2
  exit 1
fi

# Check already-tracked content and remote config before touching the index.
"$HARNESS" --tracked --quiet

git add .

# Require docs to move with non-trivial harness/system changes.
"$DOC_GUARD" --staged --quiet

# Check exactly what is about to be committed, plus all tracked content.
"$HARNESS" --all --quiet

if git diff --cached --quiet; then
  echo "No backup changes to commit."
  exit 0
fi

prefix="Manual backup"
if [[ "$MODE" == "scheduled" ]]; then
  prefix="Scheduled backup"
fi
git commit -m "$prefix $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Re-run after commit to catch any accidental tracked-state problem before push.
"$HARNESS" --tracked --quiet

git push
