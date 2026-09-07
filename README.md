# Hermes Local Layer

A filtered public showcase of the domain systems and operating harnesses built around Hermes Agent: knowledge, thought capture, tasks, reading recommendations, repository discovery, market-data workflows, and the glue that keeps their boundaries intact.

The useful part is not a single agent prompt. It is the surrounding design: explicit sources of truth, small subsystem interfaces, read-only cross-system access, generated views, and checks before and after writes.

This repository is an automatically generated **filtered public mirror**, not the private knowledge backup or a complete Hermes installation. It publishes selected code, tests, documentation, and harmless structural placeholders. The architecture below describes the larger local environment; the publication inventory identifies the subset actually present here. Missing registries, harnesses, configuration, and dependencies are intentional—not an invitation to reconstruct private data.

## System map

**Local architecture, not a manifest of everything included in this repository:**

```text
                     UPSTREAM HERMES AGENT
              CLI · Gateway · Tools · Cron · Plugins
                         Skills · Memory
                                │
                                ▼
                       LOCAL OPERATING LAYER
             domain harnesses · scripts · Discord workspaces
                                │
                orient → validate → mutate → verify
                                │
         ┌──────────────────────┼──────────────────────┐
         ▼                      ▼                      ▼
      JOURNAL                 TASKS                   WIKI
   personal thoughts       commitments            sourced knowledge
    journal_ops             task_ops                 wiki_ops
         │                      │                      │
         └────────────── read-only signals ────────────┘
                                │
                                ▼
                               FEED
                  source registry · recommendations
                                │
                                ▼
                        generated picks page

    TASK REGISTRY + HISTORY       REPO SCOUT          STOCK SCREENER
               │                 GitHub discovery     market-data workflow
               ▼                 read-only API        isolated data roots
    notes · reminders · reports         │                    │
               │                        ▼                    ▼
               └────────────── Discord views          generated review pages

                     STATIC PRESENTATION LAYER
               homepage · wiki site · feed · stock pages
             generated artifacts, not canonical databases

                        SAFETY / REVIEW LAYER
          subsystem validation · documentation checks · leak scans
               locks · atomic publication · boundary audits
```

Discord is an interaction surface, not the database. Channel context selects a workflow; the owning harness determines what that workflow may read and write. Likewise, a generated website is a presentation layer, not a second source of truth.

## Harness-first design

The operating rule is **orient → validate → mutate → verify**.

1. **Orient.** Read the owning subsystem's contract and current canonical state. Identify the intended operation and permitted write boundary before touching files.
2. **Validate.** Check the proposed data and output locations, not just the field being changed. Reject invalid state or unsafe paths before starting a mutation.
3. **Mutate through the owner.** Use the subsystem harness rather than editing registries, derived notes, and dashboards independently. Keep read-only consumers out of canonical write paths.
4. **Verify.** Validate the resulting state and inspect the generated output or external effect. A successful process exit alone does not establish that the intended result exists.

The full local environment uses compact interfaces such as `journal_ops`, `task_ops`, `wiki_ops`, and `feed_ops`. Their names describe the architectural entry points; their implementations are not included in this snapshot. Published tests and operating scripts expose parts of their contracts without publishing the underlying personal corpus.

### Each subsystem has a narrow job

| System | Responsibility in the local environment | Boundary |
| --- | --- | --- |
| Journal | Preserve and organize the user's thoughts, including uncertainty. | A reflection is not automatically a fact, task, or authorization to act. Wiki context is read-only. |
| Tasks | Manage commitments, due dates, recurrence, status, and completion history. | The registry owns task state; notes, reminders, dashboards, and reports are downstream views. |
| Wiki | Maintain a sourced knowledge base and graph. | Ingestion follows user-provided sources only. Generated site output is not edited as source. |
| Feed | Discover and rank reading from a source registry and local signals. | May read wiki, journal, and tasks as context; writes only feed-local state and its generated page. |
| Repo Scout | Discover and rank GitHub projects. | Discovery is list-only/read-only toward GitHub and does not write into the knowledge systems. |
| Stock Screener | Maintain market-data inputs and generate screening/review output. | Separate data and output roots; private scanning rules and calibration are outside the public showcase. |
| Homepage | Hold static presentation artifacts. | An artifact container, not another canonical subsystem. |
| Operating layer | Route requests, run scheduled wrappers, expose plugins, and perform maintenance checks. | Automation must respect the same subsystem boundaries as interactive work. |

