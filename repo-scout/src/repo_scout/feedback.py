from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .secure_fs import open_output_directory, require_lexically_under

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SCORE_RE = re.compile(r"^[+-]?[0-3]$")
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = REPO_ROOT / "out"
LOCK_PATH = DEFAULT_OUT_DIR / ".repo-scout.lock"


@contextmanager
def repo_scout_lock():
    with open_output_directory(DEFAULT_OUT_DIR, DEFAULT_OUT_DIR) as (root_dir, _):
        with root_dir.lock(LOCK_PATH.name):
            yield


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp_score(score: int | str) -> int:
    value = int(score)
    return max(-3, min(3, value))


def parse_feedback_args(raw_args: str) -> dict[str, Any]:
    """Parse Discord feedback args without executing any repo instructions.

    Supported forms:
    - owner/repo +2 optional note
    - owner/repo -1 optional note
    - summary
    - help
    """
    args = (raw_args or "").strip()
    if not args or args.lower() in {"help", "--help", "usage"}:
        return {"command": "help"}
    if args.lower() in {"summary", "list", "stats"}:
        return {"command": "summary"}

    parts = args.split(maxsplit=2)
    if len(parts) < 2:
        return {"command": "error", "error": "Expected: owner/repo score optional-note"}
    full_name, score_text = parts[0], parts[1]
    note = parts[2].strip() if len(parts) > 2 else ""
    if not REPO_RE.match(full_name):
        return {"command": "error", "error": "Repository must look like owner/repo."}
    if not SCORE_RE.match(score_text):
        return {"command": "error", "error": "Score must be an integer from -3 to +3."}
    score = _clamp_score(score_text)
    if score == 0 and not note:
        return {"command": "error", "error": "Use a non-zero score, or include a note with score 0."}
    return {"command": "record", "full_name": full_name, "score": score, "note": note}


def _confine_feedback_path(feedback_path: str | Path, *, root: Path | None = None) -> Path:
    return require_lexically_under(Path(feedback_path), root or DEFAULT_OUT_DIR, "feedback path")


def record_feedback(
    feedback_path: str | Path,
    *,
    full_name: str,
    score: int,
    note: str = "",
    topics: list[str] | None = None,
    language: str | None = None,
    source: str = "discord",
    created_at: str | None = None,
) -> dict[str, Any]:
    if not REPO_RE.match(full_name):
        raise ValueError("full_name must look like owner/repo")
    item = {
        "created_at": created_at or _now_iso(),
        "source": source,
        "full_name": full_name,
        "score": _clamp_score(score),
        "note": str(note or "")[:500],
        "topics": [str(t).lower() for t in (topics or []) if str(t).strip()][:30],
        "language": str(language or "").strip() or None,
    }
    path = _confine_feedback_path(feedback_path)
    with repo_scout_lock():
        with open_output_directory(DEFAULT_OUT_DIR, path.parent) as (_, parent_dir):
            parent_dir.append_json_line(path.name, item)
    return item


def iter_feedback(feedback_path: str | Path, *, root: Path | None = None):
    output_root = root or DEFAULT_OUT_DIR
    path = _confine_feedback_path(feedback_path, root=output_root)
    try:
        with open_output_directory(output_root, path.parent, create=False) as (_, parent_dir):
            text = parent_dir.read_text(path.name)
    except FileNotFoundError:
        return
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and REPO_RE.match(str(item.get("full_name", ""))):
            yield item


def load_feedback_profile(feedback_path: str | Path, *, root: Path | None = None) -> dict[str, Any]:
    repo_scores: dict[str, float] = {}
    topic_scores: dict[str, float] = {}
    language_scores: dict[str, float] = {}
    count = 0
    for item in iter_feedback(feedback_path, root=root) or []:
        count += 1
        score = float(_clamp_score(item.get("score", 0)))
        repo = str(item.get("full_name", "")).lower()
        repo_scores[repo] = repo_scores.get(repo, 0.0) + score
        for topic in item.get("topics") or []:
            topic_l = str(topic).lower().strip()
            if topic_l:
                topic_scores[topic_l] = topic_scores.get(topic_l, 0.0) + score
        language = str(item.get("language") or "").lower().strip()
        if language:
            language_scores[language] = language_scores.get(language, 0.0) + score
    return {
        "count": count,
        "repo_scores": repo_scores,
        "topic_scores": topic_scores,
        "language_scores": language_scores,
    }


def summarize_feedback(feedback_path: str | Path, *, limit: int = 8) -> str:
    profile = load_feedback_profile(feedback_path)
    if not profile["count"]:
        return "No Repo Scout feedback recorded yet."

    def top_items(data: dict[str, float], reverse: bool = True) -> str:
        items = sorted(data.items(), key=lambda kv: kv[1], reverse=reverse)[:limit]
        return ", ".join(f"{name} ({score:+.0f})" for name, score in items) or "none"

    return "\n".join([
        f"Feedback records: {profile['count']}",
        f"Liked repos: {top_items(profile['repo_scores'], True)}",
        f"Downranked repos: {top_items(profile['repo_scores'], False)}",
        f"Topic signals: {top_items(profile['topic_scores'], True)}",
        f"Language signals: {top_items(profile['language_scores'], True)}",
    ])
