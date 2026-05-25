import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from build_chart_page import chart_asset_name, svg_chart  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