Standalone project workspaces are outside this local-layer mirror.

## What is actually published

The mirror is deliberately uneven: a directory can contain useful tests or documentation while omitting the implementation or private inputs those files reference.

| Published area | Available material | Scope limit |
| --- | --- | --- |
| [Operating scripts](.hermes/scripts/) | Local-operation helpers, backup safety and locking code, workflow audits, reminders, report generation, scheduled wrappers, and selected regression tests. | Not the full operating layer. Some referenced helpers, live configuration, and inputs are absent. |
| [Plugins](.hermes/plugins/) | Selected command/feedback implementations and plugin manifests. | A manifest does not imply that its implementation or live platform setup is included. |
| [Wiki tooling](wiki/) | VitePress configuration, theme and graph/sidebar tooling, build helper, package metadata, selected tests, and structural source pages. | The personal wiki corpus and deployed site are not included. |
| [Journal](journal/) | Workflow documentation, selected harness/boundary tests, and structural index/log pages. | No personal entries, canonical registry, or mutation harness. |
| [Tasks](tasks/) | Workflow documentation and structural index/log pages; related consumer/report code lives under operating scripts. | No personal task registry, task notes, or task mutation harness. |
| [Feed](feed/) | Schema, selected tests for source handling, feedback, fetch boundaries, locking, read-only validation, and page-output safety; structural index/log pages. | No personal source registry, recommendation history, or feed harness. |
| [Repo Scout](repo-scout/) | Python package source, schema, tests, package metadata, and smoke configuration. | Not the live personalized configuration or discovery output. |
| [Stock Screener](stock-screener/) | Selected data-refresh and validation scripts, supporting configuration, package metadata, and safety tests. | Not a complete screener: private scanning implementation, rules, calibration, and live screening output are outside the showcase. |
| Root documentation | [Route manifest](routes.yaml), [deployment contract](DEPLOYMENT.md), and [review protocol](REVIEW_PROTOCOL.md). | These describe the local system; they do not provision a deployment. |

The local installation also has skills, memory, hooks, cron definitions, and channel configuration. This snapshot does **not** publish those collections. Do not infer that a file exists here merely because a retained local document refers to it. Structural index/log pages are placeholders, not samples of the owner's records.

## Canonical versus generated

These are **local architecture paths**, not a list of included data files:

| Canonical local source | Derived or runtime view |
| --- | --- |
| `wiki/src/` knowledge content | VitePress site, navigation, and graph artifacts |
| `journal/_meta/entry_registry.json` and original entry content | Journal index and organized views |
| `tasks/_meta/task_registry.json` | Pending-task notes, dashboard state, and reminders |
| Task registry plus completion history | Rebuildable completion-report JSON/CSV and charts |
| `feed/_meta/information_sources.json` | Generated source report |
| Feed-local recommendation history | Generated picks page |
| Repo Scout configuration and feedback | Ranked discovery output |
| Stock Screener's owning configuration and data inputs | Generated review pages |
| Local cron and channel registries | Scheduling and message-routing behavior |
| `routes.yaml` | Deployment documentation and serving configuration decisions |

Generated does not mean unimportant; it means the artifact has an owner and a regeneration path. When a task note and the registry disagree, repair the derivation—not the registry to match a stale note. When a report is wrong, inspect its canonical inputs and generator rather than hand-editing the chart.

The route contract describes `/`, `/wiki/`, `/feed/`, and `/stocks/`. See the manifest for path ownership rather than treating those URL paths as links to a deployed service. The homepage container, generated report bundles, and deployed site directories are not supplied by this mirror.

## Workflow examples

These illustrate the full local workflow, not copy-and-paste commands for this filtered checkout.

### Capture a thought without creating a commitment

The journal workflow preserves the original wording and labels interpretation separately. It can consult wiki context through a read-only boundary, but a phrase that sounds actionable does not silently become a task. Creating a commitment requires an explicit switch to the task workflow.

The public journal tests show the emphasis on proposed-state validation and protecting wiki state during read-only access.

### Complete a task, then derive the views

