import csv
import json
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import refresh_nasdaq_screener_metadata as metadata_refresh  # noqa: E402
import refresh_universe  # noqa: E402
import build_chart_page  # noqa: E402
from build_chart_page import promote_directory, publish_html_with_assets  # noqa: E402
from stock_screener.owned_paths import resolve_owned_path  # noqa: E402


@dataclass(frozen=True)
class DummyRow:
    symbol: str
    name: str


class RefreshSafetyTests(unittest.TestCase):
    def test_configured_output_paths_must_remain_under_owned_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(
                resolve_owned_path(root, "data/out.json", label="output"),
                root / "data" / "out.json",
            )
            for unsafe in ("/tmp/out.json", "../out.json", "data/../out.json", ""):
                with self.subTest(unsafe=unsafe):
                    with self.assertRaises(ValueError):
                        resolve_owned_path(root, unsafe, label="output")

    def test_configured_output_paths_reject_symlink_escape(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            outside = Path(td) / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "linked").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                resolve_owned_path(root, "linked/out.json", label="output")

    def test_universe_refresh_rejects_sparse_counts_before_promotion(self):
        with self.assertRaisesRegex(RuntimeError, "Refusing to promote sparse universe refresh"):
            refresh_universe.validate_refresh_counts(
                {"minimum_counts": {"nasdaq_raw": 2, "nyse_raw_xnys": 2, "combined_active": 3}},
                {"nasdaq_raw": 2, "nyse_raw_xnys": 1, "combined_active": 3},
            )

    def test_universe_write_csv_is_atomic_and_preserves_existing_on_open_failure(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rows.csv"
            path.write_text("existing\n", encoding="utf-8")
            original_open = Path.open

            def failing_open(self, *args, **kwargs):
                if self.name.startswith(".rows.csv.") and self.name.endswith(".tmp"):
                    raise OSError("simulated write failure")
                return original_open(self, *args, **kwargs)

            try:
                Path.open = failing_open
                with self.assertRaises(OSError):
                    refresh_universe.write_csv(path, [DummyRow("A", "Alpha")])
            finally:
                Path.open = original_open
            self.assertEqual(path.read_text(encoding="utf-8"), "existing\n")

    def test_metadata_refresh_rejects_sparse_live_exchange_rows(self):
        with self.assertRaisesRegex(RuntimeError, "too few nasdaq metadata rows"):
            metadata_refresh.validate_exchange_rows(
                {"minimum_exchange_rows": {"nasdaq": 2}},
                "nasdaq",
                [{"symbol": "AAPL"}],
                "github_raw",
            )

    def test_metadata_write_csv_is_atomic_and_writes_rows(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rows.csv"
            count = metadata_refresh.write_csv(path, [DummyRow("A", "Alpha")])
            self.assertEqual(count, 1)
            with path.open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(rows, [{"symbol": "A", "name": "Alpha"}])
            self.assertFalse((Path(td) / ".rows.csv.tmp").exists())

    def test_promote_directory_keeps_live_assets_if_staging_fails_before_promotion(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            live = root / "charts"
            live.mkdir()
            (live / "old.svg").write_text("old", encoding="utf-8")
            staging = root / ".charts.tmp"
            staging.mkdir()
            (staging / "new.svg").write_text("new", encoding="utf-8")
            promote_directory(staging, live)
            self.assertFalse(staging.exists())
            self.assertFalse((live / "old.svg").exists())
            self.assertEqual((live / "new.svg").read_text(encoding="utf-8"), "new")

    def test_publish_html_with_assets_rolls_assets_back_on_html_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            live = root / "charts"
            live.mkdir()
            (live / "old.svg").write_text("old", encoding="utf-8")
            staging = root / ".charts.tmp"
            staging.mkdir()
            (staging / "new.svg").write_text("new", encoding="utf-8")
            html_path = root / "index.html"
            html_path.write_text("old html", encoding="utf-8")
            old_write = build_chart_page.write_text_atomic
            try:
                build_chart_page.write_text_atomic = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("html failure"))
                with self.assertRaises(OSError):
                    publish_html_with_assets(staging, live, html_path, "new html")
            finally:
                build_chart_page.write_text_atomic = old_write
            self.assertEqual((live / "old.svg").read_text(encoding="utf-8"), "old")
            self.assertFalse((live / "new.svg").exists())
            self.assertEqual(html_path.read_text(encoding="utf-8"), "old html")


if __name__ == "__main__":
    unittest.main()
