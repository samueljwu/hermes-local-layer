#!/usr/bin/env python3
"""Bounded no-agent Notion reconciliation; quiet success, fixed safe alerts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

SYNC = Path('/home/hermes/tasks/_tools/notion_sync.py')
COMMAND = [sys.executable, '-B', str(SYNC), 'sync', '--apply', '--max-actions', '10']
PROJECT_COMMAND = [sys.executable, '-B', str(SYNC.with_name('notion_projects.py')),
                   'sync', '--apply', '--max-actions', '10']
ALERT_STATE = Path('/home/hermes/.hermes/cron/notion_sync_alert_state.json')
ALERT_AFTER_FAILURES = 3


def read_alert_state():
    try:
        value = json.loads(ALERT_STATE.read_text())
        if isinstance(value, dict):
            state = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    return {}
                # Migrate the original boolean incident latch in place.
                if type(item) is bool:
                    state[key] = {'failures': ALERT_AFTER_FAILURES, 'alerted': item}
                elif (isinstance(item, dict)
                      and type(item.get('failures')) is int and item['failures'] >= 0
                      and type(item.get('alerted')) is bool):
                    state[key] = {'failures': item['failures'], 'alerted': item['alerted']}
                else:
                    return {}
            return state
    except (OSError, ValueError, TypeError):
        pass
    return {}


def write_alert_state(state):
    ALERT_STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = ALERT_STATE.with_suffix(f'{ALERT_STATE.suffix}.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(state, sort_keys=True) + '\n')
    temporary.replace(ALERT_STATE)


def record_failure(component, message):
    """Alert once only after a component fails on consecutive runs."""
    state = read_alert_state()
    incident = state.get(component, {'failures': 0, 'alerted': False})
    incident['failures'] += 1
    should_alert = not incident['alerted'] and incident['failures'] >= ALERT_AFTER_FAILURES
    if should_alert:
        incident['alerted'] = True
    state[component] = incident
    try:
        write_alert_state(state)
    except OSError:
        # Alert rather than silently losing a mature incident if state fails.
        should_alert = incident['failures'] >= ALERT_AFTER_FAILURES
    if should_alert:
        print(message)


def clear_alert(component):
    """Re-arm the component after verified recovery, without messaging."""
    state = read_alert_state()
    if state.pop(component, None) is None:
        return
    try:
        write_alert_state(state)
    except OSError:
        pass


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


def run_projects(*, timeout):
    try:
        result = run_connector(PROJECT_COMMAND, timeout=timeout)
        report = json.loads(result.stdout)
        if result.returncode == 75 and report == {'busy': True} and type(report.get('busy')) is bool:
            return
        if (result.returncode != 0 or not isinstance(report, dict)
                or not isinstance(report.get('conflicts'), dict)
                or report['conflicts']
                or any(type(report.get(k)) is not int or report[k] < 0
                       for k in ('planned', 'applied', 'pending'))):
            raise ValueError('project report requires review')
        clear_alert('projects')
    except Exception:
        record_failure('projects', 'ALERT: Hermes Tasks Notion project sync needs review after three consecutive failed runs. Existing task reconciliation remains independent. Run python3 /home/hermes/tasks/_tools/notion_projects.py plan; do not reset project recovery state.')


def run_tasks() -> int:
    try:
        result = run_connector(COMMAND, timeout=110)
    except subprocess.TimeoutExpired:
        record_failure('tasks', 'ALERT: Hermes Tasks Notion sync timed out or failed on three consecutive runs. Pending operations will be reconciled on the next run; do not reset sync state.')
        return 0
    except Exception:
        record_failure('tasks', 'ALERT: Hermes Tasks Notion sync failed to run or clean up on three consecutive runs. Inspect the local connector; credentials and remote text are not included in this alert.')
        return 0
    if result.returncode == 75:
        try:
            busy_report = json.loads(result.stdout)
            if isinstance(busy_report, dict) and set(busy_report) == {'busy'} and busy_report['busy'] is True:
                return 0  # Another sync owns reconciliation; normal overlap.
        except (ValueError, TypeError):
            pass
    if result.returncode != 0:
        record_failure('tasks', 'ALERT: Hermes Tasks Notion sync needs review after three consecutive failed runs. The local task registry remains canonical. Run python3 /home/hermes/tasks/_tools/notion_sync.py plan to inspect conflicts or failures. Do not delete sync state or force an overwrite.')
        return 0
    try:
        report = json.loads(result.stdout)
        if (not isinstance(report, dict) or not isinstance(report.get('conflicts'), dict)
                or any(type(report.get(k)) is not int or report[k] < 0
                       for k in ('planned', 'applied', 'pending'))):
            raise ValueError('invalid report')
        if report['conflicts']:
            record_failure('tasks', 'ALERT: Hermes Tasks Notion sync reports conflicts on three consecutive runs. The local registry remains canonical; inspect the connector plan before resolving either version.')
            return 0
    except (ValueError, TypeError):
        record_failure('tasks', 'ALERT: Hermes Tasks Notion sync returned an invalid verification report on three consecutive runs. Inspect the local connector before assuming synchronization succeeded.')
        return 0
    clear_alert('tasks')
    # Return success even on a domain failure: deliver the fixed alert without
    # launching the scheduler\'s autonomous code-repair path on external data.
    return 0


def main() -> int:
    started = time.monotonic()
    run_tasks()
    # Catalog writes cannot overwrite task relations. Run even after task
    # conflicts: an unmapped new project must be provisioned for the next cycle.
    remaining = 110 - (time.monotonic() - started)
    if remaining >= 15:
        run_projects(timeout=remaining)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
