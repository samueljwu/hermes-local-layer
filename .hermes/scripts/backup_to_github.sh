#!/usr/bin/env bash
set -euo pipefail

REPO=${HERMES_BACKUP_REPO:-/home/hermes}
cd "$REPO"

HARNESS=${HERMES_BACKUP_HARNESS:-/home/hermes/.hermes/scripts/backup_security_harness.py}
DOC_GUARD=${HERMES_BACKUP_DOC_GUARD:-/home/hermes/.hermes/scripts/backup_documentation_guard.py}
LOCK=${HERMES_BACKUP_LOCK:-/home/hermes/.hermes/state/locks/knowledge-backup.lock}
LOCK_HELPER=/home/hermes/.hermes/scripts/backup_lock_exec.py
MODE=manual
ORIGINAL_ARGS=("$@")

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

# The helper opens each lock-directory component and the lock itself with
# O_NOFOLLOW, verifies the opened object by fd, takes flock, then execs this
# script with that locked fd inherited. A pathname pre-check here would leave a
# symlink-swap race between the check and shell redirection.
if [[ -z "${HERMES_BACKUP_LOCK_FD:-}" ]]; then
  exec "$LOCK_HELPER" "$LOCK" -- "${BASH_SOURCE[0]}" "${ORIGINAL_ARGS[@]}"
fi
if ! "$LOCK_HELPER" --validate "$LOCK" "$HERMES_BACKUP_LOCK_FD"; then
  echo "Refusing to continue without a verified inherited backup lock." >&2
  exit 1
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

# The backup stages into the shared repository index. Preserve its exact prior
# bytes until every pre-commit guard and the commit itself succeeds, so a failed
# guard cannot destroy unrelated work that an operator had already staged.
GIT_DIR=$(git rev-parse --absolute-git-dir)
INDEX_PATH=$(git rev-parse --git-path index)
if [[ "$INDEX_PATH" != /* ]]; then
  INDEX_PATH="$REPO/$INDEX_PATH"
fi
INDEX_BACKUP=$(mktemp "$GIT_DIR/index.backup.XXXXXX")
INDEX_EXISTED=0
INDEX_ACCEPTED=0
if [[ -e "$INDEX_PATH" ]]; then
  cp -- "$INDEX_PATH" "$INDEX_BACKUP"
  INDEX_EXISTED=1
fi
restore_or_discard_index_snapshot() {
  status=$?
  trap - EXIT HUP INT TERM
  if [[ "$INDEX_ACCEPTED" -eq 0 ]]; then
    if [[ "$INDEX_EXISTED" -eq 1 ]]; then
      mv -f -- "$INDEX_BACKUP" "$INDEX_PATH"
    else
      rm -f -- "$INDEX_PATH" "$INDEX_BACKUP"
    fi
  else
    rm -f -- "$INDEX_BACKUP"
  fi
  exit "$status"
}
trap restore_or_discard_index_snapshot EXIT HUP INT TERM

git add .

# Require docs to move with non-trivial harness/system changes.
"$DOC_GUARD" --staged --quiet

# Check exactly what is about to be committed, plus all tracked content.
"$HARNESS" --all --quiet

if git diff --cached --quiet; then
  echo "No backup changes to commit; checking for an unpushed commit."
else
  prefix="Manual backup"
  if [[ "$MODE" == "scheduled" ]]; then
    prefix="Scheduled backup"
  fi
  git commit -m "$prefix $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  INDEX_ACCEPTED=1
fi

# Re-run even when there was nothing new to commit: HEAD may still be ahead
# because an earlier push failed after a successful commit.
"$HARNESS" --tracked --quiet

# At this point either the commit succeeded or the fully staged no-change index
# passed every guard. A later push failure must not roll a committed index back.
INDEX_ACCEPTED=1
git push
