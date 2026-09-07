#!/usr/bin/env python3
"""Bounded no-agent Notion reconciliation; quiet success, fixed safe alerts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys

SYNC = Path('/home/hermes/tasks/_tools/notion_sync.py')
COMMAND = [sys.executable, '-B', str(SYNC), 'sync', '--apply', '--max-actions', '10']


def run_connector(command, *, timeout):
    """Isolate inherited derivative processes; kill the whole group on timeout.

    Reserve bounded cleanup time below the scheduler's outer timeout. Reap the
    direct child; never drain pipes indefinitely after a failed run.
    """
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, start_new_session=True)
    try:
        stdout, _ = proc.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            # A descendant that deliberately detached is outside the group;
            # don't let its inherited stdout defeat the outer wall limit.
            if proc.stdout is not None:
                proc.stdout.close()
            proc.wait(timeout=1)
            raise RuntimeError('connector cleanup deadline exceeded') from None
        raise
    return subprocess.CompletedProcess(command, proc.returncode, stdout, '')


def main() -> int:
    try:
        result = run_connector(COMMAND, timeout=110)
    except subprocess.TimeoutExpired:
        print('ALERT: Hermes Tasks Notion sync timed out. Pending operations will be reconciled on the next run; do not reset sync state.')
        return 0
    except Exception:
        print('ALERT: Hermes Tasks Notion sync failed to run or clean up. Inspect the local connector; credentials and remote text are not included in this alert.')
        return 0
    if result.returncode == 75:
        try:
            busy_report = json.loads(result.stdout)
            if isinstance(busy_report, dict) and set(busy_report) == {'busy'} and busy_report['busy'] is True:
                return 0  # Another sync owns reconciliation; normal overlap.
        except (ValueError, TypeError):
            pass
    if result.returncode != 0:
        print('ALERT: Hermes Tasks Notion sync needs review. The local task registry remains canonical. Run python3 /home/hermes/tasks/_tools/notion_sync.py plan to inspect conflicts or failures. Do not delete sync state or force an overwrite.')
        return 0
    try:
        report = json.loads(result.stdout)
        if (not isinstance(report, dict) or not isinstance(report.get('conflicts'), dict)
                or any(type(report.get(k)) is not int or report[k] < 0
                       for k in ('planned', 'applied', 'pending'))):
            raise ValueError('invalid report')
        if report['conflicts']:
            print('ALERT: Hermes Tasks Notion sync reports conflicts. The local registry remains canonical; inspect the connector plan before resolving either version.')
    except (ValueError, TypeError):
        print('ALERT: Hermes Tasks Notion sync returned an invalid verification report. Inspect the local connector before assuming synchronization succeeded.')
    # Return success even on a domain failure: deliver the fixed alert without
    # launching the scheduler\'s autonomous code-repair path on external data.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
