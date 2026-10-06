#!/usr/bin/env python3
import os
import re
import io
from contextlib import redirect_stdout
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import local_health
import restore_bootstrap

NOW = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
OLD = "2026-09-15T10:00:00+00:00"
NEW = "2026-09-16T10:00:00+00:00"


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / ".hermes"
        (self.home / "cron").mkdir(parents=True)

    def jobs(self, *jobs):
        (self.home / "cron/jobs.json").write_text(json.dumps({"jobs": list(jobs)}))

    def ledgers(self, executions=(), deliveries=()):
        with sqlite3.connect(self.home / "cron/executions.db") as db:
            db.execute("CREATE TABLE executions(id,job_id,status,claimed_at,started_at,finished_at)")
            db.executemany("INSERT INTO executions VALUES(?,?,?,?,?,?)", executions)
        with sqlite3.connect(self.home / "cron/deliveries.db") as db:
            db.execute("CREATE TABLE deliveries(execution_id,status,finished_at)")
            db.execute("CREATE TABLE delivery_tombstones(execution_id,terminal_status,finished_at)")
            db.executemany("INSERT INTO deliveries VALUES(?,?,?)", deliveries)

    def test_separate_action_delivery_and_historical_success(self):
        self.jobs({"id": "a", "last_status": "error", "last_run_at": NEW,
                   "last_error": "interrupted by shutdown PRIVATE", "next_run_at": OLD},
                  {"id": "b", "last_status": "delivery_failed", "last_run_at": NEW,
                   "last_delivery_error": "SECRET", "enabled": False})
        self.ledgers([("a0", "a", "completed", OLD, OLD, OLD),
                      ("a1", "a", "failed", NEW, NEW, NEW)],
                     [("a0", "delivered", OLD), ("a1", "delivered", NEW)])
        result = local_health.report(self.home, NOW)
        a, b = result["jobs"]
        self.assertEqual(a["action"], "failed")
        self.assertEqual(a["delivery"], "acknowledged")  # failure notification, not action success
        self.assertEqual(a["last_action_success_at"], OLD)
        self.assertEqual(a["last_delivery_acknowledged_at"], NEW)
        self.assertIsNone(a["last_delivery_success_at"])
        self.assertEqual(a["flags"], ["interrupted", "overdue"])
        self.assertEqual((b["action"], b["delivery"]), ("success", "failed"))
        self.assertEqual(b["last_action_success_at"], NEW)
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("SECRET", json.dumps(result))

    def test_no_receipt_no_delivery_success_and_no_false_stale_paused(self):
        self.jobs({"id": "a", "last_status": "ok", "last_run_at": NEW,
                   "next_run_at": OLD, "enabled": False})
        row = local_health.report(self.home, NOW)["jobs"][0]
        self.assertEqual(row["delivery"], "unknown")
        self.assertIsNone(row["last_delivery_success_at"])
        self.assertEqual(row["flags"], [])

    def test_stale_claim_unknown_receipt_and_unverified(self):
        self.jobs({"id": "a", "last_status": "ok", "last_run_at": OLD,
                   "last_delivery_unverified": ["private-target"], "fire_claim": {"at": NEW}})
        self.ledgers([("a1", "a", "running", NEW, NEW, None)], [])
        row = local_health.report(self.home, NOW)["jobs"][0]
        self.assertEqual(row["action"], "running")
        self.assertEqual(row["delivery"], "unverified")
        self.assertIn("stale_claim_or_run", row["flags"])

    def test_unverified_ack_does_not_supply_success_timestamp(self):
        self.jobs({"id": "a", "last_status": "ok", "last_run_at": NEW,
                   "last_delivery_unverified": ["private-target"]})
        self.ledgers([("a1", "a", "completed", NEW, NEW, NEW)], [("a1", "delivered", NEW)])
        row = local_health.report(self.home, NOW)["jobs"][0]
        self.assertEqual(row["delivery"], "unverified")
        self.assertIsNone(row["last_delivery_success_at"])
        self.assertEqual(row["last_delivery_acknowledged_at"], NEW)

    def test_historical_unverified_ack_never_becomes_verified_after_later_run(self):
        self.jobs({"id": "a", "last_status": "ok", "last_run_at": OLD,
                   "last_delivery_unverified": ["private-target"]})
        self.ledgers([("a0", "a", "completed", OLD, OLD, OLD)], [("a0", "delivered", OLD)])
        row = local_health.report(self.home, NOW)["jobs"][0]
        self.assertEqual(row["delivery"], "unverified")
        self.assertEqual(row["last_delivery_acknowledged_at"], OLD)
        self.assertIsNone(row["last_delivery_success_at"])
        # A later run clears latest-only verification metadata. Neither retaining
        # the full ACK nor compacting it to a tombstone supplies historical proof.
        with sqlite3.connect(self.home / "cron/executions.db") as db:
            db.execute("INSERT INTO executions VALUES(?,?,?,?,?,?)",
                       ("a1", "a", "completed", NEW, NEW, NEW))
        for tombstone in (False, True):
            if tombstone:
                with sqlite3.connect(self.home / "cron/deliveries.db") as db:
                    db.execute("INSERT INTO delivery_tombstones SELECT * FROM deliveries")
                    db.execute("DELETE FROM deliveries")
            for metadata, expected in (({}, "unknown"),
                                       ({"last_delivery_error": "private-error"}, "failed"),
                                       ({"last_delivery_unverified": ["private-target"]}, "unverified")):
                with self.subTest(tombstone=tombstone, expected=expected):
                    self.jobs({"id": "a", "last_status": "ok", "last_run_at": NEW, **metadata})
                    row = local_health.report(self.home, NOW)["jobs"][0]
                    self.assertEqual(row["delivery"], expected)
                    self.assertEqual(row["last_delivery_acknowledged_at"], OLD)
                    self.assertIsNone(row["last_delivery_success_at"])
        self.jobs({"id": "a", "last_status": "ok", "last_run_at": NEW,
                   "last_delivery_unverified": []})
        with sqlite3.connect(self.home / "cron/deliveries.db") as db:
            db.execute("INSERT INTO deliveries VALUES(?,?,?)", ("a1", "delivered", NEW))
        row = local_health.report(self.home, NOW)["jobs"][0]
        self.assertEqual(row["delivery"], "acknowledged")
        self.assertEqual(row["last_delivery_acknowledged_at"], NEW)
        self.assertIsNone(row["last_delivery_success_at"])

    def test_current_ack_does_not_override_delivery_error(self):
        self.jobs({"id": "a", "last_status": "ok", "last_run_at": NEW,
                   "last_delivery_error": "private-error"})
        self.ledgers([("a1", "a", "completed", NEW, NEW, NEW)], [("a1", "delivered", NEW)])
        row = local_health.report(self.home, NOW)["jobs"][0]
        self.assertEqual(row["delivery"], "failed")
        self.assertEqual(row["last_delivery_acknowledged_at"], NEW)
        self.assertIsNone(row["last_delivery_success_at"])

    def test_old_delivery_not_attached_to_new_run(self):
        self.jobs({"id": "a", "last_status": "ok", "last_run_at": NEW})
        self.ledgers([("a0", "a", "completed", OLD, OLD, OLD)], [("a0", "delivered", OLD)])
        row = local_health.report(self.home, NOW)["jobs"][0]
        self.assertEqual(row["delivery"], "unknown")
        self.assertEqual(row["last_delivery_acknowledged_at"], OLD)
        self.assertIsNone(row["last_delivery_success_at"])

    def test_read_only_cli_no_transcript_read_or_symlink_traversal(self):
        self.jobs({"id": "a", "last_status": "ok", "last_run_at": NEW})
        self.ledgers()
        sessions = self.home / "sessions"
        sessions.mkdir()
        checkpoints = self.home / "checkpoints"
        checkpoints.mkdir()
        (checkpoints / "opaque.bin").write_bytes(b"checkpoint")
        (sessions / "private.json").write_text("TRANSCRIPT_SECRET")
        (sessions / "outside").symlink_to(self.home.parent, target_is_directory=True)
        before = {p.relative_to(self.home): p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        cmd = [sys.executable, "-B", str(Path(local_health.__file__)), "--home", str(self.home), "--json"]
        run = subprocess.run(cmd, capture_output=True, text=True)
        result = json.loads(run.stdout)
        self.assertEqual(result["storage"]["sessions"]["files"], 1)
        self.assertEqual(result["storage"]["checkpoints"]["files"], 1)
        self.assertEqual(result["storage"]["checkpoints"]["bytes"], len(b"checkpoint"))
        self.assertEqual(result["storage"]["sessions"]["symlinks_skipped"], 1)
        self.assertNotIn("TRANSCRIPT_SECRET", run.stdout)
        after = {p.relative_to(self.home): p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        # Content-reading API refuses transcript access; inventory must use stat only.
        with patch.object(Path, "read_text", side_effect=OSError):
            self.assertEqual(local_health.inventory(sessions)["files"], 1)

    def test_invalid_timestamp_corrupt_ledger_and_active_wal_are_unknown(self):
        self.jobs({"id": "a", "last_status": "ok", "last_run_at": "not-a-date"})
        (self.home / "cron/executions.db").write_bytes(b"bad sqlite")
        result = local_health.report(self.home, NOW)
        self.assertIsNone(result["jobs"][0]["last_action_success_at"])
        self.assertTrue(result["warnings"])
        (self.home / "cron/executions.db-wal").write_bytes(b"active")
        self.assertTrue(any("active WAL" in w for w in local_health.report(self.home, NOW)["warnings"]))

    def test_delivery_tombstones_retained(self):
        self.jobs({"id": "a", "last_status": "ok", "last_run_at": OLD})
        self.ledgers([("a0", "a", "completed", OLD, OLD, OLD)])
        with sqlite3.connect(self.home / "cron/deliveries.db") as db:
            db.execute("INSERT INTO delivery_tombstones VALUES(?,?,?)", ("a0", "delivered", OLD))
        row = local_health.report(self.home, NOW)["jobs"][0]
        self.assertEqual(row["delivery"], "acknowledged")
        self.assertEqual(row["last_delivery_acknowledged_at"], OLD)
        self.assertIsNone(row["last_delivery_success_at"])


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "staged-checkout"
        (self.source / ".git").mkdir(parents=True)
        (self.source / ".git/config").write_text("[core]\n repositoryformatversion = 0\n")
        (self.source / "RESTORE.md").write_text("fixture runbook")
        (self.source / ".hermes").mkdir()
        (self.source / ".hermes/config.example.yaml").write_text("fixture: true\n")
        self.target = self.root / "home/hermes"

    def test_fresh_and_existing_home_then_mock_install(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                target = self.target / str(existing)
                if existing:
                    (target / ".hermes").mkdir(parents=True, mode=0o700)
                    (target / ".hermes/config.yaml").write_text("live: preserve\n")
                    (target / ".profile").write_text("preserve")
                # The real file-copy bootstrap executes. No git commits, network,
                # installers, or live HOME writes; this is a staged checkout fixture.
                with patch("subprocess.run", side_effect=AssertionError("no external actions")):
                    self.assertEqual(restore_bootstrap.restore(self.source, target), 3)
                self.assertEqual((target / "RESTORE.md").read_text(), "fixture runbook")
                self.assertEqual((target / ".hermes").stat().st_mode & 0o777, 0o700)
                # Installer stub runs only after restoration; it creates runtime only.
                (target / ".hermes/hermes-agent").mkdir()
                if existing:
                    self.assertEqual((target / ".hermes/config.yaml").read_text(), "live: preserve\n")
                    self.assertEqual((target / ".profile").read_text(), "preserve")
                with self.assertRaises(ValueError):
                    restore_bootstrap.restore(self.source, target)

    def test_documented_private_file_creation_preserves_existing(self):
        doc = (Path(__file__).resolve().parents[2] / "RESTORE.md").read_text()
        restore_bootstrap.restore(self.source, self.target)
        for phase, filename in ((4, ".env"), (5, "config.yaml")):
            section = doc.split(f"## Phase {phase}:", 1)[1].split("\n## Phase", 1)[0]
            match = re.search(r"python3 - <<'PY'\n(.*?)\nPY", section, re.S)
            assert match is not None, "documented private-file creation snippet missing"
            snippet = match.group(1)
            snippet = snippet.replace("/home/hermes/.hermes", str(self.target / ".hermes"))
            previous_umask = os.umask(0)
            try:
                with redirect_stdout(io.StringIO()):
                    exec(snippet, {})
                target = self.target / ".hermes" / filename
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                target.write_text("preserve existing")
                with redirect_stdout(io.StringIO()):
                    exec(snippet, {})
                self.assertEqual(target.read_text(), "preserve existing")
            finally:
                os.umask(previous_umask)

    def test_bootstrap_cli_fixture_no_install(self):
        run = subprocess.run([sys.executable, "-B", restore_bootstrap.__file__,
                              "--source", str(self.source), "--target", str(self.target)],
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue((self.target / "RESTORE.md").is_file())
        self.assertFalse((self.target / ".hermes/hermes-agent").exists())
        again = subprocess.run([sys.executable, "-B", restore_bootstrap.__file__,
                                "--source", str(self.source), "--target", str(self.target)],
                               capture_output=True, text=True)
        self.assertEqual(again.returncode, 1)

    def test_conflict_preflight_no_partial_copy(self):
        self.target.mkdir(parents=True)
        (self.target / "RESTORE.md").write_text("do not overwrite")
        with self.assertRaises(ValueError):
            restore_bootstrap.restore(self.source, self.target)
        self.assertEqual(list(p.name for p in self.target.iterdir()), ["RESTORE.md"])
        self.assertEqual((self.target / "RESTORE.md").read_text(), "do not overwrite")

    def test_symlink_ancestor_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            restore_bootstrap.restore(self.source, alias / "new")
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
