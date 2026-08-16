#!/usr/bin/env python3
"""Serialize wiki builds and atomically promote a complete staged dist tree.

All supported manual and automatic production builds enter through `npm run build`,
which invokes this guard. Generated source/public artifacts are still produced in
the canonical tree, while the served `dist/` is replaced only after every build
and copy step succeeds.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

WIKI_ROOT = Path(__file__).resolve().parents[1]
LOCK_FILE = Path(os.environ.get("HERMES_WIKI_BUILD_LOCK", "/home/hermes/.hermes/wiki-build.lock"))
DIST = WIKI_ROOT / "dist"


@contextlib.contextmanager
def build_lock(nonblocking: bool = False):
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(LOCK_FILE, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError(f"refusing non-regular wiki build lock: {LOCK_FILE}")
        lock = os.fdopen(fd, "r+", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise
    with lock:
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        fcntl.flock(lock, flags)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def run(command: list[str]) -> None:
    subprocess.run(command, cwd=WIKI_ROOT, check=True)


def copy_public_artifacts(staging: Path) -> None:
    semantic = WIKI_ROOT / "public" / "semantic"
    if semantic.exists():
        shutil.copytree(semantic, staging / "semantic", dirs_exist_ok=True)
    shutil.copy2(WIKI_ROOT / "public" / "wiki-graph.json", staging / "wiki-graph.json")
    shutil.copytree(WIKI_ROOT / "public" / "assets", staging / "assets", dirs_exist_ok=True)
    raw_assets = WIKI_ROOT / "src" / "raw" / "assets"
    if raw_assets.exists():
        shutil.copytree(raw_assets, staging / "raw" / "assets", dirs_exist_ok=True)


def fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def promote(staging: Path) -> None:
    """Promote staging with rollback; never leave a partial build as dist."""
    backup = Path(tempfile.mkdtemp(prefix=".dist.previous.", dir=WIKI_ROOT))
    backup.rmdir()
    moved_old = False
    try:
        if DIST.exists():
            os.replace(DIST, backup)
            moved_old = True
        os.replace(staging, DIST)
        fsync_directory(WIKI_ROOT)
    except BaseException:
        if moved_old and backup.exists() and not DIST.exists():
            os.replace(backup, DIST)
            fsync_directory(WIKI_ROOT)
        raise
    else:
        if backup.exists():
            shutil.rmtree(backup)


def build() -> None:
    staging = Path(tempfile.mkdtemp(prefix=".dist.build.", dir=WIKI_ROOT))
    try:
        fonts = WIKI_ROOT / "public" / "assets" / "fonts"
        fonts.mkdir(parents=True, exist_ok=True)
        shutil.copy2(WIKI_ROOT / "node_modules" / "katex" / "dist" / "katex.min.css", WIKI_ROOT / "public" / "assets" / "katex.min.css")
        for font in (WIKI_ROOT / "node_modules" / "katex" / "dist" / "fonts").iterdir():
            if font.is_file():
                shutil.copy2(font, fonts / font.name)
        run(["node", ".vitepress/gen-sidebar.mjs"])
        run(["node", ".vitepress/validate-wiki-links.mjs"])
        run(["node", ".vitepress/gen-semantic-graph.mjs"])
        run(["node", ".vitepress/validate-semantic-relationships.mjs"])
        run([str(WIKI_ROOT / "node_modules" / ".bin" / "vitepress"), "build", ".", "--outDir", str(staging)])
        copy_public_artifacts(staging)
        promote(staging)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Locked, staged wiki production build")
    parser.add_argument("--nonblocking", action="store_true", help="Exit successfully if another build owns the lock")
    args = parser.parse_args(argv)
    try:
        with build_lock(nonblocking=args.nonblocking):
            build()
    except BlockingIOError:
        print("[wiki-build] Another build is already running; skipping")
        return 75
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
