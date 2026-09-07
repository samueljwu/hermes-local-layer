#!/usr/bin/env python3
"""Security harness for /home/hermes GitHub knowledge backups.

Checks staged/tracked backup content for:
- blocked live-state/secret paths
- obvious token/secret values in file contents
- credential-bearing git remote URLs

The harness intentionally reports file paths and rule names only. It never prints
matching secret values.
"""
from __future__ import annotations

import argparse
import io
import math
import os
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Iterable

REPO = Path("/home/hermes")
MAX_FILE_BYTES = 2_000_000
MAX_SCAN_BYTES = 100_000_000
MAX_ARCHIVE_MEMBER_BYTES = 20_000_000
MAX_ARCHIVE_TOTAL_BYTES = 100_000_000
ARCHIVE_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".zip", ".jar")
UNSUPPORTED_ARCHIVE_SUFFIXES = (".7z", ".rar", ".gz", ".bz2", ".xz")
DURABLE_STATIC_DIST_PREFIXES = (
    "homepage/dist/",
    "stock-screener/site/dist/",
)
DURABLE_STATIC_ALLOWED_SUFFIXES = {
    ".css",
    ".html",
    ".ico",
    ".js",
    ".json",
    ".svg",
    ".txt",
    ".webmanifest",
    ".xml",
}

BLOCKED_PATH_RE = re.compile(
    r"(^|/)("
    r"projects/|"
    r"pdf_audit_extract\.json$|"
    r"\.env(?:\..*)?$|"
    r"\.git-credentials$|"
    r"auth\.json$|"
    r"(?:oauth[-_](?:client|token|credentials).*|client[-_]?secret.*|credentials|token|"
    r"application_default_credentials|service[-_]?account.*|.*google.*credential.*|"
    r".*google.*token.*)\.json$|"
    r"hermes-tasks-calendar/|\.config/gcloud/|"
    r"wiki/\.tmp/|"
    r".*authorization[-_]response.*|.*oauth[-_]callback.*|"
    r"config\.yaml(?:\.bak.*)?$|"
    r"\.hermes/config\.yaml(?:\.bak.*)?$|"
    r"state\.db(?:[-\w.]*)?$|"
    r"kanban\.db(?:[-\w.]*)?$|"
    r"sessions/|logs/|cache/|checkpoints/|cron/output/|pastes/|"
    r"repo-scout/out/|"
    r"node_modules/|dist/|\.vitepress/cache/|\.vitepress/\.temp/|__pycache__/|"
    r"\.hermes_history$|\.skills_prompt_snapshot\.json$|\.update_check$|"
    r"context_length_cache\.yaml$|models_dev_cache\.json$|"
    r"gateway_state\.json$|processes\.json$|channel_directory\.json$|discord_threads\.json$|"
    r"wiki-build-state\.json$|wiki-build\.lock$|interrupt_debug\.log$|"
    r".*\.lock$|.*\.pid$|.*\.pyc$|"
    r"id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?$|"
    r".*\.(?:pem|key|p12|pfx|kdbx)$"
    r")"
)

PRIVATE_GOOGLE_CALENDAR_PATHS = {
    "tasks/_tools/google_calendar_auth.py",
    "tasks/_tools/google_calendar_sync.py",
    "tasks/_tools/test_google_calendar_auth.py",
    "tasks/_tools/test_google_calendar_sync.py",
    ".hermes/scripts/sync_hermes_tasks_calendar.py",
}

SECRET_VALUE_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("github_pat", re.compile(r"github_pat_[A-Za-z0-9_]{40,}")),
    ("github_classic_pat", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{30,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{30,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("discord_bot_token_like", re.compile(r"\b[MN][A-Za-z\d_-]{23,27}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{25,45}\b")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |)PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
]

ASSIGNMENT_SECRET_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|token|secret|password|passwd|private[_-]?key|client[_-]?secret)\b"
    r"\s*[:=]\s*"
    r"['\"]?"
    r"(?!REDACTED\b|redacted\b|xxxx|xxx|example\b|placeholder\b|<[^>]+>|\$\{[^}]+\})"
    r"[A-Za-z0-9_./+=:@%!-]{20,}"
)

ALLOW_FALSE_POSITIVE_PATHS = {
    # Documentation files can describe generic credential workflows, but they
    # are still scanned for provider-specific token formats above.
}

