import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location('notion_cron_wrapper_test', Path(__file__).with_name('sync_hermes_tasks_notion.py'))
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)


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
