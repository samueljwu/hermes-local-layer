from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from .config import ScoutConfig


def parse_github_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def month_keys(now: datetime, months: int, include_current_month: bool = True) -> list[str]:
    keys: list[str] = []
    year = now.year
    month = now.month
    if not include_current_month:
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    for _ in range(months):
        keys.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return keys


def has_min_commits_each_month(
    commit_dates: list[str],
    months: int,
    min_per_month: int,
    now: datetime | None = None,
    include_current_month: bool = True,
) -> bool:
    now = now or datetime.now(timezone.utc)
    required = set(month_keys(now, months, include_current_month=include_current_month))
    counts: Counter[str] = Counter()
    for date in commit_dates:
        key = parse_github_time(date).strftime("%Y-%m")
        if key in required:
            counts[key] += 1
    return all(counts[key] >= min_per_month for key in required)


def passes_hard_filters(repo: dict, cfg: ScoutConfig, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if repo.get("archived") or repo.get("fork"):
        return False
    if repo.get("has_issues") is False:
        return False
    language = repo.get("language")
    if cfg.languages and language not in cfg.languages:
        return False
    stars = int(repo.get("stargazers_count") or 0)
    if stars < cfg.min_stars or stars > cfg.max_stars:
        return False
    pushed_at = repo.get("pushed_at")
    if not pushed_at:
        return False
    age_days = (now - parse_github_time(pushed_at)).days
    if age_days > cfg.pushed_within_days:
        return False
    license_info = repo.get("license") or {}
    spdx = license_info.get("spdx_id")
    if cfg.allowed_licenses and spdx and spdx not in cfg.allowed_licenses:
        return False
    return True
