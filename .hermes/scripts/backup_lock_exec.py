#!/usr/bin/env python3
"""Acquire the private backup lock without following symlinks, then exec a command."""
from __future__ import annotations

import errno
import fcntl
import os
import stat
import sys
from pathlib import Path

LOCK_FD_ENV = "HERMES_BACKUP_LOCK_FD"


def open_directory_nofollow(path: Path) -> int:
    """Open an absolute directory path one component at a time without symlink traversal."""
    path = Path(os.path.abspath(path))
    parts = path.parts
    fd = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in parts[1:]:
            try:
                next_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=fd,
                )
            except OSError as exc:
                if exc.errno != errno.ENOENT:
                    raise
                os.mkdir(component, 0o700, dir_fd=fd)
                next_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=fd,
                )
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def acquire(lock_path: Path) -> int:
    parent_fd = open_directory_nofollow(lock_path.parent)
    try:
        fd = os.open(
            lock_path.name,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
    finally:
        os.close(parent_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError(f"refusing non-regular backup lock: {lock_path}")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.set_inheritable(fd, True)
        return fd
    except BaseException:
        os.close(fd)
        raise


def validate_inherited(lock_path: Path, raw_fd: str) -> None:
    """Verify an inherited fd names and holds the configured regular lock."""
    if not raw_fd.isascii() or not raw_fd.isdecimal():
        raise RuntimeError("inherited lock fd is not numeric")
    fd = int(raw_fd)
    if fd < 3:
        raise RuntimeError("inherited lock fd uses a reserved descriptor")
    try:
        inherited_stat = os.fstat(fd)
    except OSError as exc:
        raise RuntimeError("inherited lock fd is not open") from exc
    if not stat.S_ISREG(inherited_stat.st_mode):
        raise RuntimeError("inherited lock fd is not a regular file")

    parent_fd = open_directory_nofollow(lock_path.parent)
    try:
        expected_fd = os.open(
            lock_path.name,
            os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
    finally:
        os.close(parent_fd)
    try:
        expected_stat = os.fstat(expected_fd)
        if not stat.S_ISREG(expected_stat.st_mode):
            raise RuntimeError(f"configured backup lock is not regular: {lock_path}")
        if (inherited_stat.st_dev, inherited_stat.st_ino) != (expected_stat.st_dev, expected_stat.st_ino):
            raise RuntimeError("inherited lock fd does not match configured lock")
    finally:
        os.close(expected_fd)

    # flock is tied to the inherited open file description. Re-taking it is
    # harmless when the helper already owns it; if an unlocked matching fd was
    # injected, this safely acquires the lock before the backup can continue.
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise RuntimeError("inherited lock fd does not own the configured lock") from exc


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "--validate":
        lock_path = Path(argv[1]).expanduser()
        try:
            validate_inherited(lock_path, argv[2])
        except (OSError, RuntimeError) as exc:
            print(f"Refusing invalid inherited backup lock: {exc}", file=sys.stderr)
            return 1
        return 0
    if len(argv) < 3 or argv[1] != "--":
        print(f"Usage: {sys.argv[0]} LOCK_PATH -- COMMAND [ARG ...]", file=sys.stderr)
        return 2
    lock_path = Path(argv[0]).expanduser()
    try:
        fd = acquire(lock_path)
    except BlockingIOError:
        print("Another Hermes knowledge backup is already running; exiting.")
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"Refusing unsafe backup lock {lock_path}: {exc}", file=sys.stderr)
        return 1

    env = os.environ.copy()
    env[LOCK_FD_ENV] = str(fd)
    os.execvpe(argv[2], argv[2:], env)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
