import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


def load_updater():
    path = Path(__file__).resolve().parent / "update_feed_sources_message.py"
    spec = importlib.util.spec_from_file_location("update_feed_sources_message_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FeedSourcePinStateTests(unittest.TestCase):
    def test_new_message_id_is_written_only_to_atomic_state_under_feed_lock(self):
        updater = load_updater()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            meta = root / "_meta"
            meta.mkdir()
            sources = meta / "information_sources.json"
            state = meta / "information_sources_message_state.json"
            lock = root / ".feed_ops.lock"
            config = {
                "channel_id": "123",
                "allowed_candidate_sources": [
                    {"id": "arxiv", "name": "arXiv", "connector": "arxiv_api", "endpoint": "https://export.arxiv.org/api/query", "enabled": True}
                ],
            }
            sources.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            original_registry = sources.read_bytes()
            updater.FEED_ROOT = root
            updater.SOURCES_PATH = sources
            updater.STATE_PATH = state
            updater.LOCK_PATH = lock
            updater.ENV_PATH = root / "missing.env"
            os.environ["DISCORD_BOT_TOKEN"] = "test-token"
            calls = []

            def fake_discord(method, endpoint, token, payload=None):
                calls.append((method, endpoint, payload))
                if method == "POST":
                    return 200, {"id": "new-message"}
                if method == "GET":
                    return 200, []
                return 204, None

            updater.discord = fake_discord
            result = updater.main([])

            self.assertEqual(result, 0)
            self.assertEqual(sources.read_bytes(), original_registry)
            self.assertEqual(json.loads(state.read_text())["message_id"], "new-message")
            self.assertEqual([call[0] for call in calls], ["POST", "GET", "PUT"])
            self.assertFalse(list(meta.glob("*.tmp")))

    def test_legacy_registry_message_id_is_ignored(self):
        updater = load_updater()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            meta = root / "_meta"
            meta.mkdir()
            sources = meta / "information_sources.json"
            state = meta / "information_sources_message_state.json"
            config = {"channel_id": "123", "message_id": "stale-registry-id", "allowed_candidate_sources": []}
            sources.write_text(json.dumps(config) + "\n", encoding="utf-8")
            updater.FEED_ROOT = root
            updater.SOURCES_PATH = sources
            updater.STATE_PATH = state
            updater.LOCK_PATH = root / ".feed_ops.lock"
            updater.ENV_PATH = root / "missing.env"
            os.environ["DISCORD_BOT_TOKEN"] = "test-token"
            methods = []

            def fake_discord(method, endpoint, token, payload=None):
                methods.append(method)
                if method == "POST":
                    return 200, {"id": "state-only-id"}
                if method == "GET":
                    return 200, []
                return 204, None

            updater.discord = fake_discord
            self.assertEqual(updater.main([]), 0)
            self.assertEqual(methods[0], "POST")
            self.assertEqual(json.loads(state.read_text())["message_id"], "state-only-id")
            self.assertEqual(json.loads(sources.read_text())["message_id"], "stale-registry-id")


if __name__ == "__main__":
    unittest.main()
