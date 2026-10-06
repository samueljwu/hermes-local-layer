import csv
import os
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone

from stock_screener.price_validation import validate_price_file, validate_price_cache, weekly_status, completed_week


class PriceValidationTests(unittest.TestCase):
    def write_csv(self, path: Path, rows: list[dict[str, object]]) -> None:
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["date", "open", "high", "low", "close", "adj_close", "volume"])
            writer.writeheader()
            writer.writerows(rows)

    def test_validate_price_file_accepts_ordered_valid_ohlcv_rows(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "AAPL.csv"
            self.write_csv(path, [
                {"date": "2024-01-01", "open": 10, "high": 12, "low": 9, "close": 11, "adj_close": 11, "volume": 100},
                {"date": "2024-01-08", "open": 11, "high": 13, "low": 10, "close": 12, "adj_close": 12, "volume": 200},
            ])
            result = validate_price_file(path, min_rows=2)
            self.assertTrue(result.valid)
            self.assertEqual(result.rows, 2)
            self.assertEqual(result.issue, "")

    def test_validate_price_file_rejects_ohlc_invariant_break(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "BAD.csv"
            self.write_csv(path, [
                {"date": "2024-01-01", "open": 10, "high": 9, "low": 8, "close": 11, "adj_close": 11, "volume": 100},
            ])
            result = validate_price_file(path, min_rows=1)
            self.assertFalse(result.valid)
            self.assertEqual(result.issue, "ohlcv_invariant")

    def test_validate_price_cache_reports_missing_and_short_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            price_dir = root / "prices"
            price_dir.mkdir()
            self.write_csv(price_dir / "AAPL.csv", [
                {"date": "2024-01-01", "open": 10, "high": 12, "low": 9, "close": 11, "adj_close": 11, "volume": 100},
            ])
            summary = validate_price_cache(["AAPL", "MSFT"], price_dir, min_rows=2)
            self.assertEqual(summary["expected_symbols"], 2)
            self.assertEqual(summary["missing_count"], 1)
            self.assertEqual(summary["short_count"], 1)
            self.assertEqual(summary["invalid_count"], 0)

    def test_week_label_monday_friday_and_in_progress_week(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "A.csv"
            as_of = datetime(2024, 1, 30, tzinfo=timezone.utc)
            self.assertEqual(completed_week(as_of).isoformat(), "2024-01-22")
            for label, expected in (("2024-01-22", "fresh"), ("2024-01-26", "fresh"),
                                    ("2024-01-29", "incomplete"), ("2024-01-19", "stale")):
                self.write_csv(path, [{"date": label, "open": 10, "high": 12, "low": 9, "close": 11, "adj_close": 11, "volume": 100}])
                os.utime(path, None)
                result = validate_price_file(path, min_rows=1)
                self.assertTrue(result.valid)
                self.assertEqual(weekly_status(result, as_of).status, expected)

    def test_exchange_close_cutoff_and_holiday_week(self):
        for instant, expected in (
            (datetime(2024, 1, 26, 20, 59, tzinfo=timezone.utc), "2024-01-15"),
            (datetime(2024, 1, 26, 21, 0, tzinfo=timezone.utc), "2024-01-22"),
            (datetime(2024, 7, 5, 19, 59, tzinfo=timezone.utc), "2024-06-24"),
            (datetime(2024, 7, 5, 20, 0, tzinfo=timezone.utc), "2024-07-01"),
            (datetime(2024, 7, 6, 2, tzinfo=timezone.utc), "2024-07-01"),
        ):
            self.assertEqual(completed_week(instant).isoformat(), expected)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "A.csv"
            self.write_csv(path, [{"date": "2024-03-28", "open": 10, "high": 12, "low": 9, "close": 11, "adj_close": 11, "volume": 100}])
            # Conservative policy: Thursday after close does not complete a
            # holiday-shortened week; Friday 16:00 ET does, without a Friday bar.
            thursday = datetime(2024, 3, 28, 21, tzinfo=timezone.utc)
            self.assertEqual(completed_week(thursday).isoformat(), "2024-03-18")
            self.assertEqual(weekly_status(validate_price_file(path, 0), thursday).status, "incomplete")
            self.assertEqual(weekly_status(validate_price_file(path, 0), datetime(2024, 3, 29, 20, tzinfo=timezone.utc)).status, "fresh")

    def test_completed_prefix_status_and_coverage_report_tail(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "A.csv"
            self.write_csv(path, [
                {"date": label, "open": 10, "high": 12, "low": 9, "close": 11, "adj_close": 11, "volume": 100}
                for label in ("2024-01-15", "2024-01-22", "2024-01-29")
            ])
            as_of = datetime(2024, 1, 30, tzinfo=timezone.utc)
            status = weekly_status(validate_price_file(path, 0), as_of)
            self.assertEqual((status.status, status.latest_date, status.latest_week, status.completed_rows, status.incomplete_tail_rows),
                             ("fresh", "2024-01-22", "2024-01-22", 2, 1))
            coverage = validate_price_cache(["A"], Path(td), min_rows=1, as_of=as_of)
            self.assertEqual((coverage["fresh_count"], coverage["incomplete_tail_count"]), (1, 1))
            self.assertEqual(coverage["incomplete_tail_sample"][0]["latest_date"], "2024-01-22")

    def test_non_finite_values_are_schema_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "A.csv"
            for value in ("nan", "inf", "-inf"):
                self.write_csv(path, [{"date": "2024-01-26", "open": value, "high": 12, "low": 9, "close": 11, "adj_close": 11, "volume": 100}])
                self.assertEqual(validate_price_file(path, min_rows=1).issue, "non_finite")

    def test_empty_history_is_invalid_even_with_zero_minimum(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "A.csv"
            self.write_csv(path, [])
            self.assertEqual(validate_price_file(path, min_rows=0).issue, "empty_history")


if __name__ == "__main__":
    unittest.main()
