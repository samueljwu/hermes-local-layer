# Tasks

The task model uses `name`, explicit open statuses, and an optional `start_date`. `_meta/task_registry.json` is the sole canonical source; Notion and Calendar are external surfaces, not competing task registries.

Objective: manage open tasks, due dates, reminders, Discord task displays, and completed/cancelled task history.

Canonical paths:
- Root: `/home/hermes/tasks/`
- Schema: `SCHEMA.md`
- Index: `index.md`
- Log: `log.md`
- Registry: `_meta/task_registry.json`
- Compact harness: `_tools/task_ops.py`

Single source of truth:
- `_meta/task_registry.json` is the only canonical task data store.
- It is a flat JSON array, not an object with a `tasks` key.
- Canonical title: `name`; legacy `task` (even beside `name`) is rejected.
- New tasks default to `not_started`; open statuses are `not_started` and `in_progress`. Closed statuses are `completed` and `cancelled`; missing/unknown/legacy `pending` statuses fail closed.
- Shared helper: `~/.hermes/scripts/task_schema.py` (`is_open`, `OPEN_STATUSES`, `VALID_STATUSES`).
- Per-tag markdown files are derived cache for open tasks only.
- If a note file and the registry disagree, the registry wins.

What belongs here:
- Actionable commitments
- Due dates, recurrence, priority, reminder dates, status, and tags
- Completed/cancelled history in `log.md`
- Derived open-task notes under tag folders

What does not belong here:
- Journal thoughts that are not explicit commitments
- Wiki research content
- Feed recommendations unless explicitly converted to a task

ID model:
- Open tasks use `T-x-y`.
- `x` is the current due-date rank among open tasks and may change when tasks are added/closed/rescheduled.
- `y` is the permanent creation-order component and is never reused.
- Legacy closed tasks may still use `Tn`.

Routine commands:
```bash
/home/hermes/tasks/_tools/task_ops.py orient
/home/hermes/tasks/_tools/task_ops.py validate
/home/hermes/tasks/_tools/task_ops.py regenerate
/home/hermes/tasks/_tools/task_ops.py add --help
/home/hermes/tasks/_tools/task_ops.py amend --help
/home/hermes/tasks/_tools/task_ops.py close --help
python3 /home/hermes/tasks/_tools/test_task_ops.py
```

`amend TASK_ID_OR_SUFFIX --name "New title" --status in_progress` changes open tasks only. The Python APIs preserve existing positional argument ordering; the title argument is now `name`. `close` also accepts a stale display ID or permanent suffix and rejects already-closed tasks. Recurring completion advances the due date and resets status to `not_started`.

Both APIs accept keyword-only `expected: dict` preconditions for canonical fields (for example `due_date`, `status`, `name`). Comparisons occur inside the lock before mutations, including recurrence advancement. Unknown keys fail; an expected old `due_date` guards a recurring occurrence. This is optimistic concurrency, not durable remote exactly-once processing; unguarded completion retries may advance again. Preconditions are API-only.

The mutation harness validates the full proposed registry and all canonical/log/index/generated parents before writing. Locks and atomic publication are descriptor-relative with `O_NOFOLLOW`; symlinked parents fail closed without changing task state or external targets. Registry, log, index, and derived-note changes are a rollback-capable transaction: if a later publication step fails, the exact prior file bundle is restored before the error returns. The direct test command delegates to pytest through `uv` and runs every regression in the file.

## Optional start dates and durable external changes

`add --start-date YYYY-MM-DD` and `amend --start-date YYYY-MM-DD` set an explicit
start. Future creation and date edits default a missing/null start once to the
due date; subsequent due edits preserve any nonblank start. Clearing a start
with a due date present therefore resets it to that due date. With no due date,
it remains null. Untouched legacy blanks and unrelated edits are not backfilled.
Start must be on/before due when both exist. Unlabelled user dates continue to
mean due dates; ranking/reminders/Calendar are due-only.
Recurring completion shifts an explicit start by the actual due-date delta.

