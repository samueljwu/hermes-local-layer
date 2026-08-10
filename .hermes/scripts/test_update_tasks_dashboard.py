#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).with_name("update_tasks_dashboard.py")
SPEC = importlib.util.spec_from_file_location("update_tasks_dashboard", SCRIPT)
assert SPEC and SPEC.loader
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)


class FakeResponse:
    def __init__(self, status_code, data=None):
        self.status_code = status_code
        self._data = data
        self.text = "" if data is None else json.dumps(data)
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._data


class DashboardRotationTests(unittest.TestCase):
    def test_concurrent_state_writes_are_atomic_and_leave_no_shared_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            with mock.patch.object(updater, "STATE_PATH", state_path):
                threads = [threading.Thread(target=updater.save_message_id, args=(str(1000 + i),)) for i in range(20)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertIn(json.loads(state_path.read_text())["message_id"], {str(1000 + i) for i in range(20)})
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_main_wraps_refresh_in_shared_task_lock(self):
        events = []

        class Lock:
            def __enter__(self):
                events.append("lock-enter")

            def __exit__(self, *_args):
                events.append("lock-exit")

        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "tasks" / "_meta" / "task_registry.json"
            registry.parent.mkdir(parents=True)
            registry.write_text("[]", encoding="utf-8")
            with mock.patch.object(updater, "REGISTRY_PATH", registry), mock.patch.object(updater, "ENV_PATH", Path(tmp) / ".env"), mock.patch.dict(updater.os.environ, {"DISCORD_BOT_TOKEN": "token"}), mock.patch.object(updater, "tasks_lock", side_effect=lambda root: Lock()) as lock, mock.patch.object(updater, "update_dashboard", side_effect=lambda *_: (events.append("refresh") or ("123", False))):
                self.assertEqual(updater.main(), 0)

        self.assertEqual(events, ["lock-enter", "refresh", "lock-exit"])
        lock.assert_called_once_with(registry.parents[1])

    def test_discord_retries_429_using_retry_after(self):
        responses = [
            FakeResponse(429, {"code": 30046, "retry_after": 2.5}),
            FakeResponse(200, {"id": "123"}),
        ]
        with mock.patch.object(updater.requests, "request", side_effect=responses) as request, mock.patch.object(updater.time, "sleep") as sleep:
            status, data = updater.discord("PATCH", "/endpoint", "token", {"content": "x"})

        self.assertEqual((status, data), (200, {"id": "123"}))
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(2.5)

    def test_updates_active_pinned_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps({"message_id": "123"}))
            calls = []

            def fake_discord(method, endpoint, token, payload=None):
                calls.append((method, endpoint, payload))
                if method == "GET":
                    return 200, [{"id": "123"}]
                return 200, None

            with mock.patch.object(updater, "STATE_PATH", state_path), mock.patch.object(updater, "discord", fake_discord):
                message_id, replaced = updater.update_dashboard("content", "token")

            self.assertEqual((message_id, replaced), ("123", False))
            self.assertEqual([call[0] for call in calls], ["PATCH", "GET"])
            self.assertEqual(json.loads(state_path.read_text())["message_id"], "123")

    def test_rotates_pin_on_discord_old_message_edit_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps({"message_id": "123"}))
            calls = []

            def fake_discord(method, endpoint, token, payload=None):
                calls.append((method, endpoint, payload))
                if method == "PATCH":
                    raise updater.DiscordAPIError(method, endpoint, 429, {"code": 30046, "retry_after": 1.0})
                if method == "POST":
                    return 200, {"id": "456"}
                if method == "GET":
                    return 200, [{"id": "456"}]
                return 204, None

            with mock.patch.object(updater, "STATE_PATH", state_path), mock.patch.object(updater, "discord", fake_discord):
                message_id, replaced = updater.update_dashboard("content", "token")

            self.assertEqual((message_id, replaced), ("456", True))
            self.assertEqual([call[0] for call in calls], ["PATCH", "POST", "PUT", "DELETE", "GET"])
            self.assertIn("/pins/456", calls[2][1])
            self.assertIn("/pins/123", calls[3][1])
            self.assertEqual(json.loads(state_path.read_text())["message_id"], "456")


if __name__ == "__main__":
    unittest.main()
