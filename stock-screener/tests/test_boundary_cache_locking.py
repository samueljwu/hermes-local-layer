import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import refresh_fmp_profiles
import refresh_universe
from stock_screener.atomic_io import atomic_write_text, promote_staged_bundle
from stock_screener import locking


class BoundaryCacheLockingTests(unittest.TestCase):
    def test_project_lock_rejects_symlink_without_creating_external_target(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lock_path = root / "test.lock"
            outside = root / "outside-created.txt"
            lock_path.symlink_to(outside)
            old_path, old_token = locking.LOCK_PATH, locking.LOCK_TOKEN
            locking.LOCK_PATH = lock_path
            locking.LOCK_TOKEN = str(lock_path.resolve())
            try:
                with self.assertRaises(OSError):
                    with locking.stock_screener_lock():
                        pass
            finally:
                locking.LOCK_PATH, locking.LOCK_TOKEN = old_path, old_token
            self.assertFalse(outside.exists())

    def test_universe_sparse_download_preserves_both_raw_caches(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            nasdaq = root / "data/raw/nasdaq.txt"
            nyse = root / "data/raw/nyse.json"
            nasdaq.parent.mkdir(parents=True)
            nasdaq.write_text("old nasdaq\n", encoding="utf-8")
            nyse.write_text('[{"old": true}]\n', encoding="utf-8")
            config = {
                "sources": {
                    "nasdaq": {"url": "https://example.test/nasdaq", "raw_path": "data/raw/nasdaq.txt"},
                    "nyse": {"url": "https://example.test/nyse", "raw_path": "data/raw/nyse.json"},
                },
                "minimum_counts": {"nasdaq_raw": 1, "nyse_raw_xnys": 1, "combined_active": 2},
            }
            valid_nyse = json.dumps([
                {"normalizedTicker": "IBM", "instrumentName": "IBM", "url": "https://www.nyse.com/quote/XNYS:IBM"}
            ]).encode()

            def sparse_fetch(url, **_kwargs):
                return b"Symbol|Security Name|ETF|Test Issue\n" if "nasdaq" in url else valid_nyse

            old_root = refresh_universe.ROOT
            refresh_universe.ROOT = root
            try:
                with mock.patch.object(refresh_universe, "fetch_url", side_effect=sparse_fetch):
                    with self.assertRaisesRegex(RuntimeError, "sparse universe refresh"):
                        refresh_universe.download_sources(config)
            finally:
                refresh_universe.ROOT = old_root
            self.assertEqual(nasdaq.read_text(encoding="utf-8"), "old nasdaq\n")
            self.assertEqual(nyse.read_text(encoding="utf-8"), '[{"old": true}]\n')
            self.assertEqual(list(nasdaq.parent.glob(".*.download")), [])

    def test_fmp_empty_force_refresh_preserves_raw_cache(self):
        with tempfile.TemporaryDirectory() as td:
            raw_dir = Path(td)
            raw = raw_dir / "AAPL.json"
            raw.write_text('[{"symbol": "AAPL", "companyName": "Apple"}]\n', encoding="utf-8")
            with mock.patch.object(refresh_fmp_profiles, "fetch_profile", return_value=[]):
                rows, fetched, error = refresh_fmp_profiles.load_or_fetch_profile(
                    base_url="https://example.test",
                    symbol="AAPL",
                    api_key="secret",
                    raw_dir=raw_dir,
                    force_refresh=True,
                    delay_seconds=0,
                )
            self.assertEqual(rows, [])
            self.assertTrue(fetched)
            self.assertIn("preserved existing raw cache", error)
            self.assertIn("Apple", raw.read_text(encoding="utf-8"))

    def test_atomic_writers_use_unique_sibling_temps_and_leave_complete_file(self):
        with tempfile.TemporaryDirectory() as td:
            destination = Path(td) / "shared.json"
            for index in range(20):
                atomic_write_text(destination, json.dumps({"writer": index, "payload": "x" * 1000}))
            payload = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["payload"]), 1000)
            self.assertEqual(list(destination.parent.glob(".shared.json.*.tmp")), [])

    def test_project_lock_rejects_concurrent_process_and_allows_reentry(self):
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "test.lock"
            code = """
import sys, time
from pathlib import Path
from stock_screener import locking
locking.LOCK_PATH = Path(sys.argv[1])
locking.LOCK_TOKEN = str(locking.LOCK_PATH.resolve())
with locking.stock_screener_lock():
    print('ready', flush=True)
    time.sleep(10)
"""
            env = os.environ.copy()
            env.pop(locking.LOCK_ENV, None)
            env["PYTHONPATH"] = str(ROOT / "src")
            proc = subprocess.Popen(
                [sys.executable, "-c", code, str(lock_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            try:
                self.assertEqual(proc.stdout.readline().strip(), "ready")
                old_path, old_token = locking.LOCK_PATH, locking.LOCK_TOKEN
                locking.LOCK_PATH = lock_path
                locking.LOCK_TOKEN = str(lock_path.resolve())
                os.environ.pop(locking.LOCK_ENV, None)
                try:
                    with self.assertRaises(locking.LockBusyError):
                        with locking.stock_screener_lock():
                            pass
                    os.environ[locking.LOCK_ENV] = locking.LOCK_TOKEN
                    os.environ[locking.LOCK_FD_ENV] = "999999"
                    with self.assertRaises(locking.LockBusyError):
                        with locking.stock_screener_lock():
                            pass
                    forged = lock_path.open("a+")
                    try:
                        os.environ[locking.LOCK_FD_ENV] = str(forged.fileno())
                        self.assertIsNone(locking.inherited_lock_fd())
                        with self.assertRaises(locking.LockBusyError):
                            with locking.stock_screener_lock():
                                pass
                    finally:
                        forged.close()
                finally:
                    os.environ.pop(locking.LOCK_ENV, None)
                    os.environ.pop(locking.LOCK_FD_ENV, None)
                    locking.LOCK_PATH, locking.LOCK_TOKEN = old_path, old_token
            finally:
                proc.terminate()
                proc.wait(timeout=5)
                if proc.stdout: proc.stdout.close()
                if proc.stderr: proc.stderr.close()

    def test_bundle_interrupt_rolls_back_and_removes_disposable_backup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            destination = root / "live.txt"
            stage = root / ".live.stage"
            destination.write_text("old", encoding="utf-8")
            stage.write_text("new", encoding="utf-8")
            with mock.patch("stock_screener.atomic_io.os.replace", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    promote_staged_bundle([(stage, destination)])
            self.assertEqual(destination.read_text(encoding="utf-8"), "old")

    def test_bundle_preserves_backup_when_rollback_replace_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first, second = root / "first.txt", root / "second.txt"
            first_stage, second_stage = root / ".first.stage", root / ".second.stage"
            first.write_text("old-first", encoding="utf-8")
            second.write_text("old-second", encoding="utf-8")
            first_stage.write_text("new-first", encoding="utf-8")
            second_stage.write_text("new-second", encoding="utf-8")
            real_replace = os.replace
            calls = 0
            def fail_promotion_and_rollback(src, dst, **kwargs):
                nonlocal calls
                calls += 1
                if calls in {4, 5}:
                    raise OSError("injected replace failure")
                return real_replace(src, dst, **kwargs)
            with mock.patch("stock_screener.atomic_io.os.replace", side_effect=fail_promotion_and_rollback):
                with self.assertRaisesRegex(RuntimeError, "rollback was incomplete"):
                    promote_staged_bundle([(first_stage, first), (second_stage, second)])
            self.assertTrue(any(root.glob(".*.bak")), "recoverable backup must be preserved")

    def test_all_documented_mutating_entrypoints_use_canonical_lock(self):
        python_entrypoints = [
            "refresh_universe.py",
            "refresh_fmp_profiles.py",
            "refresh_nasdaq_screener_metadata.py",
            "filter_universe.py",
            "refresh_price_history.py",
            "validate_price_history.py",
            "scan_patterns.py",
            "build_chart_page.py",
            "build_excluded_chart_page.py",
        ]
        for name in python_entrypoints:
            with self.subTest(name=name):
                text = (SCRIPTS / name).read_text(encoding="utf-8")
                self.assertIn("run_locked(main)", text)
        self.assertIn("stock_screener.locking", (SCRIPTS / "weekly_update.sh").read_text(encoding="utf-8"))

    def test_cron_wrapper_captures_before_descriptor_bound_log_publication(self):
        text = Path("/home/hermes/.hermes/scripts/stock_screener_weekly_update.sh").read_text(encoding="utf-8")
        self.assertLess(text.index("python3 -m stock_screener.locking"), text.index("atomic_write_bytes"))
        self.assertIn('>"$TMP_LOG" 2>&1', text)
        self.assertNotIn('>"$LOG_PATH" 2>&1', text)
        self.assertIn("%N", text)


if __name__ == "__main__":
    unittest.main()