For connectors use `task_ops.apply_external_change(task_id, fields,
expected_revision, operation_id, origin_page_id=None)`, not ordinary close retries.
The eight business fields are name/status/start_date/due_date/priority/tag/notes/
recurrence. Existing tasks require `_revision` (missing means zero); new intake
uses null task ID/revision and a unique origin page. Operation receipts and
completion repair metadata live in the canonical task record, atomically with
business state. Replay repairs log/cache without repeating the mutation. Errors
after commit require retrying the **same** operation ID and payload. The return
is the current canonical task dict; conflicts raise `ValueError`. A page already
bound to a task returns its current state without reapplying intake fields or
creating another task, including after later operations.
See SCHEMA.md for receipt shape, null clearing, revision and failure semantics.
These external commits intentionally do not use the local CLI bundle rollback.
No second canonical data store is maintained.

## Notion two-way editing

- Only the private `Hermes Tasks - Pilot` database is connected. One row represents
  one work item; `tag` remains a single select. Unused Notion template properties
  are not task fields and are not synchronized.
- Shared fields: `name`, `tag`, `status`, `start_date`, `due_date`, `priority`,
  `recurrence`, `notes`. Blank start/due dates are allowed. A single unlabeled date
  in a Hermes request means `due_date`; explicit start wording overrides the
  mutation-only default to the due date described above.
- Notion edits are proposals to `task_ops.apply_external_change`, validated and
  committed under the canonical lock. Local changes are projected back to Notion.
  Both-sided divergent edits are conflicts, not last-writer-wins updates.
- Technical identity is `hermes_task_key=hermes_tasks:<permanent suffix>`; do not
  edit it. Rank-based `T-x-y` display IDs are not Notion identity.
- Private `~/.config/hermes-tasks-notion/two-way.json` holds comparison baselines,
  bindings and pending operation intent. It is recovery bookkeeping, never an
  editable task master. Never delete/reset it to bypass a conflict.
- New open local tasks are exported; nonblank new open Notion rows are imported.
  Blank drafts are skipped. Imported tasks default to `medium` priority and
  `Other` tag when unset. The three original sample page IDs remain excluded,
  including when they are in Notion trash. Historical closed local tasks are not
  mass imported. Archiving and reopening are disabled. Future confirmed trash
  can delete an enrolled canonical task after the technical cutover below;
  missing query rows, access errors, and container trash never imply deletion.
- Completing a recurring Notion row advances the canonical occurrence once and
  writes its next dates/status back to the same row. `cancelled` closes its series.
  Closing a nonrecurring row retains it as history. Closure history is canonical
  registry/log data, not the Notion page's edit history.
- Both directions reconcile every minute via cron `9ea9d2de4e3f` (`* * * * *`),
  without LLM calls. Polling is the chosen transport; no event daemon or public
  webhook is deployed. The existing Tailscale websites remain private.
- The bounded no-agent wrapper is `~/.hermes/scripts/sync_hermes_tasks_notion.py`.
  Each run permits at most ten operations, three new Notion intakes, and three
  confirmed deletions. Normal
  runs are silent; fixed failure alerts go to the main `#tasks` channel without
  invoking an autonomous repair agent. Inspect live cron for activation/schedule.
  Connector lock contention exits `75` with `{"busy":true}`; the cron wrapper
  silently skips this normal overlap rather than reporting a task conflict.
- Inspect without writes: `python3 /home/hermes/tasks/_tools/notion_sync.py plan`.
  Manual reconciliation: `python3 /home/hermes/tasks/_tools/notion_sync.py sync --apply --max-actions 10`.
  Do not re-run `init` on an established connection.
- Deletion cutover: review `notion_sync.py enable-deletions` before explicitly
  applying with `--apply`. It verifies equal active bindings and writes only
  technical enrollment, never task fields or Notion rows. Pre-cutover trash is
  excluded. Restore an excluded page with unchanged baseline fields and allow
  one successful sync to re-enroll before editing it; changed restored rows
  remain conflicts and require operator review.
