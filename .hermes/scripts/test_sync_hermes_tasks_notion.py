import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('notion_cron_wrapper_test', Path(__file__).with_name('sync_hermes_tasks_notion.py'))
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)


@pytest.fixture(autouse=True)
def isolated_alert_state(tmp_path, monkeypatch):
    monkeypatch.setattr(wrapper, 'ALERT_STATE', tmp_path / 'alert-state.json')
    monkeypatch.setattr(wrapper, 'ALERT_AFTER_FAILURES', 1)


def test_success_is_silent_and_bounded(monkeypatch, capsys):
    calls = []
    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"planned":0,"applied":0,"pending":0,"conflicts":{}}', stderr='')
    monkeypatch.setattr(wrapper, 'run_connector', run)
    assert wrapper.main() == 0
    assert capsys.readouterr().out == ''
    assert calls[0][0][-4:] == ['sync', '--apply', '--max-actions', '10']
    assert calls[0][1]['timeout'] == 110


def test_failure_alert_never_includes_remote_text(monkeypatch, capsys):
    monkeypatch.setattr(wrapper, 'run_connector', lambda *a, **k: SimpleNamespace(returncode=2, stdout='REMOTE_INSTRUCTION', stderr='SECRET_SENTINEL'))
    assert wrapper.main() == 0
    out = capsys.readouterr().out
    assert out.startswith('ALERT:') and 'canonical' in out
    assert 'REMOTE_INSTRUCTION' not in out and 'SECRET_SENTINEL' not in out


def test_timeout_is_safe_and_not_an_autofix_trigger(monkeypatch, capsys):
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired(['secret'], 110, output='SECRET_SENTINEL')
    monkeypatch.setattr(wrapper, 'run_connector', timeout)
    assert wrapper.main() == 0
    out = capsys.readouterr().out
    assert 'timed out' in out and 'SECRET_SENTINEL' not in out


def test_busy_overlap_is_silent(monkeypatch, capsys):
    monkeypatch.setattr(wrapper, 'run_connector', lambda *a, **k: SimpleNamespace(returncode=75, stdout='{"busy":true}', stderr=''))
    assert wrapper.main() == 0
    assert capsys.readouterr().out == ''


def test_malformed_busy_is_not_silenced(monkeypatch, capsys):
    for raw in ('{}', 'not-json', '{"busy":1}', '{"busy":true,"error":"secret"}'):
        wrapper.clear_alert('tasks')
        wrapper.clear_alert('projects')
        monkeypatch.setattr(wrapper, 'run_connector', lambda *a, **k: SimpleNamespace(returncode=75, stdout=raw, stderr='SECRET'))
        assert wrapper.main() == 0
        out = capsys.readouterr().out
        assert out.startswith('ALERT:') and 'SECRET' not in out


def test_empty_report_alerts(monkeypatch, capsys):
    monkeypatch.setattr(wrapper, 'run_connector', lambda *a, **k: SimpleNamespace(returncode=0, stdout='{}', stderr=''))
    assert wrapper.main() == 0
    assert 'invalid verification report' in capsys.readouterr().out


def test_bad_report_alerts(monkeypatch, capsys):
    monkeypatch.setattr(wrapper, 'run_connector', lambda *a, **k: SimpleNamespace(returncode=0, stdout='unstructured', stderr=''))
    assert wrapper.main() == 0
    assert 'invalid verification report' in capsys.readouterr().out


def test_real_connector_success_captures_only_stdout():
    result = wrapper.run_connector([wrapper.sys.executable, '-c',
        'import sys; print("ok"); print("SECRET_SENTINEL", file=sys.stderr)'], timeout=5)
    assert result.returncode == 0 and result.stdout == 'ok\n' and result.stderr == ''


def test_real_timeout_kills_derivative_and_reaps_connector(tmp_path, monkeypatch):
    import json
    import os
    import signal
    import time
    import pytest
    pidfile = tmp_path / 'pids.json'
    script = ('import subprocess,sys,time,os,json; from pathlib import Path; '
              'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); '
              'Path(sys.argv[1]).write_text(json.dumps([os.getpid(),p.pid])); time.sleep(60)')
    popen = wrapper.subprocess.Popen
    processes = []
    def capture(*args, **kwargs):
        assert kwargs['start_new_session'] is True
        p = popen(*args, **kwargs)
        processes.append(p)
        return p
    monkeypatch.setattr(wrapper.subprocess, 'Popen', capture)
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            wrapper.run_connector([wrapper.sys.executable, '-c', script, str(pidfile)], timeout=1)
        assert time.monotonic() - started < 8
        parent, child = json.loads(pidfile.read_text())
        assert processes[0].pid == parent and processes[0].returncode == -signal.SIGKILL
        # An orphan zombie may await PID1 reaping, but must no longer execute.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                state = Path(f'/proc/{child}/stat').read_text().rsplit(')', 1)[1].split()[0]
            except FileNotFoundError:
                break
            if state == 'Z':
                break
            time.sleep(0.01)
        else:
            pytest.fail('derivative process survived group timeout')
    finally:
        for p in processes:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.wait(timeout=2)


def test_cleanup_pipe_deadline_is_bounded(monkeypatch):
    import pytest
    calls = []
    class Process:
        pid = 987654321
        stdout = SimpleNamespace(close=lambda: calls.append('close'))
        def communicate(self, *, timeout):
            calls.append(('communicate', timeout))
            raise subprocess.TimeoutExpired(['fixture'], timeout)
        def wait(self, *, timeout):
            calls.append(('wait', timeout))
            return -9
    monkeypatch.setattr(wrapper.subprocess, 'Popen', lambda *a, **k: Process())
    monkeypatch.setattr(wrapper.os, 'killpg', lambda pid, sig: calls.append(('killpg', pid, sig)))
    with pytest.raises(RuntimeError, match='cleanup deadline'):
        wrapper.run_connector(['fixture'], timeout=110)
    assert calls == [('communicate', 110), ('killpg', Process.pid, wrapper.signal.SIGKILL),
                     ('communicate', 5), 'close', ('wait', 1)]


