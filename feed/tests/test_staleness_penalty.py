import copy
import importlib.util
import unittest
from pathlib import Path


def load_feed_ops():
    path = Path(__file__).resolve().parents[1] / "_tools" / "feed_ops.py"
    spec = importlib.util.spec_from_file_location("feed_ops_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FeedHealthTests(unittest.TestCase):
    def test_active_fetch_errors_ignores_errors_superseded_by_later_source_check(self):
        feed_ops = load_feed_ops()
        state = {
            "arxiv": {"last_checked": "2026-05-16T10:03:41Z", "seen_ids": []},
            "last_fetch_errors": [
                {
                    "source": "arxiv",
                    "title": "arXiv fetch failed for one query",
                    "error": "The read operation timed out",
                    "checked_at": "2026-05-16T10:02:10Z",
                },
            ],
        }

        active = feed_ops.active_fetch_errors(state)

        self.assertEqual([err["source"] for err in active], [])

    def test_fetch_candidates_does_not_mark_failed_source_successfully_checked(self):
        feed_ops = load_feed_ops()
        original_load_json = feed_ops.load_json
        original_save_json = feed_ops.save_json
        original_candidate_source_records = feed_ops.candidate_source_records
        original_arxiv_query = feed_ops.arxiv_query
        original_fetch_hn = feed_ops.fetch_hn
        original_fetch_public_blog_candidates = feed_ops.fetch_public_blog_candidates
        original_now_iso = feed_ops.now_iso
        state = {
            "arxiv": {"last_checked": "2026-05-16T10:01:00Z", "seen_ids": []},
            "hacker_news": {"last_checked": "2026-05-16T10:01:00Z", "seen_ids": []},
        }
        saved = {}

        def fake_load_json(path, default):
            if path.name == "source_state.json":
                return copy.deepcopy(state)
            if path.name in {"recommendation_history.json", "candidates.json"}:
                return []
            return default

        def fake_save_json(path, obj):
            saved[path.name] = copy.deepcopy(obj)

        try:
            feed_ops.load_json = fake_load_json
            feed_ops.save_json = fake_save_json
            feed_ops.candidate_source_records = lambda: []
            feed_ops.arxiv_query = lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("timed out"))
            feed_ops.fetch_hn = lambda: []
            feed_ops.fetch_public_blog_candidates = lambda limit_each=12: []
            feed_ops.now_iso = lambda: "2026-05-16T10:03:00Z"

            feed_ops.fetch_candidates(
                profile={"active_interests": [{"topic": "AI agents", "terms": ["agents"]}]},
                save=True,
            )

            saved_state = saved["source_state.json"]
            self.assertEqual(saved_state["arxiv"]["last_checked"], "2026-05-16T10:01:00Z")
            self.assertEqual(saved_state["hacker_news"]["last_checked"], "2026-05-16T10:03:00Z")
            self.assertEqual(saved_state["last_fetch_errors"][0]["source"], "arxiv")
            self.assertEqual([err["source"] for err in feed_ops.active_fetch_errors(saved_state)], ["arxiv"])
        finally:
            feed_ops.load_json = original_load_json
            feed_ops.save_json = original_save_json
            feed_ops.candidate_source_records = original_candidate_source_records
            feed_ops.arxiv_query = original_arxiv_query
            feed_ops.fetch_hn = original_fetch_hn
            feed_ops.fetch_public_blog_candidates = original_fetch_public_blog_candidates
            feed_ops.now_iso = original_now_iso


class StalenessPenaltyTests(unittest.TestCase):
    def test_staleness_penalty_makes_fresh_interest_candidate_beat_stale_backlog(self):
        feed_ops = load_feed_ops()
        original_today = feed_ops.today
        original_relevance = feed_ops.relevance
        try:
            feed_ops.today = lambda: "2026-05-14"
            candidates = [
                {
                    "candidate_id": "stale_blog:old",
                    "source": "stale_blog",
                    "title": "Old but very semantically matched BCI post",
                    "url": "https://example.com/old",
                    "published_date": "2024-09-17",
                    "summary": "old BCI backlog",
                },
                {
                    "candidate_id": "fresh_blog:new",
                    "source": "fresh_blog",
                    "title": "Fresh slightly less matched BCI post",
                    "url": "https://example.com/new",
                    "published_date": "2026-05-13",
                    "summary": "fresh BCI update",
                },
                {
                    "candidate_id": "other_source:core2",
                    "source": "other_source",
                    "title": "Second core item",
                    "url": "https://example.com/core2",
                    "published_date": "2026-05-13",
                    "summary": "second core",
                },
                {
                    "candidate_id": "other_source2:core3",
                    "source": "other_source2",
                    "title": "Third core item",
                    "url": "https://example.com/core3",
                    "published_date": "2026-05-13",
                    "summary": "third core",
                },
                {
                    "candidate_id": "explore1:item",
                    "source": "explore1",
                    "title": "Exploratory one",
                    "url": "https://example.com/e1",
                    "published_date": "2026-05-13",
                    "summary": "wildcard topic",
                },
                {
                    "candidate_id": "explore2:item",
                    "source": "explore2",
                    "title": "Exploratory two",
                    "url": "https://example.com/e2",
                    "published_date": "2026-05-13",
                    "summary": "wildcard topic",
                },
            ]
            relevance_by_id = {
                "stale_blog:old": (3.0, "neurotechnology and BCI"),
                "fresh_blog:new": (2.6, "neurotechnology and BCI"),
                "other_source:core2": (2.4, "neurotechnology and BCI"),
                "other_source2:core3": (2.3, "neurotechnology and BCI"),
                "explore1:item": (0.1, ""),
                "explore2:item": (0.1, ""),
            }
            feed_ops.relevance = lambda c, profile: relevance_by_id[c["candidate_id"]]

            selected = feed_ops.select(candidates, {"active_interests": [{"topic": "neurotechnology and BCI"}]})
            core_ids = [item["candidate_id"] for item in selected[:3]]

            self.assertIn("fresh_blog:new", core_ids)
            self.assertNotIn("stale_blog:old", core_ids)
        finally:
            feed_ops.today = original_today
            feed_ops.relevance = original_relevance


if __name__ == "__main__":
    unittest.main()
