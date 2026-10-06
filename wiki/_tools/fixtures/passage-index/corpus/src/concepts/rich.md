---
title: "Rich Ω Page"
type: concept
kind: explainer
tags: [hardware, "résumé"]
aliases: ["Rich page", "GB200 NVL72", "__proto__", "constructor", "hasOwnProperty"]
query_terms: ["SRSD107", "A/B.C-9"]
read_when: evidence lookup
sources:
  - raw/papers/source-a.md
confidence: medium
contested: true
publication_date: unknown
event_date: 2026-05-04
updated: 2026-09-20
ingested: 2026-09-19
---
# Rich Ω Page

Intro has SRSD107, XS3236970433, GB200 NVL72, A/B.C-9, and PCIe-6.0 on 2025-10-13.[^caveat] ^[raw/papers/source-a.md]
The same paragraph keeps a hard-wrapped continuation.

## Nested

### Lists

- First item names MODEL-X/2.
  Continuation stays with the item.
- Second item names R2-D2.
- Third item names 800VDC.

### Small table

| Model | Count | Date |
| --- | ---: | --- |
| GB200 NVL72 | 2330 | 2026-01-02 |
| SRSD107 | 42.5% | unknown |

### Large table

| Part | Value |
| --- | ---: |
| X-1 | 100 |
| X/2 | 200 |
| X.3 | 300 |
| X-4 | 400 |
| X/5 | 500 |

### Code

```js
const model = "A/B.C-9"; // [^inside]: literal ^[raw/papers/code.md]
```

### Equation

```math
E = mc^2 \text{ [^inside] ^[raw/papers/equation.md] }
```

### Caveat

Footnote use again [^caveat].

[^caveat]: This caveat retains XS3236970433 and 2024-09.

Final prose has Unicode café and Ωmega. Identifiers foo_bar, API_KEY_2, a_b-c.d/4, and prefixfoo_barx.

### Loose lists

- Loose first item.

  Continuation paragraph retains PROV-LIST/1.

  - Nested child retains NESTED-LIST/2.
    - Deep child retains DEEP-LIST/3.

  > Nested quote retains QUOTE-LIST/4.

  ```text
  fenced LIST-FENCE/5
  ```

- Loose second item retains SECOND-LIST/6.
  Continuation stays with second item.

### Footnotes

[^plain]: Plain definition.

  [^indented]: Indented definition.
    Immediate continuation retains FOOTNOTE-LINE/1.

    Second paragraph retains FOOTNOTE-PARA/2.

[^multi]: First paragraph.
    Continued first paragraph.

    Second paragraph.

    ~~~text
    FOOTNOTE-FENCE/3
    ~~~

### Provenance

Single marker ^[raw/papers/source-single.md].

Multiple marker ^[raw/transcripts/source-one.md, src/raw/transcripts/source-two.md].

Mixed marker ^[raw/papers/source-valid.md, notes/not-raw.md, raw/papers/not-markdown.txt, raw/../escape.md].

Malformed markers ^[notes/not-raw.md] ^[raw/papers/not-markdown.txt] ^[raw/../escape.md].

### Marker-first fenced list

- ```text
  marker-first LIST-FENCE/7
  ```
- Following item remains outside the fence.
