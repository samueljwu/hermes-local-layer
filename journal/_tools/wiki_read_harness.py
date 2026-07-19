#!/usr/bin/env python3
"""Read-only wiki semantic query harness for the journal system.

Purpose:
- Let #journal consult ~/wiki/src as background context without writing to the wiki.
- Start from prebuilt wiki semantic JSON artifacts for efficiency and read-only safety.
- Return a compact, evidence-labeled read plan plus optional snippets from the returned pages.

This script never writes under the wiki path. It only reads prebuilt semantic
artifacts and markdown pages named in the generated read plan.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any


WRITE_PROHIBITION = (
    "ABSOLUTE WIKI WRITE PROHIBITION: journal/#journal may read /home/hermes/wiki "
    "and /home/hermes/wiki/src only as a knowledge base. It must never create, edit, "
    "patch, move, delete, ingest into, log to, rebuild as a side effect of, or otherwise "
    "alter wiki files."
)


CANONICAL_WIKI_ROOT = Path("/home/hermes/wiki")
CANONICAL_WIKI_SRC = CANONICAL_WIKI_ROOT / "src"
ALLOW_NONCANONICAL_ROOTS_ENV = "HERMES_ALLOW_NONCANONICAL_LOCAL_ROOTS"


def _resolve_default_wiki_paths() -> tuple[Path, Path]:
    root = Path(os.environ.get("WIKI_ROOT", str(CANONICAL_WIKI_ROOT))).expanduser()
    src = Path(os.environ.get("WIKI_SRC", str(root / "src"))).expanduser()
    try:
        root_resolved = root.resolve()
        src_resolved = src.resolve()
        canonical_root = CANONICAL_WIKI_ROOT.resolve()
        canonical_src = CANONICAL_WIKI_SRC.resolve()
    except OSError as exc:
        raise SystemExit(f"Unable to resolve wiki root/src: {exc}") from exc
    if os.environ.get(ALLOW_NONCANONICAL_ROOTS_ENV) != "1":
        if root_resolved != canonical_root:
            raise SystemExit(
                f"Refusing non-canonical WIKI_ROOT {root_resolved}; set {ALLOW_NONCANONICAL_ROOTS_ENV}=1 only for tests/dev fixtures."
            )
        if src_resolved != canonical_src:
            raise SystemExit(
                f"Refusing non-canonical WIKI_SRC {src_resolved}; set {ALLOW_NONCANONICAL_ROOTS_ENV}=1 only for tests/dev fixtures."
            )
    if src_resolved != root_resolved and root_resolved not in src_resolved.parents:
        raise SystemExit(f"Refusing WIKI_SRC outside WIKI_ROOT: {src_resolved}")
    return root, src


DEFAULT_WIKI_ROOT, DEFAULT_WIKI_SRC = _resolve_default_wiki_paths()


def _resolve_wiki_page(path_from_query: str, wiki_root: Path, wiki_src: Path) -> Path | None:
    raw = Path(path_from_query)
    candidates = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.append(wiki_root / raw)
        candidates.append(wiki_src / raw)
        if path_from_query.startswith("src/"):
            candidates.append(wiki_root / path_from_query)
        else:
            candidates.append(wiki_root / "src" / path_from_query)

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            src_resolved = wiki_src.resolve()
            if resolved.exists() and resolved.is_file() and src_resolved in resolved.parents:
                return resolved
        except OSError:
            continue
    return None


def _tokenize_query(text: str) -> list[str]:
    return [t.lower() for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}", text)]


def _load_json_file(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(
            f"Semantic artifact not found: {path}. Run the wiki build/lint outside #journal before using the read-only harness."
        ) from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid semantic artifact JSON at {path}: {exc}") from exc


def _artifact_path(wiki_root: Path, relative: str) -> Path:
    candidate = (wiki_root / relative).resolve()
    root = wiki_root.resolve()
    if root not in candidate.parents:
        raise SystemExit(f"Refusing semantic artifact outside wiki root: {candidate}")
    return candidate


def _load_semantic_artifacts(wiki_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load prebuilt semantic artifacts without invoking npm/node.

    Journal access must be read-only by construction. Running the wiki semantic
    query script from #journal is unsafe because future build/query code could
    refresh generated artifacts as a side effect before the harness detects it.
    The harness therefore queries already-built JSON artifacts directly.
    """
    index = _load_json_file(_artifact_path(wiki_root, "public/semantic/index.json"))
    graph = _load_json_file(_artifact_path(wiki_root, "public/semantic/graph.json"))
    return index, graph


