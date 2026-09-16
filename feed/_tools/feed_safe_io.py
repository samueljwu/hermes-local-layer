"""Feed-local descriptor-bound I/O (Linux); no dependency on another domain.

Uses the same no-follow traversal pattern as the source-pin updater. Paths are
policy-checked by callers; every filesystem operation after binding is relative
to the verified descriptor, even if its original pathname is subsequently moved.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import secrets
import stat


def open_directory_nofollow(path: Path, *, create: bool = False) -> int:
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise RuntimeError(f'expected absolute path without traversal: {path}')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o755, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def regular_destination(dir_fd: int, name: str) -> None:
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f'refusing non-regular destination: {name}')


def atomic_write_text(path: Path, text: str) -> None:
    parent = open_directory_nofollow(path.parent, create=True)
    temporary = None
    try:
        regular_destination(parent, path.name)
        for _ in range(128):
            name = f'.{path.name}.{secrets.token_hex(8)}.tmp'
            try:
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
                temporary = name
                break
            except FileExistsError:
                continue
        else:
            raise FileExistsError('could not allocate feed temporary file')
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        regular_destination(parent, path.name)
        os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        temporary = None
        os.fsync(parent)
    finally:
        try:
            if temporary is not None:
                os.unlink(temporary, dir_fd=parent)
        finally:
            os.close(parent)


def append_text(path: Path, text: str) -> None:
    parent = open_directory_nofollow(path.parent, create=True)
    try:
        fd = os.open(path.name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RuntimeError(f'refusing non-regular append destination: {path}')
            handle = os.fdopen(fd, 'a', encoding='utf-8')
        except BaseException:
            os.close(fd)
            raise
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.fsync(parent)
    finally:
        os.close(parent)


@contextmanager
def file_lock(path: Path):
    parent = open_directory_nofollow(path.parent, create=True)
    try:
        fd = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError(f'refusing non-regular feed lock: {path}')
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
