# Wiki

Objective: maintain a source-grounded, interlinked research knowledge base and publish it as a static VitePress site.

Canonical paths:
- Source markdown: `/home/hermes/wiki/src/`
- Site/config root: `/home/hermes/wiki/`
- Schema: `/home/hermes/wiki/src/SCHEMA.md`
- Index: `/home/hermes/wiki/src/index.md`
- Log: `/home/hermes/wiki/src/log.md`
- Built site: `/home/hermes/wiki/dist/` (generated; do not edit)
- Compact harness: `/home/hermes/wiki/_tools/wiki_ops.py`

What belongs here:
- User-provided raw sources under `src/raw/`
- Durable entity, concept, comparison, and query pages
- Source-limited syntheses, cross-links, backlinks, and provenance
- Human-facing concept diagrams when they improve readability; preserve the raw-source diagram text separately
- Generated semantic/query/graph artifacts needed by the published site

What does not belong here:
- Unsourced web research unless the user explicitly requested it
- Private journal thoughts unless explicitly provided for wiki ingestion
- Task/reminder state
- Feed recommendations unless the user explicitly asks to ingest an item

Non-negotiable rules:
- User-source-only by default. Do not search the web to fill wiki gaps unless explicitly asked.
- Raw sources are immutable. Corrections and synthesis go into wiki pages, not raw files.
- For diagram-heavy sources, keep raw diagrams text-first under `src/raw/`; if the concept/primer page is meant for browser reading, use reusable HTML/CSS flow-card classes from `.vitepress/theme/custom.css` instead of wide ASCII blocks.
- Read `SCHEMA.md` before changing conventions.
- Every new or updated substantive page must update `index.md` and append `log.md`.
- Use wikilinks for explicit, source-grounded relationships; do not create inferred graph edges as facts.
- Lecture transcript ingests should create/update substantive concept pages, not broad course/module map pages.

Diagram presentation:
- Raw provenance layer: use Markdown-friendly ASCII/text diagrams in `src/raw/` when a source figure encodes process flows, supply chains, manufacturing loops, packaging stacks, architecture, or value stacks.
- Human reading layer: concept/primer pages may upgrade dense ASCII diagrams to dependency-free HTML/CSS flow cards using reusable classes such as `.flow-diagram`, `.flow-row`, `.flow-card`, `.flow-arrow`, `.flow-lane`, `.flow-note`, `.flow-grid-two`, `.value-stack`, `.device-card`, and `.package-card`.
- Do not add Mermaid or another renderer by default; prefer the existing CSS classes unless the user explicitly asks or the diagram genuinely requires it.
- After diagram/style edits, run `wiki_ops.py validate`, `npm run build`, and usually `wiki_ops.py deploy-check`; confirm the built HTML contains expected flow classes and raw ASCII remains present when intended.

Routine commands:
```bash
/home/hermes/wiki/_tools/wiki_ops.py orient
/home/hermes/wiki/_tools/wiki_ops.py validate
/home/hermes/wiki/_tools/wiki_ops.py deploy-check
cd /home/hermes/wiki && npm run lint
cd /home/hermes/wiki && npm run test:semantic-stability
cd /home/hermes/wiki && npm run build
cd /home/hermes/wiki && npm run semantic-query -- "question" --top 8 --hops 1
```

Published site:
- Current Tailscale URL root: `https://hermes.tail5857b7.ts.net/`
- Canonical route mapping lives in `/home/hermes/DEPLOYMENT.md` and `/home/hermes/routes.yaml`; wiki is served at `/wiki/` from `/home/hermes/wiki/dist/`.
- VitePress `base` must match the `/wiki/` route.
- Use `npm run build` to regenerate static output before relying on the live site.

Directory notes:
- `.vitepress/` contains the VitePress config, generated sidebar pipeline, wikilink validator, semantic graph generator, graph stability regression test, and graph view theme code.
- `.vitepress/theme/custom.css` contains shared site styling, including reusable `.flow-*`, device/package sketch, and value-stack classes for readable concept-page diagrams.
- `public/` contains generated public semantic/graph assets copied into `dist/` during build.
- `src/_meta/` contains wiki metadata, templates, backlog files, and semantic graph artifacts.
- `src/raw/` contains immutable user-provided sources.
- `src/concepts/`, `src/entities/`, `src/comparisons/`, and `src/queries/` contain first-class wiki pages.

More detail:
- Operational setup: `SETUP.md`
- Content rules: `src/SCHEMA.md`
- Current catalog: `src/index.md`
- Recent changes: `src/log.md`
