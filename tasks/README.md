# Tasks

Objective: manage pending tasks, due dates, reminders, Discord task displays, and completed/cancelled task history.

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
- Per-tag markdown files are derived cache for pending tasks only.
- If a note file and the registry disagree, the registry wins.

What belongs here:
- Actionable commitments
- Due dates, recurrence, priority, reminder dates, status, and tags
- Completed/cancelled history in `log.md`
- Derived pending-task notes under tag folders

What does not belong here:
- Journal thoughts that are not explicit commitments
- Wiki research content
- Feed recommendations unless explicitly converted to a task

ID model:
- Pending tasks use `T-x-y`.
- `x` is the current due-date rank among pending tasks and may change when tasks are added/closed/rescheduled.
- `y` is the permanent creation-order component and is never reused.
- Legacy closed tasks may still use `Tn`.

Routine commands:
```bash
/home/hermes/tasks/_tools/task_ops.py orient
/home/hermes/tasks/_tools/task_ops.py validate
/home/hermes/tasks/_tools/task_ops.py regenerate
/home/hermes/tasks/_tools/task_ops.py add --help
/home/hermes/tasks/_tools/task_ops.py close --help
```

Google Calendar authorization:
- The authorization and synchronization helpers are private live-only files and are not included in the backup or filtered public mirror.
- Utility: `/home/hermes/tasks/_tools/google_calendar_auth.py`
- OAuth scope: `https://www.googleapis.com/auth/calendar.app.created`
- Credential/token/config directory: `~/.config/hermes-tasks-calendar/` (directory mode `700`; secret files mode `600`)
- Remote loopback authorization requires an SSH local-forward for the callback:
  ```bash
  ssh -L 53682:127.0.0.1:53682 hermes@<hermes-host> /home/hermes/tasks/_tools/google_calendar_auth.py authorize
  ```
- The authorization stage creates and verifies the dedicated `Hermes Tasks` calendar. Task-event synchronization is a separate stage.
- Read-only reconciliation preview: `/home/hermes/tasks/_tools/google_calendar_sync.py`
- The default invocation is read-only and lists proposed Calendar actions.
- Guarded writes require both `--apply` and the exact reviewed count through `--expect-actions N`; warnings block all writes and more than five deletions are refused by default.
- Canonical `add`, `amend`, `close`, and `regenerate` operations run a bounded unattended sync after releasing the task lock (`10` actions maximum, `3` deletions maximum). Failures warn without rolling back the canonical registry; the recovery job retries later.
- A silent no-agent recovery reconciliation runs every 15 minutes. It emits a fixed alert to `#tasks` on synchronization failure while returning success to the scheduler, preventing the globally configured agent-based cron autofix path from running; successful runs produce no message.
- OAuth is restricted to the exact `calendar.app.created` scope, which excludes the primary and pre-existing unrelated calendars but can cover calendars created by this OAuth app. The runtime boundary is stricter: each run loads, verifies, and pins the configured `Hermes Tasks` calendar ID for planning, writes, and read-back.

Weekly completion reporting:
- Generator: `/home/hermes/.hermes/scripts/weekly_task_completion_report.py`, launched by `/home/hermes/.hermes/scripts/weekly_task_completion_report.sh` in its dedicated virtual environment.
- Schedule: Sunday 21:00 HKT. The report covers Monday through the Sunday 21:00 HKT run time and a 10-week stacked completion chart split by task tag.
- Sources: the canonical registry plus `tasks/log.md`; cancelled tasks are excluded and recurring completed occurrences are included.
- Generated artifacts are written outside this repository under `/home/hermes/task-completion-report/` (`latest_report.json`, `latest_report_tasks.csv`, SVG, and PNG).
- Corrections to the report's significance ranking are append-only JSON records in `_meta/weekly_completion_significance_feedback.json`; they do not modify task state or completion history.

Discord automation:
- Dashboard updater script: `/home/hermes/.hermes/scripts/update_tasks_dashboard.py` (called automatically by task harness mutations after their mutation lock is released; also runnable on demand). Refreshes share `_meta/.task_ops.lock`; dashboard state is promoted with unique fsynced temporary files and `os.replace`. It honors Discord `429` retry delays; if an old dashboard remains uneditable, it posts and pins a replacement, unpins the stale dashboard, and records the active message ID in `_meta/tasks_dashboard_state.json`.
- Tag command sync: `/home/hermes/.hermes/scripts/discord_tag_commands.py`
- Due-tomorrow reminder cron: `9d28b37d3bc6` at 22:00 HKT
- Due-today reminder crons: `203eaf5378d2` at 06:00 HKT and `4a01545f43fd` at 18:00 HKT
- Reminders should deliver to the main Discord `#tasks` channel, not threads.

Non-negotiable rules:
- Never edit derived task note files directly.
- Read the registry fresh before every task operation.
- Create/modify by updating the registry, then regenerating pending notes/index.
- Completing recurring tasks advances the next occurrence by default; close the full series only when the user asks.
- Do not create per-task LLM cron jobs for normal due-date reminders; use the watchdog scripts.
- Update `log.md` when tasks are completed/cancelled or recurring occurrences advance.

More detail:
- Full rules: `SCHEMA.md`
- Current dashboard/index: `index.md`
- History: `log.md`
