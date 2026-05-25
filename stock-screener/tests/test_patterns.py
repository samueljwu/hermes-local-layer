import unittest

from stock_screener.patterns import (
    detect_double_bottom_breakout,
    detect_long_biased_override,
    detect_resistance_breakout,
    detect_support_bounce_uptrend,
    is_downtrend,
)


def row(i, close, high=None, low=None, volume=1000):
    high = high if high is not None else close * 1.02
    low = low if low is not None else close * 0.98
    return {
        "date": f"2024-W{i:02d}",
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "adj_close": close,
        "volume": volume,
    }


class PatternTests(unittest.TestCase):
    def test_detect_resistance_breakout_when_latest_close_clears_repeated_highs(self):
        rows = [row(i, 90 + i * 0.1, high=100 if i in (5, 15, 25) else 95, volume=1000) for i in range(1, 35)]
        rows.append(row(35, 102, high=103, low=100, volume=2000))
        match = detect_resistance_breakout("TEST", rows, {
            "lookback_weeks": 52,
            "resistance_tolerance_pct": 3.0,
            "breakout_buffer_pct": 1.0,
            "max_breakout_extension_pct": 15.0,
            "min_touches": 2,
            "min_touch_separation_weeks": 3,
            "volume_average_weeks": 10,
            "min_volume_ratio": 1.15,
        })
        self.assertIsNotNone(match)
        self.assertEqual(match.pattern, "resistance_breakout")
        self.assertGreaterEqual(match.evidence["touch_count"], 2)

    def test_detect_double_bottom_breakout_when_two_similar_lows_break_neckline(self):
        closes = [100, 95, 90, 84, 80, 84, 90, 96, 100, 94, 88, 82, 81, 85, 92, 101, 103]
        rows = [row(i, c, high=c * 1.01, low=c * 0.99, volume=1000) for i, c in enumerate(closes, start=1)]
        rows[-1]["volume"] = 1800
        match = detect_double_bottom_breakout("TEST", rows, {
            "lookback_weeks": 52,
            "min_bottom_separation_weeks": 4,
            "max_bottom_separation_weeks": 32,
            "max_low_difference_pct": 7.0,
            "min_depth_pct": 10.0,
            "breakout_buffer_pct": 1.0,
            "max_breakout_extension_pct": 15.0,
            "volume_average_weeks": 10,
            "min_volume_ratio": 1.10,
        })
        self.assertIsNotNone(match)
        self.assertEqual(match.pattern, "double_bottom_breakout")
        self.assertGreater(match.evidence["depth_pct"], 10)

    def test_detect_support_bounce_in_uptrend_near_sma_support(self):
        rows = [row(i, 50 + i, volume=1000) for i in range(1, 45)]
        rows[-4] = row(41, 88, low=84, high=90, volume=1000)
        rows[-3] = row(42, 86, low=83, high=88, volume=1000)
        rows[-2] = row(43, 87, low=84, high=89, volume=1000)
        rows[-1] = row(44, 91, low=88, high=93, volume=1200)
        match = detect_support_bounce_uptrend("TEST", rows, {
            "support_sma_weeks": 20,
            "trend_sma_weeks": 40,
            "support_tolerance_pct": 6.0,
            "bounce_lookback_weeks": 4,
            "min_bounce_from_low_pct": 3.0,
            "min_close_above_support_pct": 0.0,
        })
        self.assertIsNotNone(match)
        self.assertEqual(match.pattern, "support_bounce_uptrend")

    def test_is_downtrend_when_price_and_short_average_are_below_long_average(self):
        rows = [row(i, 100 - i, volume=1000) for i in range(1, 50)]
        self.assertTrue(is_downtrend(rows, {
            "long_sma_weeks": 40,
            "short_sma_weeks": 10,
            "short_history_sma_weeks": 20,
            "short_history_return_weeks": 13,
            "return_threshold_pct": -12.0,
        }))
    def test_long_biased_override_includes_support_touch(self):
        rows = [row(i, 100 + i * 0.5, volume=1000) for i in range(1, 60)]
        rows[-3] = row(57, 126, low=121, high=128)
        rows[-2] = row(58, 124, low=120, high=126)
        rows[-1] = row(59, 127, low=124, high=129)
        match = detect_long_biased_override("TEST", rows, {
            "enabled": True,
            "support_sma_weeks": [20, 50],
            "support_touch_tolerance_pct": 5.0,
            "support_lookback_weeks": 4,
            "resistance_lookback_weeks": [13],
            "resistance_touch_tolerance_pct": 1.0,
            "max_resistance_extension_pct": 3.0,
            "drawdown_high_lookback_weeks": 52,
            "include_drawdown_from_high_pct": -30.0,
            "quality": {"support_touch_assumed_bounce": 97.0},
        })
        self.assertIsNotNone(match)
        self.assertEqual(match.pattern, "long_biased_support_touch_assumed_bounce")
        self.assertTrue(match.evidence["manual_long_biased_override"])

    def test_long_biased_override_includes_deep_drawdown(self):
        rows = [row(i, 200 - i * 2, high=205 - i * 2, low=195 - i * 2, volume=1000) for i in range(1, 70)]
        match = detect_long_biased_override("TEST", rows, {
            "enabled": True,
            "support_sma_weeks": [],
            "support_touch_tolerance_pct": 0.1,
            "support_lookback_weeks": 4,
            "resistance_lookback_weeks": [13],
            "resistance_touch_tolerance_pct": 0.1,
            "max_resistance_extension_pct": 1.0,
            "drawdown_high_lookback_weeks": 52,
            "include_drawdown_from_high_pct": -30.0,
            "quality": {"downtrend_drawdown_accumulation": 90.0},
        })
        self.assertIsNotNone(match)
        self.assertEqual(match.pattern, "long_biased_downtrend_drawdown_accumulation")

    def test_weinstein_stage2_components_are_integrated_into_resistance_quality(self):
        rows = [row(i, 70 + i * 0.8, high=71 + i * 0.8, low=69 + i * 0.8, volume=1000) for i in range(1, 45)]
        for i in (20, 30, 38):
            rows[i - 1]["high"] = 103
        rows[-1] = row(45, 108, high=110, low=106, volume=2200)
        base_config = {
            "lookback_weeks": 52,
            "resistance_tolerance_pct": 3.0,
            "breakout_buffer_pct": 1.0,
            "max_breakout_extension_pct": 15.0,
            "min_touches": 2,
            "min_touch_separation_weeks": 3,
            "volume_average_weeks": 10,
            "min_volume_ratio": 1.15,
        }
        without = detect_resistance_breakout("TEST", rows, base_config)
        with_weinstein = detect_resistance_breakout("TEST", rows, {
            **base_config,
            "weinstein_stage2": {
                "enabled": True,
                "ma_weeks": 30,
                "ma_slope_lookback_weeks": 10,
                "overhead_lookback_weeks": 52,
                "volume_average_weeks": 4,
                "min_volume_ratio": 1.5,
                "max_quality_contribution": 8.0,
            },
        })
        self.assertIsNotNone(without)
        self.assertIsNotNone(with_weinstein)
        self.assertGreater(with_weinstein.quality, without.quality)
        self.assertLessEqual(with_weinstein.quality - without.quality, 8.0)
        self.assertEqual(with_weinstein.evidence["weinstein_stage2"]["alignment"], "supportive")
        self.assertEqual(with_weinstein.evidence["weinstein_stage2"]["mode"], "integrated_quality_component_not_candidate_generator")

    def test_weinstein_stage2_can_conservatively_widen_resistance_gate(self):
        rows = [row(i, 70 + i * 0.75, high=71 + i * 0.75, low=69 + i * 0.75, volume=1000) for i in range(1, 45)]
        for i in (20, 30, 38):
            rows[i - 1]["high"] = 103
        rows[-1] = row(45, 102.5, high=103.2, low=101, volume=2500)
        base_config = {
            "lookback_weeks": 52,
            "resistance_tolerance_pct": 3.0,
            "breakout_buffer_pct": 1.0,
            "max_breakout_extension_pct": 15.0,
            "min_close_vs_prior_high_pct": 0.5,
            "min_range_position_pct": 70.0,
            "major_overhead_lookback_weeks": 52,
            "max_unbroken_overhead_pct": 15.0,
            "max_overhead_pct": 15.0,
            "min_touches": 2,
            "min_touch_separation_weeks": 3,
            "volume_average_weeks": 10,
            "min_volume_ratio": 1.15,
        }
        without = detect_resistance_breakout("TEST", rows, base_config)
        with_weinstein = detect_resistance_breakout("TEST", rows, {
            **base_config,
            "weinstein_stage2": {
                "enabled": True,
                "ma_weeks": 30,
                "ma_slope_lookback_weeks": 10,
                "overhead_lookback_weeks": 52,
                "volume_average_weeks": 4,
                "min_volume_ratio": 1.5,
                "max_quality_contribution": 8.0,
                "gate_widening": {
                    "enabled": True,
                    "min_quality_contribution": 5.5,
                    "max_breakout_shortfall_pct": 1.0,
                    "max_range_position_shortfall_pct": 4.0,
                    "max_allowed_overhead_pct": 8.0,
                },
            },
        })
        self.assertIsNone(without)
        self.assertIsNotNone(with_weinstein)
        self.assertIn("weinstein_stage2_gate_widening", with_weinstein.evidence)
        self.assertTrue(with_weinstein.evidence["weinstein_stage2_gate_widening"]["used"])
        self.assertLess(with_weinstein.evidence["breakout_buffer_pct"], base_config["breakout_buffer_pct"])

    def test_manual_long_biased_override_has_no_weinstein_stage2_component(self):
        rows = [row(i, 200 - i * 2, high=205 - i * 2, low=195 - i * 2, volume=1000) for i in range(1, 70)]
        match = detect_long_biased_override("TEST", rows, {
            "enabled": True,
            "support_sma_weeks": [],
            "support_touch_tolerance_pct": 0.1,
            "support_lookback_weeks": 4,
            "resistance_lookback_weeks": [13],
            "resistance_touch_tolerance_pct": 0.1,
            "max_resistance_extension_pct": 1.0,
            "drawdown_high_lookback_weeks": 52,
            "include_drawdown_from_high_pct": -30.0,
            "quality": {"downtrend_drawdown_accumulation": 90.0},
        })
        self.assertIsNotNone(match)
        self.assertNotIn("weinstein_stage2", match.evidence)


if __name__ == "__main__":
    unittest.main()
