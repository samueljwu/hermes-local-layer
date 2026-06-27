#!/usr/bin/env python3
"""Render the public /feed/ static page from feed recommendation history."""
from __future__ import annotations

import datetime as dt
import fcntl
import html
import json
import os
import tempfile
from contextlib import contextmanager
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

FEED_ROOT = Path('/home/hermes/feed')
HISTORY_PATH = FEED_ROOT / '_meta' / 'recommendation_history.json'
OUTPUT_PATH = Path('/home/hermes/homepage/dist/feed/index.html')
RECENT_PICK_LIMIT = 60
LOCK_PATH = FEED_ROOT / '.feed_ops.lock'


def load_history(path: Path = HISTORY_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open('r', encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f'expected list in {path}')
    return [row for row in data if isinstance(row, dict)]


def is_public_http_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except Exception:
        return False
    return parsed.scheme in {'http', 'https'} and bool(parsed.netloc)


def normalized_rows(history: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in history:
        title = str(item.get('title') or '').strip()
        url = str(item.get('url') or '').strip()
        date = str(item.get('date') or '').strip()
        if not title or not is_public_http_url(url):
            continue
        rows.append({
            'date': date or 'unknown',
            'title': title,
            'url': url,
            'generated_at': str(item.get('generated_at') or ''),
            'slot': str(item.get('slot') or ''),
        })
    rows.sort(key=lambda r: (r['generated_at'], r['date'], -int(r['slot'] or '999') if (r['slot'] or '').isdigit() else -999), reverse=True)
    return rows


def rows_table_markup(rows: list[dict[str, str]], *, include_no_results: bool = False) -> str:
    if not rows:
        return '        <tr><td colspan="3" class="empty">No feed picks yet.</td></tr>'

    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    date_order: list[str] = []
    for row in rows:
        date = row['date']
        if date not in groups:
            date_order.append(date)
        groups[date].append(row)

    parts: list[str] = []
    for date in date_order:
        safe_date = html.escape(date)
        group_count = len(groups[date])
        parts.append(
            f'''        <tr class="date-group" data-group-date="{safe_date}" data-group-count="{group_count}">\n          <th scope="rowgroup" colspan="3">{safe_date} <span>{group_count} picks</span></th>\n        </tr>'''
        )
        for row in groups[date]:
            title = html.escape(row['title'])
            url = html.escape(row['url'], quote=True)
            search_text = html.escape(f"{row['date']} {row['title']}", quote=True)
            parts.append(
                f'''        <tr class="pick-row" data-date="{safe_date}" data-search="{search_text}">\n          <td class="date">{safe_date}</td>\n          <td class="title-cell"><a href="{url}" rel="noopener noreferrer">{title}</a></td>\n          <td class="link"><a href="{url}" rel="noopener noreferrer">Open</a></td>\n        </tr>'''
            )
    if include_no_results:
        parts.append('        <tr id="no-results" class="empty-row" hidden><td colspan="3" class="empty">No picks match this filter.</td></tr>')
    return '\n'.join(parts)


def archive_markup(rows: list[dict[str, str]]) -> str:
    if not rows:
        return ''
    by_year: dict[str, list[dict[str, str]]] = defaultdict(list)
    year_order: list[str] = []
    for row in rows:
        year = row['date'][:4] if row['date'][:4].isdigit() else 'unknown'
        if year not in by_year:
            year_order.append(year)
        by_year[year].append(row)
    sections = []
    for year in year_order:
        safe_year = html.escape(year)
        count = len(by_year[year])
        table = rows_table_markup(by_year[year])
        sections.append(f'''    <details class="archive-year" data-archive-year="{safe_year}">\n      <summary>{safe_year} archive <span>{count} picks</span></summary>\n      <div class="table-wrap archive-table">\n        <table>\n          <thead>\n            <tr>\n              <th scope="col">Date</th>\n              <th scope="col">Title</th>\n              <th scope="col">Link</th>\n            </tr>\n          </thead>\n          <tbody>\n{table}\n          </tbody>\n        </table>\n      </div>\n    </details>''')
    return '\n'.join(sections)


def grouped_rows_markup(rows: list[dict[str, str]]) -> tuple[str, str, int]:
    recent = rows[:RECENT_PICK_LIMIT]
    archived = rows[RECENT_PICK_LIMIT:]
    return rows_table_markup(recent, include_no_results=True), archive_markup(archived), len(archived)


def render_page(rows: list[dict[str, str]], generated_at: dt.datetime | None = None) -> str:
    generated_at = generated_at or dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    generated_label = generated_at.isoformat().replace('+00:00', 'Z')
    count = len(rows)
    latest_date = rows[0]['date'] if rows else 'none'
    rows_markup, archives_markup, archive_count = grouped_rows_markup(rows)
    archive_summary = f'{archive_count} archived' if archive_count else 'No archive yet'
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Feed Picks · Hermes</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0f1117;
      --panel: #151821;
      --panel-2: #1a1e29;
      --text: #e6e7eb;
      --muted: #9aa3b2;
      --line: #2a2f3a;
      --accent: #8ea0ff;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }}

    * {{ box-sizing: border-box; }}

    body {{
      min-height: 100vh;
      margin: 0;
      padding-top: 64px;
      background: var(--bg);
    }}

    .shell {{
      width: min(960px, calc(100vw - 24px));
      margin: 0 auto;
      padding: 14px 0 48px;
    }}

    .site-header {{ background: var(--bg); }}
    .topbar-shell {{ position: fixed; top: 0; left: 0; right: 0; z-index: 50; height: 64px; border-bottom: 1px solid var(--line); background: var(--bg); }}
    .topbar {{ display: flex; align-items: center; justify-content: space-between; gap: 24px; width: 100%; height: 64px; margin: 0 auto; padding: 0 32px; }}
    .brand {{ display: inline-flex; align-items: center; height: 64px; color: var(--text); text-decoration: none; font-size: 16px; font-weight: 600; letter-spacing: -.01em; }}
    .nav {{ display: flex; align-items: center; justify-content: flex-end; gap: 24px; height: 64px; }}
    .nav a {{ display: inline-flex; align-items: center; height: 64px; padding: 0; color: var(--muted); text-decoration: none; font-size: 13px; font-weight: 500; white-space: nowrap; }}
    .nav a:hover, .nav a:focus-visible, .nav a.active {{ color: var(--accent); outline: none; }}

    .hero {{ margin: 20px 0 18px; }}

    h1 {{
      margin: 0 0 8px;
      font-size: clamp(28px, 5vw, 42px);
      line-height: 1.05;
      letter-spacing: -0.04em;
    }}

    .sub {{
      margin: 0;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.45;
    }}

    .meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 16px 0;
    }}

    .pill {{
      display: inline-flex;
      align-items: center;
      min-height: 30px;
      padding: 5px 10px;
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      font-size: 13px;
      background: var(--panel);
    }}

    .toolbar {{ margin: 18px 0 12px; }}
    label {{ display: block; color: var(--muted); font-size: 12px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; margin-bottom: 6px; }}
    input[type="search"] {{
      width: min(100%, 520px);
      min-height: 40px;
      padding: 9px 12px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--panel);
      color: var(--text);
      font: inherit;
    }}
    input[type="search"]:focus {{ outline: 2px solid rgba(142,160,255,0.45); outline-offset: 2px; }}
    .filter-status {{ display: inline-block; margin-left: 10px; color: var(--muted); font-size: 13px; }}

    .table-wrap {{
      overflow-x: auto;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--panel);
    }}

    table {{ width: 100%; border-collapse: collapse; }}

    th, td {{
      padding: 12px 14px;
      border-top: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }}

    thead th {{
      border-top: 0;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      background: var(--panel-2);
    }}

    .date-group th {{
      color: var(--text);
      background: #11141d;
      font-size: 13px;
      font-weight: 700;
      letter-spacing: 0.01em;
    }}
    .date-group span {{ color: var(--muted); font-weight: 500; margin-left: 6px; }}
    tbody tr.pick-row:hover {{ background: rgba(255,255,255,0.025); }}

    .archive {{ margin-top: 18px; }}
    .archive-year {{
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--panel);
      margin-top: 10px;
      overflow: hidden;
    }}
    .archive-year summary {{
      cursor: pointer;
      padding: 12px 14px;
      color: var(--text);
      font-weight: 700;
      list-style-position: inside;
    }}
    .archive-year summary span {{ color: var(--muted); font-weight: 500; margin-left: 6px; }}
    .archive-table {{ border: 0; border-top: 1px solid var(--line); border-radius: 0; }}

    a {{ color: var(--accent); text-decoration: none; }}
    a:hover, a:focus-visible {{ text-decoration: underline; outline: none; }}

    .date {{
      width: 120px;
      color: var(--muted);
      white-space: nowrap;
      font-variant-numeric: tabular-nums;
    }}

    .title-cell {{ line-height: 1.35; }}
    .link {{ width: 72px; white-space: nowrap; }}
    .empty {{ color: var(--muted); text-align: center; }}

    @media (max-width: 640px) {{
      body {{ padding-top: 56px; }}
      .topbar-shell, .topbar, .brand, .nav, .nav a {{ height: 56px; }}
      .topbar {{ gap: 14px; padding: 0 14px; }}
      .brand {{ font-size: 15px; }}
      .nav {{ gap: 14px; overflow-x: auto; scrollbar-width: none; }}
      .nav::-webkit-scrollbar {{ display: none; }}
      .nav a {{ font-size: 12px; flex: 0 0 auto; }}
      .shell {{ width: min(100% - 18px, 960px); padding-top: 12px; }}
      th, td {{ padding: 10px; }}
      .date {{ width: 96px; }}
      .link {{ display: none; }}
      .filter-status {{ display: block; margin: 8px 0 0; }}
    }}
  </style>
