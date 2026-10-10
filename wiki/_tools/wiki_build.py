#!/usr/bin/env python3
"""Reviewed, isolated production builds with atomic Linux directory exchange.

Manual and hook builds share this entrypoint/lock. All compilation and evidence
asset copies read a private source snapshot. Input drift or review failures leave
the prior complete dist intact. No build automatically certifies knowledge.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import fcntl
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ingestion_gate as gate

WIKI_ROOT = Path(__file__).resolve().parents[1]
LOCK_FILE = Path(os.environ.get("HERMES_WIKI_BUILD_LOCK", "/home/hermes/.hermes/wiki-build.lock"))
DIST = WIKI_ROOT / "dist"


class PublishedWarning(OSError):
    """Commit succeeded; subsequent durability/cleanup work failed."""


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


def run(command: list[str], root: Path | None = None) -> None:
    subprocess.run(command, cwd=root or WIKI_ROOT, check=True)


def copy_public_artifacts(staging: Path, root: Path | None = None) -> None:
    root = root or WIKI_ROOT
    semantic = root / "public" / "semantic"
    if semantic.exists():
        shutil.copytree(semantic, staging / "semantic", dirs_exist_ok=True)
    shutil.copy2(root / "public" / "wiki-graph.json", staging / "wiki-graph.json")
    shutil.copytree(root / "public" / "assets", staging / "assets", dirs_exist_ok=True)
    raw_assets = root / "src" / "raw" / "assets"
    if raw_assets.exists():
        shutil.copytree(raw_assets, staging / "raw" / "assets", dirs_exist_ok=True)


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def exchange_dirs(left: Path, right: Path) -> None:
    """renameat2(RENAME_EXCHANGE): no missing-dist or interrupted-rename window."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(left), -100, os.fsencode(right), 2) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(right))


def promote(staging: Path) -> None:
    gate.need(staging.is_dir() and not staging.is_symlink(), "invalid staged release")
    gate.need(not DIST.is_symlink(), "refusing symlink dist")
    gate.need(staging.stat().st_dev == DIST.parent.stat().st_dev, "release scratch and dist must share a filesystem")
    if DIST.exists():
        gate.need(DIST.is_dir(), "dist must be a directory")
        exchange_dirs(staging, DIST)
    else:
        os.replace(staging, DIST)
    # After exchange/rename the new site is already visible. Never label a
    # subsequent fsync/cleanup error as a refused or failed publication.
    try:
        fsync_directory(DIST.parent)
        fsync_directory(staging.parent)
        if staging.exists():
            shutil.rmtree(staging)
    except OSError as exc:
        raise PublishedWarning(f"new release is live; durability/cleanup warning: {exc}") from exc


def require_unchanged(expected: dict[str, str]) -> None:
    actual = gate.inventory(WIKI_ROOT, build_inputs=True)
    gate.need(actual == expected, "canonical build inputs changed during build; retry only after review/merge")
    gate.assert_publishable(WIKI_ROOT)


@contextlib.contextmanager
def source_snapshot(expected: dict[str, str]):
    # Hermes-provided TMPDIR is the designated scratch directory, not system /tmp.
    directory = Path(tempfile.mkdtemp(prefix="wiki-build-snapshot-"))
    try:
        for name in ("src", "public", ".vitepress", "_tools", "_meta/ingestion"):
            source = WIKI_ROOT / name
            if source.exists():
                shutil.copytree(source, directory / name, ignore=lambda parent, names: [
                    name for name in names if gate.bytecode_cache((Path(parent) / name).relative_to(WIKI_ROOT).as_posix())])
        for name in ("package.json", "package-lock.json"):
            if (WIKI_ROOT / name).exists():
                shutil.copy2(WIKI_ROOT / name, directory / name)
        # Only installed dependencies are shared. Configuration, plugins, sources,
        # evidence, policy and receipts are real copied files, never live symlinks.
        (directory / "node_modules").symlink_to(WIKI_ROOT / "node_modules", target_is_directory=True)
        gate.need(gate.inventory(directory, build_inputs=True) == expected, "inputs changed while snapshotting")
        require_unchanged(expected)
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def sync_generated(snapshot: Path) -> None:
    """Copy back only named derived artifacts; never canonical review decisions."""
    for parent in (snapshot / "src/_meta/semantic", snapshot / "public/semantic", snapshot / "public/assets"):
        if not parent.exists():
            continue
        for path in parent.rglob("*"):
            if path.is_file():
                rel = path.relative_to(snapshot).as_posix()
                if gate.generated(rel):
                    gate.atomic_write(gate.safe_path(WIKI_ROOT, rel, missing=True), path.read_bytes())
    for rel in (".vitepress/_sidebar-generated.mjs", "public/wiki-graph.json"):
        gate.atomic_write(gate.safe_path(WIKI_ROOT, rel, missing=True), (snapshot / rel).read_bytes())


def build() -> None:
    admission = gate.assert_publishable(WIKI_ROOT)
    expected = gate.inventory(WIKI_ROOT, build_inputs=True)
    with source_snapshot(expected) as snapshot:
        gate.assert_publishable(snapshot)
        run([sys.executable, "-B", "_tools/wiki_ops.py", "chronology-audit"], snapshot)
        fonts = snapshot / "public/assets/fonts"
        fonts.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snapshot / "node_modules/katex/dist/katex.min.css", snapshot / "public/assets/katex.min.css")
        for font in (snapshot / "node_modules/katex/dist/fonts").iterdir():
            if font.is_file():
                shutil.copy2(font, fonts / font.name)
        run(["node", ".vitepress/gen-sidebar.mjs"], snapshot)
        run(["node", ".vitepress/validate-wiki-links.mjs"], snapshot)
        run(["node", ".vitepress/gen-semantic-graph.mjs"], snapshot)
        run(["node", ".vitepress/validate-semantic-relationships.mjs"], snapshot)
        run([sys.executable, "-B", "_tools/wiki_ops.py", "validate"], snapshot)
        staging = snapshot / "dist"
        run([str(snapshot / "node_modules/.bin/vitepress"), "build", ".", "--outDir", str(staging)], snapshot)
        copy_public_artifacts(staging, snapshot)
        gate.need(gate.inventory(snapshot, build_inputs=True) == expected, "a build step changed frozen canonical inputs")
        gate.assert_publishable(snapshot)
        gate.write_json(staging / "release-manifest.json", {
            "version": 1, "built_at": gate.now(), "input_manifest_sha256": gate.sha(gate.canonical(expected)),
            "legacy_unreviewed_files": admission["legacy_files"], "reviewed_files": admission["reviewed_files"],
            "hash_policy": gate.HASH_POLICY,
        })
        require_unchanged(expected)
        sync_generated(snapshot)
        require_unchanged(expected)
        promote(staging)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nonblocking", action="store_true", help="Return 75 if another build owns the lock")
    args = parser.parse_args(argv)
    try:
        with build_lock(nonblocking=args.nonblocking):
            build()
    except BlockingIOError:
        print("[wiki-build] Another build is already running; skipping")
        return 75
    except PublishedWarning as exc:
        print(f"[wiki-build] Published with warning: {exc}", file=sys.stderr)
        return 2
    except (gate.GateError, OSError, subprocess.CalledProcessError) as exc:
        print(f"[wiki-build] Refused publication: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
