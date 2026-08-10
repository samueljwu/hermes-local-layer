#!/usr/bin/env python3
"""Shared non-secret helpers for local Hermes operational scripts.

Keep this module free of embedded tokens/secrets. It may load environment files,
resolve public channel IDs, perform Discord REST calls with redacted errors, and
write JSON atomically.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import contextlib
import fcntl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DISCORD_API = "https://discord.com/api/v10"
DEFAULT_ENV_PATH = Path.home() / ".hermes" / ".env"
CHANNELS_PATH = Path.home() / ".hermes" / "local_channels.yaml"
CANONICAL_TASKS_ROOT = Path("/home/hermes/tasks")


def resolve_tasks_root() -> Path:
    """Return the canonical local tasks root, failing closed on accidental drift.

    Discord scripts/plugins are cross-system surfaces and must not silently read
    a different registry if HOME or TASKS_ROOT is changed by cron/gateway env.
    Test/dev fixtures may opt in explicitly with
    HERMES_ALLOW_NONCANONICAL_LOCAL_ROOTS=1.
    """
    configured = Path(os.environ.get("TASKS_ROOT", str(CANONICAL_TASKS_ROOT))).expanduser().resolve()
    canonical = CANONICAL_TASKS_ROOT.resolve()
    if configured != canonical and os.environ.get("HERMES_ALLOW_NONCANONICAL_LOCAL_ROOTS") != "1":
        raise RuntimeError(
            f"Refusing non-canonical TASKS_ROOT {configured}; expected {canonical}. "
            "Set HERMES_ALLOW_NONCANONICAL_LOCAL_ROOTS=1 only for tests/dev fixtures."
        )
    return configured


@contextlib.contextmanager
def tasks_lock(root: Path | None = None):
    """Take the task-system lock shared by task_ops and operational consumers."""
    lock_path = (root or resolve_tasks_root()) / "_meta" / ".task_ops.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def load_env_file(path: Path = DEFAULT_ENV_PATH) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _minimal_yaml_mapping(text: str) -> dict[str, Any]:
    """Tiny YAML subset parser for local_channels.yaml if PyYAML is absent."""
    out: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, out)]
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        key, _, raw_value = line.strip().partition(":")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        value = raw_value.strip()
        if not value:
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = value.strip('"').strip("'")
    return out


def load_local_channels(path: Path = CHANNELS_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return _minimal_yaml_mapping(text)


def channel_id(name: str, *, platform: str = "discord") -> str | None:
    data = load_local_channels()
    if platform == "discord":
        return (((data.get("discord") or {}).get("channels") or {}).get(name))
    return None


def atomic_json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
        try:
            dir_fd = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def redacted_error(exc: BaseException) -> str:
    text = str(exc)
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if token:
        text = text.replace(token, "<redacted DISCORD_BOT_TOKEN>")
    return text


def discord_request(method: str, endpoint: str, token: str | None = None, payload: Any | None = None, *, attempts: int = 3) -> tuple[int, Any | None]:
    token = token or os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN not set")
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Authorization": f"Bot {token}", "User-Agent": "HermesLocalOps/1.0"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    url = f"{DISCORD_API}{endpoint}"
    for attempt in range(attempts):
        req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw) if raw else None
            except Exception:
                data = {"error": raw}
            if exc.code == 429 and attempt + 1 < attempts:
                retry_after = 1.0
                if isinstance(data, dict):
                    try:
                        retry_after = float(data.get("retry_after") or retry_after)
                    except Exception:
                        pass
                time.sleep(min(max(retry_after, 0.25), 10.0))
                continue
            return exc.code, data
    raise RuntimeError("unreachable discord_request retry state")
