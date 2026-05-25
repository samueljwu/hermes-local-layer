from __future__ import annotations

from .config import ScoutConfig
from .github_api import build_search_queries


def estimate_api_budget(cfg: ScoutConfig) -> dict[str, int | str]:
    """Estimate worst-case GitHub GET request count for a live run.

    Search requests are generated query count times search_pages_per_query.
    Enrichment checks up to
    max_api_repos_for_commit_check repos, with up to min(4, commit_months)
    commit-list pages plus one contribution-label issue query per configured
    label per repo. Cache hits can reduce actual network calls, but not
    logical work.
    """
    search_queries = len(build_search_queries(cfg))
    pages_per_query = max(1, int(getattr(cfg, "search_pages_per_query", 1) or 1))
    search_requests = search_queries * pages_per_query
    repos_checked = min(cfg.max_candidates, cfg.max_api_repos_for_commit_check)
    commit_pages_per_repo = max(1, min(4, cfg.commit_months))
    commit_requests = repos_checked * commit_pages_per_repo
    label_requests = repos_checked * len(cfg.contribution_labels)
    total = search_requests + commit_requests + label_requests
    return {
        "kind": "worst_case_github_get_requests_before_cache",
        "search_queries": search_queries,
        "search_pages_per_query": pages_per_query,
        "search_requests": search_requests,
        "repos_checked_for_activity_and_labels": repos_checked,
        "commit_pages_per_repo": commit_pages_per_repo,
        "commit_requests": commit_requests,
        "contribution_label_requests": label_requests,
        "total_get_requests": total,
        "note": "Actual network calls may be lower due to cache hits and early exits when repos have fewer commit pages.",
    }
