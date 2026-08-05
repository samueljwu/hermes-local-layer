# Wiki Setup

This file records the operational setup for `/home/hermes/wiki`.

## Canonical paths

- Project root: `/home/hermes/wiki`
- Source markdown: `/home/hermes/wiki/src`
- VitePress config: `/home/hermes/wiki/.vitepress/config.ts`
- Generated sidebar: `/home/hermes/wiki/.vitepress/_sidebar-generated.mjs`
- Built static wiki site: `/home/hermes/wiki/dist`
- Source-of-truth schema: `/home/hermes/wiki/src/SCHEMA.md`
- Landing homepage: `/home/hermes/homepage/dist/index.html`

Do not edit files under `dist/` directly. Rebuild the wiki from `src/` instead.

## Current serving model

The live wiki is served as static files through Tailscale Serve under `/wiki/`. The root URL is a small static landing page that links to the wiki and stock screener.

- Homepage URL: `https://hermes.tail5857b7.ts.net/`
- Wiki URL: `https://hermes.tail5857b7.ts.net/wiki/`
- Stock screener URL: `https://hermes.tail5857b7.ts.net/stocks/`
- Tailscale Serve handlers:
  - `/` -> `/home/hermes/homepage/dist`
  - `/wiki/` -> `/home/hermes/wiki/dist`
  - `/stocks/` -> `/home/hermes/stock-screener/site/dist`
- VitePress `base`: `/wiki/`
- Tailscale daemon socket: `/run/user/1001/tailscaled.sock`

Check serving status:

```bash
/home/hermes/.local/bin/tailscale --socket=/run/user/1001/tailscaled.sock serve status
```

Expected output includes:

```text
https://hermes.tail5857b7.ts.net (tailnet only)
|-- /        path  /home/hermes/homepage/dist
|-- /wiki/   path  /home/hermes/wiki/dist
|-- /stocks/ path  /home/hermes/stock-screener/site/dist
```

Important: the public wiki path and VitePress `base` must stay matched. A mismatch between `base: '/wiki/'` and the Tailscale Serve `/wiki/` handler can produce an HTML page that loads while CSS/JS/assets 404.

## Build and validation

From `/home/hermes/wiki`:

```bash
npm run build
```

The build script runs, in order:

1. Copies KaTeX CSS/fonts into `public/assets/`.
2. `.vitepress/gen-sidebar.mjs`.
3. `.vitepress/validate-wiki-links.mjs`.
4. `.vitepress/gen-semantic-graph.mjs`.
5. `.vitepress/validate-semantic-relationships.mjs`.
6. `vitepress build .`.
7. Copies semantic, graph, KaTeX, and raw-asset public files into `dist/`. Raw source evidence is copied from the *contents* of `src/raw/assets/` into `dist/raw/assets/`, so published `/wiki/raw/assets/...` URLs retain their source-relative paths without an extra `assets/` directory.

The generated sidebar is rebuilt from the actual `src/` tree. Do not manually edit `.vitepress/_sidebar-generated.mjs`.

## Shared concept-page diagram styles

Diagram-heavy concept pages can use reusable HTML/CSS classes defined in `.vitepress/theme/custom.css` instead of wide ASCII code blocks. This is intended for the human-facing synthesis layer only; preserve raw-source ASCII/text diagrams in `src/raw/` when they are part of provenance.

Available class families include:
- Flow layout: `.flow-diagram`, `.flow-row`, `.flow-row-centered`, `.flow-diagram-vertical`, `.flow-grid`, `.flow-grid-two`, `.flow-lane`, `.flow-loop`, `.flow-stack`
- Flow elements: `.flow-card`, `.flow-card-accent`, `.flow-card-tall`, `.flow-arrow`, `.flow-arrow-down`, `.flow-note`
- Specialized sketches: `.device-card`, `.device-sketch`, `.device-gate`, `.device-fin`, `.flat-channel`, `.package-card`, `.package-sketch`, `.chip-box`, `.substrate-box`, `.board-line`, `.value-stack`, `.value-layer`

Prefer these classes over adding Mermaid or page-specific CSS. If new CSS is needed, make it generic and reusable, then rebuild and verify the generated HTML contains the expected classes.

## Local preview and development

For production-style local preview:

```bash
npm run build
node_modules/.bin/vitepress preview . --port 3000 --host 0.0.0.0
```

For local hot-reload editing only:

```bash
npm run dev -- --port 3000 --host 127.0.0.1
```

Do not expose `vitepress dev` to the tailnet or internet. Static Tailscale Serve from `dist/` avoids dev-server exposure.

## Runtime verification checklist

After setup or config changes, run:

```bash
cd /home/hermes/wiki
npm run build
/home/hermes/wiki/_tools/wiki_ops.py deploy-check
/home/hermes/.local/bin/tailscale --socket=/run/user/1001/tailscaled.sock serve status
curl -I https://hermes.tail5857b7.ts.net/
curl -I https://hermes.tail5857b7.ts.net/wiki/
curl -I https://hermes.tail5857b7.ts.net/wiki/assets/app.CsglWZS8.js
```

The exact asset filename changes after builds; use an asset path from `dist/index.html` if needed. In the userspace-networking deployment, local MagicDNS/curl may be inconclusive; the harness validates Serve config and daemon health without requiring local HTTPS unless explicitly requested.

## Common pitfalls

- `base` must match the public path. For current `/wiki/` serving, keep `base: '/wiki/'`.
- Head entries and custom client code that use absolute URLs need the `/wiki/` prefix or VitePress `withBase()`.
- Do not keep both an npm `prebuild` hook and a `build` script that run the same validators; npm will run both and duplicate work/logs.
- `vitepress preview` serves a built site, but Tailscale Serve is currently serving the `dist/` directory directly. Restarting preview does nothing unless Tailscale Serve is pointed at the preview server.
- The legacy experimental proxy `/home/hermes/wiki-proxy.js` has been removed; it is not part of the current live path.
- Short wikilinks are convenient but can become ambiguous if two pages share the same filename in different directories. Prefer full paths for ambiguous names, e.g. `[[queries/index]]` instead of `[[index]]`.
