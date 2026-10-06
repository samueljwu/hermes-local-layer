import json
import unittest
from datetime import date, datetime, timedelta, timezone

from stock_screener.technical_context import calculate_technical_context


LATEST = date(2024, 9, 30)  # Last complete week on the as-of date below.
AS_OF = datetime(2024, 10, 7, 12, tzinfo=timezone.utc)


def history(*, count=31, stock=False, friday=False):
    first = LATEST - timedelta(weeks=count - 1)
    rows = []
    for i in range(count):
        label = first + timedelta(weeks=i, days=4 if friday else 0)
        price = 100 + i
        rows.append({"date": label.isoformat(), "close": str(price), "adj_close": str(price)})
    return rows


class TechnicalContextTests(unittest.TestCase):
    def test_adjusted_ratio_deteriorates_despite_rising_raw_stock_price(self):
        stock = history(friday=True)
        benchmark = history()
        # Price appreciation in the raw series is not relative strength.
        for row in stock:
            row["close"] = "1000"
            row["adj_close"] = "50"
        stock[-1]["close"] = "1200"
        stock[-1]["adj_close"] = "45"
        result = calculate_technical_context(stock, benchmark, as_of=AS_OF)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["basis"], "adjusted_close_ratio")
        self.assertEqual(result["interval"], "1wk")
        self.assertEqual(result["latest_week"], LATEST.isoformat())
        self.assertEqual(result["prior_week"], (LATEST - timedelta(weeks=13)).isoformat())
        self.assertAlmostEqual(result["ratio_change_pct"],
                               ((45 / 130) / (50 / 117) - 1) * 100)
        self.assertFalse(result["improving"])
        self.assertAlmostEqual(result["benchmark_close"], 130)
        self.assertAlmostEqual(result["benchmark_ma_30"], sum(range(101, 131)) / 30)
        self.assertGreater(result["benchmark_ma_slope"], 0)
        self.assertEqual(result["benchmark_vs_ma"], "above")
        json.dumps(result, allow_nan=False)

    def test_positive_relative_strength_and_falling_market_context(self):
        stock = history()
        benchmark = history(friday=True)
        for i, row in enumerate(stock):
            row["adj_close"] = str(100 + 2 * i)
        for i, row in enumerate(benchmark):
            row["close"] = str(200 - i)
            row["adj_close"] = str(200 - i)
        result = calculate_technical_context(stock, benchmark, as_of=AS_OF)
        self.assertEqual(result["status"], "available")
        self.assertTrue(result["improving"])
        self.assertLess(result["benchmark_ma_slope"], 0)
        self.assertEqual(result["benchmark_vs_ma"], "below")

    def test_disabled_is_neutral_even_without_data(self):
        result = calculate_technical_context([], None, enabled=False, as_of=AS_OF)
        self.assertEqual(result["status"], "disabled")
        self.assertIsNone(result["improving"])
        self.assertIsNone(result["ratio_change_pct"])
        self.assertIsNone(result["benchmark_ma_30"])
        json.dumps(result, allow_nan=False)

    def test_missing_data_and_stale_data_have_explicit_reasons(self):
        self.assertEqual(calculate_technical_context(None, history(), as_of=AS_OF)["reason"], "missing_stock")
        self.assertEqual(calculate_technical_context(history(), None, as_of=AS_OF)["reason"], "missing_benchmark")
        stale = history(count=31)[:-1]
        self.assertEqual(calculate_technical_context(stale, history(), as_of=AS_OF)["reason"], "stale_stock")
        self.assertEqual(calculate_technical_context(history(), stale, as_of=AS_OF)["reason"], "stale_benchmark")

    def test_misaligned_latest_weeks_without_as_of_are_not_index_aligned(self):
        stock = history()
        benchmark = history()[:-1]
        result = calculate_technical_context(stock, benchmark)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "misaligned_latest_week")
        self.assertIsNone(result["ratio_change_pct"])

    def test_missing_exact_13_week_endpoint_never_compresses_window(self):
        stock = history()
        stock.pop(-14)
        result = calculate_technical_context(stock, history(), as_of=AS_OF)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "missing_prior_common_week")

    def test_benchmark_ma_and_slope_need_31_consecutive_weeks(self):
        for benchmark in (history(count=30), history()[:-20] + history()[-19:]):
            result = calculate_technical_context(history(), benchmark, as_of=AS_OF)
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["reason"], "insufficient_benchmark_ma_history")
            self.assertIsNone(result["benchmark_ma_30"])

    def test_incomplete_week_is_ignored_and_not_forward_filled(self):
        stock = history()
        benchmark = history()
        incomplete = {"date": "2024-10-07", "close": "9999", "adj_close": "9999"}
        result = calculate_technical_context(stock + [incomplete], benchmark + [incomplete], as_of=AS_OF)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["latest_week"], LATEST.isoformat())
        self.assertEqual(result["benchmark_close"], 130)

    def test_invalid_input_is_explicitly_unavailable_not_exception(self):
        stock = history()
        stock[-1]["adj_close"] = "nan"
        result = calculate_technical_context(stock, history(), as_of=AS_OF)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "invalid_stock")
        self.assertIsNone(result["improving"])
        json.dumps(result, allow_nan=False)

    def test_duplicate_week_labels_are_ambiguous_not_silently_overwritten(self):
        benchmark = history()
        benchmark.append({"date": "2024-10-04", "close": "130", "adj_close": "130"})
        result = calculate_technical_context(history(), benchmark, as_of=AS_OF)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "invalid_benchmark")


if __name__ == "__main__":
    unittest.main()