- Deletion requires a fresh individually trashed page with unchanged ownership,
  fields, edit time and canonical revision, plus active pinned containers.
  It removes the task/series without recurrence advancement or follow-up creation.
  A durable canonical deletion receipt reserves its suffix permanently and
  repairs registry/log/notes/index after a crash before any remote discovery.
  Post-receipt restoration cannot resurrect the task. Calendar removes only
  validated active managed occurrences; completed history and unknown/malformed
  events remain protected. Retry pending recovery; never erase receipt files.
- Notion offers no distributed transaction/conditional PATCH for this workflow.
  Fresh reads, narrow property patches and verified readback detect optimistic
  conflicts, but cannot eliminate the tiny read/write race with a simultaneous
  Notion edit. Do not edit the same task on both surfaces at once.
- If a conflict remains, inspect both versions and obtain the user's intended
  values before reconciling. Do not silently prefer timestamps or change recovery
  state merely to obtain a clean report. After a committed pending operation is
  reconciled and both surfaces independently agree, an operator may acknowledge
  only that operation with `resolve-equal --key hermes_tasks:N
  --expected-operation-id UUID --expected-revision N [--apply]` through the same
  connector. Default is dry-run; mismatched fields/identity/revision are refused.
  A prepared deletion blocked by a concurrent canonical edit may also be
  acknowledged after restoring the owned Notion page to active and matching the
  CURRENT canonical fields; supply the current revision, not the stale intent's.
  Any canonical deletion receipt forbids this recovery, even before registry
  removal. Receipt absence is checked under the canonical lock before and after
  remote validation. Only the named pending intent and its binding are updated.
  This never edits canonical business fields or Notion and is not a force option.
- Canonical operation receipts are deliberately retained like completion history
  to protect arbitrarily delayed replay. Total historical storage is not claimed
  to be bounded. The connector fails closed on its registry/API size limits;
  future retention changes require a separately reviewed acknowledgement policy.

Google Calendar authorization:
- The authorization and synchronization helpers are private live-only files and are not included in the backup or filtered public mirror.
- Utility: `/home/hermes/tasks/_tools/google_calendar_auth.py`
- OAuth scope: `https://www.googleapis.com/auth/calendar.app.created`
- Credential/token/config directory: `~/.config/hermes-tasks-calendar/` (directory mode `700`; secret files mode `600`)
- Remote loopback authorization requires an SSH local-forward for the callback:
  ```bash
  ssh -L 53682:127.0.0.1:53682 hermes@<hermes-host> /home/hermes/tasks/_tools/google_calendar_auth.py authorize
  ```
- Recover an expired or revoked refresh token without sending credentials through chat and without creating a replacement calendar:
  ```bash
  ssh -L 53682:127.0.0.1:53682 hermes@<hermes-host> /home/hermes/tasks/_tools/google_calendar_auth.py reauthorize
  ```
  Open the printed Google URL locally and select the account that owns the existing `Hermes Tasks` calendar. The old token remains in place until the replacement token can read and verify the exact configured calendar ID, name, and timezone; selecting another account therefore fails closed.
- The authorization stage creates and verifies the dedicated `Hermes Tasks` calendar. Task-event synchronization is a separate stage.
- Read-only reconciliation preview: `/home/hermes/tasks/_tools/google_calendar_sync.py`
- The default invocation is read-only and lists proposed Calendar actions.
- Guarded writes require both `--apply` and the exact reviewed count through `--expect-actions N`; warnings block all writes and more than five deletions are refused by default.
- Canonical `add`, `amend`, `close`, and `regenerate` operations run a bounded unattended sync after releasing the task lock (`10` actions maximum, `3` deletions maximum). Failures warn without rolling back the canonical registry; the recovery job retries later.
- A silent no-agent recovery reconciliation runs every 15 minutes. It emits a fixed alert to `#tasks` on synchronization failure while returning success to the scheduler, preventing the globally configured agent-based cron autofix path from running; successful runs produce no message.
- OAuth is restricted to the exact `calendar.app.created` scope, which excludes the primary and pre-existing unrelated calendars but can cover calendars created by this OAuth app. The runtime boundary is stricter: each run loads, verifies, and pins the configured `Hermes Tasks` calendar ID for planning, writes, and read-back.
- Production Calendar helpers pin both the task root and `~/.config/hermes-tasks-calendar` through canonical-root resolvers before any credential/config read or write. Environment root overrides are accepted only when `HERMES_ALLOW_NONCANONICAL_LOCAL_ROOTS=1` is explicitly set for tests or development fixtures.

