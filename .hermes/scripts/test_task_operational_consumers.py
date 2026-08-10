from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path("/home/hermes")


def run_probe(code: str, home: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env.pop("TASKS_ROOT", None)
    env.pop("HERMES_ALLOW_NONCANONICAL_LOCAL_ROOTS", None)
    return subprocess.run([sys.executable, "-c", code], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=ROOT)


def test_tasks_outstanding_uses_canonical_root_when_home_drifts(tmp_path):
    plugin = ROOT / ".hermes" / "plugins" / "tasks-outstanding" / "__init__.py"
    code = f"import importlib.util; s=importlib.util.spec_from_file_location('p',{str(plugin)!r}); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(m.REGISTRY_PATH)"
    proc = run_probe(code, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "/home/hermes/tasks/_meta/task_registry.json"


def test_due_reminder_consumers_use_canonical_task_ops_when_home_drifts(tmp_path):
    for script_name in ("due_today_task_reminders.py", "due_tomorrow_task_reminders.py"):
        script = ROOT / ".hermes" / "scripts" / script_name
        code = f"import importlib.util; s=importlib.util.spec_from_file_location('r',{str(script)!r}); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(m.TASK_OPS_PATH)"
        proc = run_probe(code, tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "/home/hermes/tasks/_tools/task_ops.py"


def test_operational_consumers_fail_closed_on_noncanonical_tasks_root(tmp_path):
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["TASKS_ROOT"] = str(tmp_path / "tasks")
    env.pop("HERMES_ALLOW_NONCANONICAL_LOCAL_ROOTS", None)
    plugin = ROOT / ".hermes" / "plugins" / "tasks-outstanding" / "__init__.py"
    code = f"import importlib.util; s=importlib.util.spec_from_file_location('p',{str(plugin)!r}); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)"
    proc = subprocess.run([sys.executable, "-c", code], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=ROOT)
    assert proc.returncode != 0
    assert "Refusing non-canonical TASKS_ROOT" in proc.stderr
