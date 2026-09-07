"""Isolated tests: real PluginContext + Discord SDK; fake REST, never production I/O.
Run with the Hermes venv Python: -m unittest discover -s <this directory> -v.
"""
import asyncio
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

SOURCE_HOME = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(SOURCE_HOME / "scripts"), str(SOURCE_HOME / "hermes-agent")]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeREST:
    def __init__(self, commands=()):
        self.commands = {c["id"]: copy.deepcopy(c) for c in commands}
        self.calls = []
        self.fail = None
        self.bad_readback = False

    def __call__(self, method, endpoint, token, payload=None):
        self.calls.append((method, endpoint, copy.deepcopy(payload)))
        if self.fail and method == self.fail:
            return 403, {"message": "denied"}
        key = endpoint.rsplit("/", 1)[-1]
        if method == "GET":
            if key == "commands":
                return 200, copy.deepcopy(list(self.commands.values()))
            if key not in self.commands:
                return 404, None
            result = copy.deepcopy(self.commands[key])
            if self.bad_readback:
                result["options"] = [{"name": "unexpected"}]
            return 200, result
        if method == "POST":
            key = str(len(self.commands) + 1000)
            self.commands[key] = {"id": key, **payload}
            return 201, self.commands[key]
        if method == "PATCH":
            self.commands[key].update(payload)
            return 200, self.commands[key]
        if method == "DELETE":
            del self.commands[key]
            return 204, None
        raise AssertionError(method)

    @property
    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.registry = self.home / "tasks" / "_meta" / "task_registry.json"
        self.registry.parent.mkdir(parents=True)
        self.registry.write_text("[]")
        env = patch.dict(os.environ, {"HERMES_HOME": str(self.home), "HOME": str(self.home),
                                     "TASKS_ROOT": str(self.home / "tasks"),
                                     "HERMES_ALLOW_NONCANONICAL_LOCAL_ROOTS": "1"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.sync = load("discord_tag_commands", SOURCE_HOME / "scripts" / "discord_tag_commands.py")
        self.plugin = load("test_task_tags_plugin", SOURCE_HOME / "plugins" / "tasks-tags" / "__init__.py")
        from hermes_cli import plugins
        from hermes_cli.plugins_manifest import PluginManifest
        self.manager = plugins.PluginManager(scope_key=str(self.home))
        self.manager._discovered = True
        manager_patch = patch.object(plugins, "get_plugin_manager", return_value=self.manager)
        manager_patch.start()
        self.addCleanup(manager_patch.stop)
        self.addCleanup(self.manager.unload)
        self.ctx = plugins.PluginContext(PluginManifest(name="tasks-tags"), self.manager)
        self.rest = FakeREST()
        network = patch.object(self.sync, "discord_request", self.rest)
        network.start()
        self.addCleanup(network.stop)

    def reconcile(self, tags, **kwargs):
        return self.sync.reconcile("fixture-token", tags, app_id="123", **kwargs)

    def test_diff_only_readback_options_and_unrelated_preservation(self):
        unrelated = {"id": "1", "name": "help", "type": 1, "description": "Help", "options": []}
        old = {"id": "2", "name": "school", "type": 1,
               "description": "Show pending tasks for the School tag.",
               "options": [{"name": "args", "type": 3}]}
        self.rest.commands = {"1": unrelated.copy(), "2": old}
        result = self.reconcile({"School", "New Project"})
        self.assertEqual(result["changed"], 2)
        self.assertEqual([c[0] for c in self.rest.writes], ["POST", "PATCH"])
        self.assertTrue(all(c[2]["options"] == [] for c in self.rest.writes))
        self.assertEqual(self.rest.commands["1"], unrelated)
        self.assertEqual(self.reconcile({"School", "New Project"})["changed"], 0)
        self.assertEqual(len(self.rest.writes), 2)
        self.assertGreaterEqual(sum(c[0] == "GET" and not c[1].endswith("commands") for c in self.rest.calls), 2)

    def test_collisions_and_invalid_tags_fail_before_writes(self):
        for tags in ({"New Project", "New-Project"}, {"help"}, {"tags"}, {"x" * 33}, {"abc!"}):
            with self.subTest(tags=tags), self.assertRaises(ValueError):
                self.reconcile(tags)
        self.rest.commands = {"1": {"id": "1", "name": "school", "description": "School admin", "type": 1}}
        with self.assertRaisesRegex(ValueError, "unrelated"):
            self.reconcile({"School", "AAA"})
        self.assertEqual(self.rest.writes, [])

    def test_live_rejects_only_invalid_tags_and_receipt_failure_is_nonfatal(self):
        self.registry.write_text(json.dumps([{"tag": tag} for tag in
            ["Good", "R&D", "help", "New Project", "New-Project"]]))
        live = self.plugin.LiveTags(self.ctx)
        mapping = live.refresh_handlers(dispatch_aliases=False)
        self.assertEqual(mapping, {"good": "Good"})
        self.assertEqual(set(live.tag_errors), {"r&d", "help", "new_project"})
        with patch.object(live, "_write_receipt", side_effect=OSError("disk full")):
            live.receipt(mapping)  # must not propagate and terminate the watcher
        self.registry.write_text(json.dumps([{"tag": "New Project"}]))
        from hermes_cli.plugins import get_plugin_command_handler
        live.refresh_handlers(dispatch_aliases=False)
        self.assertIsNone(get_plugin_command_handler("new-project"))
        live.refresh_handlers()
        self.assertIsNotNone(get_plugin_command_handler("new-project"))

    def test_native_capacity_keeps_existing_tags_usable(self):
        async def scenario():
            import discord
            from discord.ext import commands
            native = commands.Bot(command_prefix="!", intents=discord.Intents.none())
            adapter = SimpleNamespace(_run_simple_slash=AsyncMock())
            live = self.plugin.LiveTags(self.ctx)
            live.refresh_native(native, adapter, {"school": "School"})
            async def callback(interaction):
                pass
            for i in range(99):
                native.tree.add_command(discord.app_commands.Command(
                    name=f"fixture{i}", description="Unrelated fixture", callback=callback))
            supported = live.refresh_native(native, adapter, {"school": "School", "new": "New"})
            self.assertEqual(supported, {"school": "School"})
            self.assertIn("new", live.tag_errors)
            await native.close()
        asyncio.run(scenario())

    def test_read_failure_write_failure_and_bad_readback_are_observable(self):
        for failure in ("GET", "POST"):
            self.rest.fail = failure
            with self.subTest(failure=failure), self.assertRaises(RuntimeError):
                self.reconcile({"School"})
        self.rest.fail = None
        self.rest.bad_readback = True
        with self.assertRaisesRegex(RuntimeError, "readback"):
            self.reconcile({"School"})

    def test_dry_prune_and_precise_ownership(self):
        self.rest.commands = {
            "1": {"id": "1", **self.sync.build_tag_command("Old")},
            "2": {"id": "2", "type": 1, "name": "other", "description": "Other tasks tagged somewhere"}}
        self.assertEqual(self.reconcile(set(), prune=True, dry_run=True)["changed"], 1)
        self.assertEqual(self.rest.writes, [])
        self.reconcile(set(), prune=True)
        self.assertEqual(set(self.rest.commands), {"2"})
        self.assertEqual(self.rest.calls[-1][:2], ("GET", "/applications/123/commands/1"))

    def test_live_add_real_plugin_registry_and_sdk_callback(self):
        async def scenario():
            import discord
            from discord.ext import commands
            from hermes_cli.plugins import get_plugin_command_handler
            native = commands.Bot(command_prefix="!", intents=discord.Intents.none())
            adapter = SimpleNamespace(_run_simple_slash=AsyncMock())
            live = self.plugin.LiveTags(self.ctx)
            live.refresh_native(native, adapter, live.refresh_handlers())
            self.assertIsNone(native.tree.get_command("new_project"))
            self.registry.write_text(json.dumps([{"id": "T-1-1", "tag": "New Project", "task": "fixture task"}]))
            mapping = live.refresh_handlers()
            live.refresh_native(native, adapter, mapping)
            command = native.tree.get_command("new_project")
            self.assertEqual(command.parameters, [])
            handler = get_plugin_command_handler("new_project".replace("_", "-"))
            self.assertIn("fixture task", handler(""))
            interaction = object()
            await command.callback(interaction)
            adapter._run_simple_slash.assert_awaited_once_with(interaction, "/new_project")
            self.assertEqual(live.refresh_handlers(), mapping)
            await native.close()
        asyncio.run(scenario())

    def test_watcher_adds_new_tag_without_restart_and_repairs_remote_drift(self):
        async def scenario():
            import discord
            from discord.ext import commands
            native = commands.Bot(command_prefix="!", intents=discord.Intents.none())
            native._connection.application_id = 123
            adapter = SimpleNamespace(_run_simple_slash=AsyncMock(), config=SimpleNamespace(token="fixture"))
            live = self.plugin.LiveTags(self.ctx)
            ticks = 0
            async def sleep(_):
                nonlocal ticks
                ticks += 1
                if ticks == 1:
                    self.registry.write_text(json.dumps([{"tag": "New Project", "task": "added after boot"}]))
                elif ticks == 2:
                    self.rest.commands.clear()  # remote deletion / core sync drift
                    live.last_remote = float("-inf")
                elif ticks == 3:
                    raise asyncio.CancelledError()
            with patch("asyncio.sleep", sleep):
                with self.assertRaises(asyncio.CancelledError):
                    await live.watch(native, adapter)
            self.assertIsNotNone(native.tree.get_command("new_project"))
            self.assertEqual([c[0] for c in self.rest.writes], ["POST", "POST"])
            self.assertEqual(live.remote_tags, {"New Project"})
            await native.close()
        asyncio.run(scenario())

    def test_native_callback_keeps_real_adapter_authorization_gate(self):
        async def scenario():
            import discord
            from discord.ext import commands
            from plugins.platforms.discord.adapter import DiscordAdapter
            native = commands.Bot(command_prefix="!", intents=discord.Intents.none())
            adapter = object.__new__(DiscordAdapter)
            adapter._check_slash_authorization = AsyncMock(return_value=False)
            adapter._defer_unless_expired = AsyncMock(return_value=False)
            adapter._build_slash_event = lambda interaction, text: text
            adapter.handle_message = AsyncMock()
            live = self.plugin.LiveTags(self.ctx)
            live.refresh_native(native, adapter, {"school": "School"})
            callback = native.tree.get_command("school").callback
            interaction = SimpleNamespace(user=SimpleNamespace(id=1, name="test"), channel=None)
            await callback(interaction)
            adapter._defer_unless_expired.assert_not_awaited()
            adapter.handle_message.assert_not_awaited()
            adapter._check_slash_authorization.return_value = True
            await callback(interaction)
            adapter.handle_message.assert_awaited_once_with("/school")
            await native.close()
        asyncio.run(scenario())

    def test_watcher_lifecycle_receipt_and_long_rate_cooldown(self):
        async def scenario():
            self.registry.write_text(json.dumps([{"tag": "School", "task": "fixture"}]))
            import discord
            from discord.ext import commands
            native = commands.Bot(command_prefix="!", intents=discord.Intents.none())
            # application_id normally comes from login; fixture SDK application data.
            native._connection.application_id = 123
            adapter = SimpleNamespace(_run_simple_slash=AsyncMock(), config=SimpleNamespace(token="fixture"))
            live = self.plugin.LiveTags(self.ctx)
            ticks = 0
            async def sleep(_):
                nonlocal ticks
                ticks += 1
                if ticks >= 2:
                    raise asyncio.CancelledError()
            with patch.object(self.sync, "reconcile", side_effect=self.sync.RateLimited(900)) as sync, patch("asyncio.sleep", sleep):
                with self.assertRaises(asyncio.CancelledError):
                    await live.watch(native, adapter)
                self.assertEqual(sync.call_count, 1)
            receipt = json.loads((self.home / "gateway/task_tag_commands_status.json").read_text())
            self.assertEqual(receipt["loaded_slugs"], ["school"])
            self.assertIn("900", receipt["error"])
            live.remote_tags = {"School"}
            live.receipt({"school": "School"})
            receipt = json.loads((self.home / "gateway/task_tag_commands_status.json").read_text())
            self.assertEqual(receipt["remote_slugs"], ["school"])
            async def alias_callback(interaction):
                pass
            native.tree.add_command(discord.app_commands.Command(
                name="new-project", description=self.sync.build_tag_command("New Project")["description"],
                callback=alias_callback))
            live.wire(native, adapter)
            self.assertIsNone(native.tree.get_command("new-project"))
            first = live.task
            live.wire(native, adapter)
            await asyncio.sleep(0)
            self.assertTrue(first.cancelled())
            self.manager.unload()
            await asyncio.sleep(0)
            self.assertTrue(live.task.cancelled())
            await native.close()
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