The task harness reads fresh registry state, validates the operation, updates canonical state and history, and regenerates pending-task views. Reminder and dashboard consumers use that state rather than maintaining independent task lists.

Completion reporting is a one-way projection of registry/history into structured exports and charts. It is rebuildable and does not become a second task system. The published report generator and tests are examples of that separation; the records needed to produce a personal report are not included.

### Recommend reading without contaminating other systems

The feed uses its own source registry and can consult knowledge, reflections, and commitments as relevance signals. Fetching, ranking, feedback, and page generation remain feed-owned operations. A recommendation does not automatically enter the wiki or task registry.

Published feed tests cover this design at several edges: canonical source selection, fetch behavior, feedback, read-only validation, and generated-page destinations.

### Discover repositories without adopting them

Repo Scout queries GitHub, applies filters and ranking, and emits discovery results. Discovery does not grant authority to clone candidates, modify remote repositories, or write recommendations into other local systems. Its package is one of the more substantial source-code examples in the mirror.

### Review changes before publication

Local maintenance starts with the smallest relevant subsystem checks. Changes to schemas, behavior, routing, or write boundaries also update their governing documentation. Backup and publication checks then review the intended file set for documentation consistency and sensitive content.

This separates a useful private operational record from a suitable public artifact: the latter needs its own scope and review, not merely the absence of API keys.

## Concurrency and write safety

Interactive requests, scheduled jobs, and asynchronous consumers can overlap. The local design uses advisory locks for shared write paths and atomic publication for files or output bundles.

- **Lock the owning resource.** Task mutations, feed operations, reports, and discovery output have their own coordination points. Private backup and public-mirror jobs also coordinate access to their shared source.
- **Validate paths as well as values.** Lock and output handling must reject unsafe symlink traversal and unexpected file types. User-private lock directories reduce exposure to other processes on the host.
- **Publish complete files.** Write a temporary file on the destination filesystem, flush it where practical, then replace the destination atomically. Readers should not observe half-written JSON or HTML.
- **Publish related outputs together.** The completion-report workflow builds a generation bundle and switches the active generation rather than exposing a mixture of old and new report files.
- **Keep delivery downstream.** External displays and notifications are consumers of canonical state. Their failures must not silently redefine that state.

These are design contracts, not a claim that every filtered component has been independently certified. The mirror includes targeted regression examples for lock safety, atomic-I/O races, refresh boundaries, report publication, and operational consumers; some require omitted modules or fixtures.

## Privacy and publication boundaries

The mirror uses file selection and content checks to separate reusable operating material from private content. Personal files are omitted rather than rewritten in place into public examples. A few approved structural pages are replaced with harmless placeholders.

Outside the public scope:

- Personal wiki content, journal entries, task records, feed interests, recommendation history, and personal operational logs.
- Secrets, live authentication, API keys, OAuth material, cookies, private keys, and account-specific configuration.
- Sessions, runtime databases, checkpoints, caches, process state, lock files, and job output.
- Private scanner rules, calibration, and implementation that reveals them.
- Private integrations, live channel/account mappings, and exact operational schedules.
- Independent project workspaces and ephemeral review scratch files.

The public mirror is not a restore image, an anonymized personal dataset, or an upstream Hermes distribution. Retained local documentation and wrappers may assume omitted files and a particular filesystem layout. Do not run backup, deployment, scheduled, or delivery scripts against a real environment merely to explore the repository.

## Reading and adapting the design

Start with the system boundaries and [review protocol](REVIEW_PROTOCOL.md), then inspect the relevant published code and tests. Read dependencies and write targets before attempting execution; missing private inputs are not test fixtures to invent.

For adaptation, supply your own canonical data, installation paths, credentials outside version control, and platform configuration. Exercise individual components in an isolated workspace before enabling any network delivery or scheduled writes. This README intentionally provides no one-command setup or complete-restore procedure.

The documentation rule is the same as the data rule: **do not hand-maintain a live list in prose when a registry owns it**. Keep route facts in the route manifest, source facts in the source registry, task facts in the task registry, and scheduling facts in the local scheduler configuration. Update the nearest contract when the harness changes.

The transferable design is a set of bounded workflows around durable state: the agent can reason across systems, but each system retains control of its writes.
