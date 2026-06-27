# Hermes backup

Private backup of the durable local Hermes knowledge systems and the Hermes Agent operating layer around them.

This repository is designed to answer three questions quickly:

1. What local systems exist?
2. Which files are canonical versus generated or runtime-only?
3. How can the environment be operated or restored without copying secrets or volatile state?

This is not a complete clone of a live Hermes installation. It intentionally excludes credentials, live auth, sessions, caches, checkpoints, logs, process state, and most runtime databases.

## System map

```text
                         ┌──────────────────────────────┐
                         │      UPSTREAM HERMES AGENT    │
                         │ CLI · Gateway · Tools · Cron   │
                         │ Skills · Memory · Plugins      │
                         └──────────────┬───────────────┘
                                        │
                                        ▼
             ┌─────────────────────────────────────────────┐
             │        LOCAL /home/hermes CUSTOM LAYER       │
             │   domain systems + harnesses + Discord UI    │
             └──────────────────────┬──────────────────────┘
                                    │
              harness-first rule: orient → validate → mutate → verify
                                    │
┌───────────────────────────────────┼───────────────────────────────────┐
│                                   │                                   │
▼                                   ▼                                   ▼
┌──────────────┐             ┌──────────────┐                    ┌──────────────┐
│   JOURNAL    │             │    TASKS     │                    │     WIKI     │
│ thoughts     │             │ commitments  │                    │ sourced facts│
│ journal_ops  │             │ task_ops     │                    │ wiki_ops     │
└──────┬───────┘             └──────┬───────┘                    └──────┬───────┘
       │ read-only wiki context     │ task/project signals              │ concepts
       │                            │                                    │
       └────────────────────────────┼────────────────────────────────────┘
                                    ▼
                            ┌──────────────┐
                            │     FEED     │
                            │ interest map │
                            │ feed_ops     │
                            └──────┬───────┘
                                   │ picks + generated page
                                   ▼
                            ┌──────────────┐
                            │    /feed/    │
                            └──────────────┘

┌──────────────┐             ┌────────────────┐                  ┌──────────────┐
│  REPO SCOUT  │             │  STOCK SCREEN  │                  │ STATIC SITE  │
│ GitHub finds │             │  OHLCV scans   │                  │ homepage/dist│
│ read-only    │             │  /stocks/      │                  │ /wiki /feed  │
└──────┬───────┘             └───────┬────────┘                  │ /stocks     │
       │                             │                           └──────┬───────┘
       ▼                             ▼                                  ▲
┌──────────────┐             ┌────────────────┐                         │
│ #repo-scout  │             │#stock-screener │                         │
└──────────────┘             └────────────────┘                         │
                                                                        │
┌───────────────────────────────────────────────────────────────────────┴──┐
│ DISCORD WORKSPACES                                                        │
│ #journal · #wiki · #tasks · #feed · #repo-scout · #stock-screener         │
│ channel context chooses the workflow; each harness owns the safe write path│
└──────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────┐
│ SAFETY / REVIEW LAYER                                                     │
│ REVIEW_PROTOCOL.md · backup/documentation/security guards · audits         │
│ protects backups, generated artifacts, docs, routes, and subsystem borders │
└──────────────────────────────────────────────────────────────────────────┘
```

Local systems:

- `homepage/` — generated/static public artifact container for the root landing page. It is not a full subsystem. See `homepage/README.md`.
- `wiki/` — user-source-only research wiki and knowledge graph. Canonical content lives in `wiki/src/`; generated VitePress output lives in `wiki/dist/` and must not be edited directly.
- `journal/` — personal thought-capture system. Journal may consult wiki only through the read-only harness. Journal text does not automatically authorize wiki/task/feed writes.
- `tasks/` — flat-file task system. Canonical source is `tasks/_meta/task_registry.json`; derived notes, dashboard state, and reminders are generated from that registry.
- `feed/` — personalized reading feed. Source registry is `feed/_meta/information_sources.json`; feed may read wiki/journal/tasks as signals but writes only feed-local state and the generated `/feed/` page.
- `repo-scout/` — isolated GitHub project discovery tool. It is list-only/read-only toward GitHub and must not write to wiki, journal, tasks, or feed.
- `stock-screener/` — standalone weekly stock screener using exchange-derived universes, cached market data, OHLCV-only scans, and generated `/stocks/` pages.
- `.hermes/` — local Hermes operating layer: skills, scripts, hooks, plugins, cron definitions, memories, sanitized config reference, and operational docs.

Standalone project workspaces under `projects/` are intentionally out of scope for this backup repo.

## Public route source of truth

Route ownership is centralized in:

- `routes.yaml`
- `DEPLOYMENT.md`

Subsystem READMEs should link to those files rather than restating route mappings.

Current public surfaces are:

- `/` — homepage static artifact
- `/feed/` — generated feed picks page
- `/wiki/` — generated VitePress wiki
- `/stocks/` — generated stock screener pages

The exact filesystem paths are defined only in `routes.yaml`.

## Canonical files and generated files

Important canonical sources:

- `routes.yaml`
- `wiki/src/`
- `journal/_meta/entry_registry.json`
- `tasks/_meta/task_registry.json`
- `feed/_meta/information_sources.json`
- `feed/_meta/recommendation_history.json`
- `repo-scout/config.yaml`
- `stock-screener/` config and data roots documented in `stock-screener/README.md`
- `.hermes/cron/jobs.json`
- `.hermes/local_channels.yaml`
- subsystem README/SCHEMA files and governing skills

