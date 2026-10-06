#!/usr/bin/env python3
"""Evaluate verified passage retrieval without rebuilding or writing wiki state."""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from evaluate_retrieval import (
    EvaluationError,
    WIKI_ROOT,
    canonical_repo_path_identities,
    canonical_repo_path_identity,
    compact_json_bytes,
    load_json,
    selected_questions,
    validate_fixture,
)

FIXTURE_PATH = WIKI_ROOT / "_tools/fixtures/retrieval/questions.v1.json"
QUERY_PATH = WIKI_ROOT / ".vitepress/semantic-query.mjs"
VALID_SPLITS = ("all", "held-out", "tuning")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--split", choices=VALID_SPLITS, default="all")
    parser.add_argument("--top", type=int, default=8)
    parser.add_argument("--max-passages", type=int, default=12)
    parser.add_argument("--max-evidence-chars", type=int, default=24000)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--report-out", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.bundle_dir.is_absolute():
        parser.error("--bundle-dir must be absolute")
    if not 1 <= args.top <= 30:
        parser.error("--top must be between 1 and 30")
    if not 1 <= args.max_passages <= 30:
        parser.error("--max-passages must be between 1 and 30")
    if not 1 <= args.max_evidence_chars <= 1_000_000:
        parser.error("--max-evidence-chars must be between 1 and 1000000")
    return args


def overlaps(passage: dict[str, Any], path: str, start: int, end: int) -> bool:
    return (
        passage.get("verified") is True
        and passage.get("path") == path
        and isinstance(passage.get("lineStart"), int)
        and isinstance(passage.get("lineEnd"), int)
        and passage["lineStart"] <= end
        and passage["lineEnd"] >= start
    )


def run_query(question: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    command = [
        "node",
        str(QUERY_PATH),
        question["question"],
        "--bundle-dir",
        str(args.bundle_dir),
        "--top",
        str(args.top),
        "--max-passages",
        str(args.max_passages),
        "--max-evidence-chars",
        str(args.max_evidence_chars),
        "--json",
    ]
    started = time.perf_counter()
    completed = subprocess.run(command, cwd=WIKI_ROOT, text=True, capture_output=True, check=False)
    latency_ms = (time.perf_counter() - started) * 1000
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise EvaluationError(
            f"query {question['id']} failed with exit {completed.returncode}: {detail}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise EvaluationError(f"query {question['id']} emitted invalid JSON: {exc}") from exc
    if payload.get("status") not in {"match", "no_match"}:
        raise EvaluationError(
            f"query {question['id']} returned invalid benchmark status: {payload.get('status')!r}"
        )
    return {
        "id": question["id"],
        "latencyMs": round(latency_ms, 3),
        "jsonBytes": len(completed.stdout.encode("utf-8")),
        "payload": payload,
    }


def score_case(question: dict[str, Any], execution: dict[str, Any]) -> dict[str, Any]:
    payload = execution["payload"]
    passages = payload.get("passages") if isinstance(payload.get("passages"), list) else []
    results = payload.get("results") if isinstance(payload.get("results"), list) else []
    result_paths = [
        canonical_repo_path_identity(result.get("path"), f"{question['id']}.results.path")
        for result in results
    ]
    passage_paths = [
        canonical_repo_path_identity(passage.get("path"), f"{question['id']}.passages.path")
        for passage in passages
    ]
    returned_paths = set(result_paths) | set(passage_paths)

    required_ranges: list[dict[str, Any]] = []
    for evidence_index, item in enumerate(question["requiredEvidencePassages"]):
        path = canonical_repo_path_identity(
            item["path"], f"{question['id']}.requiredEvidencePassages[{evidence_index}].path"
        )
        for line_range in item["lineRanges"]:
            hit = any(overlaps(passage, path, line_range["start"], line_range["end"]) for passage in passages)
            required_ranges.append(
                {"path": path, "start": line_range["start"], "end": line_range["end"], "hit": hit}
            )

    required_pages = canonical_repo_path_identities(question["requiredPages"], f"{question['id']}.requiredPages")
    evidence_pages = canonical_repo_path_identities(
        (item["path"] for item in question["requiredEvidencePassages"]),
        f"{question['id']}.requiredEvidencePassages",
    )
    distractors = set(canonical_repo_path_identities(question["distractorPages"], f"{question['id']}.distractors"))
    facets = []
    for facet_index, facet in enumerate(question["requiredFacets"]):
        supporting = set(
            canonical_repo_path_identities(
                facet["supportingPages"], f"{question['id']}.requiredFacets[{facet_index}].supportingPages"
            )
        )
        facets.append({"id": facet["id"], "scorable": bool(supporting), "covered": bool(returned_paths & supporting)})

    range_hits = sum(item["hit"] for item in required_ranges)
    range_recall = range_hits / len(required_ranges) if required_ranges else None
    page_hits = sum(path in returned_paths for path in required_pages)
    evidence_page_hits = sum(path in returned_paths for path in evidence_pages)
    scorable_facets = [facet for facet in facets if facet["scorable"]]
    facet_hits = sum(facet["covered"] for facet in scorable_facets)
    expected = question["expectedStatus"]
    status = payload.get("status")
    status_ok = status == "no_match" if expected == "no_match" else status == "match"

    return {
        "id": question["id"],
        "split": question["split"],
        "question": question["question"],
        "expectedStatus": expected,
        "status": status,
        "statusOk": status_ok,
        "latencyMs": execution["latencyMs"],
        "jsonBytes": execution["jsonBytes"],
        "contextBytes": compact_json_bytes(passages),
        "returnedPages": result_paths,
        "returnedPassages": [
            {
                "path": passage["path"],
                "lineStart": passage["lineStart"],
                "lineEnd": passage["lineEnd"],
                "role": passage["role"],
                "score": passage["score"],
            }
            for passage in passages
        ],
        "requiredPageRecall": page_hits / len(required_pages) if required_pages else None,
        "evidenceParentRecall": evidence_page_hits / len(evidence_pages) if evidence_pages else None,
        "passageRangeRecall": range_recall,
        "requiredRangeHits": range_hits,
        "requiredRangeCount": len(required_ranges),
        "requiredRanges": required_ranges,
        "facetCoverage": facet_hits / len(scorable_facets) if scorable_facets else None,
        "coveredFacets": [facet["id"] for facet in scorable_facets if facet["covered"]],
        "missingFacets": [facet["id"] for facet in scorable_facets if not facet["covered"]],
        "distractorPages": [path for path in result_paths if path in distractors],
    }


def mean(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return round(statistics.fmean(present), 6) if present else None


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * fraction + 0.999999) - 1))
    return round(ordered[index], 3)


