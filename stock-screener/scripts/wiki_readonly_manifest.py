#!/usr/bin/env python3
"""Content manifest for the stock-screener's read-only wiki deploy check."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

WIKI_ROOT = Path("/home/hermes/wiki")
PROTECTED = ("src", "dist", "_tools", ".vitepress", "package.json", "package-lock.json")


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    for relative_root in PROTECTED:
        root = WIKI_ROOT / relative_root
        if not root.exists():
            print(f"MISSING {relative_root}")
            continue
        if root.is_file():
            print(f"FILE {file_digest(root)} {relative_root}")
            continue
        for current, dirs, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            dirs[:] = sorted(name for name in dirs if not (current_path / name).is_symlink())
            for name in sorted(files):
                path = current_path / name
                rel = path.relative_to(WIKI_ROOT).as_posix()
                if path.is_symlink():
                    print(f"SYMLINK {rel}")
                else:
                    print(f"FILE {file_digest(path)} {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
