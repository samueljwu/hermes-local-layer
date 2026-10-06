#!/usr/bin/env python3
"""Focused tests for simplified passage-retrieval scoring."""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from evaluate_passage_retrieval import overlaps, run_query, score_case
from evaluate_retrieval import EvaluationError


class PassageEvaluationTests(unittest.TestCase):
    def test_run_query_rejects_missing_or_stale_bundle(self) -> None:
        args = argparse.Namespace(
            bundle_dir=Path('/home/hermes/.hermes/cache/scratch/wiki-retrieval/definitely-missing-bundle'),
            top=8,
            max_passages=12,
            max_evidence_chars=24000,
        )
        with self.assertRaises(EvaluationError):
            run_query({"id": "MISSING", "question": "test"}, args)

    def test_overlap_requires_verified_same_path_and_intersection(self) -> None:
        passage = {"verified": True, "path": "src/SCHEMA.md", "lineStart": 10, "lineEnd": 20}
        self.assertTrue(overlaps(passage, "src/SCHEMA.md", 20, 25))
        self.assertTrue(overlaps(passage, "src/SCHEMA.md", 5, 10))
        self.assertFalse(overlaps(passage, "src/SCHEMA.md", 21, 25))
        self.assertFalse(overlaps({**passage, "verified": False}, "src/SCHEMA.md", 10, 20))
        self.assertFalse(overlaps(passage, "src/index.md", 10, 20))

    def test_score_case_counts_ranges_facets_and_no_match(self) -> None:
        question = {
            "id": "X01",
            "split": "held-out",
            "question": "question",
            "expectedStatus": "match",
            "requiredPages": ["src/SCHEMA.md"],
            "acceptableAlternativePages": [],
            "requiredEvidencePassages": [
                {
                    "path": "src/SCHEMA.md",
                    "requirement": "evidence",
                    "lineRanges": [{"start": 10, "end": 12}, {"start": 30, "end": 31}],
                }
            ],
            "requiredFacets": [{"id": "a", "supportingPages": ["src/SCHEMA.md"]}],
            "distractorPages": ["src/index.md"],
        }
        execution = {
            "latencyMs": 12.5,
            "jsonBytes": 100,
            "payload": {
                "status": "match",
                "results": [{"path": "src/SCHEMA.md"}, {"path": "src/index.md"}],
                "passages": [
                    {
                        "verified": True,
                        "path": "src/SCHEMA.md",
                        "lineStart": 11,
                        "lineEnd": 14,
                        "role": "content",
                        "score": 2.0,
                    }
                ],
            },
        }
        case = score_case(question, execution)
        self.assertEqual(case["requiredRangeHits"], 1)
        self.assertEqual(case["requiredRangeCount"], 2)
        self.assertEqual(case["passageRangeRecall"], 0.5)
        self.assertEqual(case["requiredPageRecall"], 1.0)
        self.assertEqual(case["facetCoverage"], 1.0)
        self.assertEqual(case["distractorPages"], ["src/index.md"])
        self.assertTrue(case["statusOk"])

        no_match_question = {**question, "id": "X02", "expectedStatus": "no_match", "requiredPages": [], "requiredEvidencePassages": [], "requiredFacets": []}
        no_match_execution = {**execution, "payload": {"status": "no_match", "results": [], "passages": []}}
        self.assertTrue(score_case(no_match_question, no_match_execution)["statusOk"])


if __name__ == "__main__":
    unittest.main()
