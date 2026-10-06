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
            "weekly_completed_estimated_hours_last_10_weeks.svg": "<svg/>\n",
            "weekly_completed_estimated_hours_last_10_weeks.png": b"PNG",
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
                "weekly_completed_estimated_hours_last_10_weeks.svg": "new hours svg\n",
                "weekly_completed_estimated_hours_last_10_weeks.png": b"new hours png",
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

    def test_publish_upgrades_existing_four_artifact_generation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "task-completion-report"
            out.mkdir()
            lock = root / "report.lock"
            for name in report.LEGACY_OUTPUT_NAMES:
                (out / name).write_bytes(f"old:{name}".encode())

            with mock.patch.object(report, "OUT", out), mock.patch.object(report, "LOCK", lock), mock.patch.object(
                report, "ensure_output_root", side_effect=lambda: None
            ):
                report.publish_report_files(self.payloads())

            payloads = self.payloads()
            for name in report.OUTPUT_NAMES:
                path = out / name
                expected = payloads[name]
                if isinstance(expected, str):
                    expected = expected.encode()
                self.assertTrue(path.is_symlink())
                self.assertEqual(os.readlink(path), f"{report.CURRENT_LINK_NAME}/{name}")
                self.assertEqual(path.read_bytes(), expected)

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
                "weekly_completed_estimated_hours_last_10_weeks.svg": "<svg/>\n",
                "weekly_completed_estimated_hours_last_10_weeks.png": b"PNG",
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

    def test_occurrence_log_survives_current_name_status_and_rank_changes(self):
        registry = [{"id": "T-1-1", "name": "Current recurring name", "done": False,
                     "project_id": "P-1", "notes": "Canonical recurring notes",
                     "due_date": None, "reminder": None, "recurrence": None, "priority": "medium"}]
        records = report.build_completion_records(
            "## 2026-01-03\n- **T-9-1** — Historical recurring title — completed occurrence\n",
            registry, date(2026, 1, 1), date(2026, 1, 4), projects=[{"id": "P-1", "name": "Recurring"}])
        self.assertEqual(records, [{"id": "T-9-1", "task": "Historical recurring title",
                                   "status": "completed occurrence", "date": date(2026, 1, 3),
                                   "tag": "Recurring", "notes": "Canonical recurring notes",
                                   "est_time": 1}])

    def test_completion_records_use_est_time_and_default_null_or_missing_to_one_hour(self):
        base = {"name": "Task", "done": True, "project_id": "P-1", "notes": "",
                "due_date": None, "reminder": None, "recurrence": None, "priority": "medium"}
        registry = [
            {**base, "id": "T-1-1", "est_time": 2},
            {**base, "id": "T-2-2", "est_time": None},
            {**base, "id": "T-3-3"},
            {**base, "id": "T-4-4", "est_time": 0.5},
        ]
        log_text = "## 2026-01-03\n" + "\n".join(
            f"- **T-{i}-{i}** — Task {i} — completed" for i in range(1, 5)
        )
        records = report.build_completion_records(
            log_text, registry, date(2026, 1, 1), date(2026, 1, 4),
            projects=[{"id": "P-1", "name": "Admin"}],
        )
        self.assertEqual([row["est_time"] for row in records], [2, 1, 1, 0.5])

    def test_completion_date_not_due_date_drives_report_day_and_week(self) -> None:
        registry = [{
            "id": "T-1-1",
            "name": "Renamed current task",
            "done": False,
            "due_date": "2026-01-01",
            "project_id": "P-2", "reminder": "2025-12-31", "recurrence": None, "priority": "medium",
            "notes": "Canonical notes",
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
            projects=[{"id": "P-2", "name": "Other"}],
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["task"], "Example task")
        self.assertEqual(records[0]["status"], "completed")
        self.assertEqual(records[0]["tag"], "Other")
        self.assertEqual(records[0]["notes"], "Canonical notes")
        self.assertNotIn("name", records[0])
        self.assertEqual(records[0]["date"], date(2026, 1, 3))
        self.assertEqual(report.completion_week_start(records[0]["date"]), date(2025, 12, 29))
        self.assertNotEqual(records[0]["date"], date.fromisoformat(registry[0]["due_date"]))

    def test_completed_task_remains_reportable_after_cancellation_archive(self) -> None:
        snapshot = {
            "id": "T-4-9", "name": "Archived task", "done": True,
            "project_id": "P-2", "notes": "Archived notes", "due_date": None,
            "reminder": None, "recurrence": None, "priority": "medium",
        }
        records = report.build_completion_records(
            "## 2026-01-03\n- **T-1-9** — Historical title — completed\n",
            [], date(2026, 1, 1), date(2026, 1, 4),
            projects=[{"id": "P-2", "name": "Admin"}],
            deletions={"9": {"task_id": "T-4-9", "snapshot": snapshot}},
        )
        self.assertEqual(records[0]["tag"], "Admin")
        self.assertEqual(records[0]["notes"], "Archived notes")

    def test_legacy_migration_archive_can_supply_historical_project(self) -> None:
        snapshot = {"id": "T-8-12", "name": "Old task", "status": "cancelled",
                    "tag": "Legacy project", "notes": "Old notes"}
        records = report.build_completion_records(
            "## 2026-01-03\n- **T-2-12** — Earlier occurrence — completed occurrence\n",
            [], date(2026, 1, 1), date(2026, 1, 4), projects=[],
            deletions={"12": {"task_id": "T-8-12", "snapshot": snapshot}},
        )
        self.assertEqual((records[0]["tag"], records[0]["notes"]),
                         ("Legacy project", "Old notes"))

    def test_archive_identity_collision_fails_closed(self) -> None:
        active = {"id": "T-1-9", "name": "Active", "done": False,
                  "project_id": "P-2", "notes": "", "due_date": None,
                  "reminder": None, "recurrence": None, "priority": "medium"}
        with self.assertRaisesRegex(RuntimeError, "registry and deletion ledger"):
            report.build_completion_records(
                "", [active], date(2026, 1, 1), date(2026, 1, 4),
                projects=[{"id": "P-2", "name": "Admin"}],
                deletions={"9": {"task_id": "T-1-9", "snapshot": dict(active)}},
            )

    def test_invalid_archived_estimate_fails_closed(self) -> None:
        snapshot = {"id": "T-1-9", "name": "Archived", "done": True,
                    "project_id": "P-2", "notes": "", "due_date": None,
                    "reminder": None, "recurrence": None, "priority": "medium",
                    "est_time": "two"}
        with self.assertRaisesRegex(RuntimeError, "Invalid archived task estimate"):
            report.build_completion_records(
                "## 2026-01-03\n- **T-1-9** — Archived — completed\n",
                [], date(2026, 1, 1), date(2026, 1, 4),
                projects=[{"id": "P-2", "name": "Admin"}],
                deletions={"9": {"task_id": "T-1-9", "snapshot": snapshot}},
            )


if __name__ == "__main__":
    unittest.main()