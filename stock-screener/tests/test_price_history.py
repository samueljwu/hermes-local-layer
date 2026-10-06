import csv
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
from stock_screener.price_validation import validate_price_file
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

    def test_is_cache_fresh_checks_bar_date_and_row_count_not_mtime(self):
        rows = "date,open,high,low,close,adj_close,volume\n" + "\n".join(
            f"2024-01-{i:02d},1,2,0.5,1.5,1.4,100" for i in (1, 8, 15, 22, 26)
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "AAPL.csv"
            path.write_text(rows, encoding="utf-8")
            now = datetime(2024, 1, 29, tzinfo=timezone.utc)
            self.assertTrue(is_cache_fresh(path, freshness_days=5, min_rows=5, now=now, interval="1wk"))
            self.assertFalse(is_cache_fresh(path, freshness_days=5, min_rows=6, now=now, interval="1wk"))
            self.assertFalse(is_cache_fresh(path, freshness_days=5, min_rows=5, now=datetime(2024, 2, 5, tzinfo=timezone.utc), interval="1wk"))

    def test_weekly_normalization_excludes_current_bar_and_non_finite(self):
        payload = {"chart": {"result": [{"timestamp": [1705881600, 1706486400, 1707091200],
            "indicators": {"quote": [{"open": [10, 11, float('nan')], "high": [12, 13, 14],
                                    "low": [9, 10, 11], "close": [11, 12, 12], "volume": [100, 100, 100]}],
                           "adjclose": [{"adjclose": [11, 12, 12]}]}}], "error": None}}
        rows = normalize_yahoo_chart(payload, interval="1wk", as_of=datetime(2024, 1, 30, tzinfo=timezone.utc))
        self.assertEqual([row["date"] for row in rows], ["2024-01-22"])

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

    def test_shorter_rolling_window_with_newer_week_replaces_stale_cache(self):
        from datetime import timedelta

        def weekly_rows(start, count):
            return [
                {"date": (start + timedelta(weeks=i)).isoformat(), "open": 10,
                 "high": 12, "low": 9, "close": 11, "adj_close": 11, "volume": 100}
                for i in range(count)
            ]

        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td)
            path = output_dir / "AAPL.csv"
            old = weekly_rows(datetime(2023, 5, 22).date(), 158)
            new = weekly_rows(datetime(2023, 10, 2).date(), 157)
            write_price_csv(path, old)
            with mock.patch("stock_screener.price_history.fetch_yahoo_payload", return_value={}), \
                 mock.patch("stock_screener.price_history.normalize_yahoo_chart", return_value=new):
                result = fetch_symbol_with_retries(
                    "AAPL", output_dir, 3, "1wk", 1, 1, [0], min_expected_rows=120,
                )
            self.assertEqual(result.status, "fetched")
            with path.open(newline="", encoding="utf-8") as fh:
                self.assertEqual(len(list(csv.DictReader(fh))), 157)
            self.assertEqual(validate_price_file(path, min_rows=120).latest_date, new[-1]["date"])

    def test_shorter_sparse_newer_history_preserves_cache(self):
        from datetime import timedelta
        start = datetime(2023, 5, 22).date()
        def row(day):
            return {"date": day.isoformat(), "open": 10, "high": 12, "low": 9,
                    "close": 11, "adj_close": 11, "volume": 100}
        old = [row(start + timedelta(weeks=i)) for i in range(158)]
        new = [row(start + timedelta(weeks=i)) for i in range(40, 170)]
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td)
            path = output_dir / "AAPL.csv"
            write_price_csv(path, old)
            with mock.patch("stock_screener.price_history.fetch_yahoo_payload", return_value={}), \
                 mock.patch("stock_screener.price_history.normalize_yahoo_chart", return_value=new):
                result = fetch_symbol_with_retries(
                    "AAPL", output_dir, 3, "1wk", 1, 1, [0], min_expected_rows=120,
                )
            self.assertEqual(result.status, "fetched_short_preserved_cache")
            self.assertEqual(validate_price_file(path, min_rows=120).latest_date, old[-1]["date"])

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

    def test_weekly_cache_uses_completed_rows_for_minimum(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "A.csv"
            path.write_text("date,open,high,low,close,adj_close,volume\n2024-01-22,10,12,9,11,11,100\n2024-01-29,10,12,9,11,11,100\n")
            now = datetime(2024, 1, 30, tzinfo=timezone.utc)
            self.assertTrue(is_cache_fresh(path, 5, 1, now, interval="1wk"))
            self.assertFalse(is_cache_fresh(path, 5, 2, now, interval="1wk"))

    def test_retry_deadline_clips_timeout_and_backoff(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("stock_screener.price_history.time.perf_counter", side_effect=[0.2, 0.7, 1.0]), \
                 mock.patch("stock_screener.price_history.time.sleep") as sleep, \
                 mock.patch("stock_screener.price_history.fetch_yahoo_payload", side_effect=TimeoutError("offline")) as fetch:
                result = fetch_symbol_with_retries("A", Path(td), 5, "1wk", 20, 3, [5], 1, deadline=1)
            self.assertEqual((result.status, result.attempts), ("budget_exhausted", 1))
            self.assertEqual(fetch.call_args.args[-1], 0.8)
            self.assertAlmostEqual(sleep.call_args.args[0], 0.3)

    def test_expired_deadline_never_requests(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("stock_screener.price_history.time.perf_counter", return_value=5), \
                 mock.patch("stock_screener.price_history.fetch_yahoo_payload") as fetch:
                result = fetch_symbol_with_retries("A", Path(td), 5, "1wk", 20, 3, [5], 1, deadline=5)
            self.assertEqual((result.status, result.attempts), ("budget_exhausted", 0))
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
