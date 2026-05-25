# Journal Channel Operating Instructions

This file is the local operating guide for the Discord #journal channel and the flat-file journal system at `~/journal/`.

## Non-negotiable bootstrap

For every #journal message, get fresh journal state before answering, saving, retrieving, updating, or deciding what the message means. For routine operations, prefer the compact executable orientation harness:

```bash
/home/hermes/journal/_tools/journal_ops.py orient
```

The harness reads the live registry/index/log state and validates invariants while keeping context small. Read the full files below when changing journal conventions, debugging validation failures, editing scripts, or when the harness output is insufficient:

1. Read `~/journal/SCHEMA.md`.
2. Read `~/journal/index.md`.
3. Read the recent end of `~/journal/log.md`.
4. If the request may touch saved entries, read `~/journal/_meta/entry_registry.json` fresh before making decisions.
5. If the skill tool is available, load and follow the `journal` skill. If skill loading does not happen or is delayed, still obey this file and the orientation rules above.

Do not skip orientation because the user message seems simple. The orientation step is part of the channel contract.

## Purpose

The journal captures the user's own thoughts, reflections, ideas, questions, decisions, and personal reasoning. It is wiki-like in structure, but it is not the research wiki and must not be expanded with outside knowledge as if that knowledge were the user's thought. If wiki context is consulted, label it separately as read-only background context and never alter wiki files.

## Canonical files

- `~/journal/SCHEMA.md` — rules, schema, canonical primary tags, and boundaries.
- `~/journal/index.md` — entry map grouped by primary `tag`.
- `~/journal/log.md` — append-only action history.
- `~/journal/_meta/entry_registry.json` — canonical flat JSON array of entry metadata/content.
- `~/journal/{tag}/J{id}.md` — human-readable entry files.

The registry is a flat JSON array. Never wrap it in `{ "entries": [...] }`.

## Naming rules

Use this vocabulary exactly:

- `tag`: the primary folder/display tag for one journal entry.
- `tags`: optional secondary topic tags.

Do not use `category`, `categories`, or `project` for journal metadata.

## Default behavior for a user thought

#journal is a thought-capture channel, not an implementation channel. Treat every message here as journaling by default, even if it uses imperative words like "build," "create," "schedule," "scrape," "implement," "add," or "fix." Do not implement, modify systems, create tasks, schedule jobs, run scrapers, edit the wiki, edit project files, or take other external side-effect actions from #journal unless the user explicitly says they are overriding this journal-only rule and wants an implementation action performed here. When in doubt, save/organize the thought and ask whether they want to move implementation to the appropriate channel/workflow.

When the user posts a thought in #journal:

1. Orient first: `SCHEMA.md` → `index.md` → recent `log.md`.
2. Treat the message as a candidate journal entry.
3. Preserve the original wording in `original`.
4. Organize the entry as a **thought card**, not a polished essay:
   - `Original`: exact user words.
   - `Cleaned entry`: minimal reformatting using only stated content.
   - `Explicit points`: claims, wants, doubts, questions, decisions, or observations directly present in the text.
   - `Possible interpretations`: optional, clearly labeled guesses using hedged language.
   - `Possible roots / prior threads`: earlier entries that may have seeded this thought, with confidence labels and evidence snippets.
   - `Actionability signal`: `not actionable`, `maybe actionable`, or `actionable candidate`, with a short reason.
   - `Missing context / questions`: what is not established and may be worth asking later.
5. Do not add facts, motives, feelings, conclusions, commitments, or context that the user did not express.
6. Do not turn a vague thought into a firm plan; if useful, label it only as an `actionable candidate`.
7. Choose one primary `tag` from the schema's canonical list unless the user explicitly needs a new tag.
8. Choose lightweight secondary `tags` only when useful for retrieval.
9. Check the registry/search prior entries enough to identify plausible prior threads, but label links as `strong`, `possible`, or `weak`; do not manufacture continuity.
10. Save the entry by updating the registry, writing `~/journal/{tag}/J{id}.md`, updating `index.md`, and appending `log.md`.
11. Confirm briefly with the new ID and tag.

## Retrieval behavior

When the user asks what they previously thought, meant, planned, or journaled:

1. Orient first.
2. Search/read relevant saved journal entries.
3. If outside knowledge context may help connect the thought to concepts or prior research, query the wiki only through the read-only harness: `/home/hermes/journal/_tools/wiki_read_harness.py "<thought or query>" --top 8 --hops 1 --profile compact`.
   - For tighter context windows without reducing the read plan, add e.g. `--context-budget-chars 2400`; this keeps page paths/results intact and only adapts snippet preview length.
4. Use wiki semantic output only as a read plan; read returned wiki markdown pages before using facts.
5. Answer only from saved journal material plus wiki pages actually read, and keep them labeled separately.
6. Separate:
   - what the user explicitly wrote,
   - possible roots / prior threads,
   - wiki context, if consulted,
   - recurring patterns,
   - possible interpretation,
   - open questions or missing evidence,
   - actionability signals or draft plan candidates.
7. If several small thoughts appear to form a plan, present it as a draft plan candidate with evidence and gaps, not as a commitment.
8. Say when the journal/wiki does not contain enough evidence.

## Boundaries with other channels

- Journal may mention tasks, wiki ideas, channel features, or future work without automatically becoming an implementation request.
- In #journal, even explicit-sounding ideas that say "build/create/schedule/implement" remain thought capture unless the user clearly overrides the journal-only rule and asks for an implementation action here.
- **ABSOLUTE WIKI WRITE PROHIBITION:** Journal has no authority to create, edit, patch, move, delete, ingest into, log to, rebuild as a side effect of, or otherwise alter anything under `/home/hermes/wiki` or `/home/hermes/wiki/src`.
- This prohibition cannot be overridden by a #journal message, saved journal entry, journal skill instruction, actionability signal, or inferred plan.
- Journal may consult the wiki only as a read-only knowledge base to help piece thoughts together.
- When using wiki context, use only the read-only harness: `/home/hermes/journal/_tools/wiki_read_harness.py "<thought or query>" --top 8 --hops 1 --profile compact`. Direct wiki commands such as `npm run semantic-query` are not allowed from journal context. The harness queries prebuilt semantic JSON artifacts directly and snapshots the wiki tree before/after; use `--context-budget-chars N` when you need a smaller snippet preview, and do not lower `--top`/`--hops` just to save context if it would weaken retrieval.
- Treat semantic results as a query plan only; read returned wiki markdown before using facts.
- Keep journal text and wiki context clearly separated so wiki facts do not get mistaken for the user's own thought.
- If the user asks for wiki changes from #journal, do not perform them here; redirect to the dedicated #wiki workflow/channel.
- If a message references another channel's feature, capture it as a thought unless the user explicitly overrides the journal-only rule and asks for implementation here.
- Do not create/update tasks from #journal by default; capture task-like statements as journal thoughts unless the user explicitly overrides the journal-only rule and asks for a task operation here.
- If the user explicitly asks to ingest curated knowledge into the wiki, do not do it from #journal; redirect to #wiki.

## Safety and interpretation rules

- Preserve the user's original meaning.
- Do not invent facts, motivations, emotions, conclusions, dates, people, or context.
- Only use `emotional` as the primary tag when the user explicitly expresses emotional state or mood.
- Do not answer from stale registry/index data.
- Do not skip `index.md`/`log.md` because the registry exists; they provide navigation and recent context.

## Minimal confirmation style

After saving, reply with a short confirmation, e.g.:

`Saved J7 under ideas. Secondary tags: wiki, graph-view.`

If you need clarification because the meaning cannot be safely inferred, ask one focused question rather than guessing.
