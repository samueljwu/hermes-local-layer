import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from stock_screener.price_history import (
    convert_yahoo_symbol,
    is_cache_fresh,
    normalize_yahoo_chart,
    write_price_csv,
    fetch_symbol_with_retries,
)
from unittest import mock


class PriceHistoryTests(unittest.TestCase):
    def test_convert_yahoo_symbol_replaces_class_dot_with_dash(self):
        self.assertEqual(convert_yahoo_symbol("BRK.B"), "BRK-B")
        self.assertEqual(convert_yahoo_symbol("bf.a"), "BF-A")
        self.assertEqual(convert_yahoo_symbol("AAPL"), "AAPL")

    def test_normalize_yahoo_chart_drops_null_incomplete_week(self):
        payload = {
            "chart": {
                "result": [
                    {
                        "timestamp": [1704067200, 1704672000],
                        "indicators": {
                            "quote": [
                                {
                                    "open": [10.0, None],
                                    "high": [12.0, None],
                                    "low": [9.5, None],
                                    "close": [11.0, None],
                                    "volume": [12345, None],
                                }
                            ],
                            "adjclose": [{"adjclose": [10.8, None]}],
                        },
                    }
                ],
                "error": None,
            }
        }
        rows = normalize_yahoo_chart(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date"], "2024-01-01")
        self.assertEqual(rows[0]["open"], 10.0)
        self.assertEqual(rows[0]["adj_close"], 10.8)

    def test_normalize_yahoo_chart_rejects_error_payload(self):
        payload = {"chart": {"result": None, "error": {"description": "bad symbol"}}}
        with self.assertRaises(ValueError):
            normalize_yahoo_chart(payload)

    def test_write_price_csv_round_trips_rows(self):
        rows = [
            {
                "date": "2024-01-01",
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.5,
                "adj_close": 1.4,
                "volume": 100,
            }
        ]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "AAPL.csv"
            write_price_csv(path, rows)
            self.assertIn("date,open,high,low,close,adj_close,volume", path.read_text())
            self.assertIn("2024-01-01", path.read_text())

    def test_is_cache_fresh_checks_mtime_and_row_count(self):
        rows = "date,open,high,low,close,adj_close,volume\n" + "\n".join(
            f"2024-01-{i:02d},1,2,0.5,1.5,1.4,100" for i in range(1, 6)
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "AAPL.csv"
            path.write_text(rows, encoding="utf-8")
            now = datetime.now(timezone.utc)
            self.assertTrue(is_cache_fresh(path, freshness_days=5, min_rows=5, now=now))
            self.assertFalse(is_cache_fresh(path, freshness_days=5, min_rows=6, now=now))

    def test_shorter_valid_yahoo_response_never_replaces_more_complete_valid_cache(self):
        def rows(count):
            return [
                {"date": f"2024-01-{index:02d}", "open": 10, "high": 12, "low": 9,
                 "close": 11, "adj_close": 11, "volume": 100}
                for index in range(1, count + 1)
            ]

        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td)
            path = output_dir / "AAPL.csv"
            write_price_csv(path, rows(3))
            old_bytes = path.read_bytes()
            with mock.patch("stock_screener.price_history.fetch_yahoo_payload", return_value={}), \
                 mock.patch("stock_screener.price_history.normalize_yahoo_chart", return_value=rows(2)):
                result = fetch_symbol_with_retries(
                    "AAPL", output_dir, 5, "1wk", 1, 1, [0], min_expected_rows=1,
                )
            self.assertEqual(result.status, "fetched_short_preserved_cache")
            self.assertEqual(result.rows, 3)
            self.assertEqual(path.read_bytes(), old_bytes)

    def test_shorter_response_can_replace_cache_that_does_not_match_schema(self):
        rows = [
            {"date": "2024-01-01", "open": 10, "high": 12, "low": 9,
             "close": 11, "adj_close": 11, "volume": 100},
        ]
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td)
            path = output_dir / "AAPL.csv"
            path.write_text("wrong,header\n" + "x,y\n" * 10, encoding="utf-8")
            with mock.patch("stock_screener.price_history.fetch_yahoo_payload", return_value={}), \
                 mock.patch("stock_screener.price_history.normalize_yahoo_chart", return_value=rows):
                result = fetch_symbol_with_retries(
                    "AAPL", output_dir, 5, "1wk", 1, 1, [0], min_expected_rows=1,
                )
            self.assertEqual(result.status, "fetched")
            self.assertIn("date,open,high,low,close,adj_close,volume", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
