import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from build_chart_page import chart_asset_name, ensure_daily_price_file, resolve_owned_output_path, svg_chart  # noqa: E402


class ChartPageAssetTests(unittest.TestCase):
    def test_svg_chart_is_valid_standalone_svg_image_asset(self):
        rows = [
            {"date": "2024-01-01", "open": "10", "high": "12", "low": "9", "close": "11", "volume": "1000"},
            {"date": "2024-01-02", "open": "11", "high": "13", "low": "10", "close": "12", "volume": "1200"},
            {"date": "2024-01-03", "open": "12", "high": "14", "low": "11", "close": "13", "volume": "1500"},
        ]
        svg = svg_chart("AAPL", rows, "resistance_breakout", {}, lookback=3)
        self.assertIn('xmlns="http://www.w3.org/2000/svg"', svg[:160])
        self.assertIn('viewBox="0 0 560 330"', svg[:160])
        self.assertTrue(svg.endswith("</svg>"))

    def test_chart_asset_name_sanitizes_symbols(self):
        self.assertEqual(chart_asset_name(7, "BRK.B"), "007-BRK.B.svg")
        self.assertEqual(chart_asset_name(8, "BAD / SYMBOL"), "008-BAD-SYMBOL.svg")

    def test_chart_page_generation_is_cache_only_by_default(self):
        with self.assertRaises(ValueError):
            resolve_owned_output_path(ROOT / "site" / "dist", "/tmp/escape")
        with self.assertRaises(ValueError):
            resolve_owned_output_path(ROOT / "site" / "dist", "../outside")

    def test_ensure_daily_price_file_does_not_fetch_when_disabled(self):
        # Guard against accidental network/provider fetches in page rendering.
        import build_chart_page
        old_fetch = build_chart_page.fetch_symbol_with_retries
        try:
            build_chart_page.fetch_symbol_with_retries = lambda **kwargs: (_ for _ in ()).throw(AssertionError("fetch called"))
            usable, status = ensure_daily_price_file("TEST", ROOT / "data" / "missing" / "TEST.csv", {"fetch_missing_daily": False})
        finally:
            build_chart_page.fetch_symbol_with_retries = old_fetch
        self.assertIsNone(usable)
        self.assertEqual(status, "missing")


if __name__ == "__main__":
    unittest.main()
