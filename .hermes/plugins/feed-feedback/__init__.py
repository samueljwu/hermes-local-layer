"""feed-feedback plugin: Discord commands for the #feed workflow.

The former /feedscore command was removed. Exploratory picks are selected for
low correlation with the active profile; /feedinterest is an explicit positive
signal for cases where a wildcard actually belongs near the user's interests.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


FEED_ROOT = Path("/home/hermes/feed")
FEED_OPS = FEED_ROOT / "_tools" / "feed_ops.py"


def _run_feed_ops(args: list[str], *, timeout: int) -> str:
    if not FEED_OPS.exists():
        return f"Feed harness not found: {FEED_OPS}"
    try:
        proc = subprocess.run(
            [str(FEED_OPS), *args],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "Feed command timed out."
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "unknown error").strip()
        return f"Feed command failed: {err}"
    return (proc.stdout or "").strip() or "OK"


def _truncate_discord(text: str, limit: int = 1850) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 80].rstrip() + "\n…truncated. Run `feed_ops.py health` or `feed_ops.py balance` for full output."


def _handle_feedhealth(raw_args: str = "") -> str:
    """Return read-only feed health plus source contribution balance."""
    args = (raw_args or "").strip().lower()
    if args in {"json", "--json"}:
        return _truncate_discord(_run_feed_ops(["health", "--json"], timeout=30))
    if args and args not in {"help", "--help"}:
        return "Usage: `/feedhealth`\nShows feed health, latest run/page status, transient fetch errors, and source contribution balance."
    if args in {"help", "--help"}:
        return "Usage: `/feedhealth`\nShows feed health, latest run/page status, transient fetch errors, and source contribution balance."
    health = _run_feed_ops(["health"], timeout=30)
    balance = _run_feed_ops(["balance"], timeout=30)
    if health.startswith("Feed command") or health.startswith("Feed harness"):
        return health
    if balance.startswith("Feed command") or balance.startswith("Feed harness"):
        return _truncate_discord(health + "\n\n" + balance)
    return _truncate_discord(health + "\n\n" + balance)


def _handle_feedinterest(raw_args: str = "") -> str:
    """Promote an exploratory pick into feed-local profile evidence."""
    args = (raw_args or "").strip()
    if not args or args in {"help", "--help"}:
        return (
            "Usage: `/feedinterest <pick_id> [known topic or note]`\n"
            "Example: `/feedinterest 2026-05-21-1807:4 AI agents and developer tooling`\n"
            "Records positive interest feedback for an exploratory pick without writing to wiki/journal/tasks."
        )
    parts = args.split(maxsplit=1)
    pick_id = parts[0]
    detail = parts[1] if len(parts) > 1 else ""
    return _truncate_discord(_run_feed_ops(["feedback", "promote", pick_id, detail], timeout=30))


def _handle_feedsource(raw_args: str = "") -> str:
    """Inspect #feed sources without invoking the agent loop.

    Supported forms:
    - /feedsource list
    - /feedsource validate [source_id]
    - /feedsource lint [source_id]

    Source additions intentionally go through a normal agent request so the
    agent can interpret the user's goal, choose add-url vs add-rss or a custom
    connector, update docs when behavior changes, and run the full validation
    workflow before relying on a source.
    """
    args = (raw_args or "").strip()
    if not args or args in {"help", "--help"}:
        return _feedsource_usage()
    parts = args.split()
    cmd = parts.pop(0).lower()
    if cmd in {"add", "add-url", "add-rss"}:
        return (
            "Source additions no longer use `/feedsource`. Tell Hermes what "
            "source you want to add in normal language; the agent will run the "
            "feed harness add-url/add-rss workflow, validate it, update the "
            "pinned source list, and update docs if behavior changes."
        )
    if not FEED_OPS.exists():
        return f"Feed harness not found: {FEED_OPS}"
    try:
        if cmd == "list":
            proc = subprocess.run(
                [str(FEED_OPS), "sources", "list", "--compact"],
                text=True,
                capture_output=True,
                timeout=30,
            )
        elif cmd == "validate":
            run = [str(FEED_OPS), "sources", "validate", "--limit", "2"]
            if parts:
                run += ["--id", parts[0]]
            proc = subprocess.run(run, text=True, capture_output=True, timeout=60)
        elif cmd == "lint":
            run = [str(FEED_OPS), "sources", "lint", "--limit", "5"]
            if parts:
                run += ["--id", parts[0]]
            proc = subprocess.run(run, text=True, capture_output=True, timeout=90)
        else:
            return _feedsource_usage()
    except subprocess.TimeoutExpired:
        return "Feed source command timed out."
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "unknown error").strip()
        return f"Feed source command failed: {err}"
    return (proc.stdout or "").strip() or "OK"


def _feedsource_usage() -> str:
    return (
        "Usage:\n"
        "- `/feedsource list`\n"
        "- `/feedsource validate [source_id]`\n"
        "- `/feedsource lint [source_id]`\n"
        "To add a source, tell Hermes what source you want in normal language. "
        "The agent will use the feed harness, validate quality/usefulness, "
        "refresh the pinned #feed source list, and update docs when needed."
    )


def register(ctx) -> None:
    ctx.register_command(
        "feedhealth",
        handler=_handle_feedhealth,
        description="Show read-only #feed operational health and source balance.",
    )
    ctx.register_command(
        "feedinterest",
        handler=_handle_feedinterest,
        description="Promote an exploratory #feed pick into interest-profile evidence.",
        args_hint="<pick_id> [known topic or note]",
    )
    ctx.register_command(
        "feedsource",
        handler=_handle_feedsource,
        description="List, lint, or validate approved #feed sources.",
        args_hint="list | lint [id] | validate [id]",
    )
