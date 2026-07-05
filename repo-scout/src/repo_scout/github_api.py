from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from .config import ScoutConfig

GITHUB_API = "https://api.github.com"


@dataclass
class GitHubAPIError(Exception):
    """Structured, non-secret GitHub API error for graceful reporting."""

    status: int | None
    reason: str
    url: str
    message: str = ""
    rate_limit_limit: str | None = None
    rate_limit_remaining: str | None = None
    rate_limit_reset: str | None = None
    retry_after: str | None = None
    request_kind: str | None = None

    def __str__(self) -> str:
        if self.status is None:
            return self.reason
        return f"HTTP {self.status}: {self.reason}"

    def to_dict(self) -> dict[str, str | int | None]:
        return {
            "type": self.__class__.__name__,
            "status": self.status,
            "reason": self.reason,
            "message": self.message,
            "url": self.url,
            "rate_limit_limit": self.rate_limit_limit,
            "rate_limit_remaining": self.rate_limit_remaining,
            "rate_limit_reset": self.rate_limit_reset,
            "rate_limit_reset_utc": _format_reset(self.rate_limit_reset),
            "retry_after": self.retry_after,
            "request_kind": self.request_kind,
        }


class GitHubRateLimitError(GitHubAPIError):
    """Raised when GitHub returns a primary/secondary rate-limit response."""


