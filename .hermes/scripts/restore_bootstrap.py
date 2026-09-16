#!/usr/bin/env python3
"""Install a staged backup checkout into a home, refusing every file collision.

No network, installer, service or git commands. Run against a quiescent destination.
The source must be a reviewed checkout (including its .git directory).
"""
import argparse
from pathlib import Path
import os
import shutil
import stat


def restore(source, target):
    source = source.resolve()
    target = target.absolute()
    if source == target or source in target.parents or target in source.parents:
        raise ValueError("source and target must be disjoint")
    if not (source / ".git").is_dir() or not (source / "RESTORE.md").is_file():
        raise ValueError("source must be a backup checkout with RESTORE.md and .git")
    # Refuse symlink ancestors, including broken links. Never traverse a target
    # alias into an unrelated home or runtime tree.
    for parent in (target, *target.parents):
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            raise ValueError("destination ancestor is not a plain directory")
    entries = sorted(source.rglob("*"), key=lambda p: (len(p.parts), str(p)))
    for entry in entries:
        mode = entry.lstat().st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise ValueError("source contains symlink or special file; review manually")
        dest = target / entry.relative_to(source)
        if dest.is_symlink() or (dest.exists() and not (entry.is_dir() and dest.is_dir())):
            raise ValueError("destination collision; nothing copied")
    # An existing .git directory is itself a collision, even if empty.
    if (target / ".git").exists():
        raise ValueError("destination already has .git; nothing copied")
    target.mkdir(parents=True, exist_ok=True)
    count = 0
    for entry in entries:
        dest = target / entry.relative_to(source)
        if entry.is_dir():
            dest.mkdir(mode=0o700 if entry.relative_to(source) == Path(".hermes") else 0o777,
                       exist_ok=True)
        else:
            # Exclusive creation also refuses a concurrent file creation. This
            # is not a multi-file transaction; an I/O failure can leave a partial
            # restore, which must be inspected rather than retried over files.
            with entry.open("rb") as inp, dest.open("xb") as out:
                shutil.copyfileobj(inp, out)
            os.chmod(dest, stat.S_IMODE(entry.stat().st_mode))
            count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    args = parser.parse_args()
    try:
        count = restore(args.source, args.target)
    except (OSError, ValueError) as error:
        # No filenames or source data in failures (may contain private names).
        print(f"Restore refused/failed ({type(error).__name__}); inspect conflicts and partial files. No overwrites attempted.")
        return 1
    print(f"Restored {count} files without overwriting existing files. No install or service was run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
