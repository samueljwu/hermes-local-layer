#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path('/home/hermes/repo-scout')
OUT = ROOT / 'out'
FEEDBACK = OUT / 'feedback.jsonl'


def run(args: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['PYTHONPATH'] = str(ROOT / 'src')
    return subprocess.run(
        args,
        cwd=str(ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=1200,
        check=False,
    )


def load_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


URL_RE = re.compile(r"(?<![<\(])https?://[^\s<>)]+")


def fmt_url(url: str | None) -> str:
    clean = (url or 'https://github.com').strip().strip('<>')
    return f'<{clean}>'


def suppress_url_previews(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        url = match.group(0)
        suffix = ''
        while url and url[-1] in '.,;:!?)':
            suffix = url[-1] + suffix
            url = url[:-1]
        return f'<{url}>{suffix}'

    return URL_RE.sub(repl, text)


def fmt_repo(item: dict, idx: int) -> str:
    name = item.get('full_name') or 'unknown/repo'
    url = fmt_url(item.get('html_url') or f'https://github.com/{name}')
    desc = suppress_url_previews((item.get('description') or 'No description.').strip())
    if len(desc) > 160:
        desc = desc[:157].rstrip() + '...'
    score = item.get('score', '?')
    stars = item.get('stargazers_count', 0)
    issues = item.get('open_issues_count', 0)
    pushed = str(item.get('pushed_at') or 'unknown')[:10]
    return f"{idx}. {name} — {url} — score {score}; {stars} stars; {issues} issues; pushed {pushed}\n   {desc}"


def main() -> int:
    config = 'config.yaml'
    dry_cmd = [sys.executable, '-m', 'repo_scout.cli', '--dry-run', '--config', config, '--out', str(OUT), '--feedback', str(FEEDBACK)]
    live_cmd = [sys.executable, '-m', 'repo_scout.cli', '--config', config, '--out', str(OUT), '--feedback', str(FEEDBACK)]
    dry = run(dry_cmd)
    if dry.returncode != 0:
        print('Repo Scout weekly run failed during dry-run.')
        print((dry.stderr or dry.stdout or 'unknown error')[-1200:])
        return dry.returncode or 1
    dry_result = load_json(dry.stdout)
    if not ((dry_result.get('github_auth') or {}).get('token_present')):
        config = 'config.smoke.yaml'
        dry_cmd = [sys.executable, '-m', 'repo_scout.cli', '--dry-run', '--config', config, '--out', str(OUT), '--feedback', str(FEEDBACK)]
        live_cmd = [sys.executable, '-m', 'repo_scout.cli', '--config', config, '--out', str(OUT), '--feedback', str(FEEDBACK)]
        dry = run(dry_cmd)
        if dry.returncode != 0:
            print('Repo Scout weekly run failed during smoke dry-run.')
            print((dry.stderr or dry.stdout or 'unknown error')[-1200:])
            return dry.returncode or 1
    live = run(live_cmd)
    result = load_json(live.stdout)
    if live.returncode != 0 or result.get('mode') != 'live-readonly':
        print('Repo Scout weekly run failed during live read-only scan.')
        err = result.get('error') or {}
        if err:
            print(f"Error: {err.get('type', 'GitHubAPIError')} {err.get('status')}: {err.get('message') or err.get('reason')}")
            if err.get('rate_limit_reset_utc'):
                print(f"Rate-limit reset: {err['rate_limit_reset_utc']}")
        else:
            print((live.stderr or live.stdout or 'unknown error')[-1200:])
        return live.returncode or 1

    counts = result.get('counts') or {}
    feedback = result.get('feedback') or {}
    shortlist = result.get('shortlist') or []
    lines = [
        'Repo Scout weekly run complete.',
        f"Status: {counts.get('candidates', 0)} candidates | {counts.get('hard_filtered', 0)} hard-filtered | {counts.get('activity_validated', 0)} activity-validated | {counts.get('shortlisted', 0)} shortlisted",
        f"Feedback records applied: {feedback.get('records', 0)}",
        'Safety: read-only GitHub API; no cloning; no candidate code execution; no GitHub writes.',
        '',
    ]
    if shortlist:
        lines.append('Top suggestions:')
        lines.extend(fmt_repo(item, i) for i, item in enumerate(shortlist[:10], 1))
        lines.append('')
        lines.append('Give feedback: `/scoutfeedback owner/repo +2 note` or `/scoutfeedback owner/repo -1 note`.')
    else:
        lines.append('No repositories made the shortlist this run.')
    print('\n'.join(lines))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
