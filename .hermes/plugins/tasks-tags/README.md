# tasks-tags: live Discord task commands

`/tags` and per-tag handlers read the canonical task registry through
`local_ops.resolve_tasks_root()`. Markdown task notes are not inputs.

## Automatic reconciliation

The public `ctx.register_platform_handler("discord", factory)` hook starts a
`ctx.spawn_task`-supervised watcher only when Discord connects. Every 30 seconds
it validates the registry tag namespace, registers missing live Hermes handlers,
and adds missing parameterless commands to the **live Discord SDK command tree**.
New tag sets trigger per-command REST reconciliation immediately; otherwise a
remote drift check runs every 300 seconds. No restart is required per tag and no
agent prompt or task harness integration is required. Configure these intervals
under `plugins.entries.tasks-tags.settings.poll_seconds` and
`remote_check_seconds` (minimum 5 seconds).

Hermes and Discord maintain separate dispatch registries. Updating REST alone
leaves discord.py unable to invoke a post-startup command. Updating Hermes alone
also leaves that SDK tree stale. This plugin updates all three layers, using SDK
`tree.add_command` without override or bulk `tree.sync`. For multiword tags the
established native `/new_project` spelling has a Hermes `new-project` alias,
because the gateway normalizes underscores on lookup. Dispatch aliases are added
after startup picker discovery; reconnect wiring removes owned alias-only SDK
entries so they do not consume additional picker slots.

The factory and task APIs are supported plugin APIs. Native callbacks use the
adapter's existing `_run_simple_slash` bridge **without modifying the adapter**:
this is a deliberate, tested private-method dependency because there is no public
native-interaction-to-gateway slash bridge. It preserves authorization, interaction
deferral, source construction, busy policy and command access. Missing bridge
support fails visibly; there is no authorization-bypassing fallback. Reconnect
cancels the previous watcher; plugin unload cancels tasks and removes SDK commands
added by this plugin by identity.

## REST safety and diagnostics

`~/.hermes/scripts/discord_tag_commands.py` is shared by the watcher and manual
CLI. Plain `python3` CLI invocations re-exec the installed Hermes venv when present,
so other plugins' reserved command names can be discovered with their dependencies.

- Only missing commands are POSTed; changed owned commands are PATCHed.
- Payloads explicitly contain `type: 1` and `options: []`. Descriptions match
  plugin registration, preventing startup/manual description churn.
- Full collision preflight checks built-ins, aliases, other plugins, duplicate
  normalized tags, invalid/overlength names and unrelated remote commands.
- The live watcher isolates invalid/colliding local tags and SDK-capacity failures
  in `tag_errors`, so valid supported tags can still reconcile. Manual REST plans
  remain all-or-nothing on validation errors. Discord allows 100 native commands
  in total; full capacity is reported rather than evicting unrelated commands.
- Only exact legacy/current task-tag descriptions matching the command slug are
  recognized as owned. Substring matches never authorize writes/deletes.
- Mutations have exact-target GET verification; failed reads are not empty lists.
- HTTP/transport/readback errors raise, CLI exits nonzero, watcher logs and retries.
  Long `429 retry_after` cooldowns delay remote retry without stopping local refresh.
- A profile-local advisory lock serializes manual and watcher REST mutations.
  Core's own startup/reconnect synchronizer is independent; matching payloads and
  periodic readback repair drift without invoking its bulk synchronizer.
- Default reconciliation is additive/non-destructive. Tag removal does not delete
  commands automatically. Explicit `--prune` deletes only recognized owned commands
  absent from the registry, never unrelated or currently reserved commands.
  Stale tag handlers return not-found rather than partial-matching another tag.
- The watcher never refreshes the dashboard. Manual CLI refreshes it only when
  commands changed unless `--skip-dashboard` is supplied.

`$HERMES_HOME/gateway/task_tag_commands_status.json` records PID, last check time,
`loaded_slugs` (successfully reconciled Hermes + SDK set), `remote_slugs` (last
verified remote tag set), per-tag `tag_errors`, and `error`. Network failures are
also logged. Status-file write failures are logged without terminating the watcher. A
successful prior receipt is not proof the current process is running: check PID
and timestamp. The lock lives alongside it as `discord_task_tags.lock`.

## Activation / verification

One gateway restart is required to load this code release. After that, registry
changes need no restart. Do not restart per tag or install a separate cron job.

1. Run the isolated tests below (no Discord writes).
2. Inspect the non-mutating remote plan:
   `python3 ~/.hermes/scripts/discord_tag_commands.py --dry-run --skip-dashboard`.
3. Restart the gateway once using its actual service supervisor. The connect hook
   activates live reconciliation automatically.
4. Read the status receipt; check current gateway PID, fresh timestamp, expected
   native slugs and `error: null`. Use `--list` and invoke a tag command in Discord.
5. For an actual new tag, wait one polling interval and verify all three layers
   again. REST failures may delay picker registration; global picker caching is
   separate from successful REST readback.

## Tests

```sh
/home/hermes/.hermes/hermes-agent/venv/bin/python -m unittest discover \
  -s /home/hermes/.hermes/plugins/tasks-tags/tests -v
```

Tests isolate HOME/HERMES_HOME/TASKS_ROOT in temporary directories. They use real
PluginContext/PluginManager, actual discord.py command trees and the core adapter's
authorization bridge, with fixture REST transport only. No gateway restart,
production registry edits, live Discord requests, or dependency installation.
