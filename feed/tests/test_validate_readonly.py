from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

FEED_OPS = Path(__file__).resolve().parents[1] / "_tools" / "feed_ops.py"


def load_feed_ops(root: Path):
    old = os.environ.get("HERMES_FEED_ROOT")
    os.environ["HERMES_FEED_ROOT"] = str(root)
    try:
        spec = importlib.util.spec_from_file_location("feed_ops_readonly_test", FEED_OPS)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if old is None:
            os.environ.pop("HERMES_FEED_ROOT", None)
        else:
            os.environ["HERMES_FEED_ROOT"] = old


class FeedValidateReadonlyTests(unittest.TestCase):
    def test_validate_reports_missing_state_without_creating_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "SCHEMA.md").write_text("schema\n", encoding="utf-8")
            (root / "index.md").write_text("index\n", encoding="utf-8")
            (root / "log.md").write_text("log\n", encoding="utf-8")
            ops = load_feed_ops(root)

            errors = ops.validate()

            self.assertTrue(any("source_state.json" in error for error in errors))
            self.assertFalse((root / "_meta" / "source_state.json").exists())
            self.assertFalse((root / "runs").exists())


if __name__ == "__main__":
    unittest.main()
