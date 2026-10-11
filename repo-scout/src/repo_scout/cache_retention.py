"""Expire response files under the caller-held canonical Repo Scout lock."""
from __future__ import annotations

import math
import os
import stat
import time
from collections.abc import Iterator

from .secure_fs import SecureDirectory


def expired_responses(directory: SecureDirectory, ttl_seconds: float, *, now: float | None = None) -> Iterator[tuple[str, os.stat_result]]:
    """Inspect only immediate regular JSON files through the held descriptor."""
    if not math.isfinite(ttl_seconds) or ttl_seconds < 0:
        raise ValueError("cache TTL must be finite and nonnegative")
    cutoff = (time.time() if now is None else now) - ttl_seconds
    for name in os.listdir(directory.fd):
        if not name.endswith(".json"):
            continue
        try:
            info = directory.stat_file(name)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_mtime <= cutoff:
            yield name, info


def prune_expired_responses(directory: SecureDirectory, ttl_seconds: float, *, now: float | None = None) -> dict[str, int]:
    """Caller must hold the shared output lock; never follow symlinks/recurse."""
    removed = 0
    allocated_bytes = 0
    for name, info in expired_responses(directory, ttl_seconds, now=now):
        try:
            current = directory.stat_file(name)
            if (current.st_ino, current.st_size, current.st_mtime_ns, current.st_mode) != (info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode):
                continue
            directory.unlink(name)
        except FileNotFoundError:
            continue
        removed += 1
        allocated_bytes += info.st_blocks * 512
    return {"removed_files": removed, "allocated_bytes": allocated_bytes}