</head>
<body>
  <header class="site-header">
    <div class="topbar-shell">
      <div class="topbar">
        <a class="brand" href="/">Hermes</a>
        <nav class="nav" aria-label="Main sections"><a href="/">Home</a><a href="/wiki/">Wiki</a><a href="/stocks/">Stocks</a><a class="active" href="/feed/">Feed</a></nav>
      </div>
    </div>
  </header>
  <main class="shell">
    <section class="hero">
      <h1>Feed Picks</h1>
      <p class="sub">All items recommended by the reading feed. This page lists only the pick date, title, and link.</p>
      <div class="meta" aria-label="Feed page metadata">
        <span class="pill">{count} picks</span>
        <span class="pill">Latest: {html.escape(latest_date)}</span>
        <span class="pill">Updated: {html.escape(generated_label)}</span>
        <span class="pill">Recent window: {min(count, RECENT_PICK_LIMIT)} picks</span>
        <span class="pill">{html.escape(archive_summary)}</span>
      </div>
    </section>

    <div class="toolbar">
      <label for="pick-filter">Filter picks</label>
      <input id="pick-filter" type="search" inputmode="search" autocomplete="off" placeholder="Search title or date" aria-describedby="filter-status">
      <span id="filter-status" class="filter-status">Showing {count} picks</span>
    </div>

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th scope="col">Date</th>
            <th scope="col">Title</th>
            <th scope="col">Link</th>
          </tr>
        </thead>
        <tbody>
{rows_markup}
        </tbody>
      </table>
    </div>

    <section class="archive" aria-label="Older feed picks archive">
{archives_markup}
    </section>
  </main>
  <script>
    (() => {{
      const input = document.getElementById('pick-filter');
      const status = document.getElementById('filter-status');
      const rows = Array.from(document.querySelectorAll('tr.pick-row'));
      const groups = Array.from(document.querySelectorAll('tr.date-group'));
      const noResults = document.getElementById('no-results');
      const archives = Array.from(document.querySelectorAll('details.archive-year'));
      const total = rows.length;
      const label = (n) => n === 1 ? 'pick' : 'picks';
      const apply = () => {{
        const q = input.value.trim().toLowerCase();
        let shown = 0;
        rows.forEach((row) => {{
          const visible = !q || row.dataset.search.toLowerCase().includes(q);
          row.hidden = !visible;
          if (visible) shown += 1;
        }});
        groups.forEach((group) => {{
          const date = group.dataset.groupDate;
          const anyVisible = rows.some((row) => row.dataset.date === date && !row.hidden);
          group.hidden = !anyVisible;
        }});
        archives.forEach((archive) => {{
          const anyVisible = Array.from(archive.querySelectorAll('tr.pick-row')).some((row) => !row.hidden);
          archive.hidden = !anyVisible;
          if (q && anyVisible) archive.open = true;
        }});
        if (noResults) noResults.hidden = shown !== 0 || total === 0;
        status.textContent = q ? `Showing ${{shown}} of ${{total}} ${{label(total)}}` : `Showing ${{total}} ${{label(total)}}`;
      }};
      input.addEventListener('input', apply);
      apply();
    }})();
  </script>