def aggregate(cases: list[dict[str, Any]], args: argparse.Namespace, fixture_validation: dict[str, Any]) -> dict[str, Any]:
    total_ranges = sum(case["requiredRangeCount"] for case in cases)
    range_hits = sum(case["requiredRangeHits"] for case in cases)
    facet_denominator = sum(len(case["coveredFacets"]) + len(case["missingFacets"]) for case in cases)
    facet_hits = sum(len(case["coveredFacets"]) for case in cases)
    latencies = [case["latencyMs"] for case in cases]
    return {
        "benchmark": "simplified-passage-search-v1",
        "date": "2026-09-21",
        "configuration": {
            "split": args.split,
            "bundleDir": str(args.bundle_dir),
            "top": args.top,
            "maxPassages": args.max_passages,
            "maxEvidenceChars": args.max_evidence_chars,
            "heldOutPassageRecallTarget": 0.9,
        },
        "fixtureValidation": fixture_validation,
        "metrics": {
            "questionCount": len(cases),
            "passageRangeRecallMacro": mean([case["passageRangeRecall"] for case in cases]),
            "passageRangeRecallMicro": round(range_hits / total_ranges, 6) if total_ranges else None,
            "passageRangeHits": range_hits,
            "passageRangeCount": total_ranges,
            "requiredPageRecallMacro": mean([case["requiredPageRecall"] for case in cases]),
            "evidenceParentRecallMacro": mean([case["evidenceParentRecall"] for case in cases]),
            "facetCoverageMicro": round(facet_hits / facet_denominator, 6) if facet_denominator else None,
            "statusCorrect": sum(case["statusOk"] for case in cases),
            "statusEvaluated": len(cases),
            "distractorPageAdmissions": sum(len(case["distractorPages"]) for case in cases),
            "meanLatencyMs": round(statistics.fmean(latencies), 3),
            "p95LatencyMs": percentile(latencies, 0.95),
            "maxLatencyMs": round(max(latencies), 3),
            "meanContextBytes": round(statistics.fmean(case["contextBytes"] for case in cases), 3),
            "maxContextBytes": max(case["contextBytes"] for case in cases),
        },
        "cases": cases,
    }


def report(result: dict[str, Any]) -> str:
    metrics = result["metrics"]
    lines = [
        "# Simplified passage retrieval evaluation",
        "",
        f"Date: {result['date']}",
        f"Split: {result['configuration']['split']}",
        f"Questions: {metrics['questionCount']}",
        "",
        "## Metrics",
        "",
        f"- Passage range recall, macro: {metrics['passageRangeRecallMacro']}",
        f"- Passage range recall, micro: {metrics['passageRangeRecallMicro']} ({metrics['passageRangeHits']}/{metrics['passageRangeCount']})",
        f"- Required-page recall, macro: {metrics['requiredPageRecallMacro']}",
        f"- Evidence-parent recall, macro: {metrics['evidenceParentRecallMacro']}",
        f"- Facet coverage, micro: {metrics['facetCoverageMicro']}",
        f"- Status correct: {metrics['statusCorrect']}/{metrics['statusEvaluated']}",
        f"- Distractor-page admissions: {metrics['distractorPageAdmissions']}",
        f"- Latency mean/p95/max: {metrics['meanLatencyMs']} / {metrics['p95LatencyMs']} / {metrics['maxLatencyMs']} ms",
        f"- Context bytes mean/max: {metrics['meanContextBytes']} / {metrics['maxContextBytes']}",
        "",
        "## Cases",
        "",
        "| ID | Status | Passage recall | Page recall | Facets | Distractors | Latency ms | Context bytes |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for case in result["cases"]:
        lines.append(
            f"| {case['id']} | {case['status']} | {case['passageRangeRecall']} | "
            f"{case['requiredPageRecall']} | {case['facetCoverage']} | {len(case['distractorPages'])} | "
            f"{case['latencyMs']} | {case['contextBytes']} |"
        )
    return "\n".join(lines) + "\n"


def write_output(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    fixture = load_json(FIXTURE_PATH)
    fixture_validation = validate_fixture(fixture)
    questions = selected_questions(fixture, args.split)
    executions = [run_query(question, args) for question in questions]
    cases = [score_case(question, execution) for question, execution in zip(questions, executions, strict=True)]
    result = aggregate(cases, args, fixture_validation)
    write_output(args.json_out, json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    write_output(args.report_out, report(result))
    print(json.dumps({"status": "ok", **result["metrics"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