Weekly completion reporting:
- Generator: `/home/hermes/.hermes/scripts/weekly_task_completion_report.py`, launched by `/home/hermes/.hermes/scripts/weekly_task_completion_report.sh` in its dedicated virtual environment.
- Schedule: Sunday 21:00 HKT. The report covers Monday through the Sunday 21:00 HKT run time and a 10-week stacked completion chart split by task tag.
- Sources: the canonical registry plus `tasks/log.md`; cancelled tasks are excluded and recurring completed occurrences are included.
- Generated artifacts are written outside this repository under `/home/hermes/task-completion-report/` (`latest_report.json`, `latest_report_tasks.csv`, SVG, and PNG). The generator builds each complete bundle in a versioned same-filesystem generation directory; the stable filenames resolve through `current/`, and one atomic `current` symlink switch publishes all four while holding `/home/hermes/.hermes/state/locks/task-completion-report.lock`.
- Corrections to the report's significance ranking are append-only JSON records in `_meta/weekly_completion_significance_feedback.json`; they do not modify task state or completion history. Record them only through `task_ops.py record-weekly-feedback --report-week START..END --originally-selected ... --user-preferred ... --inferred-rule ...`, which holds the canonical task lock and atomically rewrites the JSON array.

Discord automation:
- Dashboard updater script: `/home/hermes/.hermes/scripts/update_tasks_dashboard.py` (called automatically by task harness mutations after their mutation lock is released; also runnable on demand). Refreshes share `_meta/.task_ops.lock`; dashboard state is promoted with unique fsynced temporary files and `os.replace`. It honors Discord `429` retry delays; if an old dashboard remains uneditable, it posts and pins a replacement, unpins the stale dashboard, and records the active message ID in `_meta/tasks_dashboard_state.json`.
- Tag commands: the `tasks-tags` gateway plugin watches the canonical registry and reconciles both live command handlers and native Discord registration, including tags created through the CLI. Per-tag gateway restarts are not required. `/home/hermes/.hermes/scripts/discord_tag_commands.py` remains the manual diagnostic/repair entry point; native REST presence and a live handler must both be verified. Registration failures do not roll back tasks and are retried by the plugin.
- Due-tomorrow reminder cron: `9d28b37d3bc6` at 22:00 HKT
- Due-today reminder crons: `203eaf5378d2` at 06:00 HKT and `4a01545f43fd` at 18:00 HKT
- Reminders should deliver to the main Discord `#tasks` channel, not threads.
- `task_ops.py close` and `validate --with-cron` parse the multiline `hermes cron list --all` format and recognize per-task reminder references by both the current visible ID and the stable creation-order suffix, so task renumbering does not hide stale reminders.

Non-negotiable rules:
- Never edit derived task note files directly.
- Read the registry fresh before every task operation.
- Create/modify through the harness, preserving revisions and receipts, then regenerate open notes/index.
- Completing recurring tasks advances the next occurrence by default; close the full series only when the user asks.
- Do not create per-task LLM cron jobs for normal due-date reminders; use the watchdog scripts.
- Update `log.md` when tasks are completed/cancelled or recurring occurrences advance.

More detail:
- Full rules: `SCHEMA.md`
- Current dashboard/index: `index.md`
- History: `log.md`
