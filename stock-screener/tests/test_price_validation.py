import csv
import tempfile
import unittest
from pathlib import Path

from stock_screener.price_validation import validate_price_file, validate_price_cache


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


if __name__ == "__main__":
    unittest.main()