def _format_reset(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except ValueError:
        try:
            return parsedate_to_datetime(value).astimezone(timezone.utc).isoformat()
        except Exception:
            return None


def _read_error_message(err: urllib.error.HTTPError) -> str:
    try:
        body = err.read().decode("utf-8", errors="replace")
    except Exception:
        return ""
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return body[:500]
    message = parsed.get("message")
    if isinstance(message, str):
        return message
    return body[:500]


def _read_token_from_hermes_env() -> str | None:
    """Read repo-scout-compatible GitHub token keys from ~/.hermes/.env.

    This is intentionally tiny and non-expanding: it accepts only literal
    GITHUB_TOKEN/GH_TOKEN values and never prints or persists them.
    """
    env_path = Path.home() / ".hermes" / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() not in {"GITHUB_TOKEN", "GH_TOKEN"}:
            continue
        token = value.strip().strip('"\'')
        if token:
            return token
    return None


def resolve_github_token(explicit: str | None = None) -> str | None:
    return explicit or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or _read_token_from_hermes_env()


def _rate_limit_sleep_seconds(error: GitHubAPIError, now: float | None = None) -> float | None:
    if error.retry_after:
        try:
            return max(0.0, float(error.retry_after))
        except ValueError:
            return None
    if error.rate_limit_reset:
        try:
            return max(0.0, float(error.rate_limit_reset) - (time.time() if now is None else now) + 1.0)
        except ValueError:
            return None
    if isinstance(error.message, str) and "secondary rate limit" in error.message.lower():
        return 60.0
    return None


def _request_kind(path_or_url: str) -> str:
    path = urllib.parse.urlparse(path_or_url).path if path_or_url.startswith("http") else path_or_url
    if path.startswith("/search/"):
        return "search"
    return "core"


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


@dataclass
class RequestPacer:
    """Small conservative pacer for uncached GitHub GET requests.

    GitHub Search has a much lower primary per-minute limit than most REST
    endpoints, and bursts across mixed endpoints can trigger secondary limits.
    The pacer only sleeps immediately before real network calls; cached reads
    remain instant.
    """

    search_interval: float
    core_interval: float
    last_request_at: dict[str, float] = field(default_factory=dict)

    def wait(self, kind: str) -> None:
        interval = self.search_interval if kind == "search" else self.core_interval
        if interval <= 0:
            return
        now = time.monotonic()
        last = self.last_request_at.get(kind)
        if last is not None:
            delay = interval - (now - last)
            if delay > 0:
                time.sleep(delay)
        self.last_request_at[kind] = time.monotonic()


class GitHubClient:
    """Small read-only GitHub API client.

    Security posture:
    - Uses only GET requests.
    - Does not clone repositories.
    - Does not execute repository code.
    - Token is read from the explicit argument, environment, or ~/.hermes/.env
      and never printed or persisted by this client.
    """

    def __init__(
        self,
        token: str | None = None,
        cache_dir: str | Path | None = None,
        cache_ttl_hours: int = 24,
        max_rate_limit_sleep: int = 75,
        search_request_interval: float | None = None,
        core_request_interval: float = 0.5,
    ):
        self.token = resolve_github_token(token)
        self.cache_dir = Path(cache_dir).expanduser() if cache_dir else None
        self.cache_ttl = cache_ttl_hours * 3600
        self.max_rate_limit_sleep = max_rate_limit_sleep
        # Authenticated GitHub Search allows 30 requests/minute; unauthenticated
        # allows 10/minute. Stay below both to avoid primary and secondary limits.
        if search_request_interval is None:
            search_request_interval = 2.2 if self.token else 6.2
        self.pacer = RequestPacer(search_interval=search_request_interval, core_interval=core_request_interval)
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def is_authenticated(self) -> bool:
        return bool(self.token)

    def _cache_path(self, url: str) -> Path | None:
        if not self.cache_dir:
            return None
        safe = urllib.parse.quote(url, safe="")[:220]
        return self.cache_dir / f"{safe}.json"

    def get_json(self, path_or_url: str) -> Any:
        if path_or_url.startswith("http"):
            parsed = urllib.parse.urlparse(path_or_url)
            if parsed.scheme != "https" or parsed.netloc != "api.github.com":
                raise ValueError("GitHubClient refuses non-api.github.com absolute URLs")
            url = path_or_url
        else:
            url = f"{GITHUB_API}{path_or_url}"
        kind = _request_kind(path_or_url)
        cache_path = self._cache_path(url)
        if cache_path and cache_path.exists() and time.time() - cache_path.stat().st_mtime < self.cache_ttl:
            try:
                return json.loads(cache_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                try:
                    cache_path.unlink()
                except OSError:
                    pass

        attempts = 0
        while True:
            attempts += 1
            req = urllib.request.Request(url)
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("X-GitHub-Api-Version", "2022-11-28")
            req.add_header("User-Agent", "hermes-repo-scout-readonly")
            if self.token:
                req.add_header("Authorization", f"Bearer {self.token}")
            self.pacer.wait(kind)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as err:
                message = _read_error_message(err)
                headers = err.headers
                payload = {
                    "status": err.code,
                    "reason": err.reason,
                    "url": url,
                    "message": message,
                    "rate_limit_limit": headers.get("X-RateLimit-Limit"),
                    "rate_limit_remaining": headers.get("X-RateLimit-Remaining"),
                    "rate_limit_reset": headers.get("X-RateLimit-Reset"),
                    "retry_after": headers.get("Retry-After"),
                    "request_kind": kind,
                }
                is_rate_limit = err.code in {403, 429} and (
                    payload["rate_limit_remaining"] == "0"
                    or "rate limit" in message.lower()
                    or "secondary rate" in message.lower()
                )
                if is_rate_limit:
                    error = GitHubRateLimitError(**payload)
                    sleep_seconds = _rate_limit_sleep_seconds(error)
                    if attempts == 1 and sleep_seconds is not None and sleep_seconds <= self.max_rate_limit_sleep:
                        time.sleep(sleep_seconds)
                        continue
                    raise error from err
                raise GitHubAPIError(**payload) from err
            except urllib.error.URLError as err:
                raise GitHubAPIError(status=None, reason="URL error", url=url, message=str(err.reason)) from err
        if cache_path:
            _atomic_write_json(cache_path, data)
        return data


def build_search_queries(cfg: ScoutConfig) -> list[str]:
    queries: list[str] = []
    language_terms = cfg.languages or [None]
    topic_or_keyword = list(dict.fromkeys([*cfg.topics, *cfg.keywords]))
    for term in topic_or_keyword:
        for lang in language_terms:
            parts = [str(term), "archived:false", "fork:false"]
            if lang:
                parts.append(f"language:{lang}")
            if cfg.min_stars:
                parts.append(f"stars:>={cfg.min_stars}")
            queries.append(" ".join(parts))
    return queries


def search_repositories(client: GitHubClient, cfg: ScoutConfig) -> list[dict]:
    seen: set[str] = set()
    repos: list[dict] = []
    queries = build_search_queries(cfg)
    pages_per_query = max(1, int(getattr(cfg, "search_pages_per_query", 1) or 1))
    per_query = max(5, min(100, cfg.max_candidates // max(1, len(queries) * pages_per_query) + 1))
    for query in queries:
        if len(repos) >= cfg.max_candidates:
            break
        for page in range(1, pages_per_query + 1):
            encoded = urllib.parse.urlencode({"q": query, "sort": "updated", "order": "desc", "per_page": per_query, "page": page})
            data = client.get_json(f"/search/repositories?{encoded}")
            items = data.get("items", []) if isinstance(data, dict) else []
            if not items:
                break
            for repo in items:
                name = repo.get("full_name")
                if name and name not in seen:
                    seen.add(name)
                    repos.append(repo)
                    if len(repos) >= cfg.max_candidates:
                        break
            if len(items) < per_query or len(repos) >= cfg.max_candidates:
                    break
    return repos


def fetch_contribution_labels(client: GitHubClient, full_name: str, cfg: ScoutConfig) -> list[str]:
    # Query labels independently so a repo with any configured contribution label
    # is detected; GitHub's comma-separated label filter behaves like an all-label
    # match and under-detects approachable issues.
    labels: set[str] = set()
    for wanted in {label.lower() for label in cfg.contribution_labels}:
        encoded_label = urllib.parse.quote(wanted)
        data = client.get_json(f"/repos/{full_name}/issues?state=open&labels={encoded_label}&per_page=5")
        if not isinstance(data, list):
            continue
        for issue in data:
            if "pull_request" in issue:
                continue
            for label in issue.get("labels", []):
                name = str(label.get("name", "")).lower()
                if name == wanted:
                    labels.add(name)
    return sorted(labels)


def fetch_recent_commit_dates(client: GitHubClient, full_name: str, months: int) -> list[str]:
    # Fetch enough pages for the 5/month x 6-month criterion with headroom.
    dates: list[str] = []
    max_pages = max(1, min(4, months))
    for page in range(1, max_pages + 1):
        data = client.get_json(f"/repos/{full_name}/commits?per_page=100&page={page}")
        if not isinstance(data, list):
            break
        for item in data:
            date = (((item.get("commit") or {}).get("committer") or {}).get("date"))
            if date:
                dates.append(date)
        if len(data) < 100:
            break
    return dates
