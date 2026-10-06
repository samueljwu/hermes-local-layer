#!/usr/bin/env python3
"""Shared non-secret helpers for local Hermes operational scripts.

Keep this module free of embedded tokens/secrets. It may load environment files,
resolve public channel IDs, perform Discord REST calls with redacted errors, and
write JSON atomically.
"""
from __future__ import annotations

import json
import os
import secrets
import time
import contextlib
import fcntl
import stat
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DISCORD_API = "https://discord.com/api/v10"
DEFAULT_ENV_PATH = Path.home() / ".hermes" / ".env"
CHANNELS_PATH = Path.home() / ".hermes" / "local_channels.yaml"
CANONICAL_TASKS_ROOT = Path("/home/hermes/tasks")


def open_directory_nofollow(path: Path, *, create: bool = False) -> int:
    """Open a directory one component at a time without following symlinks."""
    directory = Path(path)
    fd = os.open("/" if directory.is_absolute() else ".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = directory.parts[1:] if directory.is_absolute() else directory.parts
        for component in parts:
            if component in {"", "."}:
                continue
            if component == "..":
                raise ValueError(f"refusing parent traversal in directory path: {directory}")
            try:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o755, dir_fd=fd)
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


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
    parent_fd = open_directory_nofollow(lock_path.parent, create=True)
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
    try:
        fd = os.open(lock_path.name, flags, 0o600, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError(f"refusing non-regular task lock: {lock_path}")
        lock = os.fdopen(fd, "r+", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise
    with lock:
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
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    dir_fd = open_directory_nofollow(path.parent, create=True)
    tmp_name = f".{path.name}.{secrets.token_hex(8)}.tmp"
    fd = -1
    try:
        fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        os.close(dir_fd)


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
