from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "your", "have",
    "will", "about", "using", "only", "read", "write", "system", "task", "tasks",
    "wiki", "journal", "feed", "notes", "note", "user", "hermes", "http", "https",
}
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]{2,}")


def build_interest_profile(
    roots: list[str | Path],
    max_files: int = 200,
    max_bytes_per_file: int = 8192,
) -> dict:
    """Read configured roots only and return compact term counts.

    This is intentionally shallow/read-only: no writes, no hidden traversal outside roots,
    and only text-like files are considered.
    """
    counts: Counter[str] = Counter()
    files_read = 0
    allowed_suffixes = {".md", ".txt", ".json", ".yaml", ".yml"}
    for root in [Path(r).expanduser().resolve() for r in roots]:
        if not root.exists():
            continue
        candidates = [root] if root.is_file() else root.rglob("*")
        for path in candidates:
            if files_read >= max_files:
                break
            if not path.is_file() or path.suffix.lower() not in allowed_suffixes:
                continue
            # Resolve and verify it did not escape the configured root via symlink.
            resolved = path.resolve()
            if root.is_dir() and root not in resolved.parents and resolved != root:
                continue
            try:
                text = resolved.read_text(encoding="utf-8", errors="ignore")[:max_bytes_per_file]
            except OSError:
                continue
            for token in TOKEN_RE.findall(text.lower()):
                if token not in STOPWORDS and not token.isdigit():
                    counts[token] += 1
            files_read += 1
    return {"terms": dict(counts.most_common(100)), "files_read": files_read}
