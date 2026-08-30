import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def load_updater():
    path = Path(__file__).resolve().parent / "update_feed_sources_message.py"
    spec = importlib.util.spec_from_file_location("update_feed_sources_message_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FeedSourcePinStateTests(unittest.TestCase):
    def test_save_json_rejects_symlinked_ancestor_without_touching_target(self):
        updater = load_updater()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outside = root / "outside"
            (outside / "feed" / "_meta").mkdir(parents=True)
            protected = outside / "feed" / "_meta" / "state.json"
            protected.write_text("preserve me\n", encoding="utf-8")
            ancestor = root / "linked-ancestor"
            ancestor.symlink_to(outside, target_is_directory=True)

            with self.assertRaises(OSError):
                updater.save_json(ancestor / "feed" / "_meta" / "state.json", {"changed": True})

            self.assertEqual(protected.read_text(encoding="utf-8"), "preserve me\n")

    def test_save_json_parent_swap_stays_bound_to_opened_hierarchy(self):
        updater = load_updater()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            feed = root / "safe" / "feed"
            meta = feed / "_meta"
            meta.mkdir(parents=True)
            outside = root / "outside"
            (outside / "_meta").mkdir(parents=True)
            protected = outside / "_meta" / "state.json"
            protected.write_text("preserve me\n", encoding="utf-8")
            detached = root / "safe" / "detached-feed"
            real_open = os.open
            swapped = False

            def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                fd = real_open(path, flags, mode, dir_fd=dir_fd)
                if path == "_meta" and dir_fd is not None and not swapped:
                    swapped = True
                    feed.rename(detached)
                    feed.symlink_to(outside, target_is_directory=True)
                return fd

            with mock.patch.object(updater.os, "open", side_effect=swapping_open):
                updater.save_json(meta / "state.json", {"safe": True})

            self.assertTrue(swapped)
            self.assertEqual(json.loads((detached / "_meta" / "state.json").read_text()), {"safe": True})
            self.assertEqual(protected.read_text(encoding="utf-8"), "preserve me\n")

    def test_feed_lock_rejects_symlinked_ancestor(self):
        updater = load_updater()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outside = root / "outside"
            (outside / "feed").mkdir(parents=True)
            ancestor = root / "linked-ancestor"
            ancestor.symlink_to(outside, target_is_directory=True)
            setattr(updater, "LOCK_PATH", ancestor / "feed" / ".feed_ops.lock")

            with self.assertRaises(OSError):
                with updater.feed_lock():
                    self.fail("symlinked ancestor unexpectedly accepted")

            self.assertFalse((outside / "feed" / ".feed_ops.lock").exists())

    def test_feed_lock_parent_swap_stays_bound_to_opened_hierarchy(self):
        updater = load_updater()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            feed = root / "safe" / "feed"
            feed.mkdir(parents=True)
            outside = root / "outside"
            outside.mkdir()
            detached = root / "safe" / "detached-feed"
            setattr(updater, "LOCK_PATH", feed / ".feed_ops.lock")
            real_open = os.open
            swapped = False

            def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                fd = real_open(path, flags, mode, dir_fd=dir_fd)
                if path == "feed" and dir_fd is not None and not swapped:
                    swapped = True
                    feed.rename(detached)
                    feed.symlink_to(outside, target_is_directory=True)
                return fd

            with mock.patch.object(updater.os, "open", side_effect=swapping_open):
                with updater.feed_lock():
                    self.assertTrue(swapped)

            self.assertTrue((detached / ".feed_ops.lock").is_file())
            self.assertFalse((outside / ".feed_ops.lock").exists())

    def test_save_json_rejects_symlinked_state_parent_without_touching_target(self):
        updater = load_updater()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outside = root / "outside"
            outside.mkdir()
            protected = outside / "state.json"
            protected.write_text("preserve me\n", encoding="utf-8")
            linked_parent = root / "linked-meta"
            linked_parent.symlink_to(outside, target_is_directory=True)

            with self.assertRaises(OSError):
                updater.save_json(linked_parent / "state.json", {"changed": True})

            self.assertEqual(protected.read_text(encoding="utf-8"), "preserve me\n")
            self.assertEqual(list(outside.iterdir()), [protected])

    def test_save_json_replaces_destination_symlink_not_its_target(self):
        updater = load_updater()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            protected = root / "protected.json"
            protected.write_text("preserve me\n", encoding="utf-8")
            state = root / "state.json"
            state.symlink_to(protected)

            updater.save_json(state, {"safe": True})

            self.assertFalse(state.is_symlink())
            self.assertEqual(json.loads(state.read_text()), {"safe": True})
            self.assertEqual(protected.read_text(encoding="utf-8"), "preserve me\n")

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
