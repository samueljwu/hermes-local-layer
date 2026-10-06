#!/usr/bin/env bash
set -euo pipefail

# No-agent cron entrypoint: the feed harness owns fetching, protected-root
# snapshots, selection, state writes, page rendering, and final Discord text.
exec /home/hermes/feed/_tools/feed_ops.py digest