# Public article/source slugs can contain company names that look like provider
# token prefixes. Keep exemptions token-specific, value-prefix-specific, and
# path-scoped so real keys in the same file are still caught.
ALLOW_SECRET_VALUE_PREFIXES: dict[str, tuple[tuple[str, str], ...]] = {
    "openai_key": (
        ("wiki/", "sk-hynix-"),
        ("feed/_meta/candidates.json", "sk-telecom-"),
    ),
}


def allowed_secret_value_false_positive(path: str, rule_name: str, token: str) -> bool:
    return any(
        path.startswith(path_prefix) and token.startswith(value_prefix)
        for path_prefix, value_prefix in ALLOW_SECRET_VALUE_PREFIXES.get(rule_name, ())
    )


def git(args: list[str], *, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=REPO,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(proc.returncode)
    return proc.stdout


def nul_split(data: str) -> list[str]:
    return [p for p in data.split("\0") if p]


def staged_paths() -> list[str]:
    return nul_split(git(["diff", "--cached", "--name-only", "-z"]))


def tracked_paths() -> list[str]:
    return nul_split(git(["ls-files", "-z"]))


def path_is_blocked(path: str) -> bool:
    if path in PRIVATE_GOOGLE_CALENDAR_PATHS:
        return True
    allowed_config_paths = {
        "repo-scout/config.yaml",
    }
    if path in allowed_config_paths:
        return False
    # Some static-site outputs are durable published artifacts in this backup
    # repo, unlike generic package build directories. Exempt only their `dist/`
    # segment from the broad build-output rule; keep every other blocked-path
    # rule active inside those trees (for example `.env`, keys, locks, pyc).
    for prefix in DURABLE_STATIC_DIST_PREFIXES:
        if path.startswith(prefix):
            scrubbed = prefix.replace("dist/", "__durable_static_dist__/") + path[len(prefix):]
            return bool(BLOCKED_PATH_RE.search(scrubbed))
    return bool(BLOCKED_PATH_RE.search(path))


def durable_static_dist_issue(path: str, *, staged: bool = False) -> str | None:
    """Return a path-only finding for unsafe durable static-site artifacts.

    The backup intentionally keeps a few generated public static trees. Those
    exemptions must stay narrow: text-like web artifacts only, no symlinks, and
    no oversized/binary blobs that would bypass content scanning.
    """
    if not any(path.startswith(prefix) for prefix in DURABLE_STATIC_DIST_PREFIXES):
        return None
    suffix = Path(path).suffix.lower()
    if suffix not in DURABLE_STATIC_ALLOWED_SUFFIXES:
        return f"{path}: durable static artifact extension not allowed"
    try:
        if staged:
            meta = git(["ls-files", "--stage", "--", path], check=False).strip()
            if meta.startswith("120000 "):
                return f"{path}: durable static artifact symlink not allowed"
            size = git(["cat-file", "-s", f":{path}"], check=False).strip()
            if size and int(size) > MAX_FILE_BYTES:
                return f"{path}: durable static artifact too large"
            data = subprocess.run(
                ["git", "show", f":{path}"],
                cwd=REPO,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            ).stdout[:4096]
        else:
            full = REPO / path
            if full.is_symlink():
                return f"{path}: durable static artifact symlink not allowed"
            if full.exists() and full.stat().st_size > MAX_FILE_BYTES:
                return f"{path}: durable static artifact too large"
            data = full.read_bytes()[:4096] if full.is_file() else b""
    except (OSError, ValueError):
        return f"{path}: durable static artifact unreadable"
    if b"\x00" in data:
        return f"{path}: durable static artifact binary content not allowed"
    return None


def entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = {ch: s.count(ch) for ch in set(s)}
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def suspicious_high_entropy_assignments(text: str) -> bool:
    # Arbitrary binary bytes can accidentally resemble assignment syntax. Exact
    # provider-token rules still scan binary payloads, but this text heuristic is
    # meaningful only for text-like content.
    if "\x00" in text[:4096]:
        return False
    # Secondary heuristic for non-provider-specific secrets. Reports only when
    # a secret-ish key is assigned a long, high-entropy value. Some documentation
    # mirrors public image URLs that contain query params like ?token=...; those
    # are not credentials for this repo and would otherwise dominate results.
    sanitized = re.sub(r"([?&](?:amp;|#x26;)?token=)[^\s\"'<>]+", r"\1URLTOKEN", text, flags=re.IGNORECASE)
    for m in ASSIGNMENT_SECRET_RE.finditer(sanitized):
        token = re.split(r"[:=]", m.group(0), maxsplit=1)[-1].strip().strip("'\"")
        if len(token) >= 24 and entropy(token) >= 3.6:
            return True
    return False


def _archive_suffix(path: str) -> str | None:
    lower = path.lower()
    return next((suffix for suffix in ARCHIVE_SUFFIXES if lower.endswith(suffix)), None)


def _archive_kind(path: str, data: bytes) -> str | None:
    """Identify supported archives by content, not only attacker-controlled names."""
    # is_zipfile locates the central directory and therefore also catches
    # prefixed/self-extracting ZIPs whose first bytes are not a ZIP signature.
    if zipfile.is_zipfile(io.BytesIO(data)):
        return "zip"
    if data.startswith(b"\x1f\x8b") or (len(data) > 262 and data[257:262] == b"ustar"):
        return "tar"
    suffix = _archive_suffix(path)
    if suffix:
        return "zip" if suffix in {".zip", ".jar"} else "tar"
    return None


def _scan_archive_bytes(path: str, data: bytes, kind: str) -> tuple[list[str], str | None]:
    """Return decoded archive member payloads, or a path-only fail-closed error."""
    texts: list[str] = []
    total = 0
    try:
        if kind == "zip":
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                members = ((info.filename, info.file_size, archive.open(info)) for info in archive.infolist() if not info.is_dir())
                for name, size, stream in members:
                    if _archive_suffix(name):
                        return [], "nested archive not safely scannable"
                    if size > MAX_ARCHIVE_MEMBER_BYTES or total + size > MAX_ARCHIVE_TOTAL_BYTES:
                        return [], "archive scan limit exceeded"
                    payload = stream.read(MAX_ARCHIVE_MEMBER_BYTES + 1)
                    stream.close()
                    if len(payload) != size:
                        return [], "archive member unreadable"
                    if _archive_kind(name, payload):
                        return [], "nested archive not safely scannable"
                    total += size
                    texts.append(name.decode("utf-8", errors="ignore") if isinstance(name, bytes) else name)
                    texts.append(payload.decode("utf-8", errors="ignore"))
        else:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
                for info in archive:
                    if not info.isfile():
                        continue
                    if _archive_suffix(info.name):
                        return [], "nested archive not safely scannable"
                    if info.size > MAX_ARCHIVE_MEMBER_BYTES or total + info.size > MAX_ARCHIVE_TOTAL_BYTES:
                        return [], "archive scan limit exceeded"
                    stream = archive.extractfile(info)
                    if stream is None:
                        return [], "archive member unreadable"
                    payload = stream.read(MAX_ARCHIVE_MEMBER_BYTES + 1)
                    if len(payload) != info.size:
                        return [], "archive member unreadable"
                    if _archive_kind(info.name, payload):
                        return [], "nested archive not safely scannable"
                    total += info.size
                    texts.append(info.name)
                    texts.append(payload.decode("utf-8", errors="ignore"))
    except (OSError, tarfile.TarError, zipfile.BadZipFile, RuntimeError):
        return [], "invalid or unreadable archive"
    return texts, None


def read_payloads_for_scan(
    path: str, *, staged: bool = False, tracked_index: bool = False
) -> tuple[list[str], str | None]:
    """Read every byte that can carry a secret; never silently skip a file."""
    if staged or tracked_index:
        unreadable = "staged content unreadable" if staged else "tracked index content unreadable"
        size_proc = subprocess.run(
            ["git", "cat-file", "-s", f":{path}"], cwd=REPO,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, text=True,
        )
        if size_proc.returncode != 0:
            return [], unreadable
        try:
            size = int(size_proc.stdout.strip())
        except ValueError:
            return [], unreadable
        if size > MAX_SCAN_BYTES:
            return [], "content exceeds bounded scan limit"
        proc = subprocess.run(
            ["git", "show", f":{path}"], cwd=REPO,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if proc.returncode != 0:
            return [], unreadable
        data = proc.stdout
    else:
        full = REPO / path
        try:
            if not full.is_file():
                return [], "tracked content unreadable"
            if full.stat().st_size > MAX_SCAN_BYTES:
                return [], "content exceeds bounded scan limit"
            data = full.read_bytes()
        except OSError:
            return [], "tracked content unreadable"

    archive_kind = _archive_kind(path, data)
    if archive_kind:
        return _scan_archive_bytes(path, data, archive_kind)
    if path.lower().endswith(UNSUPPORTED_ARCHIVE_SUFFIXES):
        return [], "unsupported archive format"
    # Decode all bytes, including binary and files above the old size cutoff.
    # Provider tokens and assignment syntax are ASCII-compatible, while ignored
    # bytes cannot manufacture a match or expose the matching value in output.
    return [data.decode("utf-8", errors="ignore")], None


def scan_content(
    paths: Iterable[str], *, staged: bool = False, tracked_index: bool = False
) -> list[str]:
    findings: list[str] = []
    for path in sorted(set(paths)):
        texts, issue = read_payloads_for_scan(
            path, staged=staged, tracked_index=tracked_index
        )
        if issue:
            findings.append(f"{path}: content scan failed closed ({issue})")
            continue
        matched_rules: set[str] = set()
        for text in texts:
            for name, pattern in SECRET_VALUE_RULES:
                if name not in matched_rules and any(
                    not allowed_secret_value_false_positive(path, name, match.group(0))
                    for match in pattern.finditer(text)
                ):
                    findings.append(f"{path}: content rule {name}")
                    matched_rules.add(name)
            if "secret_assignment_high_entropy" not in matched_rules and suspicious_high_entropy_assignments(text):
                findings.append(f"{path}: content rule secret_assignment_high_entropy")
                matched_rules.add("secret_assignment_high_entropy")
    return findings


def scan_remote_urls() -> list[str]:
    findings: list[str] = []
    out = git(["remote", "-v"], check=False)
    for line in out.splitlines():
        # Flag any HTTPS userinfo before github.com. Credentials can appear as
        # either user:password or token-only userinfo (for example
        # https://ghp_...@github.com/org/repo.git). Report only the remote name.
        if re.search(r"https://[^\s/@]+@github\.com", line):
            name = line.split()[0] if line.split() else "remote"
            findings.append(f"git remote {name}: credential embedded in URL")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Check /home/hermes backup safety before commit/push")
    parser.add_argument("--staged", action="store_true", help="scan staged paths and content")
    parser.add_argument("--tracked", action="store_true", help="scan tracked paths and content")
    parser.add_argument("--all", action="store_true", help="scan both staged and tracked paths/content")
    parser.add_argument("--quiet", action="store_true", help="only print failures")
    args = parser.parse_args()

    if not (REPO / ".git").exists():
        print(f"ERROR: {REPO} is not a git repo", file=sys.stderr)
        return 2

    include_staged = args.staged or args.all or not args.tracked
    include_tracked = args.tracked or args.all

    staged_selected: set[str] = set(staged_paths()) if include_staged else set()
    staged_deleted = set(nul_split(git(["diff", "--cached", "--name-only", "--diff-filter=D", "-z"]))) if include_staged else set()
    # A blocked path already present in history must be removable. There is no
    # staged blob to scan, and rejecting its deletion would trap unsafe content.
    staged_selected -= staged_deleted
    tracked_selected: set[str] = set(tracked_paths()) if include_tracked else set()
    selected: set[str] = set()
    selected.update(staged_selected)
    selected.update(tracked_selected)

    findings: list[str] = []
    for path in sorted(selected):
        if path_is_blocked(path):
            findings.append(f"{path}: blocked path")
        issue = durable_static_dist_issue(path, staged=path in staged_selected)
        if issue:
            findings.append(issue)

    if staged_selected:
        findings.extend(scan_content(staged_selected, staged=True))
    if tracked_selected:
        # Scan the tracked bytes in the index, not the mutable worktree. An
        # ordinary unstaged deletion is expected backup state and must remain
        # stageable; new/modified worktree bytes are scanned after `git add`.
        findings.extend(scan_content(tracked_selected, tracked_index=True))
    findings.extend(scan_remote_urls())

    if findings:
        print("SECURITY HARNESS FAILED: refusing backup. Findings below show paths/rules only; secret values are not printed.", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1

    if not args.quiet:
        scope = []
        if include_staged:
            scope.append("staged")
        if include_tracked:
            scope.append("tracked")
        print(f"Security harness passed for {', '.join(scope)} content ({len(selected)} paths checked).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
