#!/usr/bin/env python3
"""Regression tests for the filtered public local-layer backup."""
from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

SCRIPT = Path(__file__).with_name("hermes_local_layer_backup.py")


def load_module():
    spec = importlib.util.spec_from_file_location("hermes_local_layer_backup_tested", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LocalLayerBackupTests(unittest.TestCase):
    def test_copy_filtered_rejects_symlink_candidates(self):
        mod = load_module()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "src"
            work = root / "work"
            src.mkdir()
            (src / ".hermes" / "scripts").mkdir(parents=True)
            secret = root / "outside.txt"
            secret.write_text("outside private text\n", encoding="utf-8")
            (src / ".hermes" / "scripts" / "leak.py").symlink_to(secret)

            old_src, old_work = mod.SRC, mod.WORK
            mod_mut = cast(Any, mod)
            try:
                mod_mut.SRC = src
                mod_mut.WORK = work
                included, omitted = mod.copy_filtered()
            finally:
                mod_mut.SRC = old_src
                mod_mut.WORK = old_work

            self.assertEqual(included, [])
            self.assertIn((".hermes/scripts/leak.py", "symlink"), omitted)
            self.assertFalse((work / ".hermes" / "scripts" / "leak.py").exists())

    def test_verify_staged_tree_reports_secret_paths_without_values(self):
        mod = load_module()
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            subprocess.run(["git", "init"], cwd=work, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=work, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=work, check=True)
            secret_value = "A" * 24
            path = work / "README.md"
            path.write_text(f"api_key = '{secret_value}'\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=work, check=True)

            old_work = mod.WORK
            mod_mut = cast(Any, mod)
            try:
                mod_mut.WORK = work
                with self.assertRaises(RuntimeError) as ctx:
                    mod.verify_staged_tree()
            finally:
                mod_mut.WORK = old_work

            message = str(ctx.exception)
            self.assertIn("README.md", message)
            self.assertNotIn(secret_value, message)


if __name__ == "__main__":
    unittest.main()
