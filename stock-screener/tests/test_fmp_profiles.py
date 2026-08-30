import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "refresh_fmp_profiles.py"
spec = importlib.util.spec_from_file_location("refresh_fmp_profiles", SCRIPT_PATH)
refresh_fmp_profiles = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = refresh_fmp_profiles
spec.loader.exec_module(refresh_fmp_profiles)


class FmpProfileRefreshTests(unittest.TestCase):
    def test_processed_bundle_rolls_back_profiles_summaries_and_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = [root / "profiles.csv", root / "sector.csv", root / "industry.csv", root / "metadata.json"]
            for path in paths:
                path.write_text(f"old-{path.name}", encoding="utf-8")
            row = refresh_fmp_profiles.ProfileRow(
                "AAPL", "Apple", "NASDAQ", "XNAS", "AAPL", "Apple", "Tech", "Hardware",
                "US", "NASDAQ", "1", "1", "1", "1", "1", "false", "true", "",
            )
            real_replace = os.replace
            calls = 0

            def fail_mid_promotion(src, dst, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 4:
                    raise OSError("injected promotion failure")
                return real_replace(src, dst, **kwargs)

            with mock.patch("stock_screener.atomic_io.os.replace", side_effect=fail_mid_promotion):
                with self.assertRaisesRegex(OSError, "injected promotion failure"):
                    refresh_fmp_profiles.publish_processed_outputs(
                        paths[0], paths[1], paths[2], paths[3], [row], {"generation": "new"},
                    )
            for path in paths:
                self.assertEqual(path.read_text(encoding="utf-8"), f"old-{path.name}")

    def test_error_refresh_preserves_complete_existing_processed_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outputs = [root / "company_profiles.csv", root / "sector_summary.csv", root / "industry_summary.csv"]
            for path in outputs:
                path.write_text("existing\n", encoding="utf-8")
            rows = [
                refresh_fmp_profiles.ProfileRow(
                    symbol="AAPL",
                    name="Apple Inc.",
                    exchange="NASDAQ",
                    mic="XNAS",
                    fmp_symbol="AAPL",
                    company_name="",
                    sector="",
                    industry="",
                    country="",
                    exchange_short_name="",
                    market_cap="",
                    price="",
                    beta="",
                    volume="",
                    avg_volume="",
                    is_etf="",
                    is_actively_trading="",
                    error="HTTP 429 rate/plan limit reached",
                )
            ]
            promote, reason = refresh_fmp_profiles.should_promote_processed_outputs(
                rows=rows,
                error_count=1,
                max_error_rate=0.0,
                existing_output_paths=outputs,
            )
            self.assertFalse(promote)
            self.assertIn("preserved existing processed outputs", reason)

    def test_error_refresh_can_promote_when_no_previous_outputs_exist(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outputs = [root / "company_profiles.csv", root / "sector_summary.csv", root / "industry_summary.csv"]
            row = refresh_fmp_profiles.ProfileRow(
                symbol="AAPL",
                name="Apple Inc.",
                exchange="NASDAQ",
                mic="XNAS",
                fmp_symbol="AAPL",
                company_name="",
                sector="",
                industry="",
                country="",
                exchange_short_name="",
                market_cap="",
                price="",
                beta="",
                volume="",
                avg_volume="",
                is_etf="",
                is_actively_trading="",
                error="HTTP 429 rate/plan limit reached",
            )
            promote, reason = refresh_fmp_profiles.should_promote_processed_outputs(
                rows=[row],
                error_count=1,
                max_error_rate=0.0,
                existing_output_paths=outputs,
            )
            self.assertTrue(promote)
            self.assertIn("no complete existing processed outputs", reason)


if __name__ == "__main__":
    unittest.main()
