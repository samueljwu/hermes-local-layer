#!/usr/bin/env python3
"""Rebuild ~/wiki only when source/config files have changed.

Designed for Hermes hooks. Default mode is quiet on no-op and one-line output on
build success/failure. Use --verbose for no-op details and full npm output.
"""
from __future__ import annotations

import argparse

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
WIKI = HOME / "wiki"
STATE_FILE = HOME / ".hermes" / "wiki-build-state.json"
LOG_FILE = HOME / ".hermes" / "logs" / "wiki-autobuild.log"

WATCH_PATHS = [
    WIKI / "src",
    WIKI / "package.json",
    WIKI / "package-lock.json",
    WIKI / ".vitepress" / "config.ts",
    WIKI / ".vitepress" / "gen-sidebar.mjs",
    WIKI / ".vitepress" / "gen-graph-data.mjs",
    WIKI / ".vitepress" / "validate-wiki-links.mjs",
    WIKI / ".vitepress" / "plugins",
    WIKI / ".vitepress" / "theme",
]

SKIP_DIRS = {"node_modules", "dist", ".cache", "cache"}
SKIP_SUFFIXES = {".swp", ".tmp", "~"}


def iter_files(path: Path):
    if not path.exists():
        return
    if path.is_file():
        yield path
        return
    for child in sorted(path.rglob("*")):
        if any(part in SKIP_DIRS for part in child.parts):
            continue
        if child.is_file() and not any(str(child).endswith(s) for s in SKIP_SUFFIXES):
            yield child


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot() -> dict:
    files = []
    seen = set()
    for watch_path in WATCH_PATHS:
        for path in iter_files(watch_path) or []:
            rel = path.relative_to(WIKI).as_posix()
            if rel in seen:
                continue
            seen.add(rel)
            st = path.stat()
            files.append({
                "path": rel,
                "size": st.st_size,
                "sha256": file_digest(path),
            })
    files.sort(key=lambda item: item["path"])
    h = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"digest": h, "files": files, "file_count": len(files)}


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(snap: dict, status: str) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "digest": snap["digest"],
        "file_count": snap["file_count"],
        "last_build_status": status,
        "last_build_at": datetime.now(timezone.utc).isoformat(),
    }
    fd, tmp_name = tempfile.mkstemp(prefix=f".{STATE_FILE.name}.", suffix=".tmp", dir=str(STATE_FILE.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, STATE_FILE)
        try:
            dir_fd = os.open(STATE_FILE.parent, os.O_DIRECTORY)
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


def append_log(text: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(text.rstrip() + "\n")


def run_build(verbose: bool) -> int:
    env = os.environ.copy()
    env.setdefault("HOME", str(HOME))
    started = datetime.now(timezone.utc).isoformat()
    proc = subprocess.run(
        [sys.executable, str(WIKI / "_tools" / "wiki_build.py"), "--nonblocking"],
        cwd=WIKI,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=300,
        env=env,
    )
    log_text = f"\n=== {started} wiki autobuild exit={proc.returncode} ===\n{proc.stdout}"
    append_log(log_text)
    if verbose or proc.returncode != 0:
        sys.stdout.write(proc.stdout)
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Build even if watched files are unchanged")
    parser.add_argument("--verbose", action="store_true", help="Print no-op details and build output")
    args = parser.parse_args()

    if not WIKI.exists():
        print(f"[wiki-autobuild] Wiki directory not found: {WIKI}", file=sys.stderr)
        return 1

    snap = snapshot()
    state = load_state()
    if not args.force and state.get("digest") == snap["digest"]:
        if args.verbose:
            print(f"[wiki-autobuild] No wiki changes detected ({snap['file_count']} watched files)")
        return 0

    print(f"[wiki-autobuild] Wiki changes detected; rebuilding ({snap['file_count']} watched files)", flush=True)
    rc = run_build(args.verbose)
    if rc == 75:
        if args.verbose:
            print("[wiki-autobuild] Build already running; leaving state dirty for the next hook")
        return 0
    if rc == 0:
        save_state(snap, "ok")
        print("[wiki-autobuild] Build complete", flush=True)
    else:
        print(f"[wiki-autobuild] Build failed with exit code {rc}; see {LOG_FILE}", file=sys.stderr, flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
