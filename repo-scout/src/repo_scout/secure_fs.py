from __future__ import annotations

import fcntl
import json
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def lexical_path(path: Path) -> Path:
    """Return an absolute normalized path without following symlinks."""
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def require_lexically_under(path: Path, root: Path, label: str) -> Path:
    candidate = lexical_path(path)
    root_path = lexical_path(root)
    try:
        candidate.relative_to(root_path)
    except ValueError as exc:
        raise ValueError(f"{label} must stay under {root_path}: {candidate}") from exc
    return candidate


def _verify_owned_directory(fd: int, path: Path) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"refusing non-directory Repo Scout output path: {path}")
    if info.st_uid != os.geteuid():
        raise RuntimeError(f"refusing Repo Scout output directory not owned by current user: {path}")


class SecureDirectory:
    """An owned, non-symlink directory held by descriptor for race-safe I/O."""

    def __init__(self, path: Path, fd: int):
        self.path = path
        self.fd = fd

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def child(self, name: str, *, create: bool = True) -> "SecureDirectory":
        if not name or name in {".", ".."} or "/" in name or os.sep in name:
            raise ValueError(f"invalid output directory component: {name!r}")
        if create:
            try:
                os.mkdir(name, mode=0o700, dir_fd=self.fd)
            except FileExistsError:
                pass
        fd = os.open(name, _DIR_FLAGS, dir_fd=self.fd)
        try:
            child_path = self.path / name
            _verify_owned_directory(fd, child_path)
            return SecureDirectory(child_path, fd)
        except BaseException:
            os.close(fd)
            raise

    def _validate_filename(self, name: str) -> None:
        if not name or name in {".", ".."} or "/" in name or os.sep in name:
            raise ValueError(f"invalid output filename: {name!r}")

    def atomic_write_json(self, name: str, data: Any) -> None:
        self._validate_filename(name)
        text = json.dumps(data, indent=2, sort_keys=True) + "\n"
        for _ in range(100):
            tmp_name = f".{name}.{secrets.token_hex(8)}.tmp"
            try:
                fd = os.open(
                    tmp_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_NOFOLLOW,
                    0o600,
                    dir_fd=self.fd,
                )
                break
            except FileExistsError:
                continue
        else:
            raise FileExistsError(f"could not create unique temporary output in {self.path}")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(tmp_name, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def read_text(self, name: str) -> str:
        self._validate_filename(name)
        fd = os.open(name, os.O_RDONLY | _FILE_NOFOLLOW, dir_fd=self.fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RuntimeError(f"refusing non-regular Repo Scout output file: {self.path / name}")
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                fd = -1
                return stream.read()
        finally:
            if fd >= 0:
                os.close(fd)

    def stat_file(self, name: str) -> os.stat_result:
        self._validate_filename(name)
        return os.stat(name, dir_fd=self.fd, follow_symlinks=False)

    def unlink(self, name: str) -> None:
        self._validate_filename(name)
        os.unlink(name, dir_fd=self.fd)

    def append_json_line(self, name: str, data: Any) -> None:
        self._validate_filename(name)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | _FILE_NOFOLLOW
        fd = os.open(name, flags, 0o600, dir_fd=self.fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RuntimeError(f"refusing non-regular Repo Scout output file: {self.path / name}")
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                fd = -1
                stream.write(json.dumps(data, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if fd >= 0:
                os.close(fd)

    @contextmanager
    def lock(self, name: str = ".repo-scout.lock") -> Iterator[None]:
        self._validate_filename(name)
        flags = os.O_RDWR | os.O_CREAT | _FILE_NOFOLLOW
        fd = os.open(name, flags, 0o600, dir_fd=self.fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RuntimeError(f"refusing non-regular Repo Scout lock: {self.path / name}")
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@contextmanager
def open_output_directory(root: Path, target: Path, *, create: bool = True) -> Iterator[tuple[SecureDirectory, SecureDirectory]]:
    """Open target beneath root without following any absolute-path symlink."""
    root_path = lexical_path(root)
    target_path = require_lexically_under(target, root_path, "Repo Scout output path")

    # Anchor at the filesystem root and walk every absolute component with
    # openat(O_DIRECTORY|O_NOFOLLOW). Opening root_path as one pathname would
    # still follow a symlink in any ancestor before reaching canonical out/.
    current_fd = os.open(os.path.sep, _DIR_FLAGS)
    try:
        root_parts = root_path.parts[1:]
        for index, component in enumerate(root_parts):
            is_root_leaf = index == len(root_parts) - 1
            if create and is_root_leaf:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
            next_fd = os.open(component, _DIR_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        _verify_owned_directory(current_fd, root_path)
        root_dir = SecureDirectory(root_path, current_fd)
        current_fd = -1
    finally:
        if current_fd >= 0:
            os.close(current_fd)

    opened: list[SecureDirectory] = []
    try:
        current = root_dir
        for component in target_path.relative_to(root_path).parts:
            next_dir = current.child(component, create=create)
            opened.append(next_dir)
            current = next_dir
        yield root_dir, current
    finally:
        for directory in reversed(opened):
            directory.close()
        root_dir.close()
