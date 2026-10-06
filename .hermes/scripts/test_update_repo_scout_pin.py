from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("update_repo_scout_pin.py")


def load_module():
    sys.modules.setdefault("requests", mock.Mock())
    spec = importlib.util.spec_from_file_location("update_repo_scout_pin_tested", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RepoScoutPinStateTests(unittest.TestCase):
    def test_save_json_does_not_follow_predictable_temp_symlink(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            destination = root / "state.json"
            victim = root / "victim.txt"
            victim.write_text("preserve me\n", encoding="utf-8")
            destination.with_suffix(".json.tmp").symlink_to(victim)

            module.save_json(destination, {"ok": True})

            self.assertEqual(victim.read_text(encoding="utf-8"), "preserve me\n")
            self.assertIn('"ok": true', destination.read_text(encoding="utf-8"))

    def test_save_json_rejects_symlinked_parent_without_external_write(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outside = root / "outside"
            outside.mkdir()
            linked_parent = root / "linked"
            linked_parent.symlink_to(outside, target_is_directory=True)

            with self.assertRaises(ValueError):
                module.save_json(linked_parent / "state.json", {"ok": True})

            self.assertFalse((outside / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
