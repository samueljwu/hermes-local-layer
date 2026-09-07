# Journal

Objective: capture and organize the user's own thoughts without turning them into external facts, tasks, or wiki knowledge.

Canonical paths:
- Root: `/home/hermes/journal/`
- Schema: `SCHEMA.md`
- Index: `index.md`
- Log: `log.md`
- Registry: `_meta/entry_registry.json`
- Compact harness: `_tools/journal_ops.py`
- Channel bootstrap: `BOOTSTRAP.md`

What belongs here:
- Personal reflections, ideas, decisions, questions, observations, and raw thought captures
- Cleaned thought cards that preserve the user's wording and uncertainty
- Possible interpretations and related-entry links, clearly labeled as tentative

What does not belong here:
- Wiki ingestion or wiki edits
- Task creation/reminders unless the user explicitly switches workflows
- External research that was not part of the user's thought
- Claims about the user's motives, feelings, or commitments that were not stated

Data model:
- `_meta/entry_registry.json` is a flat JSON array and the canonical metadata store.
- Entry IDs are short stable IDs: `J1`, `J2`, ...
- `tag` is the primary folder/display tag.
- `tags` is an optional secondary topic-tag array.
- Entry markdown files live under `{tag}/J{id}.md`.

Routine commands:
```bash
/home/hermes/journal/_tools/journal_ops.py orient
/home/hermes/journal/_tools/journal_ops.py validate
/home/hermes/journal/_tools/journal_ops.py regenerate
```

Read-only wiki context:
- Journal may consult wiki context only through:
```bash
/home/hermes/journal/_tools/wiki_read_harness.py "query" --top 8 --hops 1 --profile compact
```
- The harness queries prebuilt wiki semantic JSON artifacts directly, snapshots the wiki before/after, and fails if any wiki mutation occurs.
- Keep labels explicit: `Journal text`, `Wiki context`, `Possible connection`, `Missing evidence`.

Non-negotiable rules:
- Preserve the user's original words exactly in the `original` field.
- Do not invent missing meaning, facts, motives, emotions, or commitments.
- Do not turn action-like wording into implementation from the journal workflow.
- Never mutate `/home/hermes/wiki` from journal context.
- Update `index.md` and append `log.md` for journal changes.
- Use `journal_ops.py` for mutations: it validates the complete proposed registry and all output parents before any registry/log/index/derived write, uses descriptor-relative `O_NOFOLLOW` locks and atomic publication, and restores the exact prior registry/log/index/entry bundle if a later publication step fails.

More detail:
- Full rules: `SCHEMA.md`
- Current entry map: `index.md`
- Recent changes: `log.md`