def test_projects_run_after_clean_tasks_with_shared_wall_budget(monkeypatch, capsys):
    calls = []
    ticks = iter([100.0, 120.0])
    monkeypatch.setattr(wrapper.time, 'monotonic', lambda: next(ticks))
    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"planned":0,"applied":0,"pending":0,"conflicts":{}}')
    monkeypatch.setattr(wrapper, 'run_connector', run)
    assert wrapper.main() == 0
    assert len(calls) == 2
    assert calls[0][0] == wrapper.COMMAND
    assert calls[1][0] == wrapper.PROJECT_COMMAND
    assert calls[1][1]['timeout'] == 90
    assert capsys.readouterr().out == ''


def test_project_catalog_runs_despite_task_conflict_pending_or_backlog(monkeypatch, capsys):
    for raw in ('{"planned":0,"applied":0,"pending":0,"conflicts":{"key":"conflict"}}',
                '{"planned":0,"applied":0,"pending":1,"conflicts":{}}',
                '{"planned":12,"applied":10,"pending":0,"conflicts":{}}'):
        calls = []
        def run(cmd, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout=raw)
        monkeypatch.setattr(wrapper, 'run_connector', run)
        assert wrapper.main() == 0
        assert calls == [wrapper.COMMAND, wrapper.PROJECT_COMMAND]
        capsys.readouterr()


def test_projects_failure_is_separate_safe_alert(monkeypatch, capsys):
    results = iter([SimpleNamespace(returncode=0, stdout='{"planned":0,"applied":0,"pending":0,"conflicts":{}}'),
                    SimpleNamespace(returncode=2, stdout='SECRET_REMOTE')])
    monkeypatch.setattr(wrapper, 'run_connector', lambda *a, **k: next(results))
    assert wrapper.main() == 0
    out = capsys.readouterr().out
    assert 'project' in out.lower() and 'SECRET_REMOTE' not in out


def test_projects_skip_when_wall_budget_exhausted(monkeypatch, capsys):
    ticks = iter([100.0, 205.0])
    monkeypatch.setattr(wrapper.time, 'monotonic', lambda: next(ticks))
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout='{"planned":0,"applied":0,"pending":0,"conflicts":{}}')
    monkeypatch.setattr(wrapper, 'run_connector', run)
    assert wrapper.main() == 0
    assert calls == [wrapper.COMMAND]
    assert capsys.readouterr().out == ''


def test_project_reports_are_validated_without_leaking(monkeypatch, capsys):
    clean = SimpleNamespace(returncode=0, stdout='{"planned":0,"applied":0,"pending":0,"conflicts":{}}')
    for code, raw, silent in [(75, '{"busy":true}', True), (75, '{"busy":1}', False),
                              (0, '{}', False), (0, '{"planned":0,"applied":0,"pending":0,"conflicts":{"key":"SECRET_REMOTE"}}', False)]:
        wrapper.clear_alert('projects')
        results = iter([clean, SimpleNamespace(returncode=code, stdout=raw)])
        monkeypatch.setattr(wrapper, 'run_connector', lambda *a, **k: next(results))
        assert wrapper.main() == 0
        out = capsys.readouterr().out
        assert (out == '') is silent
        assert 'SECRET_REMOTE' not in out


def test_project_timeout_preserves_task_success(monkeypatch, capsys):
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == wrapper.PROJECT_COMMAND:
            raise subprocess.TimeoutExpired(cmd, kwargs['timeout'], output='SECRET_REMOTE')
        return SimpleNamespace(returncode=0, stdout='{"planned":0,"applied":0,"pending":0,"conflicts":{}}')
    monkeypatch.setattr(wrapper, 'run_connector', run)
    assert wrapper.main() == 0
    assert calls == [wrapper.COMMAND, wrapper.PROJECT_COMMAND]
    out = capsys.readouterr().out
    assert 'project sync needs review' in out and 'SECRET_REMOTE' not in out


def test_continuous_failure_alerts_once_until_recovery(monkeypatch, capsys):
    failed = SimpleNamespace(returncode=2, stdout='SECRET_REMOTE', stderr='SECRET')
    clean = SimpleNamespace(returncode=0, stdout='{"planned":0,"applied":0,"pending":0,"conflicts":{}}')
    monkeypatch.setattr(wrapper, 'ALERT_AFTER_FAILURES', 3)

    monkeypatch.setattr(wrapper, 'run_connector', lambda *a, **k: failed)
    assert wrapper.main() == 0
    assert capsys.readouterr().out == ''
    assert wrapper.main() == 0
    assert capsys.readouterr().out == ''
    assert wrapper.main() == 0
    assert capsys.readouterr().out.count('ALERT:') == 2
    assert wrapper.main() == 0
    assert capsys.readouterr().out == ''

    monkeypatch.setattr(wrapper, 'run_connector', lambda *a, **k: clean)
    assert wrapper.main() == 0
    assert capsys.readouterr().out == ''

    monkeypatch.setattr(wrapper, 'run_connector', lambda *a, **k: failed)
    assert wrapper.main() == 0
    assert capsys.readouterr().out == ''
    assert wrapper.main() == 0
    assert capsys.readouterr().out == ''
    assert wrapper.main() == 0
    assert capsys.readouterr().out.count('ALERT:') == 2