</body>
</html>
'''


@contextmanager
def feed_lock():
    FEED_ROOT.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open('w', encoding='utf-8') as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def assert_allowed_output_path(path: Path) -> None:
    if path != OUTPUT_PATH:
        raise RuntimeError(f'refusing to render feed page outside canonical output: {path}')
    expected_parent = OUTPUT_PATH.parent
    if not expected_parent.exists():
        expected_parent.mkdir(parents=True, exist_ok=True)
    if expected_parent.is_symlink():
        raise RuntimeError(f'refusing to render through symlinked output directory: {expected_parent}')
    try:
        if expected_parent.resolve(strict=True) != expected_parent:
            raise RuntimeError(f'refusing non-canonical feed output directory: {expected_parent}')
    except FileNotFoundError as exc:
        raise RuntimeError(f'missing feed output directory: {expected_parent}') from exc
    if path.exists() and path.is_symlink():
        raise RuntimeError(f'refusing to overwrite symlinked feed output file: {path}')


def atomic_write_text(path: Path, text: str) -> None:
    assert_allowed_output_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def render_to_file(history_path: Path = HISTORY_PATH, output_path: Path = OUTPUT_PATH, *, locked: bool = False) -> Path:
    def _render() -> Path:
        rows = normalized_rows(load_history(history_path))
        html_text = render_page(rows)
        atomic_write_text(output_path, html_text)
        return output_path
    if locked:
        return _render()
    with feed_lock():
        return _render()


def main() -> int:
    path = render_to_file()
    print(path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
