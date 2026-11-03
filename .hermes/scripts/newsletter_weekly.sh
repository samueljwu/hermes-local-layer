#!/usr/bin/env bash
set -euo pipefail
exec /home/hermes/.hermes/hermes-agent/venv/bin/python /home/hermes/newsletter/newsletter.py run --kind weekly
