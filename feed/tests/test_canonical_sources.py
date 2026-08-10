import contextlib
import copy
import importlib.util
import io
import json
import unittest
from pathlib import Path


def load_feed_ops():
    path = Path(__file__).resolve().parents[1] / "_tools" / "feed_ops.py"
    spec = importlib.util.spec_from_file_location("feed_ops_canonical_sources", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CanonicalSourceFetchTests(unittest.TestCase):
    def test_disabled_arxiv_and_hn_registry_rows_are_not_fetched(self):
        feed_ops = load_feed_ops()
        calls = []
        feed_ops.candidate_source_records = lambda: [
            {"id": "arxiv", "connector": "arxiv_api", "endpoint": "https://arxiv.invalid/api", "enabled": False},
            {"id": "hacker_news", "connector": "hn_front_page", "endpoint": "https://hn.invalid/news", "enabled": False},
        ]
        feed_ops.arxiv_query = lambda *args, **kwargs: calls.append("arxiv") or []
        feed_ops.fetch_hn = lambda *args, **kwargs: calls.append("hn") or []
        feed_ops.fetch_public_blog_candidates = lambda limit_each=12: []
        feed_ops.time.sleep = lambda *_: None
        feed_ops.existing_candidate_ids = lambda: set()

        feed_ops.fetch_candidates(profile={"active_interests": [{"topic": "AI", "terms": ["agents"]}]}, save=False)

        self.assertEqual(calls, [])

    def test_arxiv_and_hn_fetches_use_registry_endpoints_and_source_ids(self):
        feed_ops = load_feed_ops()
        calls = []
        records = [
            {"id": "papers", "name": "Papers", "connector": "arxiv_api", "endpoint": "https://papers.example/api", "enabled": True},
            {"id": "medical", "name": "Medical", "connector": "pubmed_api", "endpoint": "https://medical.example/eutils/", "enabled": True},
            {"id": "tech_news", "name": "Tech News", "connector": "hn_front_page", "endpoint": "https://news.example/front", "enabled": True},
        ]
        feed_ops.candidate_source_records = lambda: copy.deepcopy(records)

        def fake_arxiv(query, max_results=8, endpoint=None, source_id="arxiv"):
            calls.append(("arxiv", endpoint, source_id))
            return []

        def fake_hn(endpoint=None, source_id="hacker_news", limit=None):
            calls.append(("hn", endpoint, source_id))
            return []

        def fake_pubmed(queries, max_results=5, query_topics=None, per_query_sleep=0.4, endpoint=None, source_id="pubmed"):
            calls.append(("pubmed", endpoint, source_id))
            return []

        feed_ops.arxiv_query = fake_arxiv
        feed_ops.pubmed_candidates_for_queries = fake_pubmed
        feed_ops.fetch_hn = fake_hn
        feed_ops.fetch_public_blog_candidates = lambda limit_each=12: []
        feed_ops.time.sleep = lambda *_: None
        feed_ops.existing_candidate_ids = lambda: set()

        feed_ops.fetch_candidates(profile={"active_interests": [{"topic": "AI", "terms": ["agents"]}]}, save=False)

        self.assertIn(("arxiv", "https://papers.example/api", "papers"), calls)
        self.assertIn(("pubmed", "https://medical.example/eutils/", "medical"), calls)
        self.assertIn(("hn", "https://news.example/front", "tech_news"), calls)

    def test_lint_and_validate_include_native_arxiv_and_hn_connectors(self):
        feed_ops = load_feed_ops()
        records = [
            {"id": "arxiv", "name": "arXiv", "connector": "arxiv_api", "endpoint": "https://export.arxiv.org/api/query", "enabled": True},
            {"id": "hacker_news", "name": "HN", "connector": "hn_front_page", "endpoint": "https://news.ycombinator.com/news", "enabled": True},
            {"id": "paused_feed", "name": "Paused", "connector": "rss", "endpoint": "https://example.com/feed.xml", "enabled": False},
        ]
        feed_ops.candidate_source_records = lambda: copy.deepcopy(records)
        feed_ops.source_candidate_items = lambda rec, limit=5: [{"candidate_id": f"{rec['id']}:1", "source": rec["id"], "title": "Useful source item", "url": rec["endpoint"], "summary": "A sufficiently detailed source summary for native validation."}]
        feed_ops.build_profile = lambda save=False: {"active_interests": []}
        feed_ops.semantic_usefulness_report = lambda *args, **kwargs: {"ok": True, "errors": [], "warnings": [], "metrics": {}, "samples": []}

        class Helper:
            @staticmethod
            def lint_records(items, source, min_items=1):
                return {"ok": bool(items), "errors": [], "warnings": [], "metrics": {"items": len(items)}, "samples": []}

        feed_ops.load_site_feed_extractors_helper = lambda: Helper

        reports = feed_ops.lint_source_quality(limit=1)
        self.assertEqual([row["id"] for row in reports], ["arxiv", "hacker_news", "paused_feed"])
        self.assertEqual(reports[-1]["skipped"], "disabled")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            feed_ops.validate_source(limit=1)
        validated = json.loads(output.getvalue())
        self.assertEqual([row["id"] for row in validated], ["arxiv", "hacker_news", "paused_feed"])
        self.assertEqual(validated[-1]["skipped"], "disabled")

    def test_science_xyz_success_is_tracked(self):
        feed_ops = load_feed_ops()
        saved = {}
        state = {"science_xyz_news": {"last_checked": "2026-01-01T00:00:00Z", "seen_ids": []}}
        feed_ops.load_json = lambda path, default: copy.deepcopy(state) if path.name == "source_state.json" else ([] if path.name in {"recommendation_history.json", "candidates.json"} else default)
        feed_ops.save_json = lambda path, obj: saved.setdefault(path.name, copy.deepcopy(obj))
        feed_ops.candidate_source_records = lambda: [{"id": "science_xyz_news", "connector": "science_xyz_news", "endpoint": "https://science.xyz/news/", "enabled": True}]
        feed_ops.arxiv_query = lambda *args, **kwargs: []
        feed_ops.fetch_hn = lambda *args, **kwargs: []
        feed_ops.fetch_public_blog_candidates = lambda limit_each=12: [{"candidate_id": "science_xyz_news:one", "source": "science_xyz_news", "title": "Science", "summary": "Update", "url": "https://science.xyz/news/one"}]
        feed_ops.now_iso = lambda: "2026-08-08T12:00:00Z"
        feed_ops.time.sleep = lambda *_: None
        feed_ops.existing_candidate_ids = lambda: set()

        feed_ops.fetch_candidates(profile={"active_interests": [{"topic": "AI", "terms": ["agents"]}]}, save=True)

        self.assertEqual(saved["source_state.json"]["science_xyz_news"]["last_checked"], "2026-08-08T12:00:00Z")
        self.assertEqual(saved["source_state.json"]["science_xyz_news"]["seen_ids"], ["science_xyz_news:one"])


if __name__ == "__main__":
    unittest.main()
