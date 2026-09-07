#!/usr/bin/env python3
"""Regression tests for weekly task-completion date bucketing."""
from datetime import date
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import weekly_task_completion_report as report


class WeeklyTaskCompletionReportTests(unittest.TestCase):
    @staticmethod
    def payloads() -> dict[str, str | bytes]:
        return {
            "latest_report.json": "{}\n",
            "latest_report_tasks.csv": "task_id\n",
            "weekly_completed_tasks_last_10_weeks.svg": "<svg/>\n",
            "weekly_completed_tasks_last_10_weeks.png": b"PNG",
        }

    def test_failed_generation_switch_preserves_complete_previous_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "task-completion-report"
            out.mkdir()
            lock = root / "report.lock"
            for name in report.OUTPUT_NAMES:
                (out / name).write_bytes(f"old:{name}".encode())
            payloads = {
                "latest_report.json": "new json\n",
                "latest_report_tasks.csv": "new csv\n",
                "weekly_completed_tasks_last_10_weeks.svg": "new svg\n",
                "weekly_completed_tasks_last_10_weeks.png": b"new png",
            }

            with mock.patch.object(report, "OUT", out), mock.patch.object(report, "LOCK", lock), mock.patch.object(
                report, "ensure_output_root", side_effect=lambda: None
            ), mock.patch.object(report, "_switch_generation", side_effect=OSError("injected switch failure")):
                with self.assertRaisesRegex(OSError, "injected switch failure"):
                    report.publish_report_files(payloads)

            for name in report.OUTPUT_NAMES:
                self.assertTrue((out / name).is_symlink())
                self.assertEqual((out / name).read_bytes(), f"old:{name}".encode())

    def test_publish_repairs_partial_fixed_link_layout_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "task-completion-report"
            out.mkdir()
            lock = root / "report.lock"
            for name in report.OUTPUT_NAMES:
                (out / name).write_bytes(f"old:{name}".encode())

            with mock.patch.object(report, "OUT", out), mock.patch.object(report, "LOCK", lock), mock.patch.object(
                report, "ensure_output_root", side_effect=lambda: None
            ):
                report.publish_report_files(self.payloads())
                broken = out / report.OUTPUT_NAMES[0]
                broken.unlink()
                broken.write_text("stale regular file\n", encoding="utf-8")
                report.publish_report_files(self.payloads())

            for name in report.OUTPUT_NAMES:
                path = out / name
                self.assertTrue(path.is_symlink())
                self.assertEqual(os.readlink(path), f"{report.CURRENT_LINK_NAME}/{name}")

    def test_publish_stages_complete_bundle_and_rejects_symlinked_lock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "task-completion-report"
            out.mkdir()
            lock = root / "report.lock"
            target = root / "protected.txt"
            target.write_text("preserve me\n", encoding="utf-8")
            lock.symlink_to(target)
            payloads = {
                "latest_report.json": "{}\n",
                "latest_report_tasks.csv": "task_id\n",
                "weekly_completed_tasks_last_10_weeks.svg": "<svg/>\n",
                "weekly_completed_tasks_last_10_weeks.png": b"PNG",
            }

            with mock.patch.object(report, "OUT", out), mock.patch.object(report, "LOCK", lock), mock.patch.object(
                report, "ensure_output_root", side_effect=lambda: None
            ):
                with self.assertRaises(OSError):
                    report.publish_report_files(payloads)

            self.assertEqual(target.read_text(encoding="utf-8"), "preserve me\n")
            self.assertFalse(any((out / name).exists() for name in report.OUTPUT_NAMES))

    def test_publish_rejects_symlinked_generations_parent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "task-completion-report"
            out.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (out / report.GENERATIONS_DIRNAME).symlink_to(outside, target_is_directory=True)
            with mock.patch.object(report, "OUT", out), mock.patch.object(
                report, "LOCK", root / "locks" / "report.lock"
            ), mock.patch.object(report, "ensure_output_root", side_effect=lambda: None):
                with self.assertRaises(OSError):
                    report.publish_report_files(self.payloads())
            self.assertEqual(list(outside.iterdir()), [])

    def test_report_lock_rejects_symlinked_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outside = root / "outside"
            outside.mkdir()
            linked = root / "linked"
            linked.symlink_to(outside, target_is_directory=True)
            with mock.patch.object(report, "LOCK", linked / "locks" / "report.lock"):
                with self.assertRaises(OSError):
                    with report.report_lock():
                        pass
            self.assertFalse((outside / "locks" / "report.lock").exists())

    def test_completion_date_not_due_date_drives_report_day_and_week(self) -> None:
        registry = [{
            "id": "T-1-1",
            "task": "Example task",
            "due_date": "2026-01-01",
            "tag": "Other",
            "notes": "",
        }]
        log_text = """# Task Log

## 2026-01-03

- **T-1-1** — Example task — completed
"""

        records = report.build_completion_records(
            log_text,
            registry,
            date(2025, 12, 29),
            date(2026, 1, 4),
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["date"], date(2026, 1, 3))
        self.assertEqual(report.completion_week_start(records[0]["date"]), date(2025, 12, 29))
        self.assertNotEqual(records[0]["date"], date.fromisoformat(registry[0]["due_date"]))


if __name__ == "__main__":
    unittest.main()