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


def load_feed_plugin():
    path = Path("/home/hermes/.hermes/plugins/feed-feedback/__init__.py")
    spec = importlib.util.spec_from_file_location("feed_feedback_plugin_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FeedInterestFeedbackTests(unittest.TestCase):
    def test_render_digest_adds_pick_id_and_copyable_feedinterest_command_to_exploratory_cards(self):
        feed_ops = load_feed_ops()
        selected = [
            {"slot": 1, "title": "Core", "source": "arxiv", "url": "https://example.com/core", "relation_type": "direct_interest", "matched_interest": "neurotechnology and BCI", "summary": "Core summary."},
            {"slot": 2, "title": "Core 2", "source": "pubmed", "url": "https://example.com/core2", "relation_type": "adjacent_interest", "matched_interest": "AI agents and developer tooling", "summary": "Core summary."},
            {"slot": 3, "title": "Core 3", "source": "openai_news", "url": "https://example.com/core3", "relation_type": "adjacent_interest", "matched_interest": "AI agents and developer tooling", "summary": "Core summary."},
            {"slot": 4, "title": "Wildcard", "source": "hacker_news", "url": "https://example.com/wild", "relation_type": "exploratory", "why_recommended": "Wildcard worth testing.", "summary": "Wildcard summary."},
            {"slot": 5, "title": "Wildcard 2", "source": "quanta_magazine", "url": "https://example.com/wild2", "relation_type": "exploratory", "why_recommended": "Wildcard worth testing.", "summary": "Wildcard summary."},
        ]

        text = feed_ops.render_digest("2026-05-21-1807", selected, {"active_interests": []})

        self.assertEqual(selected[3]["pick_id"], "2026-05-21-1807:4")
        self.assertIn("ID: `2026-05-21-1807:4`", text)
        self.assertIn("`/feedinterest 2026-05-21-1807:4 AI agents and developer tooling`", text)
        self.assertNotIn("/feedscore", text)

    def test_promote_interest_feedback_records_topic_match_without_mutating_history(self):
        feed_ops = load_feed_ops()
        original_load_json = feed_ops.load_json
        original_save_json = feed_ops.save_json
        original_now_iso = feed_ops.now_iso
        saved = {}
        history = [
            {
                "run_id": "2026-05-21-1807",
                "slot": 4,
                "candidate_id": "hn:48207660",
                "source": "hacker_news",
                "title": "GitHub confirms breach of 3,800 repos via malicious VSCode extension",
                "url": "https://example.com/item",
                "relation_type": "exploratory",
                "summary": "A developer tooling security story.",
            }
        ]

        def fake_load_json(path, default):
            if path.name == "recommendation_history.json":
                return copy.deepcopy(history)
            if path.name == "interest_feedback.json":
                return []
            return default

        def fake_save_json(path, obj):
            saved[path.name] = copy.deepcopy(obj)

        try:
            feed_ops.load_json = fake_load_json
            feed_ops.save_json = fake_save_json
            feed_ops.now_iso = lambda: "2026-05-21T11:20:00Z"

            result = feed_ops.promote_interest_feedback("2026-05-21-1807:4", "AI agents and developer tooling")

            self.assertEqual(result["pick_id"], "2026-05-21-1807:4")
            self.assertEqual(result["topic"], "AI agents and developer tooling")
            self.assertEqual(result["note"], "")
            self.assertEqual(saved["interest_feedback.json"][0]["candidate_id"], "hn:48207660")
            self.assertNotIn("recommendation_history.json", saved)
        finally:
            feed_ops.load_json = original_load_json
            feed_ops.save_json = original_save_json
            feed_ops.now_iso = original_now_iso

    def test_collect_signals_includes_promoted_exploratory_feedback_as_feed_local_signal(self):
        feed_ops = load_feed_ops()
        original_tail_text = feed_ops.tail_text
        original_load_json = feed_ops.load_json
        try:
            feed_ops.tail_text = lambda path, limit: ""

            def fake_load_json(path, default):
                if path.name == "interest_feedback.json":
                    return [
                        {
                            "action": "promote_to_interest",
                            "topic": "AI agents and developer tooling",
                            "note": "developer security tooling",
                            "title": "GitHub confirms breach via malicious VSCode extension",
                            "summary": "Security issue in developer tooling.",
                        }
                    ]
                if path.name in {"entry_registry.json", "task_registry.json"}:
                    return []
                return default

            feed_ops.load_json = fake_load_json

            signals = feed_ops.collect_signals()["signals"]

            feedback_signals = [s for s in signals if s.get("source_system") == "feed" and s.get("kind") == "promoted_exploratory"]
            self.assertEqual(len(feedback_signals), 1)
            self.assertIn("AI agents and developer tooling", feedback_signals[0]["text"])
            self.assertIn("developer security tooling", feedback_signals[0]["text"])
            self.assertGreaterEqual(feedback_signals[0]["weight"], 0.5)
        finally:
            feed_ops.tail_text = original_tail_text
            feed_ops.load_json = original_load_json

    def test_feedinterest_plugin_passes_raw_pick_and_topic_or_note_to_harness(self):
        plugin = load_feed_plugin()
        calls = []
        original_run_feed_ops = plugin._run_feed_ops
        try:
            def fake_run_feed_ops(args, *, timeout):
                calls.append((args, timeout))
                return "Recorded interest feedback for pick 2026-05-21-1807:4"

            plugin._run_feed_ops = fake_run_feed_ops

            response = plugin._handle_feedinterest("2026-05-21-1807:4 AI agents and developer tooling")

            self.assertIn("Recorded interest feedback", response)
            self.assertEqual(calls, [(["feedback", "promote", "2026-05-21-1807:4", "AI agents and developer tooling"], 30)])
        finally:
            plugin._run_feed_ops = original_run_feed_ops


if __name__ == "__main__":
    unittest.main()
