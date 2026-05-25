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

Discord automation:
- Dashboard updater: `/home/hermes/.hermes/scripts/update_tasks_dashboard.py`
- Tag command sync: `/home/hermes/.hermes/scripts/discord_tag_commands.py`
- Due-tomorrow reminder cron: `9d28b37d3bc6` at 09:00 HKT
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
