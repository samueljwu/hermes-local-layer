# Reading Feed Schema

> Separate personalized reading recommendation layer. Created 2026-05-10.

## Purpose

The reading feed periodically recommends external reading material based on recent interest signals. It is separate from the wiki, journal, and tasks systems.

## Hard Anti-Contamination Rule

The feed may read from these protected systems as signals:

- `/home/hermes/wiki/src`
- `/home/hermes/journal`
- `/home/hermes/tasks`

The feed may write operational state only under:

- `/home/hermes/feed`

The feed page renderer has one publication exception:

- `/home/hermes/homepage/dist/feed/index.html` — static `/feed/` page generated from feed-local recommendation history

The feed must never write to, patch, rebuild, append logs to, or mutate:

- `/home/hermes/wiki`
- `/home/hermes/wiki/src`
- `/home/hermes/journal`
- `/home/hermes/tasks`

Digest runs snapshot protected systems before and after generation using content hashes, not mtimes, so mtime-only rewrites do not fail the run. The snapshot intentionally excludes regenerated wiki semantic build artifacts under `/home/hermes/wiki/src/_meta/semantic/` except durable curation files such as `accepted-edges.json` and `rejected-edges.json`; wiki builds can rewrite the generated graph/lint/state files with timestamp churn while the feed is running, and those derived files are not canonical wiki knowledge.

If a user wants a recommendation ingested into wiki, captured in journal, or converted to a task, that is a separate explicit workflow and must switch to the relevant skill.

## Source Universe

The live source universe is `_meta/information_sources.json`. It is the only canonical source list.

`source-report.md` is a generated human-readable snapshot of the registry. README and SCHEMA must document policy, invariants, source-add workflow, and anti-contamination rules, not duplicate every source row. This prevents stale docs when individual sources are enabled, disabled, added, or removed.

No broad web search and no arbitrary website scraping unless the user explicitly expands the source universe. API-backed paper sources use native metadata APIs. Approved-source HTML/direct extraction belongs in bounded source-specific connectors. Use `blogwatcher` as the default for ordinary blog/RSS monitoring; add site-specific extractor logic only for approved-source quirks. RSS source preflight must check update freshness and candidate usefulness, not only parseability. Stale or unparseably thin RSS should not be accepted if a compact same-site direct connector is needed.

Source semantic usefulness is typed, but the matching decision is article-level. `_meta/information_sources.json` may declare `semantic_role` (`core_interest`, `adjacent_interest`, `broad_exploratory`, or `mixed`) and `expected_topics`; these fields describe what mix a source is expected to contribute, not a blanket relevance label for every article. `sources lint` classifies each sample article as `core_interest`, `adjacent_interest`, `purpose_match`, `broad_exploratory`, `weak_semantic`, or `low_semantic`, then aggregates the article labels against the declared role. A normally strong source such as Neuralink can therefore still produce a low-alignment article, and a broad exploratory source can occasionally produce an aligned article. Broad editorial feeds may be accepted for wildcard slots when multiple individual articles are well-described and low-correlation.

## Digest Shape

Each digest contains exactly five items:

- Slots 1-3: interest-related
- Slots 4-5: exploratory/unrelated

Exploratory slots are selected from low-correlation candidates where possible. They must not be tuned by explicit user scores, because scoring exploratory picks would gradually make exploration converge toward known interests. The selector should instead rank exploratory candidates by useful quality while penalizing correlation with the active interest profile. A broad, well-described item with very low semantic correlation receives a small exploratory boost; this lets feeds like generic Quanta surface wildcard science topics even when they fail interest-oriented semantic usefulness.

Each rendered pick has a stable feed-local `pick_id` of `<run_id>:<slot>`; historical rows without `pick_id` can still be resolved from `run_id` plus `slot`. Exploratory cards print the ID and a copyable backticked `/feedinterest <pick_id> ...` command so Discord/mobile users can promote a wildcard when it actually belongs near their interests.

Exploratory slots do not ask for scores. The item-level `Why it’s interesting` line must still explain the specific item — for example its core idea, active discussion, or why the topic is a useful wildcard — instead of repeating generic low-correlation boilerplate.

Discord-facing digest links must be rendered as `<https://...>` / `<http://...>` bare URLs rather than plain bare URLs, so Discord suppresses link preview cards while keeping links clickable.

## Source Diversity Selection Rule

The digest selector must avoid letting the source with the most candidate material dominate the output.

- Prefer source diversity across the five daily slots.
- For interest-related slots, first try to fill Core Picks from distinct sources. Repeated Core Pick sources are allowed only in fallback passes, either for super-high relevance (`relevance >= 3.0`) or to fill the required three interest-related slots.
- For exploratory slots, prefer no repeated sources when alternatives exist; fallback repeats are allowed only to fill the required two exploratory slots.
- The fixed digest shape remains authoritative: exactly three interest-related items and exactly two exploratory items.
- This policy applies during selection/ranking. It does not remove any approved source from the source universe.