Important generated artifacts:

- `homepage/dist/`
- `homepage/dist/feed/index.html`
- `wiki/dist/`
- `stock-screener/site/dist/`
- `feed/source-report.md`
- derived task notes
- generated indexes, logs, and reports where documented by subsystem harnesses

Generated artifacts should be regenerated through the owning harness, not edited manually, unless that subsystem explicitly documents manual editing.

## Included in backup

Durable knowledge and operating procedures:

- `homepage/README.md` and tracked static artifacts under `homepage/dist/`
- wiki source, config, package metadata, and docs
- journal registry, entries, docs, and harnesses
- tasks registry, derived notes, docs, and harnesses
- feed registry, state, run logs, docs, source report, and tools
- repo-scout source, tests, config, docs, and schema
- stock-screener source, tests, configs, cached screening data, docs, and static site output
- `.hermes` skills, scripts, hooks, plugins, cron job definitions, memories, and sanitized config reference
- backup, restore, review, documentation guard, and security harness scripts

## Excluded intentionally

The backup must not contain:

- `.hermes/.env`
- `.hermes/auth.json`
- live `.hermes/config.yaml` unless sanitized as `config.example.yaml`
- API keys, OAuth tokens, private keys, cookies, or Git credentials
- sessions, checkpoints, logs, caches, cron output, pid files, or lock files
- live runtime databases such as `.hermes/state.db*`
- dependencies such as `node_modules/`
- generated build output that is explicitly excluded by policy
- `repo-scout/out/`
- `projects/`

Secrets and live authentication must be recreated outside this repository.

## Operating quick checks

Run the smallest relevant check first. For broad maintenance, run all applicable checks.

```bash
# Routes and deployment docs
cd /home/hermes
test -s routes.yaml
test -s DEPLOYMENT.md

# Wiki
/home/hermes/wiki/_tools/wiki_ops.py orient
/home/hermes/wiki/_tools/wiki_ops.py validate
cd /home/hermes/wiki && npm run lint && npm run build

# Journal
/home/hermes/journal/_tools/journal_ops.py orient
/home/hermes/journal/_tools/journal_ops.py validate

# Tasks
/home/hermes/tasks/_tools/task_ops.py orient
/home/hermes/tasks/_tools/task_ops.py validate

# Feed
/home/hermes/feed/_tools/feed_ops.py orient
/home/hermes/feed/_tools/feed_ops.py validate
/home/hermes/feed/_tools/feed_ops.py sources report
/home/hermes/feed/_tools/feed_ops.py render-page

# Repo Scout
cd /home/hermes/repo-scout
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m repo_scout.cli --dry-run --config config.yaml --out out

# Stock Screener
cd /home/hermes/stock-screener
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 scripts/validate_price_history.py

# Backup safety
/home/hermes/.hermes/scripts/backup_security_harness.py --all
```

The backup security harness reports only paths and rule names. Narrow false-positive exemptions are allowed only when path-scoped and value-prefix-specific; for example, public wiki source slugs beginning with `sk-hynix-` are exempt from the OpenAI `sk-...` token rule while real matching keys in the same files remain blocked.

## Concurrency and write safety

Shared write paths should use advisory locking plus atomic temp-file promotion.

Current important locks:

- feed: `/home/hermes/feed/.feed_ops.lock`
- repo-scout: `repo-scout/out/.repo-scout.lock`
- stock-screener weekly wrapper: documented in stock-screener scripts/README

JSON and generated page writes should use temp-file, fsync where practical, and `os.replace`.

## Documentation policy

Do not hand-maintain live lists in prose when a registry exists.

Examples:

- `routes.yaml` owns route-to-path facts.
- `feed/_meta/information_sources.json` owns the feed source universe.
- `feed/source-report.md` is generated from the feed source registry.
- `.hermes/local_channels.yaml` owns public Discord channel IDs.
- `.hermes/cron/jobs.json` owns current cron job definitions.
- `.hermes/cron/README.md` owns cron output retention policy.

When changing harness behavior, schemas, hooks, cron jobs, routing, backup scope, or subsystem boundaries, update the nearest README/SCHEMA/RESTORE/skill documentation in the same change.

## Backup automation

Scheduled private backup jobs currently run daily from the cron registry:

- `Scheduled backup` (`scheduled_backup.sh`) at `0 20 * * *` UTC.
- `Scheduled local-layer backup` (`hermes_local_layer_backup.py`) at `10 20 * * *` UTC.

The weekly full-systems review is scheduled later on Saturdays (`0 23 * * 6` UTC) to avoid overlapping the git-mutating backup window.

Commit message conventions:

- `Manual backup <UTC timestamp>`
- `Scheduled backup <UTC timestamp>`

The shared backup script is:

```bash
/home/hermes/.hermes/scripts/backup_to_github.sh
```

The backup script must run the documentation guard and security harness before pushing.

## Restore

Use `RESTORE.md` for fresh-machine recovery.

Use `DEPLOYMENT.md` and `routes.yaml` for route restoration.

Do not infer restore behavior from generated files alone.
