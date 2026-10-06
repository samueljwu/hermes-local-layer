#!/usr/bin/env python3
"""Evaluate the existing wiki semantic-query planner without rebuilding artifacts."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import re
import stat
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, TypeVar

SCRIPT_PATH = Path(__file__).resolve()
WIKI_ROOT = SCRIPT_PATH.parents[1]
FIXTURE_PATH = WIKI_ROOT / "_tools/fixtures/retrieval/questions.v1.json"
SCHEMA_PATH = WIKI_ROOT / "_tools/fixtures/retrieval/retrieval-eval.schema.json"
SEMANTIC_QUERY_PATH = WIKI_ROOT / ".vitepress/semantic-query.mjs"
SEMANTIC_GRAPH_PATH = WIKI_ROOT / "src/_meta/semantic/graph.json"
SEMANTIC_INDEX_PATH = WIKI_ROOT / "src/_meta/semantic/index.json"
BASELINE_JSON_PATH = FIXTURE_PATH.parent / "baseline.v1.json"
BASELINE_REPORT_PATH = FIXTURE_PATH.parent / "baseline.v1.md"
BASELINE_RESULTS_PATH = FIXTURE_PATH.parent / "baseline.v1.results.json"
DEFAULT_TOP = 8
DEFAULT_HOPS = 1
VALID_SPLITS = {"tuning", "held-out", "all"}
VALID_STATUSES = {"match", "partial_unsupported", "no_match"}
RESULT_RELEVANCE_CLASSES = (
    "required",
    "acceptable_alternative",
    "facet_supporting",
    "enumerated_distractor",
    "unclassified",
)
PAGE_KEYS = (
    "requiredPages",
    "acceptableAlternativePages",
    "distractorPages",
)
LIVE_EXCLUDED_DIRECTORY_NAMES = {
    ".git",
    ".pytest_cache",
    ".tmp",
    ".venv",
    "__pycache__",
    "dist",
    "node_modules",
}
LIVE_EXCLUDED_DIRECTORY_PREFIXES = (".dist.build.", ".dist.previous.")
VCS_METADATA_DIRECTORY_NAMES = frozenset({".git", ".hg", ".svn"})
T = TypeVar("T")


class EvaluationError(RuntimeError):
    """Raised for invalid inputs or failed retrieval execution."""


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Score the existing .vitepress/semantic-query.mjs page-ranking CLI "
            "against the Phase 1 retrieval fixture. No generator or build is run."
        )
    )
    parser.add_argument("--split", choices=sorted(VALID_SPLITS), default="all")
    parser.add_argument("--top", type=int, default=None, help=f"Top K pages (default: {DEFAULT_TOP})")
    parser.add_argument("--hops", type=int, default=None, help=f"Graph hops (default: {DEFAULT_HOPS})")
    parser.add_argument("--json-out", type=Path, help="Write the machine-readable evaluation JSON")
    parser.add_argument("--report-out", type=Path, help="Write the Markdown evaluation report")
    replay = parser.add_mutually_exclusive_group()
    replay.add_argument(
        "--input-results",
        type=Path,
        help="Re-score a saved result snapshot without invoking Node",
    )
    replay.add_argument(
        "--save-results",
        type=Path,
        help="Save live semantic-query payloads and measured timings for replay",
    )
    args = parser.parse_args(argv)

    if args.top is not None and not 1 <= args.top <= 30:
        parser.error("--top must be between 1 and 30")
    if args.hops is not None and not 0 <= args.hops <= 3:
        parser.error("--hops must be between 0 and 3")
    if args.json_out is None and args.report_out is None:
        args.json_out = Path("-")
    if args.json_out == Path("-") and args.report_out == Path("-"):
        parser.error("--json-out - and --report-out - cannot both use stdout")
    if args.input_results == Path("-"):
        parser.error("--input-results does not support '-'; provide a saved JSON file path")
    if args.save_results == Path("-"):
        parser.error("--save-results does not support '-'; provide a destination file path")
    return args


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvaluationError(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise EvaluationError(f"invalid JSON in {path}: {exc}") from exc


def require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def json_path(parts: Iterable[Any]) -> str:
    rendered = "$"
    for part in parts:
        rendered += f"[{part}]" if isinstance(part, int) else f".{part}"
    return rendered


def validate_against_schema(fixture: Any, schema_path: Path = SCHEMA_PATH) -> None:
    try:
        jsonschema = importlib.import_module("jsonschema")
    except ModuleNotFoundError as exc:
        if exc.name != "jsonschema":
            raise
        raise EvaluationError(
            "fixture schema validation requires the Python package 'jsonschema'; "
            "run the evaluator with 'uv run --with jsonschema python3 "
            "_tools/evaluate_retrieval.py ...'"
        ) from exc

    schema = load_json(schema_path)
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
        validator = jsonschema.Draft202012Validator(
            schema,
            format_checker=jsonschema.FormatChecker(),
        )
    except jsonschema.exceptions.SchemaError as exc:
        raise EvaluationError(f"invalid fixture schema {schema_path}: {exc.message}") from exc

    validation_errors = sorted(
        validator.iter_errors(fixture),
        key=lambda error: (json_path(error.absolute_path), error.message),
    )
    if validation_errors:
        rendered = [
            f"{json_path(error.absolute_path)}: {error.message}"
            for error in validation_errors[:50]
        ]
        if len(validation_errors) > 50:
            rendered.append(f"... {len(validation_errors) - 50} more")
        raise EvaluationError(
            f"fixture schema validation failed ({len(validation_errors)} errors):\n- "
            + "\n- ".join(rendered)
        )


def ensure_repo_path(
    path_text: Any,
    label: str,
    errors: list[str],
    wiki_root: Path = WIKI_ROOT,
) -> tuple[str, Path] | None:
    require(isinstance(path_text, str), f"{label}: path must be a string", errors)
    if not isinstance(path_text, str):
        return None

    pure_path = PurePosixPath(path_text)
    components = path_text.split("/")
    require(bool(path_text), f"{label}: path must not be empty", errors)
    require("\\" not in path_text, f"{label}: path must use POSIX '/' separators: {path_text}", errors)
    require(not pure_path.is_absolute(), f"{label}: path must be repository-relative: {path_text}", errors)
    require(
        all(component not in {"", ".", ".."} for component in components),
        f"{label}: path must not contain empty, '.', or '..' components: {path_text}",
        errors,
    )
    require(
        len(components) > 1 and components[0] == "src",
        f"{label}: path must be under the src/ root: {path_text}",
        errors,
    )
    require(path_text.endswith(".md"), f"{label}: path must end in .md: {path_text}", errors)
    if (
        not path_text
        or "\\" in path_text
        or pure_path.is_absolute()
        or len(components) <= 1
        or components[0] != "src"
        or not path_text.endswith(".md")
    ):
        return None

    root = wiki_root.resolve()
    allowed_root = (root / "src").resolve(strict=False)
    try:
        candidate = root.joinpath(*components).resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError):
        errors.append(f"{label}: missing or unresolvable file: {path_text}")
        return None
    try:
        candidate.relative_to(allowed_root)
        canonical_identity = candidate.relative_to(root).as_posix()
    except ValueError:
        errors.append(f"{label}: path resolves outside the allowed src/ root: {path_text}")
        return None
    require(candidate.is_file(), f"{label}: missing file: {path_text}", errors)
    require(
        path_text == canonical_identity,
        (
            f"{label}: path must equal its canonical POSIX repository-relative identity "
            f"{canonical_identity!r}, found {path_text!r}"
        ),
        errors,
    )
    return canonical_identity, candidate


def canonical_repo_path_identity(
    path_text: Any,
    label: str,
    wiki_root: Path = WIKI_ROOT,
) -> str:
    errors: list[str] = []
    validated = ensure_repo_path(path_text, label, errors, wiki_root)
    if errors or validated is None:
        detail = "; ".join(errors) if errors else f"{label}: invalid repository path"
        raise EvaluationError(detail)
    return validated[0]


def canonical_repo_path_identities(
    paths: Iterable[Any],
    label: str,
    wiki_root: Path = WIKI_ROOT,
) -> list[str]:
    return list(
        dict.fromkeys(
            canonical_repo_path_identity(path, f"{label}[{index}]", wiki_root)
            for index, path in enumerate(paths)
        )
    )


def validate_fixture(
    fixture: Any,
    schema_path: Path = SCHEMA_PATH,
    wiki_root: Path = WIKI_ROOT,
) -> dict[str, Any]:
    validate_against_schema(fixture, schema_path)
    assert isinstance(fixture, dict)
    questions = fixture["questions"]
    assert isinstance(questions, list)
    errors: list[str] = []
    seen_ids: set[str] = set()
    path_cache: dict[str, tuple[Path, int]] = {}
    all_paths: set[str] = set()
    mapped_ranges = 0
    evidence_entries = 0
    facet_count = 0

    def validate_path(path_text: str, label: str) -> str | None:
        validated = ensure_repo_path(path_text, label, errors, wiki_root)
        if validated is None:
            return None
        identity, path = validated
        all_paths.add(identity)
        if identity in path_cache:
            return identity
        try:
            with path.open("r", encoding="utf-8") as handle:
                line_count = sum(1 for _ in handle)
        except UnicodeDecodeError as exc:
            errors.append(f"{label}: non-UTF-8 markdown file: {path_text}: {exc}")
            return identity
        path_cache[identity] = (path, line_count)
        return identity

    for index, question in enumerate(questions):
        assert isinstance(question, dict)
        label = f"questions[{index}]"
        question_id = question["id"]
        require(bool(question_id.strip()), f"{label}: id must not be whitespace-only", errors)
        require(question_id not in seen_ids, f"duplicate question id: {question_id}", errors)
        seen_ids.add(question_id)
        require(bool(question["question"].strip()), f"{label}: question must not be whitespace-only", errors)

        canonical_page_lists: dict[str, list[str]] = {}
        for key in PAGE_KEYS:
            canonical_paths: list[str] = []
            seen_canonical_paths: set[str] = set()
            for path_index, path_text in enumerate(question[key]):
                identity = validate_path(path_text, f"{label}.{key}[{path_index}]")
                if identity is None:
                    continue
                require(
                    identity not in seen_canonical_paths,
                    f"{label}.{key} contains duplicate canonical path: {identity}",
                    errors,
                )
                if identity not in seen_canonical_paths:
                    canonical_paths.append(identity)
                    seen_canonical_paths.add(identity)
            canonical_page_lists[key] = canonical_paths

        evidence_paths: set[str] = set()
        evidence = question["requiredEvidencePassages"]
        evidence_entries += len(evidence)
        for evidence_index, item in enumerate(evidence):
            item_label = f"{label}.requiredEvidencePassages[{evidence_index}]"
            path_text = item["path"]
            identity = validate_path(path_text, f"{item_label}.path")
            if identity is not None:
                require(
                    identity not in evidence_paths,
                    (
                        f"{label}.requiredEvidencePassages contains duplicate canonical "
                        f"parent path: {identity}"
                    ),
                    errors,
                )
                evidence_paths.add(identity)
            line_count = path_cache.get(identity, (None, 0))[1] if identity is not None else 0
            for range_index, line_range in enumerate(item["lineRanges"]):
                range_label = f"{item_label}.lineRanges[{range_index}]"
                start = line_range["start"]
                end = line_range["end"]
                require(start <= end, f"{range_label}: start exceeds end", errors)
                require(
                    end <= line_count,
                    f"{range_label}: end {end} exceeds {path_text} line count {line_count}",
                    errors,
                )
                mapped_ranges += 1

        facets = question["requiredFacets"]
        facet_count += len(facets)
        facet_ids: set[str] = set()
        facet_supporting_paths: set[str] = set()
        for facet_index, facet in enumerate(facets):
            facet_label = f"{label}.requiredFacets[{facet_index}]"
            facet_id = facet["id"]
            require(facet_id not in facet_ids, f"{label}: duplicate facet id {facet_id}", errors)
            facet_ids.add(facet_id)
            for path_index, path_text in enumerate(facet["supportingPages"]):
                identity = validate_path(
                    path_text,
                    f"{facet_label}.supportingPages[{path_index}]",
                )
                if identity is not None:
                    facet_supporting_paths.add(identity)

        required_paths = set(canonical_page_lists["requiredPages"])
        alternative_paths = set(canonical_page_lists["acceptableAlternativePages"])
        distractor_paths = set(canonical_page_lists["distractorPages"])
        required_alternative_overlap = sorted(required_paths.intersection(alternative_paths))
        require(
            not required_alternative_overlap,
            (
                f"{label}: required and acceptable-alternative page sets overlap: "
                f"{required_alternative_overlap}"
            ),
            errors,
        )
        relevant_paths = (
            required_paths
            | alternative_paths
            | evidence_paths
            | facet_supporting_paths
        )
        overlap = sorted(relevant_paths.intersection(distractor_paths))
        require(
            not overlap,
            f"{label}: relevant and distractor page sets overlap: {overlap}",
            errors,
        )

    split_counts = Counter(question["split"] for question in questions)
    status_counts = Counter(question["expectedStatus"] for question in questions)
    require(split_counts["tuning"] > 0, "tuning split must contain at least one question", errors)
    require(split_counts["held-out"] > 0, "held-out split must contain at least one question", errors)
    declared_counts = fixture.get("splitPolicy", {}).get("counts")
    if not isinstance(declared_counts, dict):
        errors.append("splitPolicy.counts must be an object with tuning, held-out, and all counts")
    else:
        actual_counts = {
            "tuning": split_counts["tuning"],
            "held-out": split_counts["held-out"],
            "all": len(questions),
        }
        for key, actual in actual_counts.items():
            require(
                declared_counts.get(key) == actual,
                f"splitPolicy.counts.{key} must equal actual count {actual}, found {declared_counts.get(key)!r}",
                errors,
            )

    if errors:
        preview = "\n".join(f"- {error}" for error in errors[:50])
        suffix = f"\n- ... {len(errors) - 50} more" if len(errors) > 50 else ""
        raise EvaluationError(f"fixture semantic validation failed ({len(errors)} errors):\n{preview}{suffix}")

    return {
        "status": "passed",
        "questionCount": len(questions),
        "splitCounts": dict(sorted(split_counts.items())),
        "expectedStatusCounts": dict(sorted(status_counts.items())),
        "uniqueRepositoryPaths": len(all_paths),
        "evidenceEntries": evidence_entries,
        "mappedLineRanges": mapped_ranges,
        "requiredFacets": facet_count,
        "schemaDocumentPresent": schema_path.is_file(),
        "schemaValidation": "draft-2020-12-with-format-checker",
    }


def selected_questions(fixture: dict[str, Any], split: str) -> list[dict[str, Any]]:
    questions = fixture["questions"]
    return questions if split == "all" else [question for question in questions if question["split"] == split]


def compact_json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def is_nonnegative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate_execution_case(
    case: Any,
    question: dict[str, Any],
    top: int,
    hops: int,
    label: str,
) -> dict[str, Any]:
    errors: list[str] = []
    if not isinstance(case, dict):
        raise EvaluationError(f"{label}: case must be an object")
    expected_case_keys = {"id", "query", "latencyMs", "jsonBytes", "contextBytes", "payload"}
    extra_case_keys = sorted(set(case) - expected_case_keys)
    missing_case_keys = sorted(expected_case_keys - set(case))
    require(not missing_case_keys, f"{label}: missing case keys: {missing_case_keys}", errors)
    require(not extra_case_keys, f"{label}: unexpected case keys: {extra_case_keys}", errors)
    require(case.get("id") == question["id"], f"{label}: id does not match question {question['id']!r}", errors)
    require(case.get("query") == question["question"], f"{label}: query does not match fixture", errors)

    latency = case.get("latencyMs")
    require(
        is_finite_number(latency) and float(latency) >= 0,
        f"{label}.latencyMs must be a finite nonnegative number",
        errors,
    )
    for key in ("jsonBytes", "contextBytes"):
        require(
            is_nonnegative_integer(case.get(key)),
            f"{label}.{key} must be a nonnegative integer",
            errors,
        )

    payload = case.get("payload")
    if not isinstance(payload, dict):
        errors.append(f"{label}.payload must be an object")
    else:
        expected_payload_keys = {
            "query",
            "generatedAt",
            "graphGeneratedAt",
            "options",
            "inferredRelationshipTypes",
            "counts",
            "readThesePagesFirst",
            "results",
        }
        missing_payload_keys = sorted(expected_payload_keys - set(payload))
        extra_payload_keys = sorted(set(payload) - expected_payload_keys)
        require(
            not missing_payload_keys,
            f"{label}.payload missing keys: {missing_payload_keys}",
            errors,
        )
        require(
            not extra_payload_keys,
            f"{label}.payload has unexpected keys: {extra_payload_keys}",
            errors,
        )
        require(payload.get("query") == question["question"], f"{label}.payload.query does not match fixture", errors)
        options = payload.get("options")
        if not isinstance(options, dict):
            errors.append(f"{label}.payload.options must be an object")
        else:
            expected_option_keys = {"top", "hops", "json", "includeCandidates", "query"}
            require(
                set(options) == expected_option_keys,
                f"{label}.payload.options keys must equal {sorted(expected_option_keys)}",
                errors,
            )
            require(options.get("top") == top, f"{label}.payload.options.top must equal {top}", errors)
            require(options.get("hops") == hops, f"{label}.payload.options.hops must equal {hops}", errors)
            require(options.get("json") is True, f"{label}.payload.options.json must be true", errors)
            require(
                isinstance(options.get("includeCandidates"), bool),
                f"{label}.payload.options.includeCandidates must be a boolean",
                errors,
            )
            require(
                options.get("query") == question["question"],
                f"{label}.payload.options.query does not match fixture",
                errors,
            )

        result_path_identities: list[str] = []
        results = payload.get("results")
        if not isinstance(results, list):
            errors.append(f"{label}.payload.results must be an array")
        else:
            require(len(results) <= top, f"{label}.payload.results exceeds configured top={top}", errors)
            seen_path_identities: set[str] = set()
            results_valid_for_size = True
            expected_result_keys = {
                "aliases",
                "baseScore",
                "graphBoost",
                "id",
                "importantEdges",
                "kind",
                "path",
                "readWhen",
                "reasons",
                "route",
                "score",
                "summary",
                "supportingEdges",
                "tags",
                "title",
            }
            for result_index, result in enumerate(results):
                result_label = f"{label}.payload.results[{result_index}]"
                if not isinstance(result, dict):
                    errors.append(f"{result_label} must be an object")
                    results_valid_for_size = False
                    continue
                require(
                    set(result) == expected_result_keys,
                    f"{result_label} keys must equal {sorted(expected_result_keys)}",
                    errors,
                )
                path_text = result.get("path")
                path_errors_before = len(errors)
                validated_path = ensure_repo_path(path_text, f"{result_label}.path", errors)
                if len(errors) != path_errors_before:
                    results_valid_for_size = False
                if validated_path is not None:
                    path_identity = validated_path[0]
                    require(
                        path_identity not in seen_path_identities,
                        (
                            f"{label}.payload.results has duplicate canonical path: "
                            f"{path_identity}"
                        ),
                        errors,
                    )
                    seen_path_identities.add(path_identity)
                    result_path_identities.append(path_identity)
                require(
                    is_finite_number(result.get("score")),
                    f"{result_label}.score must be a finite number",
                    errors,
                )
                require(
                    isinstance(result.get("id"), str) and bool(result["id"].strip()),
                    f"{result_label}.id must be a non-empty string",
                    errors,
                )
                for text_key in ("kind", "route"):
                    require(
                        isinstance(result.get(text_key), str) and bool(result[text_key].strip()),
                        f"{result_label}.{text_key} must be a non-empty string",
                        errors,
                    )
                for text_key in ("title", "summary"):
                    require(
                        result.get(text_key) is None or isinstance(result.get(text_key), str),
                        f"{result_label}.{text_key} must be a string or null",
                        errors,
                    )
                for list_key in ("aliases", "readWhen", "reasons", "tags"):
                    require(
                        isinstance(result.get(list_key), list)
                        and all(isinstance(value, str) for value in result[list_key]),
                        f"{result_label}.{list_key} must be an array of strings",
                        errors,
                    )
                for list_key in ("importantEdges", "supportingEdges"):
                    require(
                        isinstance(result.get(list_key), list),
                        f"{result_label}.{list_key} must be an array",
                        errors,
                    )
                supporting_edges = result.get("supportingEdges")
                if isinstance(supporting_edges, list):
                    for edge_index, edge in enumerate(supporting_edges):
                        edge_label = f"{result_label}.supportingEdges[{edge_index}]"
                        if not isinstance(edge, dict):
                            errors.append(f"{edge_label} must be an object")
                            continue
                        edge_evidence = edge.get("evidence")
                        if not isinstance(edge_evidence, list):
                            errors.append(f"{edge_label}.evidence must be an array")
                            continue
                        for evidence_index, evidence_item in enumerate(edge_evidence):
                            evidence_label = f"{edge_label}.evidence[{evidence_index}]"
                            if not isinstance(evidence_item, dict):
                                errors.append(f"{evidence_label} must be an object")
                                continue
                            ensure_repo_path(
                                evidence_item.get("page"),
                                f"{evidence_label}.page",
                                errors,
                            )
                for score_key in ("baseScore", "graphBoost"):
                    require(
                        is_finite_number(result.get(score_key)),
                        f"{result_label}.{score_key} must be a finite number",
                        errors,
                    )
            if results_valid_for_size and is_nonnegative_integer(case.get("contextBytes")):
                require(
                    case["contextBytes"] == compact_json_bytes(results),
                    f"{label}.contextBytes does not match the compact results payload",
                    errors,
                )

        counts = payload.get("counts")
        if not isinstance(counts, dict):
            errors.append(f"{label}.payload.counts must be an object")
        elif isinstance(payload.get("results"), list):
            expected_count_keys = {"candidates", "returned", "graphNodes", "graphEdges"}
            require(
                set(counts) == expected_count_keys,
                f"{label}.payload.counts keys must equal {sorted(expected_count_keys)}",
                errors,
            )
            require(
                counts.get("returned") == len(payload["results"]),
                f"{label}.payload.counts.returned does not match results length",
                errors,
            )
            for count_key in expected_count_keys:
                require(
                    is_nonnegative_integer(counts.get(count_key)),
                    f"{label}.payload.counts.{count_key} must be a nonnegative integer",
                    errors,
                )
            if is_nonnegative_integer(counts.get("candidates")) and is_nonnegative_integer(counts.get("returned")):
                require(
                    counts["candidates"] >= counts["returned"],
                    f"{label}.payload.counts.candidates must be at least returned",
                    errors,
                )

        first_pages = payload.get("readThesePagesFirst")
        if not isinstance(first_pages, list):
            errors.append(f"{label}.payload.readThesePagesFirst must be an array")
        else:
            require(
                all(isinstance(path, str) for path in first_pages),
                f"{label}.payload.readThesePagesFirst must contain only strings",
                errors,
            )
            if all(isinstance(path, str) for path in first_pages):
                first_page_identities: list[str] = []
                for path_index, path_text in enumerate(first_pages):
                    validated_path = ensure_repo_path(
                        path_text,
                        f"{label}.payload.readThesePagesFirst[{path_index}]",
                        errors,
                    )
                    if validated_path is not None:
                        first_page_identities.append(validated_path[0])
                require(
                    len(first_page_identities) == len(set(first_page_identities)),
                    f"{label}.payload.readThesePagesFirst contains duplicate canonical paths",
                    errors,
                )
                require(
                    all(path in result_path_identities for path in first_page_identities),
                    f"{label}.payload.readThesePagesFirst must be drawn from results",
                    errors,
                )

        relation_types = payload.get("inferredRelationshipTypes")
        require(
            isinstance(relation_types, list) and all(isinstance(value, str) for value in relation_types),
            f"{label}.payload.inferredRelationshipTypes must be an array of strings",
            errors,
        )
        for timestamp_key in ("generatedAt", "graphGeneratedAt"):
            require(
                isinstance(payload.get(timestamp_key), str) and bool(payload[timestamp_key].strip()),
                f"{label}.payload.{timestamp_key} must be a non-empty string",
                errors,
            )

    if errors:
        preview = "\n".join(f"- {error}" for error in errors[:30])
        suffix = f"\n- ... {len(errors) - 30} more" if len(errors) > 30 else ""
        raise EvaluationError(f"{label} validation failed ({len(errors)} errors):\n{preview}{suffix}")
    return case


def run_semantic_query(question: dict[str, Any], top: int, hops: int) -> dict[str, Any]:
    command = [
        "node",
        str(SEMANTIC_QUERY_PATH.relative_to(WIKI_ROOT)),
        question["question"],
        "--top",
        str(top),
        "--hops",
        str(hops),
        "--json",
    ]
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=WIKI_ROOT,
            check=False,
            capture_output=True,
            text=False,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise EvaluationError("node executable was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise EvaluationError(f"semantic query timed out for {question['id']}") from exc
    latency_ms = (time.perf_counter() - started) * 1000.0
    stdout = completed.stdout
    stderr = completed.stderr.decode("utf-8", errors="replace").strip()
    if completed.returncode != 0:
        raise EvaluationError(
            f"semantic query failed for {question['id']} with exit {completed.returncode}: {stderr or '<no stderr>'}"
        )
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"semantic query returned invalid JSON for {question['id']}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise EvaluationError(f"semantic query returned an invalid payload shape for {question['id']}")
    execution = {
        "id": question["id"],
        "query": question["question"],
        "latencyMs": round(latency_ms, 3),
        "jsonBytes": len(stdout),
        "contextBytes": compact_json_bytes(payload["results"]),
        "payload": payload,
    }
    return validate_execution_case(
        execution,
        question,
        top,
        hops,
        f"semantic query result for {question['id']}",
    )


def build_live_results(questions: list[dict[str, Any]], top: int, hops: int) -> list[dict[str, Any]]:
    return [run_semantic_query(question, top, hops) for question in questions]


def result_snapshot(
    fixture: dict[str, Any],
    split: str,
    top: int,
    hops: int,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "benchmarkVersion": fixture["benchmarkVersion"],
        "configuration": {"split": split, "top": top, "hops": hops},
        "questionCount": len(results),
        "cases": results,
    }


def load_replay_results(
    path: Path,
    fixture: dict[str, Any],
    questions: list[dict[str, Any]],
    requested_split: str,
    requested_top: int | None,
    requested_hops: int | None,
) -> tuple[int, int, list[dict[str, Any]]]:
    snapshot = load_json(path)
    if not isinstance(snapshot, dict):
        raise EvaluationError(f"invalid result snapshot: {path}")
    expected_snapshot_keys = {
        "schemaVersion",
        "benchmarkVersion",
        "configuration",
        "questionCount",
        "cases",
    }
    if set(snapshot) != expected_snapshot_keys:
        raise EvaluationError(
            "result snapshot keys must equal " + repr(sorted(expected_snapshot_keys))
        )
    if snapshot.get("schemaVersion") != 1:
        raise EvaluationError(f"invalid result snapshot schemaVersion: {path}")
    if snapshot.get("benchmarkVersion") != fixture["benchmarkVersion"]:
        raise EvaluationError("result snapshot benchmarkVersion does not match fixture")
    configuration = snapshot.get("configuration")
    cases = snapshot.get("cases")
    if not isinstance(configuration, dict) or not isinstance(cases, list):
        raise EvaluationError("result snapshot is missing configuration/cases")
    expected_configuration_keys = {"split", "top", "hops"}
    if set(configuration) != expected_configuration_keys:
        raise EvaluationError(
            "result snapshot configuration keys must equal "
            + repr(sorted(expected_configuration_keys))
        )
    snapshot_split = configuration.get("split")
    if snapshot_split not in VALID_SPLITS:
        raise EvaluationError("result snapshot has invalid split")
    if requested_split not in VALID_SPLITS:
        raise EvaluationError(f"invalid requested split: {requested_split}")
    if snapshot_split != requested_split:
        raise EvaluationError(
            f"replay split mismatch: snapshot split={snapshot_split!r}, requested={requested_split!r}"
        )
    snapshot_top = configuration.get("top")
    snapshot_hops = configuration.get("hops")
    if not is_nonnegative_integer(snapshot_top) or not 1 <= snapshot_top <= 30:
        raise EvaluationError("result snapshot has invalid top")
    if not is_nonnegative_integer(snapshot_hops) or not 0 <= snapshot_hops <= 3:
        raise EvaluationError("result snapshot has invalid hops")
    top = snapshot_top if requested_top is None else requested_top
    hops = snapshot_hops if requested_hops is None else requested_hops
    if top != snapshot_top or hops != snapshot_hops:
        raise EvaluationError(
            f"replay configuration mismatch: snapshot top/hops={snapshot_top}/{snapshot_hops}, requested={top}/{hops}"
        )

    if not is_nonnegative_integer(snapshot.get("questionCount")):
        raise EvaluationError("result snapshot questionCount must be a nonnegative integer")
    if snapshot["questionCount"] != len(cases):
        raise EvaluationError(
            f"result snapshot questionCount={snapshot['questionCount']} does not match {len(cases)} cases"
        )
    if len(cases) != len(questions):
        raise EvaluationError(
            f"result snapshot contains {len(cases)} cases but requested split has {len(questions)} questions"
        )

    by_id: dict[str, dict[str, Any]] = {}
    for case_index, case in enumerate(cases):
        if not isinstance(case, dict) or not isinstance(case.get("id"), str):
            raise EvaluationError(f"result snapshot cases[{case_index}] is invalid")
        if case["id"] in by_id:
            raise EvaluationError(f"result snapshot contains duplicate case id: {case['id']}")
        by_id[case["id"]] = case
    expected_ids = {question["id"] for question in questions}
    actual_ids = set(by_id)
    missing_ids = sorted(expected_ids - actual_ids)
    extra_ids = sorted(actual_ids - expected_ids)
    if missing_ids or extra_ids:
        raise EvaluationError(
            f"result snapshot case ids do not match requested split; missing={missing_ids}, extra={extra_ids}"
        )
    selected: list[dict[str, Any]] = []
    for question in questions:
        selected.append(
            validate_execution_case(
                by_id[question["id"]],
                question,
                top,
                hops,
                f"result snapshot case {question['id']}",
            )
        )
    return top, hops, selected


def normalized_text(value: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", value.lower(), flags=re.UNICODE))


def passage_range_label(path: str, line_range: dict[str, Any]) -> str:
    start = line_range["start"]
    end = line_range["end"]
    location = f"{path}:L{start}" if start == end else f"{path}:L{start}-L{end}"
    description = line_range.get("label")
    return f"{location} — {description}" if description else location


def classify_result_path(
    path: Any,
    required_pages: set[str],
    acceptable_alternatives: set[str],
    facet_supporting_pages: set[str],
    enumerated_distractors: set[str],
) -> str:
    if path in required_pages:
        return "required"
    if path in acceptable_alternatives:
        return "acceptable_alternative"
    if path in facet_supporting_pages:
        return "facet_supporting"
    if path in enumerated_distractors:
        return "enumerated_distractor"
    return "unclassified"


def score_case(question: dict[str, Any], execution: dict[str, Any], top: int) -> dict[str, Any]:
    payload = execution["payload"]
    raw_results = payload.get("results", [])[:top]
    required_pages = canonical_repo_path_identities(
        question["requiredPages"],
        f"question {question['id']}.requiredPages",
    )
    alternatives = canonical_repo_path_identities(
        question["acceptableAlternativePages"],
        f"question {question['id']}.acceptableAlternativePages",
    )
    distractor_pages = canonical_repo_path_identities(
        question["distractorPages"],
        f"question {question['id']}.distractorPages",
    )
    required_page_set = set(required_pages)
    alternative_page_set = set(alternatives)
    facet_supporting_page_set = {
        identity
        for facet_index, facet in enumerate(question["requiredFacets"])
        for identity in canonical_repo_path_identities(
            facet["supportingPages"],
            f"question {question['id']}.requiredFacets[{facet_index}].supportingPages",
        )
    }
    distractor_page_set = set(distractor_pages)
    ranked_pages: list[dict[str, Any]] = []
    rank_by_path: dict[str, int] = {}
    result_class_counts: Counter[str] = Counter()
    result_class_payload_bytes: Counter[str] = Counter()
    result_pages_by_class: dict[str, list[Any]] = {name: [] for name in RESULT_RELEVANCE_CLASSES}
    for rank, result in enumerate(raw_results, start=1):
        path = canonical_repo_path_identity(
            result.get("path"),
            f"question {question['id']}.results[{rank - 1}].path",
        )
        if path not in rank_by_path:
            rank_by_path[path] = rank
        relevance_class = classify_result_path(
            path,
            required_page_set,
            alternative_page_set,
            facet_supporting_page_set,
            distractor_page_set,
        )
        result_payload_bytes = compact_json_bytes(result)
        result_class_counts[relevance_class] += 1
        result_class_payload_bytes[relevance_class] += result_payload_bytes
        result_pages_by_class[relevance_class].append(path)
        ranked_pages.append(
            {
                "rank": rank,
                "path": path,
                "score": result.get("score"),
                "title": result.get("title"),
                "relevanceClass": relevance_class,
                "resultPayloadBytes": result_payload_bytes,
            }
        )
    ranked_path_set = set(rank_by_path)

    required_hits = [path for path in required_pages if path in ranked_path_set]
    missing_required = [path for path in required_pages if path not in ranked_path_set]
    required_ranks = [rank_by_path[path] for path in required_hits]
    reciprocal_rank = 1.0 / min(required_ranks) if required_ranks else 0.0
    required_recall = len(required_hits) / len(required_pages) if required_pages else None

    alternative_hits = [path for path in alternatives if path in ranked_path_set]

    evidence_paths = canonical_repo_path_identities(
        (item["path"] for item in question["requiredEvidencePassages"]),
        f"question {question['id']}.requiredEvidencePassages",
    )
    evidence_hits = [path for path in evidence_paths if path in ranked_path_set]
    missing_evidence = [path for path in evidence_paths if path not in ranked_path_set]
    evidence_page_recall = len(evidence_hits) / len(evidence_paths) if evidence_paths else None
    evidence_passages = []
    for evidence_index, item in enumerate(question["requiredEvidencePassages"]):
        path = canonical_repo_path_identity(
            item["path"],
            f"question {question['id']}.requiredEvidencePassages[{evidence_index}].path",
        )
        ranges = [
            {
                **line_range,
                "rangeLabel": passage_range_label(path, line_range),
            }
            for line_range in item["lineRanges"]
        ]
        evidence_passages.append(
            {
                "parentPage": path,
                "parentPageReturned": path in ranked_path_set,
                "parentPageRank": rank_by_path.get(path),
                "requirement": item["requirement"],
                "lineRanges": ranges,
                "passageLabels": [line_range["rangeLabel"] for line_range in ranges],
            }
        )

    canonical_facet_pages = {
        facet["id"]: canonical_repo_path_identities(
            facet["supportingPages"],
            f"question {question['id']}.requiredFacets[{facet_index}].supportingPages",
        )
        for facet_index, facet in enumerate(question["requiredFacets"])
    }
    scorable_facets = [
        facet for facet in question["requiredFacets"] if canonical_facet_pages[facet["id"]]
    ]
    unscorable_facets = [
        facet["id"]
        for facet in question["requiredFacets"]
        if not canonical_facet_pages[facet["id"]]
    ]
    covered_facets = [
        facet["id"]
        for facet in scorable_facets
        if ranked_path_set.intersection(canonical_facet_pages[facet["id"]])
    ]
    missing_facets = [facet["id"] for facet in scorable_facets if facet["id"] not in covered_facets]
    facet_coverage = len(covered_facets) / len(scorable_facets) if scorable_facets else None

    admitted_distractor_pages = [path for path in distractor_pages if path in ranked_path_set]
    metadata_blob = normalized_text(json.dumps(raw_results, ensure_ascii=False, sort_keys=True))
    admitted_distractor_terms = []
    for term in question["distractorTerms"]:
        normalized_term = normalized_text(term)
        if normalized_term and f" {normalized_term} " in f" {metadata_blob} ":
            admitted_distractor_terms.append(term)

    returned_result_count = len(ranked_pages)
    classified_relevant_count = sum(
        result_class_counts[name]
        for name in ("required", "acceptable_alternative", "facet_supporting")
    )
    enumerated_distractor_result_count = result_class_counts["enumerated_distractor"]
    unclassified_result_count = result_class_counts["unclassified"]
    irrelevant_result_count = enumerated_distractor_result_count + unclassified_result_count
    total_result_payload_bytes = sum(result_class_payload_bytes.values())
    enumerated_distractor_payload_bytes = result_class_payload_bytes["enumerated_distractor"]
    unclassified_payload_bytes = result_class_payload_bytes["unclassified"]
    irrelevant_payload_bytes = enumerated_distractor_payload_bytes + unclassified_payload_bytes

    expected_status = question["expectedStatus"]
    status_evaluated = expected_status in {"partial_unsupported", "no_match"}
    status_failure = status_evaluated

    failures: list[str] = []
    failures.extend(f"missing_required_page:{path}" for path in missing_required)
    failures.extend(f"missing_evidence_parent_page:{path}" for path in missing_evidence)
    failures.extend(f"uncovered_facet:{facet_id}" for facet_id in missing_facets)
    failures.extend(f"distractor_page_admitted:{path}" for path in admitted_distractor_pages)
    failures.extend(f"distractor_term_admitted:{term}" for term in admitted_distractor_terms)
    if status_failure:
        failures.append(f"expected_status_unavailable:{expected_status}")

    return {
        "id": question["id"],
        "split": question["split"],
        "sourceSet": question["sourceSet"],
        "expectedStatus": expected_status,
        "question": question["question"],
        "returnedPageCount": len(ranked_pages),
        "rankedPages": ranked_pages,
        "pageRanking": {
            "reciprocalRank": round(reciprocal_rank, 6),
            "recallAtK": round(required_recall, 6) if required_recall is not None else None,
            "requiredPageCount": len(required_pages),
            "requiredPagesHit": required_hits,
            "missingRequiredPages": missing_required,
            "acceptableAlternativePagesHit": alternative_hits,
        },
        "requiredEvidence": {
            "parentPageRecallAtK": round(evidence_page_recall, 6) if evidence_page_recall is not None else None,
            "parentPageCount": len(evidence_paths),
            "parentPagesHit": evidence_hits,
            "missingParentPages": missing_evidence,
            "annotatedPassageCount": len(evidence_passages),
            "annotatedLineRangeCount": sum(len(item["lineRanges"]) for item in evidence_passages),
            "passages": evidence_passages,
            "passageRecallAtK": {
                "available": False,
                "value": None,
                "reason": "semantic-query ranks pages and emits no passages or line ranges",
            },
        },
        "facetCoverageProxy": {
            "value": round(facet_coverage, 6) if facet_coverage is not None else None,
            "coveredCount": len(covered_facets),
            "scorableCount": len(scorable_facets),
            "coveredFacetIds": covered_facets,
            "missingFacetIds": missing_facets,
            "unscorableFacetIds": unscorable_facets,
        },
        "distractorAdmission": {
            "pages": admitted_distractor_pages,
            "terms": admitted_distractor_terms,
            "any": bool(admitted_distractor_pages or admitted_distractor_terms),
        },
        "irrelevantContextProxy": {
            "policy": (
                "each returned result is assigned exactly one class in precedence order: "
                "required, acceptable_alternative, facet_supporting, enumerated_distractor, unclassified"
            ),
            "returnedResultCount": returned_result_count,
            "classCounts": {name: result_class_counts[name] for name in RESULT_RELEVANCE_CLASSES},
            "classRates": {
                name: round(result_class_counts[name] / returned_result_count, 6)
                if returned_result_count
                else 0.0
                for name in RESULT_RELEVANCE_CLASSES
            },
            "pagesByClass": result_pages_by_class,
            "classifiedRelevantResultCount": classified_relevant_count,
            "classifiedRelevantResultRate": round(classified_relevant_count / returned_result_count, 6)
            if returned_result_count
            else 0.0,
            "irrelevantResultCount": irrelevant_result_count,
            "irrelevantResultRate": round(irrelevant_result_count / returned_result_count, 6)
            if returned_result_count
            else 0.0,
            "enumeratedDistractorResultCount": enumerated_distractor_result_count,
            "enumeratedDistractorResultRate": round(
                enumerated_distractor_result_count / returned_result_count, 6
            )
            if returned_result_count
            else 0.0,
            "unclassifiedResultCount": unclassified_result_count,
            "unclassifiedResultRate": round(unclassified_result_count / returned_result_count, 6)
            if returned_result_count
            else 0.0,
            "resultPayloadBytes": {
                "total": total_result_payload_bytes,
                "byClass": {name: result_class_payload_bytes[name] for name in RESULT_RELEVANCE_CLASSES},
                "irrelevant": irrelevant_payload_bytes,
                "irrelevantRate": round(irrelevant_payload_bytes / total_result_payload_bytes, 6)
                if total_result_payload_bytes
                else 0.0,
                "enumeratedDistractor": enumerated_distractor_payload_bytes,
                "enumeratedDistractorRate": round(
                    enumerated_distractor_payload_bytes / total_result_payload_bytes, 6
                )
                if total_result_payload_bytes
                else 0.0,
                "unclassified": unclassified_payload_bytes,
                "unclassifiedRate": round(unclassified_payload_bytes / total_result_payload_bytes, 6)
                if total_result_payload_bytes
                else 0.0,
            },
        },
        "expectedStatusHandling": {
            "evaluated": status_evaluated,
            "engineStatusAvailable": False,
            "passed": False if status_evaluated else None,
            "reason": "semantic-query emits retrieval candidates but no answer status" if status_evaluated else None,
        },
        "performance": {
            "latencyMs": round(float(execution["latencyMs"]), 3),
            "jsonBytes": int(execution["jsonBytes"]),
            "contextBytes": int(execution["contextBytes"]),
            "estimatedContextTokens": int(math.ceil(float(execution["contextBytes"]) / 4.0)),
        },
        "failures": failures,
    }


def mean_or_zero(values: Iterable[float]) -> float:
    materialized = list(values)
    return statistics.fmean(materialized) if materialized else 0.0


def percentile(values: Iterable[float], fraction: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def summarize_distribution(values: Iterable[float], digits: int = 3) -> dict[str, float]:
    materialized = [float(value) for value in values]
    return {
        "total": round(sum(materialized), digits),
        "mean": round(mean_or_zero(materialized), digits),
        "median": round(percentile(materialized, 0.5), digits),
        "p95": round(percentile(materialized, 0.95), digits),
        "min": round(min(materialized), digits) if materialized else 0.0,
        "max": round(max(materialized), digits) if materialized else 0.0,
    }


def aggregate_metrics(
    cases: list[dict[str, Any]],
    top: int,
    questions_by_id: dict[str, dict[str, Any]],
    include_by_split: bool = True,
) -> dict[str, Any]:
    questions = [questions_by_id[case["id"]] for case in cases]
    page_cases = [case for case in cases if case["pageRanking"]["requiredPageCount"] > 0]
    required_page_total = sum(case["pageRanking"]["requiredPageCount"] for case in page_cases)
    required_page_hits = sum(len(case["pageRanking"]["requiredPagesHit"]) for case in page_cases)

    evidence_cases = [case for case in cases if case["requiredEvidence"]["parentPageCount"] > 0]
    evidence_total = sum(case["requiredEvidence"]["parentPageCount"] for case in evidence_cases)
    evidence_hits = sum(len(case["requiredEvidence"]["parentPagesHit"]) for case in evidence_cases)
    annotated_passages = sum(case["requiredEvidence"]["annotatedPassageCount"] for case in cases)
    annotated_line_ranges = sum(case["requiredEvidence"]["annotatedLineRangeCount"] for case in cases)

    facet_cases = [case for case in cases if case["facetCoverageProxy"]["scorableCount"] > 0]
    facet_total = sum(case["facetCoverageProxy"]["scorableCount"] for case in facet_cases)
    facet_hits = sum(case["facetCoverageProxy"]["coveredCount"] for case in facet_cases)
    unscorable_facets = sum(len(case["facetCoverageProxy"]["unscorableFacetIds"]) for case in cases)

    distractor_pages_by_question = {
        question["id"]: canonical_repo_path_identities(
            question["distractorPages"],
            f"question {question['id']}.distractorPages",
        )
        for question in questions
    }
    distractor_page_total = sum(
        len(distractor_pages_by_question[question["id"]])
        for question in questions
    )
    distractor_term_total = sum(
        len(next_question["distractorTerms"])
        for next_question in questions
    )
    distractor_page_hits = sum(len(case["distractorAdmission"]["pages"]) for case in cases)
    distractor_term_hits = sum(len(case["distractorAdmission"]["terms"]) for case in cases)
    distractor_page_cases = [
        question
        for question in questions
        if distractor_pages_by_question[question["id"]]
    ]
    distractor_term_cases = [question for question in questions if question["distractorTerms"]]
    case_by_id = {case["id"]: case for case in cases}
    distractor_page_case_hits = sum(bool(case_by_id[question["id"]]["distractorAdmission"]["pages"]) for question in distractor_page_cases)
    distractor_term_case_hits = sum(bool(case_by_id[question["id"]]["distractorAdmission"]["terms"]) for question in distractor_term_cases)

    result_class_counts: Counter[str] = Counter()
    result_class_payload_bytes: Counter[str] = Counter()
    for case in cases:
        result_class_counts.update(case["irrelevantContextProxy"]["classCounts"])
        result_class_payload_bytes.update(case["irrelevantContextProxy"]["resultPayloadBytes"]["byClass"])
    returned_result_count = sum(result_class_counts.values())
    classified_relevant_count = sum(
        result_class_counts[name]
        for name in ("required", "acceptable_alternative", "facet_supporting")
    )
    enumerated_distractor_result_count = result_class_counts["enumerated_distractor"]
    unclassified_result_count = result_class_counts["unclassified"]
    irrelevant_result_count = enumerated_distractor_result_count + unclassified_result_count
    total_result_payload_bytes = sum(result_class_payload_bytes.values())
    enumerated_distractor_payload_bytes = result_class_payload_bytes["enumerated_distractor"]
    unclassified_payload_bytes = result_class_payload_bytes["unclassified"]
    irrelevant_payload_bytes = enumerated_distractor_payload_bytes + unclassified_payload_bytes

    status_cases = [case for case in cases if case["expectedStatusHandling"]["evaluated"]]
    by_expected_status: dict[str, dict[str, Any]] = {}
    for status in ("partial_unsupported", "no_match"):
        relevant = [case for case in status_cases if case["expectedStatus"] == status]
        by_expected_status[status] = {
            "cases": len(relevant),
            "failures": sum(case["expectedStatusHandling"]["passed"] is False for case in relevant),
            "failureRate": round(
                mean_or_zero(case["expectedStatusHandling"]["passed"] is False for case in relevant), 6
            ),
        }

    latency_values = [case["performance"]["latencyMs"] for case in cases]
    json_values = [case["performance"]["jsonBytes"] for case in cases]
    context_values = [case["performance"]["contextBytes"] for case in cases]
    token_values = [case["performance"]["estimatedContextTokens"] for case in cases]

    metrics: dict[str, Any] = {
        "questionCount": len(cases),
        "pageRanking": {
            "k": top,
            "scorableCases": len(page_cases),
            "mrr": round(mean_or_zero(case["pageRanking"]["reciprocalRank"] for case in page_cases), 6),
            "recallAtKMacro": round(mean_or_zero(case["pageRanking"]["recallAtK"] for case in page_cases), 6),
            "requiredPageCoverageMicro": round(required_page_hits / required_page_total, 6) if required_page_total else 0.0,
            "requiredPagesHit": required_page_hits,
            "requiredPagesTotal": required_page_total,
        },
        "requiredEvidence": {
            "parentPageRecallAtKMacro": round(mean_or_zero(case["requiredEvidence"]["parentPageRecallAtK"] for case in evidence_cases), 6),
            "parentPageCoverageMicro": round(evidence_hits / evidence_total, 6) if evidence_total else 0.0,
            "parentPagesHit": evidence_hits,
            "parentPagesTotal": evidence_total,
            "annotatedPassages": annotated_passages,
            "annotatedLineRanges": annotated_line_ranges,
            "passageRecallAtK": {
                "available": False,
                "value": None,
                "reason": "current engine returns page metadata only; no passage or line-range ranking is emitted",
            },
        },
        "facetCoverageProxy": {
            "macro": round(mean_or_zero(case["facetCoverageProxy"]["value"] for case in facet_cases), 6),
            "micro": round(facet_hits / facet_total, 6) if facet_total else 0.0,
            "coveredFacets": facet_hits,
            "scorableFacets": facet_total,
            "unscorableFacets": unscorable_facets,
            "interpretation": "a facet is covered when at least one mapped supporting page appears in the top K",
        },
        "distractorAdmission": {
            "pageAdmissions": distractor_page_hits,
            "listedDistractorPages": distractor_page_total,
            "pageAdmissionRate": round(distractor_page_hits / distractor_page_total, 6) if distractor_page_total else 0.0,
            "casesWithPageAdmission": distractor_page_case_hits,
            "casesWithListedDistractorPages": len(distractor_page_cases),
            "pageCaseAdmissionRate": round(distractor_page_case_hits / len(distractor_page_cases), 6) if distractor_page_cases else 0.0,
            "termAdmissions": distractor_term_hits,
            "listedDistractorTerms": distractor_term_total,
            "termAdmissionRate": round(distractor_term_hits / distractor_term_total, 6) if distractor_term_total else 0.0,
            "casesWithTermAdmission": distractor_term_case_hits,
            "casesWithListedDistractorTerms": len(distractor_term_cases),
            "termCaseAdmissionRate": round(distractor_term_case_hits / len(distractor_term_cases), 6) if distractor_term_cases else 0.0,
            "casesWithAnyAdmission": sum(case["distractorAdmission"]["any"] for case in cases),
            "anyCaseAdmissionRate": round(mean_or_zero(case["distractorAdmission"]["any"] for case in cases), 6),
        },
        "irrelevantContextProxy": {
            "policy": (
                "each returned result is assigned exactly one class in precedence order: "
                "required, acceptable_alternative, facet_supporting, enumerated_distractor, unclassified"
            ),
            "returnedResultCount": returned_result_count,
            "classCounts": {name: result_class_counts[name] for name in RESULT_RELEVANCE_CLASSES},
            "classRates": {
                name: round(result_class_counts[name] / returned_result_count, 6)
                if returned_result_count
                else 0.0
                for name in RESULT_RELEVANCE_CLASSES
            },
            "classifiedRelevantResultCount": classified_relevant_count,
            "classifiedRelevantResultRate": round(classified_relevant_count / returned_result_count, 6)
            if returned_result_count
            else 0.0,
            "irrelevantResultCount": irrelevant_result_count,
            "irrelevantResultRate": round(irrelevant_result_count / returned_result_count, 6)
            if returned_result_count
            else 0.0,
            "enumeratedDistractorResultCount": enumerated_distractor_result_count,
            "enumeratedDistractorResultRate": round(
                enumerated_distractor_result_count / returned_result_count, 6
            )
            if returned_result_count
            else 0.0,
            "unclassifiedResultCount": unclassified_result_count,
            "unclassifiedResultRate": round(unclassified_result_count / returned_result_count, 6)
            if returned_result_count
            else 0.0,
            "casesWithUnclassifiedResults": sum(
                case["irrelevantContextProxy"]["unclassifiedResultCount"] > 0 for case in cases
            ),
            "unclassifiedCaseRate": round(
                mean_or_zero(case["irrelevantContextProxy"]["unclassifiedResultCount"] > 0 for case in cases),
                6,
            ),
            "resultPayloadBytes": {
                "total": total_result_payload_bytes,
                "byClass": {name: result_class_payload_bytes[name] for name in RESULT_RELEVANCE_CLASSES},
                "irrelevant": irrelevant_payload_bytes,
                "irrelevantRate": round(irrelevant_payload_bytes / total_result_payload_bytes, 6)
                if total_result_payload_bytes
                else 0.0,
                "enumeratedDistractor": enumerated_distractor_payload_bytes,
                "enumeratedDistractorRate": round(
                    enumerated_distractor_payload_bytes / total_result_payload_bytes, 6
                )
                if total_result_payload_bytes
                else 0.0,
                "unclassified": unclassified_payload_bytes,
                "unclassifiedRate": round(unclassified_payload_bytes / total_result_payload_bytes, 6)
                if total_result_payload_bytes
                else 0.0,
            },
        },
        "expectedStatusHandling": {
            "engineStatusAvailable": False,
            "evaluatedCases": len(status_cases),
            "failures": sum(case["expectedStatusHandling"]["passed"] is False for case in status_cases),
            "failureRate": round(mean_or_zero(case["expectedStatusHandling"]["passed"] is False for case in status_cases), 6),
            "byExpectedStatus": by_expected_status,
            "partialUnsupportedRelevantPageRecallAtKMacro": round(
                mean_or_zero(
                    case["pageRanking"]["recallAtK"]
                    for case in cases
                    if case["expectedStatus"] == "partial_unsupported" and case["pageRanking"]["recallAtK"] is not None
                ),
                6,
            ),
            "noMatchCasesWithDistractorAdmission": sum(
                case["distractorAdmission"]["any"] for case in cases if case["expectedStatus"] == "no_match"
            ),
            "reason": "semantic-query is a page planner and emits no match/partial/no_match status",
        },
        "performance": {
            "latencyMs": summarize_distribution(latency_values),
            "jsonBytes": summarize_distribution(json_values, digits=1),
            "contextBytes": summarize_distribution(context_values, digits=1),
            "estimatedContextTokens": summarize_distribution(token_values, digits=1),
        },
        "answerQuality": {
            "citationCorrectness": {"available": False, "value": None},
            "claimSupport": {"available": False, "value": None},
            "answerCompleteness": {"available": False, "value": None},
            "reason": "the baseline ranks pages and does not generate an answer or citations",
        },
        "failureSummary": {
            "casesWithFailures": sum(bool(case["failures"]) for case in cases),
            "totalFailureItems": sum(len(case["failures"]) for case in cases),
        },
    }
    if include_by_split:
        metrics["bySplit"] = {
            split: aggregate_metrics(
                [case for case in cases if case["split"] == split],
                top,
                questions_by_id,
                include_by_split=False,
            )
            for split in ("tuning", "held-out")
            if any(case["split"] == split for case in cases)
        }
    return metrics


def git_root() -> Path:
    completed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=WIKI_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise EvaluationError(f"cannot locate git repository: {completed.stderr.strip()}")
    return Path(completed.stdout.strip()).resolve()


def output_path(
    path: Path | None,
    option_name: str,
    allow_stdout: bool = False,
) -> Path | None:
    if path is None:
        return None
    if path == Path("-"):
        if allow_stdout:
            return None
        raise EvaluationError(
            f"{option_name} does not support '-'; provide a filesystem path"
        )
    return path.absolute() if path.is_absolute() else (WIKI_ROOT / path).absolute()


def sha256_file(path: Path) -> tuple[str, int, str]:
    if path.is_symlink():
        data = os.readlink(path).encode("utf-8", errors="surrogateescape")
        return hashlib.sha256(b"SYMLINK\0" + data).hexdigest(), len(data), "symlink"
    if not path.exists():
        return hashlib.sha256(b"MISSING").hexdigest(), 0, "missing"
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size, "file"


def _path_key(path: Path) -> Path:
    return path.resolve(strict=False)


def _has_symlink_component(path: Path) -> bool:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
    return False


def _is_vcs_metadata_path(path: Path) -> bool:
    candidates = [path, Path(os.path.abspath(path)), path.resolve(strict=False)]
    return any(
        part in VCS_METADATA_DIRECTORY_NAMES
        for candidate in candidates
        for part in candidate.parts
    )


def _iter_tree_files(root: Path) -> Iterable[Path]:
    if root.is_symlink() or root.is_file():
        yield root.absolute()
        return
    if not root.is_dir():
        return
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        kept_directories: list[str] = []
        for name in directory_names:
            candidate = current_path / name
            if name in LIVE_EXCLUDED_DIRECTORY_NAMES or name.startswith(LIVE_EXCLUDED_DIRECTORY_PREFIXES):
                continue
            if candidate.is_symlink():
                yield candidate.absolute()
            else:
                kept_directories.append(name)
        directory_names[:] = kept_directories
        for name in file_names:
            yield (current_path / name).absolute()


def _fixture_repository_paths(fixture_path: Path, wiki_root: Path) -> set[Path]:
    if not fixture_path.is_file():
        return set()
    try:
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return set()
    paths: set[Path] = set()
    for question in fixture.get("questions", []) if isinstance(fixture, dict) else []:
        if not isinstance(question, dict):
            continue
        for key in PAGE_KEYS:
            values = question.get(key, [])
            if isinstance(values, list):
                paths.update(
                    (wiki_root / value).absolute()
                    for value in values
                    if isinstance(value, str)
                )
        evidence = question.get("requiredEvidencePassages", [])
        if isinstance(evidence, list):
            paths.update(
                (wiki_root / item["path"]).absolute()
                for item in evidence
                if isinstance(item, dict) and isinstance(item.get("path"), str)
            )
        facets = question.get("requiredFacets", [])
        if isinstance(facets, list):
            for facet in facets:
                if not isinstance(facet, dict) or not isinstance(facet.get("supportingPages"), list):
                    continue
                paths.update(
                    (wiki_root / value).absolute()
                    for value in facet["supportingPages"]
                    if isinstance(value, str)
                )
    return paths


def protected_repository_paths(
    repo_root: Path,
    excluded: set[Path],
    input_results: Path | None,
    mode: str,
    wiki_root: Path = WIKI_ROOT,
) -> list[Path]:
    if mode not in {"live", "input_results"}:
        raise EvaluationError(f"invalid integrity mode: {mode}")
    wiki_root = wiki_root.absolute()
    normalized_excluded = {_path_key(path) for path in excluded}
    script_path = wiki_root / "_tools/evaluate_retrieval.py"
    fixture_path = wiki_root / "_tools/fixtures/retrieval/questions.v1.json"
    schema_path = wiki_root / "_tools/fixtures/retrieval/retrieval-eval.schema.json"
    fixture_dir = fixture_path.parent
    paths: set[Path] = {
        path.absolute()
        for path in (script_path, fixture_path, schema_path, wiki_root / ".gitignore")
        if path.exists() or path.is_symlink()
    }
    paths.update(_iter_tree_files(fixture_dir))
    paths.update(_fixture_repository_paths(fixture_path, wiki_root))
    if mode == "live":
        paths.update(_iter_tree_files(wiki_root / ".vitepress"))
        paths.update(
            path.absolute()
            for path in (
                wiki_root / "src/_meta/semantic/graph.json",
                wiki_root / "src/_meta/semantic/index.json",
            )
            if path.exists() or path.is_symlink()
        )
    if input_results is not None:
        paths.add(input_results.absolute())
    return sorted(
        path
        for path in paths
        if _path_key(path) not in normalized_excluded
    )


def repository_hash_state(
    repo_root: Path,
    excluded: set[Path],
    input_results: Path | None,
    mode: str,
    wiki_root: Path = WIKI_ROOT,
) -> tuple[dict[str, Any], dict[str, str]]:
    manifest_entries: list[bytes] = []
    per_file: dict[str, str] = {}
    total_bytes = 0
    paths = protected_repository_paths(repo_root, excluded, input_results, mode, wiki_root)
    for path in paths:
        try:
            relative = path.relative_to(repo_root).as_posix()
        except ValueError:
            relative = str(path)
        digest, size, kind = sha256_file(path)
        total_bytes += size
        per_file[relative] = digest
        manifest_entries.append(f"{kind}\0{relative}\0{size}\0{digest}\n".encode("utf-8"))
    manifest_digest = hashlib.sha256(b"".join(manifest_entries)).hexdigest()
    return (
        {
            "scope": (
                "evaluator, fixture/schema/support files, fixture-referenced source pages, and "
                + (
                    "the full existing .vitepress tree plus semantic graph/index inputs"
                    if mode == "live"
                    else "the saved replay input"
                )
                + "; includes existing untracked/ignored protected files and excludes declared outputs"
            ),
            "fileCount": len(paths),
            "totalBytes": total_bytes,
            "manifestSha256": manifest_digest,
        },
        per_file,
    )


def parse_git_status(raw: bytes, repo_root: Path, excluded: set[Path]) -> list[str]:
    parts = raw.split(b"\0")
    entries: list[str] = []
    index = 0
    while index < len(parts):
        item = parts[index]
        index += 1
        if not item:
            continue
        text = os.fsdecode(item)
        if len(text) < 4:
            entries.append(text)
            continue
        status = text[:2]
        paths = [text[3:]]
        if (status[0] in "RC" or status[1] in "RC") and index < len(parts) and parts[index]:
            paths.append(os.fsdecode(parts[index]))
            index += 1
        resolved_paths = {(repo_root / path).resolve() for path in paths}
        if resolved_paths and resolved_paths.issubset(excluded):
            continue
        entries.append(f"{status} {' -> '.join(paths)}")
    return sorted(entries)


def repository_status_state(
    repo_root: Path,
    excluded: set[Path],
    wiki_root: Path = WIKI_ROOT,
) -> dict[str, Any]:
    try:
        wiki_relative = wiki_root.absolute().relative_to(repo_root.absolute()).as_posix()
    except ValueError:
        wiki_relative = str(wiki_root.absolute())
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--",
            wiki_relative,
        ],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        empty_digest = hashlib.sha256(b"").hexdigest()
        return {"entryCount": 0, "sha256": empty_digest, "entries": [], "source": "no-git"}
    entries = parse_git_status(completed.stdout, repo_root, excluded)
    digest = hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()
    return {"entryCount": len(entries), "sha256": digest, "entries": entries, "source": "git"}


def changed_hash_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def preflight_paths(
    json_path: Path | None,
    report_path: Path | None,
    save_results_path: Path | None,
    input_results_path: Path | None,
    repo_root: Path,
    mode: str = "live",
    wiki_root: Path = WIKI_ROOT,
) -> None:
    named_outputs = [
        ("--json-out", json_path),
        ("--report-out", report_path),
        ("--save-results", save_results_path),
    ]
    outputs = [(name, path) for name, path in named_outputs if path is not None]
    resolved_outputs = [(_path_key(path), name, path) for name, path in outputs]
    if len({resolved for resolved, _, _ in resolved_outputs}) != len(resolved_outputs):
        raise EvaluationError("declared output paths must be pairwise distinct")
    if input_results_path is not None:
        input_key = _path_key(input_results_path)
        collisions = [name for resolved, name, _ in resolved_outputs if resolved == input_key]
        if collisions:
            raise EvaluationError(
                f"--input-results cannot also be a declared output path ({', '.join(collisions)})"
            )

    protected = {
        _path_key(path)
        for path in protected_repository_paths(repo_root, set(), input_results_path, mode, wiki_root)
    }
    writable_baseline_by_option = (
        {
            "--json-out": BASELINE_JSON_PATH.resolve(),
            "--report-out": BASELINE_REPORT_PATH.resolve(),
            "--save-results": BASELINE_RESULTS_PATH.resolve(),
        }
        if wiki_root.resolve() == WIKI_ROOT
        else {}
    )
    protected_directories = [wiki_root / "src", wiki_root / ".vitepress"]
    for resolved, name, original in resolved_outputs:
        if _is_vcs_metadata_path(original) or _is_vcs_metadata_path(resolved):
            raise EvaluationError(
                f"{name} cannot write inside VCS metadata (.git, .hg, or .svn): {original}"
            )
        if _has_symlink_component(original):
            raise EvaluationError(f"{name} cannot use a symbolic-link path: {original}")
        if original.exists() and not original.is_file():
            raise EvaluationError(f"{name} must name a file, not a directory: {original}")
        if resolved == writable_baseline_by_option.get(name):
            continue
        in_protected_directory = any(
            resolved == directory.resolve() or resolved.is_relative_to(directory.resolve())
            for directory in protected_directories
        )
        existing_repository_file = (
            original.exists()
            and (resolved == wiki_root.resolve() or resolved.is_relative_to(wiki_root.resolve()))
        )
        if resolved in protected or in_protected_directory or existing_repository_file:
            raise EvaluationError(f"{name} resolves to a protected repository path: {original}")


def _integrity_failure_detail(changed_files: list[str], status_unchanged: bool) -> str:
    parts = []
    if changed_files:
        parts.append("changed protected files: " + ", ".join(changed_files))
    if not status_unchanged:
        parts.append("repository status changed")
    return "; ".join(parts) if parts else "unknown integrity mismatch"


def guarded_repository_operation(
    repo_root: Path,
    excluded: set[Path],
    input_results: Path | None,
    mode: str,
    operation: Callable[[], T],
    wiki_root: Path = WIKI_ROOT,
) -> tuple[T, dict[str, Any]]:
    before_hash_summary, before_hashes = repository_hash_state(
        repo_root, excluded, input_results, mode, wiki_root
    )
    before_status = repository_status_state(repo_root, excluded, wiki_root)
    result: T | None = None
    primary_error: BaseException | None = None
    verification_error: BaseException | None = None
    after_hash_summary: dict[str, Any] | None = None
    after_hashes: dict[str, str] | None = None
    after_status: dict[str, Any] | None = None
    try:
        result = operation()
    except BaseException as exc:
        primary_error = exc
    finally:
        try:
            after_hash_summary, after_hashes = repository_hash_state(
                repo_root, excluded, input_results, mode, wiki_root
            )
            after_status = repository_status_state(repo_root, excluded, wiki_root)
        except BaseException as exc:
            verification_error = exc

    if verification_error is not None:
        message = f"repository integrity verification failed: {verification_error}"
        if primary_error is not None:
            raise EvaluationError(f"{primary_error}; {message}") from primary_error
        raise EvaluationError(message) from verification_error

    assert after_hash_summary is not None and after_hashes is not None and after_status is not None
    changed_files = changed_hash_paths(before_hashes, after_hashes)
    status_unchanged = before_status["entries"] == after_status["entries"]
    integrity_status = "passed" if not changed_files and status_unchanged else "failed"
    integrity = {
        "status": integrity_status,
        "before": before_hash_summary,
        "after": after_hash_summary,
        "changedFiles": changed_files,
        "statusBefore": {key: value for key, value in before_status.items() if key != "entries"},
        "statusAfter": {key: value for key, value in after_status.items() if key != "entries"},
        "statusUnchanged": status_unchanged,
        "postWriteStatusUnchanged": None,
    }
    if primary_error is not None:
        if integrity_status == "failed":
            detail = _integrity_failure_detail(changed_files, status_unchanged)
            raise EvaluationError(
                f"{primary_error}; repository integrity verification failed: {detail}"
            ) from primary_error
        raise primary_error
    if integrity_status == "failed":
        detail = _integrity_failure_detail(changed_files, status_unchanged)
        raise EvaluationError(f"repository integrity verification failed: {detail}")
    assert result is not None
    return result, integrity


def atomic_write(path: Path, text: str) -> None:
    if path.is_symlink():
        raise EvaluationError(f"refusing to replace symbolic-link output: {path}")
    destination_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, destination_mode)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def escape_markdown_identifier(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    replacements = {
        "\\": "&#92;",
        "|": "&#124;",
        "`": "&#96;",
        "*": "&#42;",
        "_": "&#95;",
        "#": "&#35;",
        "[": "&#91;",
        "]": "&#93;",
        "<": "&lt;",
        ">": "&gt;",
    }
    return "".join(replacements.get(character, character) for character in text)


def markdown_report(evaluation: dict[str, Any]) -> str:
    metrics = evaluation["metrics"]
    ranking = metrics["pageRanking"]
    evidence = metrics["requiredEvidence"]
    facets = metrics["facetCoverageProxy"]
    distractors = metrics["distractorAdmission"]
    irrelevant = metrics["irrelevantContextProxy"]
    statuses = metrics["expectedStatusHandling"]
    performance = metrics["performance"]
    integrity = evaluation["repositoryIntegrity"]

    lines = [
        "# Phase 1 retrieval baseline",
        "",
        f"- Benchmark: `{evaluation['benchmarkVersion']}`",
        f"- Questions: **{evaluation['questionCount']}** (`{evaluation['configuration']['split']}` split)",
        f"- Command: `node .vitepress/semantic-query.mjs <query> --top {evaluation['configuration']['top']} --hops {evaluation['configuration']['hops']} --json`",
        f"- Repository integrity: **{integrity['status']}**; {integrity['before']['fileCount']} protected files; {len(integrity['changedFiles'])} non-output hash changes; status unchanged: `{integrity['statusUnchanged']}`",
        "",
        "## Measured metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Page MRR | {ranking['mrr']:.4f} |",
        f"| Page Recall@{ranking['k']} (macro) | {ranking['recallAtKMacro']:.4f} |",
        f"| Required-page coverage (micro) | {ranking['requiredPageCoverageMicro']:.4f} ({ranking['requiredPagesHit']}/{ranking['requiredPagesTotal']}) |",
        f"| Required-evidence parent-page recall (macro) | {evidence['parentPageRecallAtKMacro']:.4f} |",
        f"| Required-evidence parent-page coverage (micro) | {evidence['parentPageCoverageMicro']:.4f} ({evidence['parentPagesHit']}/{evidence['parentPagesTotal']}) |",
        f"| Required-evidence annotations | {evidence['annotatedPassages']} parents / {evidence['annotatedLineRanges']} line ranges |",
        "| Required-evidence passage recall | n/a — unavailable (engine emits no passages/line ranges) |",
        f"| Facet coverage proxy (macro) | {facets['macro']:.4f} |",
        f"| Facet coverage proxy (micro) | {facets['micro']:.4f} ({facets['coveredFacets']}/{facets['scorableFacets']}) |",
        f"| Classified-relevant returned results | {irrelevant['classifiedRelevantResultRate']:.4f} ({irrelevant['classifiedRelevantResultCount']}/{irrelevant['returnedResultCount']}) |",
        f"| Irrelevant-context proxy | {irrelevant['irrelevantResultRate']:.4f} ({irrelevant['irrelevantResultCount']}/{irrelevant['returnedResultCount']}) |",
        f"| Unclassified returned results | {irrelevant['unclassifiedResultRate']:.4f} ({irrelevant['unclassifiedResultCount']}/{irrelevant['returnedResultCount']}) |",
        f"| Unclassified result-payload bytes | {irrelevant['resultPayloadBytes']['unclassifiedRate']:.4f} ({irrelevant['resultPayloadBytes']['unclassified']}/{irrelevant['resultPayloadBytes']['total']}) |",
        f"| Enumerated-distractor returned results | {irrelevant['enumeratedDistractorResultRate']:.4f} ({irrelevant['enumeratedDistractorResultCount']}/{irrelevant['returnedResultCount']}) |",
        f"| Distractor page admission | {distractors['pageAdmissionRate']:.4f} ({distractors['pageAdmissions']}/{distractors['listedDistractorPages']}) |",
        f"| Distractor term admission | {distractors['termAdmissionRate']:.4f} ({distractors['termAdmissions']}/{distractors['listedDistractorTerms']}) |",
        f"| Expected partial/no-match handling failures | {statuses['failures']}/{statuses['evaluatedCases']} |",
        f"| Mean latency | {performance['latencyMs']['mean']:.3f} ms |",
        f"| P95 latency | {performance['latencyMs']['p95']:.3f} ms |",
        f"| Total JSON bytes | {int(performance['jsonBytes']['total'])} |",
        f"| Total context bytes | {int(performance['contextBytes']['total'])} |",
        f"| Estimated context tokens (bytes/4) | {int(performance['estimatedContextTokens']['total'])} |",
        "",
        "## Measurement boundaries",
        "",
        "- Passage recall is reported as **unavailable (`null`)**, not as a synthetic zero or success score, because the current planner returns page metadata only.",
        "- Every required-evidence parent has one or more verified 1-indexed line ranges. The report preserves parent/range labels, but cannot claim a passage hit until the planner emits passages or line ranges.",
        "- The irrelevant-context proxy classifies every returned result exactly once as required, acceptable alternative, facet-supporting, enumerated distractor, or unclassified. The irrelevant rate combines enumerated distractors and unclassified results; both components are reported separately.",
        "- The planner emits no `match`, `partial_unsupported`, or `no_match` answer status. Every expected partial/no-match case is therefore counted as a status-handling failure; relevant-page retrieval for partial cases is reported separately.",
        "- Citation correctness, claim support, and answer completeness are not measurable because no answer or citations are generated.",
        "- Facet coverage is a page-group proxy: one returned supporting page covers a mapped facet, but does not prove the answer would state the facet correctly.",
        "",
        "## Per-case results",
        "",
        f"| ID | Split | Expected | RR | Recall@{ranking['k']} | Evidence page recall | Facet proxy | Unclassified | Distractor | Latency ms | Failures |",
        "|---|---|---|---:|---:|---:|---:|---:|---|---:|---:|",
    ]
    for case in evaluation["cases"]:
        recall = case["pageRanking"]["recallAtK"]
        evidence_recall = case["requiredEvidence"]["parentPageRecallAtK"]
        facet = case["facetCoverageProxy"]["value"]
        lines.append(
            "| {id} | {split} | {status} | {rr:.3f} | {recall} | {evidence} | {facet} | {unclassified} | {distractor} | {latency:.3f} | {failures} |".format(
                id=escape_markdown_identifier(case["id"]),
                split=case["split"],
                status=case["expectedStatus"],
                rr=case["pageRanking"]["reciprocalRank"],
                recall="n/a" if recall is None else f"{recall:.3f}",
                evidence="n/a" if evidence_recall is None else f"{evidence_recall:.3f}",
                facet="n/a" if facet is None else f"{facet:.3f}",
                unclassified=(
                    f"{case['irrelevantContextProxy']['unclassifiedResultCount']}/"
                    f"{case['irrelevantContextProxy']['returnedResultCount']}"
                ),
                distractor="yes" if case["distractorAdmission"]["any"] else "no",
                latency=case["performance"]["latencyMs"],
                failures=len(case["failures"]),
            )
        )

    lines.extend(["", "## Required-evidence passage annotations", ""])
    for case in evaluation["cases"]:
        passages = case["requiredEvidence"]["passages"]
        safe_id = escape_markdown_identifier(case["id"])
        if not passages:
            lines.append(f"- **{safe_id}:** none (expected `{case['expectedStatus']}` case).")
            continue
        labels = [label for passage in passages for label in passage["passageLabels"]]
        lines.append(f"- **{safe_id}:** " + "; ".join(f"`{label}`" for label in labels))

    lines.extend(["", "## Per-case failures", ""])
    for case in evaluation["cases"]:
        if not case["failures"]:
            continue
        lines.append(f"### {escape_markdown_identifier(case['id'])}")
        lines.extend(f"- `{failure}`" for failure in case["failures"])
        lines.append("")
    if not any(case["failures"] for case in evaluation["cases"]):
        lines.append("No failures recorded.")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_requested_outputs(
    evaluation: dict[str, Any],
    json_path: Path | None,
    report_path: Path | None,
    json_stdout: bool,
    report_stdout: bool,
) -> None:
    json_text = json.dumps(evaluation, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    report_text = markdown_report(evaluation)
    if json_path is not None:
        atomic_write(json_path, json_text)
    if report_path is not None:
        atomic_write(report_path, report_text)
    if json_stdout:
        sys.stdout.write(json_text)
    if report_stdout:
        sys.stdout.write(report_text)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    fixture = load_json(FIXTURE_PATH)
    fixture_validation = validate_fixture(fixture)
    questions = selected_questions(fixture, args.split)
    questions_by_id = {question["id"]: question for question in fixture["questions"]}

    json_path = output_path(args.json_out, "--json-out", allow_stdout=True)
    report_path = output_path(args.report_out, "--report-out", allow_stdout=True)
    save_results_path = output_path(args.save_results, "--save-results")
    input_results_path = output_path(args.input_results, "--input-results")
    declared_outputs = {path for path in (json_path, report_path, save_results_path) if path is not None}
    repo_root = git_root()
    mode = "input_results" if input_results_path is not None else "live"
    preflight_paths(
        json_path,
        report_path,
        save_results_path,
        input_results_path,
        repo_root,
        mode,
    )

    def evaluate() -> dict[str, Any]:
        if input_results_path is not None:
            top, hops, executions = load_replay_results(
                input_results_path,
                fixture,
                questions,
                args.split,
                args.top,
                args.hops,
            )
        else:
            top = DEFAULT_TOP if args.top is None else args.top
            hops = DEFAULT_HOPS if args.hops is None else args.hops
            executions = build_live_results(questions, top, hops)
            if save_results_path is not None:
                snapshot = result_snapshot(fixture, args.split, top, hops, executions)
                atomic_write(
                    save_results_path,
                    json.dumps(snapshot, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                )
        cases = [
            score_case(question, execution, top)
            for question, execution in zip(questions, executions, strict=True)
        ]
        return {
            "top": top,
            "hops": hops,
            "cases": cases,
            "metrics": aggregate_metrics(cases, top, questions_by_id),
        }

    operation, integrity = guarded_repository_operation(
        repo_root,
        declared_outputs,
        input_results_path,
        mode,
        evaluate,
    )
    top = operation["top"]
    hops = operation["hops"]
    cases = operation["cases"]
    metrics = operation["metrics"]

    evaluation: dict[str, Any] = {
        "schemaVersion": 1,
        "benchmarkVersion": fixture["benchmarkVersion"],
        "fixture": FIXTURE_PATH.relative_to(WIKI_ROOT).as_posix(),
        "configuration": {
            "split": args.split,
            "top": top,
            "hops": hops,
            "mode": mode,
            "semanticQueryCommand": f"node .vitepress/semantic-query.mjs <query> --top {top} --hops {hops} --json",
        },
        "questionCount": len(cases),
        "fixtureValidation": fixture_validation,
        "repositoryIntegrity": integrity,
        "metrics": metrics,
        "cases": cases,
    }

    pre_write_status = repository_status_state(repo_root, declared_outputs)
    if json_path is not None or report_path is not None:
        write_requested_outputs(evaluation, json_path, report_path, False, False)
    final_status = repository_status_state(repo_root, declared_outputs)
    post_write_status_unchanged = pre_write_status["entries"] == final_status["entries"]
    evaluation["repositoryIntegrity"]["postWriteStatusUnchanged"] = post_write_status_unchanged
    if not post_write_status_unchanged:
        evaluation["repositoryIntegrity"]["status"] = "failed"

    if json_path is not None or report_path is not None:
        write_requested_outputs(evaluation, json_path, report_path, False, False)
    if args.json_out == Path("-") or args.report_out == Path("-"):
        write_requested_outputs(
            evaluation,
            None,
            None,
            args.json_out == Path("-"),
            args.report_out == Path("-"),
        )

    if evaluation["repositoryIntegrity"]["status"] != "passed":
        raise EvaluationError("repository integrity verification failed after writing declared outputs")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EvaluationError as exc:
        print(f"[evaluate-retrieval] {exc}", file=sys.stderr)
        raise SystemExit(2)