## Freshness and Staleness Selection Rule

Core Pick selection should not drain old public-source backlog ahead of fresher, still-relevant items. For public-blog/direct/RSS candidates, the selector adds a bounded recency bonus for fresh items and applies a staleness penalty after a 45-day grace period, ramping to the full penalty by 180 days. Interest-related candidates are ranked by total score (`relevance + quality`, where quality includes freshness/staleness), while still requiring at least adjacent relevance for the primary Core Pick passes.

The staleness rule is a ranking policy, not a source removal policy. Approved older items can still appear if the current source universe cannot fill the fixed digest shape with fresher useful items, but stale backlog should not repeatedly displace newer on-topic candidates.

## Interest Feedback and Removed Score Feedback

`/feedscore` and the harness `score` subcommand are removed. Historical `_meta/feedback.json` data may remain as a legacy archive, but current profile building and exploratory selection must not read it. Delivered digests should not include score prompts.

`/feedinterest <pick_id> [known topic or note]` is allowed because it is not scalar score tuning. It records an explicit positive signal that an exploratory pick is interest-relevant. The harness command is `feed_ops.py feedback promote <pick_id> [known topic or note]`; it writes only `_meta/interest_feedback.json`, does not mutate recommendation history, and must reject non-exploratory picks. `build_profile` reads recent promoted-interest records as feed-local signals (`source_system=feed`, `kind=promoted_exploratory`, weight 0.6) alongside wiki/journal/tasks. If the argument exactly matches a curated topic, store it as `topic`; otherwise store it as a free-form `note` and let semantic matching use the note plus item title/summary.

## Canonical State Files

- `_meta/interest_profile.json` — compact operational interest profile
- `_meta/recommendation_history.json` — all recommended item IDs and run metadata; source of truth for static `/feed/` picks page
- `_meta/feedback.json` — legacy/inactive score archive; current selection does not read it
- `_meta/interest_feedback.json` — append-only positive-interest promotions from `/feedinterest`; current profile building may read it as feed-local evidence, but exploratory ranking must not treat it as score feedback
- `_meta/source_state.json` — source-specific seen/check state plus `last_fetch_errors` for upstream/API fetch failures; fetch errors are operational telemetry, not candidates, and health only reports errors that have not been superseded by a later check for the same source
- `_meta/information_sources.json` — canonical candidate/read-only source list rendered into pinned Discord #feed message
- `_meta/information_sources_message_state.json` — Discord message ID/hash state for the pinned source-list updater
- `runs/YYYY-MM-DD-HHMM.md` — rendered digest for each current twice-daily run; older `runs/YYYY-MM-DD.md` files may exist from the previous daily schedule
- `/home/hermes/homepage/dist/feed/index.html` — generated static HTML picks index, updated by successful `digest` runs and manually by `feed_ops.py render-page`; it shows the most recent 60 picks expanded, groups rows by date, provides client-side filtering over date/title only, and compacts older picks into collapsed yearly archive sections

## Health and Balance Summary

`feed_ops.py health` is a read-only operational summary for quick internal checks. It reports validation status, enabled/total sources, recommendation-history latest run, candidate-cache count and timestamp, latest run file, public page existence/freshness versus recommendation history, source contribution balance, recent source contribution counts, and active transient `source_state.json` fetch errors. A stored fetch error is considered inactive once that same source has a later `last_checked` timestamp, so one partial timeout does not keep health red after a subsequent successful source check. Use `--json` for machine-readable output. Health checks must not fetch new candidates or mutate feed state.

`feed_ops.py balance` is a focused read-only source contribution report. It summarizes top contributing sources over the last 7 and 30 days, underused enabled sources in those windows, top-source share, and repeated-source pressure within digest runs.

Discord `/feedhealth` is a read-only operational command backed by the same harness checks. It should show `feed_ops.py health` plus `feed_ops.py balance` in a compact Discord-safe response, and must not fetch new candidates or mutate feed state.

Discord `/feedinterest` is a write command limited to feed-local interest feedback. It calls the harness `feedback promote` path and returns a confirmation that wiki/journal/tasks were not changed. The running Hermes gateway must be restarted after plugin changes before the slash command is live.

## Source Add Workflow

Source additions are initiated by normal-language requests to Hermes, not by `/feedsource add-*` Discord subcommands. The agent should use the harness source-add pipeline (`sources add-url` first, `sources add-rss` as an explicit fallback): same-site discovery/preflight where applicable, strict source lint with semantic usefulness metrics, fetch validation, and pinned #feed source-message refresh. `/feedsource` remains available only for read-only source inspection (`list`, `lint`, `validate`).

## Harness

Use `/home/hermes/feed/_tools/feed_ops.py` for orientation, validation, digest generation, source management, low-correlation exploratory selection, and explicit exploratory-interest promotion. The harness has no score-capture command.
