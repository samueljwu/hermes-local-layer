from __future__ import annotations

import math
import re
from collections.abc import Mapping

from .config import ScoutConfig

WORD_RE = re.compile(r"[a-z0-9][a-z0-9_+-]{1,}")


def _text_for_repo(repo: Mapping) -> str:
    parts: list[str] = []
    for key in ("full_name", "name", "description", "language"):
        value = repo.get(key)
        if value:
            parts.append(str(value))
    parts.extend(str(t) for t in repo.get("topics") or [])
    return " ".join(parts).lower()


def rank_repo(repo: dict, interest_profile: dict, cfg: ScoutConfig, feedback_profile: dict | None = None) -> dict:
    """Return a conservative deterministic score; no network or LLM calls here."""
    text = _text_for_repo(repo)
    terms = interest_profile.get("terms") or {}
    reasons: list[str] = []
    score = 0.0

    matched_terms = []
    for term, weight in terms.items():
        term_l = str(term).lower()
        if len(term_l) < 3:
            continue
        if term_l in text:
            matched_terms.append(term_l)
            score += min(float(weight), 10.0) * 1.5
    if matched_terms:
        reasons.append("interest_match")

    labels = {str(label).lower() for label in repo.get("contribution_labels") or []}
    configured_labels = {label.lower() for label in cfg.contribution_labels}
    if labels & configured_labels:
        score += 15.0 + 3.0 * len(labels & configured_labels)
        reasons.append("contribution_labels")

    issues = int(repo.get("open_issues_count") or 0)
    if 3 <= issues <= 200:
        score += min(10.0, math.log1p(issues) * 3.0)
        reasons.append("has_open_issues")

    stars = int(repo.get("stargazers_count") or 0)
    if stars:
        # Prefer known but not impossibly crowded projects.
        score += min(12.0, math.log10(stars + 1) * 4.0)
        if stars <= 3000:
            score += 4.0
            reasons.append("approachable_size")

    topics = {str(t).lower() for t in repo.get("topics") or []}
    if topics & {topic.lower() for topic in cfg.topics}:
        score += 8.0
        reasons.append("configured_topic")

    feedback = feedback_profile or {}
    repo_feedback = float((feedback.get("repo_scores") or {}).get(str(repo.get("full_name") or "").lower(), 0.0))
    if repo_feedback:
        score += max(-18.0, min(18.0, repo_feedback * 6.0))
        reasons.append("user_feedback_positive" if repo_feedback > 0 else "user_feedback_negative")

    topic_feedback = 0.0
    topic_scores = feedback.get("topic_scores") or {}
    for topic in topics:
        topic_feedback += float(topic_scores.get(topic, 0.0))
    if topic_feedback:
        score += max(-8.0, min(8.0, topic_feedback * 1.25))
        reasons.append("user_feedback_topic")

    language_feedback = float((feedback.get("language_scores") or {}).get(str(repo.get("language") or "").lower(), 0.0))
    if language_feedback:
        score += max(-4.0, min(4.0, language_feedback * 0.75))
        reasons.append("user_feedback_language")

    ranked = dict(repo)
    ranked["score"] = round(score, 2)
    ranked["reasons"] = sorted(set(reasons))
    return ranked


def rank_repos(repos: list[dict], interest_profile: dict, cfg: ScoutConfig, feedback_profile: dict | None = None) -> list[dict]:
    return sorted(
        (rank_repo(repo, interest_profile, cfg, feedback_profile) for repo in repos),
        key=lambda r: r["score"],
        reverse=True,
    )
