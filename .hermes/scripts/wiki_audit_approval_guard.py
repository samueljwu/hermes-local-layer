#!/usr/bin/env python3
"""Restore default-profile headless approval after the wiki audit or hard deadline.

This job never grants approval. It is quiet until it restores the prior 'deny'.
"""
import json
import subprocess
from datetime import datetime, timezone

BOARD = 'wiki-factual-audit-20261011'
FINAL_CARD = 't_8ec4cbe2'
DEADLINE = datetime.fromisoformat('2026-11-11T03:20:57+00:00')


def run(*args):
    result = subprocess.run(['hermes', *args], capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise RuntimeError('hermes command failed: ' + ' '.join(args[:3]) + ': ' + result.stderr[-300:])
    return result.stdout.strip()


def main():
    mode = run('config', 'get', 'approvals.single_query_mode')
    if mode == 'deny':
        return
    if mode != 'approve':
        raise RuntimeError('Unexpected single-query approval mode; refusing to modify it')
    expired = datetime.now(timezone.utc) >= DEADLINE
    finished = False
    if not expired:
        try:
            rows = json.loads(run('kanban', '--board', BOARD, 'list', '--json'))
            final = [row for row in rows if row['id'] == FINAL_CARD]
            finished = len(final) == 1 and final[0]['status'] == 'done'
        except (ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired):
            return  # No guessed completion; the fixed deadline is the fallback.
    if expired or finished:
        run('config', 'set', 'approvals.single_query_mode', 'deny')
        if run('config', 'get', 'approvals.single_query_mode') != 'deny':
            raise RuntimeError('Approval policy readback did not return deny')
        print('Wiki-audit temporary headless script approval restored to deny; reason: ' + ('12-hour safety deadline' if expired else 'final audit card completed'))


if __name__ == '__main__':
    main()