def _page_text_for_scoring(page: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("title", "kind", "summary_1line", "summary_compact", "read_when", "confidence"):
        value = page.get(key)
        if isinstance(value, str):
            parts.append(value)
    for key in ("tags", "aliases", "query_terms"):
        value = page.get(key)
        if isinstance(value, list):
            parts.extend(str(v) for v in value if isinstance(v, (str, int, float)))
    return " \n".join(parts).lower()


def _score_page(query_terms: list[str], slug: str, page: dict[str, Any]) -> tuple[float, list[str]]:
    if not query_terms:
        return 0.0, []
    haystack = _page_text_for_scoring(page)
    slug_text = slug.replace("/", " ").replace("-", " ").lower()
    title = str(page.get("title", "")).lower()
    tags = {str(t).lower() for t in page.get("tags", []) if isinstance(t, str)}
    aliases = [str(a).lower() for a in page.get("aliases", []) if isinstance(a, str)]
    page_terms = [str(t).lower() for t in page.get("query_terms", []) if isinstance(t, str)]
    score = 0.0
    reasons: list[str] = []
    for term in query_terms:
        term_score = 0.0
        if term in title:
            term_score += 4.0
        if term in slug_text:
            term_score += 3.0
        if term in tags:
            term_score += 3.0
        if any(term in alias for alias in aliases):
            term_score += 2.0
        if any(term in page_term for page_term in page_terms):
            term_score += 1.5
        count = haystack.count(term)
        if count:
            term_score += min(2.0, 0.4 * count)
        if term_score:
            reasons.append(f"matched query term '{term}'")
            score += term_score
    degree = page.get("degree") if isinstance(page.get("degree"), dict) else {}
    try:
        score += min(1.5, math.log1p(float(degree.get("total", 0))) / 3.0)
    except (TypeError, ValueError):
        pass
    return score, reasons[:8]


def _expand_read_plan(results: list[dict[str, Any]], pages: dict[str, Any], hops: int, top: int) -> list[str]:
    read_plan = [r["path"] for r in results if r.get("path")]
    if hops <= 0:
        return read_plan
    for result in results:
        slug = str(result.get("slug") or "")
        page = pages.get(slug, {})
        edges = page.get("important_edges") if isinstance(page, dict) else None
        if not isinstance(edges, list):
            continue
        for edge in edges:
            if not isinstance(edge, list) or len(edge) < 2:
                continue
            target = str(edge[1])
            target_page = pages.get(target)
            if target_page and target_page.get("path") and target_page["path"] not in read_plan:
                read_plan.append(target_page["path"])
            if len(read_plan) >= max(top, len(results)) + max(0, hops) * 4:
                return read_plan
    return read_plan


def _load_semantic_plan(query: str, wiki_root: Path, top: int, hops: int, include_candidates: bool) -> dict[str, Any]:
    index, graph = _load_semantic_artifacts(wiki_root)
    pages = index.get("pages", {})
    if not isinstance(pages, dict):
        raise SystemExit("Invalid semantic index: missing object field 'pages'")
    query_terms = _tokenize_query(query)
    scored: list[tuple[float, str, dict[str, Any], list[str]]] = []
    for slug, page in pages.items():
        if not isinstance(page, dict):
            continue
        score, reasons = _score_page(query_terms, str(slug), page)
        if score > 0:
            scored.append((score, str(slug), page, reasons))
    scored.sort(key=lambda item: (-item[0], item[1]))
    selected = scored[: max(0, top)]
    results = [
        {
            "title": page.get("title"),
            "path": page.get("path"),
            "score": round(score, 3),
            "kind": page.get("kind"),
            "tags": page.get("tags", []),
            "reasons": reasons,
            "slug": slug,
        }
        for score, slug, page, reasons in selected
        if page.get("path")
    ]
    read_plan = _expand_read_plan(results, pages, hops, top)
    output: dict[str, Any] = {
        "graphGeneratedAt": index.get("generatedAt") or graph.get("generatedAt"),
        "counts": graph.get("counts", {}),
        "inferredRelationshipTypes": graph.get("relationshipTypes", []),
        "readThesePagesFirst": read_plan,
        "results": results,
        "queryEngine": "read-only-artifact-query",
    }
    if include_candidates:
        candidates_path = _artifact_path(wiki_root, "public/semantic/candidates.json")
        if candidates_path.exists():
            output["candidates"] = _load_json_file(candidates_path)
    return output


def _extract_snippet(text: str, query: str, chars: int) -> str:
    """Extract compact, query-centered context without dropping the read plan.

    The harness is a planner, not an answerer. Snippets are only preview context;
    callers still get exact page paths and must read markdown before relying on facts.
    For a fixed character budget, multiple short windows around distinct query-term
    hits are usually more informative than one long slice around the first hit.
    """
    compact = re.sub(r"\s+", " ", text).strip()
    if chars <= 0 or not compact:
        return ""

    terms = [t.lower() for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}", query)]
    lower = compact.lower()
    positions = []
    for term in terms:
        pos = lower.find(term)
        if pos >= 0:
            positions.append(pos)

    if not positions:
        return compact[:chars]

    # Keep distinct windows; avoid spending the whole budget on adjacent hits.
    positions = sorted(set(positions))
    selected: list[int] = []
    min_gap = max(80, chars // 4)
    for pos in positions:
        if all(abs(pos - chosen) >= min_gap for chosen in selected):
            selected.append(pos)
        if len(selected) >= 3:
            break

    per_window = max(120, chars // max(1, len(selected)))
    windows = []
    for pos in selected:
        start = max(0, pos - per_window // 3)
        end = min(len(compact), start + per_window)
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(compact) else ""
        windows.append(prefix + compact[start:end] + suffix)

    snippet = " / ".join(windows)
    return snippet[:chars]


def _snippet_chars_for_page(page_count: int, default_chars: int, context_budget_chars: int, min_snippet_chars: int) -> int:
    if default_chars <= 0:
        return 0
    if context_budget_chars <= 0 or page_count <= 0:
        return default_chars
    per_page = context_budget_chars // page_count
    if per_page <= 0:
        return 0
    # If the caller supplied a very small budget, honor it rather than silently
    # exceeding the requested context window.
    if context_budget_chars < page_count * min_snippet_chars:
        return min(default_chars, per_page)
    return min(default_chars, max(min_snippet_chars, per_page))


def _snapshot_tree(root: Path) -> dict[str, tuple[int, int]]:
    """Return a cheap read-only snapshot of file mtimes/sizes under root."""
    snap: dict[str, tuple[int, int]] = {}
    for path in root.rglob("*"):
        if path.is_file():
            try:
                stat = path.stat()
            except OSError:
                continue
            snap[str(path)] = (stat.st_mtime_ns, stat.st_size)
    return snap


def _assert_tree_unchanged(before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]) -> None:
    if before == after:
        return
    changed = []
    for path in sorted(set(before) | set(after)):
        if before.get(path) != after.get(path):
            changed.append(path)
        if len(changed) >= 20:
            break
    raise SystemExit(
        WRITE_PROHIBITION
        + "\nERROR: wiki tree changed during read-only harness execution. Changed paths:\n- "
        + "\n- ".join(changed)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only semantic wiki query harness for journal use.")
    parser.add_argument("query", help="Journal thought or lookup question to query against the wiki")
    parser.add_argument("--top", type=int, default=8, help="Number of pages to return from the semantic artifact query")
    parser.add_argument("--hops", type=int, default=1, help="Graph expansion hops")
    parser.add_argument("--profile", choices=["compact", "plan", "full"], default="compact", help="Context profile: compact keeps top/hops with a 2400-char snippet budget; plan returns paths only; full uses 900 chars per page")
    parser.add_argument("--snippet-chars", type=int, default=None, help="Maximum characters to show from each returned page; 0 disables page reads. Defaults depend on --profile")
    parser.add_argument("--context-budget-chars", type=int, default=None, help="Optional total snippet context budget across returned pages. Defaults depend on --profile")
    parser.add_argument("--min-snippet-chars", type=int, default=240, help="Minimum per-page snippet target when --context-budget-chars is large enough")
    parser.add_argument("--include-candidates", action="store_true", help="Include prebuilt semantic candidate output when available")
    parser.add_argument("--json", action="store_true", help="Emit compact JSON instead of markdown")
    args = parser.parse_args()

    wiki_root = DEFAULT_WIKI_ROOT
    wiki_src = DEFAULT_WIKI_SRC
    schema = wiki_src / "SCHEMA.md"
    if not schema.exists():
        raise SystemExit(f"Wiki source not found: expected {schema}")

    before = _snapshot_tree(wiki_root)
    profile_defaults = {
        "compact": {"snippet_chars": 900, "context_budget_chars": 2400},
        "plan": {"snippet_chars": 0, "context_budget_chars": 0},
        "full": {"snippet_chars": 900, "context_budget_chars": 0},
    }[args.profile]
    snippet_chars_requested = args.snippet_chars if args.snippet_chars is not None else profile_defaults["snippet_chars"]
    context_budget_chars = args.context_budget_chars if args.context_budget_chars is not None else profile_defaults["context_budget_chars"]

    plan = _load_semantic_plan(args.query, wiki_root, args.top, args.hops, args.include_candidates)
    read_plan = list(plan.get("readThesePagesFirst", []))
    # Preserve the artifact query read plan, but do not drop top result paths if
    # the plan omits them. This keeps retrieval quality intact while making the
    # harness output a complete read target list.
    for result in plan.get("results", []):
        result_path = result.get("path")
        if result_path and result_path not in read_plan:
            read_plan.append(result_path)
    snippet_chars_for_page = _snippet_chars_for_page(
        len(read_plan),
        max(0, snippet_chars_requested),
        max(0, context_budget_chars),
        max(0, args.min_snippet_chars),
    )
    pages = []
    for p in read_plan:
        resolved = _resolve_wiki_page(p, wiki_root, wiki_src)
        page_info: dict[str, Any] = {"path": p, "resolved": str(resolved) if resolved else None}
        if resolved and snippet_chars_for_page > 0:
            try:
                page_info["snippet"] = _extract_snippet(resolved.read_text(encoding="utf-8", errors="replace"), args.query, snippet_chars_for_page)
            except OSError as exc:
                page_info["read_error"] = str(exc)
        pages.append(page_info)

    output = {
        "mode": "read_only_wiki_context_for_journal",
        "query": args.query,
        "wiki_root": str(wiki_root),
        "wiki_src": str(wiki_src),
        "semantic_generated_at": plan.get("graphGeneratedAt"),
        "context_controls": {
            "profile": args.profile,
            "top": args.top,
            "hops": args.hops,
            "snippet_chars_requested": max(0, snippet_chars_requested),
            "context_budget_chars": max(0, context_budget_chars),
            "snippet_chars_per_page": snippet_chars_for_page,
            "min_snippet_chars": max(0, args.min_snippet_chars),
        },
        "counts": plan.get("counts", {}),
        "inferred_relationship_types": plan.get("inferredRelationshipTypes", []),
        "read_these_pages_first": pages,
        "results": [
            {
                "title": r.get("title"),
                "path": r.get("path"),
                "score": r.get("score"),
                "kind": r.get("kind"),
                "tags": r.get("tags", []),
                "reasons": r.get("reasons", []),
            }
            for r in plan.get("results", [])
        ],
        "rules": [
            WRITE_PROHIBITION,
            "Use wiki output as background context only; do not write to wiki from #journal.",
            "Do not answer from semantic summaries alone; read returned markdown pages before using facts.",
            "Separate user journal thoughts from wiki facts.",
            "Treat graph/candidate edges as retrieval hints, not claims, until supported by page text.",
        ],
    }

    _assert_tree_unchanged(before, _snapshot_tree(wiki_root))

    if args.json:
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        print(f"# Read-only wiki context plan\n")
        print(f"Query: {args.query}")
        print(f"Wiki: {wiki_src}")
        print(f"Semantic graph generated at: {output['semantic_generated_at']}")
        print(f"Context controls: {output['context_controls']}")
        print(f"Counts: {output['counts']}\n")
        print("## Read these pages first")
        if not pages:
            print("- No semantic matches returned.")
        for page in pages:
            print(f"- {page['path']}")
            if page.get("resolved"):
                print(f"  resolved: {page['resolved']}")
            if page.get("snippet"):
                print(f"  snippet: {page['snippet']}")
        print("\n## Rules")
        for rule in output["rules"]:
            print(f"- {rule}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
