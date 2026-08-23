# Hermes Local System Review Protocol

This protocol governs audits, maintenance, and pre-publication review for the local Hermes systems under `/home/hermes`.

Use this file for Hermes-local system boundaries and validation policy. Use the `requesting-code-review` skill for generic pre-commit code review mechanics.

## Scope

In scope:

- `/home/hermes/homepage`
- `/home/hermes/wiki`
- `/home/hermes/journal`
- `/home/hermes/tasks`
- `/home/hermes/task-completion-report` (generated projection; canonical sources remain under `tasks/`)
- `/home/hermes/feed`
- `/home/hermes/repo-scout`
- `/home/hermes/stock-screener`
- `/home/hermes/.hermes` operational layer
- backup, restore, cron, hooks, plugins, skills, and local scripts that operate these systems

Out of scope by default:

- `/home/hermes/projects/*`
- `/home/hermes/task-completion-analysis` (superseded ignored prototype; maintained task reporting lives under `tasks/` and generated `task-completion-report/`)

Project workspaces are isolated future/public repositories. A full-system Hermes review may verify that `projects/` is ignored and excluded, but must not inspect, stage, validate, or modify project contents unless the user explicitly requests a project-scoped review.

## Canonical sources of truth

Do not duplicate these facts in review notes or docs.

Routes:

- `routes.yaml`
- `DEPLOYMENT.md`

Feed sources:

- `feed/_meta/information_sources.json`
- `feed/source-report.md` is generated from the registry

Tasks:

- `tasks/_meta/task_registry.json`

Journal:

- `journal/_meta/entry_registry.json`

Cron:

- `.hermes/cron/jobs.json`
- `.hermes/cron/README.md` for retention policy

Discord public channel IDs:

- `.hermes/local_channels.yaml`

Hermes operational classification:

- `.hermes/README.md`

Subsystem contracts:

- each subsystem README/SCHEMA
- relevant skills under `.hermes/skills/`

## Boundary rules

Wiki:

- user-source-only
- no outside-web facts unless explicitly provided/requested by the user
- `wiki/dist` is generated and must not be edited directly

Journal:

- thought-capture only
- journal text does not imply implementation authority
- journal may consult wiki only through the read-only harness

Tasks:

- task registry is canonical
- derived notes and dashboard/reminders must match registry state

Feed:

- may read wiki/journal/tasks as signals
- writes only feed-local state plus `homepage/dist/feed/index.html`
- source universe comes from `information_sources.json`
- source docs must not hand-maintain full live source lists
- digest shape and anti-contamination rules must remain documented in feed README/SCHEMA/skill

Repo Scout:

- GitHub discovery/list-only
- read-only GitHub API behavior
- no cloning candidate repos
- no installing candidate dependencies
- no running candidate code
- no GitHub write actions
- writes only repo-scout-local output/cache

Stock Screener:

- standalone from wiki/journal/tasks/feed
- exchange-derived universe
- cached provider data before scanning
- OHLCV/price-pattern based ranking
- generated `/stocks/` pages owned by stock-screener workflow

Homepage:

- generated/static artifact container
- not a full subsystem
- route ownership comes from `routes.yaml`

`.hermes`:

- distinguish durable source, durable private config, volatile runtime, generated cache, and backup-excluded state
- secrets must remain excluded

## Review profiles

Choose the cheapest profile that covers the risk.

Quick review:
Use for docs-only, generated artifact sanity, small config-only changes, or targeted test updates.

Required:

- git status
- relevant file diff
- secret scan over changed files
- directly relevant validation command

Standard review:
Use for normal executable code changes.

Required:

- quick review requirements
- targeted unit tests/lints
- syntax/compile checks
- docs/skill update if behavior changed
- independent code review of final diff

High-risk review:
Use for auth, secrets, permissions, shell/filesystem/network boundaries, parsers, migrations, cron, backup, hooks, generated artifacts, locking, or cross-system writes.

Required:

- standard review requirements
- boundary audit
- lock/idempotency audit
- focused security scan
- backup/documentation guards where relevant

Full-system review:
Use only when explicitly requested for broad Hermes maintenance/audit.

Required:

- all phases in this protocol
- full validation matrix
- backup security harness
- final publication report

## Phase 1: orientation

Record:

```bash
cd /home/hermes
git status --short --untracked-files=all
git branch --show-current
git remote -v
```

Do not assume the working tree is clean. Preserve unrelated local work.

The documentation guard also inventories non-hidden top-level directories so a new first-class system cannot disappear behind the repository's deny-by-default `.gitignore`; every new root must be explicitly classified before backup.

Read or inspect:

- `README.md`
- `RESTORE.md`
- `REVIEW_PROTOCOL.md`
- `DEPLOYMENT.md`
- `routes.yaml`
- `.hermes/README.md`
- `.hermes/local_channels.yaml`
- `.hermes/cron/README.md`
- relevant subsystem README/SCHEMA files

Run compact orientations where available:

```bash
/home/hermes/tasks/_tools/task_ops.py orient
/home/hermes/journal/_tools/journal_ops.py orient
/home/hermes/wiki/_tools/wiki_ops.py orient
/home/hermes/feed/_tools/feed_ops.py orient
```

## Phase 2: invariant audit

Check system invariants before editing.

Tasks:

- registry is valid JSON
- IDs/status/priority/date/reminders are valid
- derived notes match registry
- no stale derived notes
- no path traversal from task fields
- weekly completion feedback is written only through the task harness under the task lock
- the weekly report stages a complete JSON/CSV/SVG/PNG bundle and serializes promotion

Journal:

- registry is valid JSON
- entry files match registry
- no stale files
- tag paths are safe
- `#journal` write boundary remains intact

Wiki:

- source/build/index consistency
- no direct `dist` edits
- links/frontmatter/tags valid
- user-source-only rule respected

Feed:

- validation passes
- source registry is current source of truth
- source report regenerates
- dry-run digest does not mutate protected roots
- feed writes are lock-protected and atomic where shared
- generated `/feed/` page comes from feed-local recommendation history

Repo Scout:

- read-only/list-only GitHub behavior
- no candidate clone/install/execute behavior
- output/cache writes are repo-scout-local
- overlapping scheduled/interactive writes are serialized

Stock Screener:

- local cached OHLCV inputs are used for scans
- partial provider failures do not destroy valid cache
- cron wrapper uses lock
- generated pages match documented workflow

Routes:

- subsystem docs link to `DEPLOYMENT.md`/`routes.yaml`
- route mappings are not redefined inconsistently in READMEs

`.hermes`:

- volatile runtime paths are classified
- cron output retention is documented
- channel IDs come from `local_channels.yaml` where practical
- hooks use shared utilities for common safety logic where practical

## Phase 3: implementation policy

Before modifying files, write a minimal plan:

- finding
- severity
- files affected
- minimal fix
- verification command

Implement immediately only:

- critical/high correctness fixes
- security boundary fixes
- data corruption fixes
- automation hazards
- contract drift caused by the current change

Defer unless explicitly approved:

- semantic wiki content improvements
- feed ranking judgment changes
- stock candidate judgment changes
- broad refactors unrelated to correctness/security
- skills curation that is not needed for current correctness

## Phase 4: write-safety requirements

Shared outputs must use one of:

- `fcntl.flock` advisory lock
- `flock` wrapper
- per-run temp dir plus atomic promotion

Shared JSON/text/page writes should use:

- temp file in same filesystem
- flush/fsync where practical
- `os.replace` rename/promotion

Review these especially:

- `feed/_meta/*.json`
- `feed/log.md`
- `feed/runs/*.md`
- `homepage/dist/feed/index.html`
- `repo-scout/out/**`
- `repo-scout/out/_cache/**`
- repo-scout feedback files
- stock-screener generated pages/cache
- `.hermes` cron/hook/plugin state

## Phase 5: validation matrix

Run relevant checks for the changed scope.

Core docs/routes:

```bash
cd /home/hermes
test -s routes.yaml
test -s DEPLOYMENT.md
```

Wiki:

```bash
/home/hermes/wiki/_tools/wiki_ops.py validate
cd /home/hermes/wiki && npm run lint && npm run build
```

Journal:

```bash
/home/hermes/journal/_tools/journal_ops.py validate
```

Tasks:

```bash
/home/hermes/tasks/_tools/task_ops.py validate
python3 /home/hermes/tasks/_tools/test_task_ops.py
/home/hermes/.hermes/scripts/weekly_task_completion_report.sh
test -s /home/hermes/task-completion-report/latest_report.json
test -s /home/hermes/task-completion-report/weekly_completed_tasks_last_10_weeks.png
```

Feed:

```bash
/home/hermes/feed/_tools/feed_ops.py validate
/home/hermes/feed/_tools/feed_ops.py sources report
/home/hermes/feed/_tools/feed_ops.py fetch --no-save
/home/hermes/feed/_tools/feed_ops.py digest --dry-run
/home/hermes/feed/_tools/feed_ops.py render-page
```

Repo Scout:

```bash
cd /home/hermes/repo-scout
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m repo_scout.cli --dry-run --config config.yaml --out out
```

Stock Screener:

```bash
cd /home/hermes/stock-screener
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 scripts/validate_price_history.py
```

Backup/security:

```bash
cd /home/hermes
/home/hermes/.hermes/scripts/backup_documentation_guard.py --staged
/home/hermes/.hermes/scripts/backup_security_harness.py --all
```

Use targeted subsets for quick/standard reviews, but high-risk/full-system reviews should run the full relevant matrix.

## Phase 6: static/security scans

Review changed files for:

- secrets
- token-like strings
- `shell=True` or command interpolation
- unsafe path joins
- symlink escape risks
- `eval`/`exec`
- unsafe deserialization
- SQL/GraphQL interpolation
- XSS sinks in generated HTML
- network calls without timeout/bounds
- untrusted content treated as instructions

Report file/path/pattern only. Do not print secret values.

## Phase 7: documentation gate

If behavior changed, update docs in the same change.

Required documentation targets may include:

- subsystem `README.md`
- subsystem `SCHEMA.md`
- `RESTORE.md`
- `DEPLOYMENT.md`
- `routes.yaml`
- `.hermes/README.md`
- `.hermes/cron/README.md`
- relevant skill `SKILL.md`
- generated reports such as `feed/source-report.md`

Do not update docs by copying live registries into prose. Link to or regenerate from the source of truth.

## Phase 8: final review and publication

Before commit/push:

```bash
cd /home/hermes
git diff --check
git diff --cached --check
git status --short --untracked-files=all
```

Run an independent code review over the final diff for standard/high-risk/full-system changes.

Commit only the intended files.

Commit message should be clear and maintenance-oriented.

After push, report:

- commit SHA
- branch
- changed files summary
- validation commands run
- skipped checks and why
- unresolved risks or deferred findings

## Standing severity rules

Critical/high, fix immediately:

- secret exposure
- path traversal
- symlink escape
- shell/SQL injection
- untrusted code execution
- cross-system writes outside documented roots
- generated/canonical corruption
- unsafe cron overlap
- missing locks for shared writes
- dry-runs that mutate protected state
- docs/skill contracts that contradict harness behavior

Medium/low, defer unless requested:

- wording-only improvements
- old incident-reference cleanup
- broad module splitting
- ranking/quality tuning
- non-critical refactors
- cosmetic generated-page changes
