from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import ScoutConfig, load_config
from .filters import has_min_commits_each_month, passes_hard_filters
from .github_api import GitHubAPIError, GitHubClient, fetch_contribution_labels, fetch_recent_commit_dates, search_repositories
from .feedback import load_feedback_profile
from .interests import build_interest_profile
from .ranking import rank_repos
from .api_budget import estimate_api_budget

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = REPO_ROOT / "out"


def _require_under(path: Path, root: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    root_resolved = root.resolve()
    if resolved == root_resolved or root_resolved in resolved.parents:
        return resolved
    raise ValueError(f"{label} must stay under {root_resolved}: {resolved}")


def resolve_output_dir(path: Path) -> Path:
    if not path.is_absolute():
        path = REPO_ROOT / path
    return _require_under(path, DEFAULT_OUT_DIR, "Repo Scout output directory")


def resolve_feedback_path(path: Path | None, out_dir: Path) -> Path | None:
    if path is None:
        return None
    if not path.is_absolute():
        path = out_dir / path
    return _require_under(path, DEFAULT_OUT_DIR, "Repo Scout feedback path")


@contextmanager
def _scout_lock(out_dir: Path):
    DEFAULT_OUT_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = DEFAULT_OUT_DIR / ".repo-scout.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError(f"refusing non-regular Repo Scout lock: {lock_path}")
        f = os.fdopen(fd, "r+", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise
    with f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _error_result(error: GitHubAPIError, cfg: ScoutConfig, out_dir: Path, started_at: datetime, counts: dict | None = None) -> dict:
    result = {
        "mode": "live-readonly-error",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "started_at": started_at.isoformat(),
        "config": cfg.__dict__,
        "counts": counts or {"candidates": 0, "hard_filtered": 0, "activity_validated": 0, "shortlisted": 0},
        "api_budget_estimate": estimate_api_budget(cfg),
        "error": error.to_dict(),
        "security_note": "Failure report only; no cloning, no code execution, and no GitHub write actions were attempted.",
    }
    _write_json(out_dir / "error_report.json", result)
    return result


def run_scout(config_path: Path, out_dir: Path, dry_run: bool = False, feedback_path: Path | None = None) -> dict:
    out_dir = resolve_output_dir(out_dir)
    feedback_path = resolve_feedback_path(feedback_path, out_dir)
    with _scout_lock(out_dir):
        return _run_scout_locked(config_path, out_dir, dry_run=dry_run, feedback_path=feedback_path)


def _run_scout_locked(config_path: Path, out_dir: Path, dry_run: bool = False, feedback_path: Path | None = None) -> dict:
    cfg = load_config(config_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    interest_profile = build_interest_profile([Path(p) for p in cfg.interest_roots])
    feedback_path = feedback_path or out_dir / "feedback.jsonl"
    feedback_profile = load_feedback_profile(feedback_path)
    client = GitHubClient(cache_dir=out_dir / "_cache", cache_ttl_hours=cfg.cache_ttl_hours)

    if dry_run:
        api_estimate = estimate_api_budget(cfg)
        result = {
            "mode": "dry-run",
            "config": cfg.__dict__,
            "interest_profile_terms": list((interest_profile.get("terms") or {}).items())[:20],
            "api_budget_estimate": api_estimate,
            "github_auth": {"token_present": client.is_authenticated, "accepted_env_vars": ["GITHUB_TOKEN", "GH_TOKEN"]},
            "feedback": {"path": str(feedback_path), "records": feedback_profile.get("count", 0)},
            "security_note": "Dry run performs no GitHub calls, no cloning, and no code execution.",
        }
        _write_json(out_dir / "dry_run.json", result)
        return result

    now = datetime.now(timezone.utc)
    counts = {"candidates": 0, "hard_filtered": 0, "activity_validated": 0, "shortlisted": 0}
    try:
        candidates = search_repositories(client, cfg)
        counts["candidates"] = len(candidates)
        hard_filtered = [repo for repo in candidates if passes_hard_filters(repo, cfg, now=now)]
        counts["hard_filtered"] = len(hard_filtered)

        enriched: list[dict] = []
        for repo in hard_filtered[: cfg.max_api_repos_for_commit_check]:
            full_name = repo["full_name"]
            commit_dates = fetch_recent_commit_dates(client, full_name, cfg.commit_months)
            if not has_min_commits_each_month(
                commit_dates,
                cfg.commit_months,
                cfg.min_commits_per_month,
                now=now,
                include_current_month=cfg.include_current_month,
            ):
                continue
            compact = {
                "full_name": full_name,
                "html_url": repo.get("html_url"),
                "description": repo.get("description"),
                "language": repo.get("language"),
                "topics": repo.get("topics") or [],
                "stargazers_count": repo.get("stargazers_count") or 0,
                "open_issues_count": repo.get("open_issues_count") or 0,
                "pushed_at": repo.get("pushed_at"),
                "license": repo.get("license"),
                "contribution_labels": fetch_contribution_labels(client, full_name, cfg),
            }
            enriched.append(compact)
            counts["activity_validated"] = len(enriched)
    except GitHubAPIError as error:
        return _error_result(error, cfg, out_dir, now, counts)

    ranked = rank_repos(enriched, interest_profile, cfg, feedback_profile)[: cfg.shortlist_size]
    counts["shortlisted"] = len(ranked)
    result = {
        "mode": "live-readonly",
        "generated_at": now.isoformat(),
        "counts": counts,
        "api_budget_estimate": estimate_api_budget(cfg),
        "feedback": {"path": str(feedback_path), "records": feedback_profile.get("count", 0)},
        "shortlist": ranked,
    }
    _write_json(out_dir / "shortlist.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only GitHub repository scout for contribution candidates")
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML")
    parser.add_argument("--out", default="out", help="Output/cache directory")
    parser.add_argument("--dry-run", action="store_true", help="No GitHub calls; show config and budget estimates only")
    parser.add_argument("--feedback", default=None, help="Feedback JSONL path used to tune deterministic ranking")
    args = parser.parse_args(argv)

    result = run_scout(Path(args.config), Path(args.out), dry_run=args.dry_run, feedback_path=Path(args.feedback) if args.feedback else None)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result.get("mode") == "live-readonly-error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
