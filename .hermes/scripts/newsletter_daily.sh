#!/usr/bin/env bash
set -euo pipefail
# Script-only cron; publishing and Discord delivery are owned by newsletter.py.
exec /home/hermes/.hermes/hermes-agent/venv/bin/python /home/hermes/newsletter/newsletter.py run --kind daily
