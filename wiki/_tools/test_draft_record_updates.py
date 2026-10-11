"""Draft-only update boundaries; synthetic fixtures never assert source review."""
import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import ingestion_gate as gate
import draft_record_update
import test_ingestion_gate as fixtures


class DraftUpdates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="draft-update-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.record_path = self.root / "_meta/ingestion/records/demo.json"
        self.record_path.parent.mkdir(parents=True)
        self.proposal = self.root / ".ingest-work/demo/record-update.json"
        self.proposal.parent.mkdir(parents=True)
        self.live = self.root / "src/concepts/unchanged.md"
        self.live.parent.mkdir(parents=True)
        self.live.write_bytes(b"Live source stays unchanged.\n")
        self.record = {"id": "demo", "state": "draft", "author": "synthetic-writer",
                       "files": {"src/concepts/unchanged.md": {"before": "a", "after": "b"}},
                       "claims": [], "sources": [], "visuals": [], "annotations": {}}
        gate.write_json(self.record_path, self.record)

    def propose(self, updates):
        before = self.record_path.read_bytes()
        gate.write_json(self.proposal, {"expected_record_sha256": gate.sha(before), "updates": updates})
        return before

    def test_cli_changes_metadata_only_and_cannot_apply(self):
        updates = {"claims": [{"id": "synthetic-C1"}], "annotations": {"fixture_only": True}}
        self.propose(updates)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(gate.main(["--root", str(self.root), "update-draft", "demo"]), 0)
        result = json.loads(out.getvalue())
        after = gate.read_json(self.record_path)
        self.assertEqual(after, dict(self.record, **updates))
        self.assertEqual(result["record_sha256"], gate.sha(self.record_path.read_bytes()))
        self.assertTrue(result["admission_review_required"])
        self.assertEqual(self.live.read_bytes(), b"Live source stays unchanged.\n")
        with self.assertRaisesRegex(gate.GateError, "independent review required"):
            gate.apply(self.root, "demo")

    def test_stale_state_and_privilege_changes_leave_bytes_untouched(self):
        cases = [{"state": "reviewed"}, {"author": "different"}, {"files": {}},
                 {"review": {"verdict": "pass"}}, {"payload_sha256": "0" * 64},
                 {"claims": {}}, {"claims": ["not-an-object"]}, {"annotations": []}, {}]
        for updates in cases:
            before = self.propose(updates)
            with self.subTest(updates=updates), self.assertRaises(gate.GateError):
                gate.update_draft(self.root, "demo")
            self.assertEqual(self.record_path.read_bytes(), before)
        before = self.propose({"claims": []})
        proposal = gate.read_json(self.proposal)
        proposal["expected_record_sha256"] = "0" * 64
        gate.write_json(self.proposal, proposal)
        with self.assertRaisesRegex(gate.GateError, "record changed"):
            gate.update_draft(self.root, "demo")
        self.assertEqual(self.record_path.read_bytes(), before)

    def test_non_drafts_and_seal_fields_cannot_be_reopened(self):
        cases = [{"state": state} for state in ("sealed", "reviewed", "published")]
        cases += [{key: None} for key in ("payload_sha256", "review", "review_sha256", "applied_at")]
        for extra in cases:
            gate.write_json(self.record_path, dict(self.record, **extra))
            before = self.propose({"claims": []})
            with self.subTest(extra=extra), self.assertRaises(gate.GateError):
                gate.update_draft(self.root, "demo")
            self.assertEqual(self.record_path.read_bytes(), before)

    def test_symlinks_duplicate_keys_and_traversal_refused(self):
        before = self.propose({"claims": []})
        outside = self.root / "outside.json"
        outside.write_bytes(self.proposal.read_bytes())
        self.proposal.unlink()
        self.proposal.symlink_to(outside)
        with self.assertRaises((gate.GateError, OSError)):
            gate.update_draft(self.root, "demo")
        self.proposal.unlink()
        self.proposal.write_text('{"updates":{},"updates":{}}')
        with self.assertRaisesRegex(gate.GateError, "duplicate JSON key"):
            gate.update_draft(self.root, "demo")
        with self.assertRaisesRegex(gate.GateError, "invalid ingest id"):
            gate.update_draft(self.root, "../demo")
        self.assertEqual(self.record_path.read_bytes(), before)


    def test_parent_swap_does_not_write_through_new_symlink(self):
        self.propose({"annotations": {"fixture_only": True}})
        parent = self.record_path.parent
        moved = parent.with_name("records-original")
        external = self.root / "external"
        external.mkdir()
        external_record = external / "demo.json"
        external_record.write_bytes(self.record_path.read_bytes())
        outside_before = external_record.read_bytes()
        original_replace = draft_record_update.replace_at

        def swap(fd, name, data, expected, api):
            parent.rename(moved)
            parent.symlink_to(external, target_is_directory=True)
            return original_replace(fd, name, data, expected, api)

        with patch.object(draft_record_update, "replace_at", side_effect=swap):
            gate.update_draft(self.root, "demo")
        self.assertEqual(external_record.read_bytes(), outside_before)
        self.assertEqual(gate.read_json(moved / "demo.json")["annotations"], {"fixture_only": True})


    def test_concurrent_seal_waits_for_update_and_never_reopens_record(self):
        fixture = fixtures.GateTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        record = fixture.prepare()
        record["state"] = "draft"
        record.pop("payload_sha256")
        gate.save_record(fixture.root, record)
        path = gate.record_path(fixture.root, "demo")
        proposal = fixture.root / ".ingest-work/demo/record-update.json"
        gate.write_json(proposal, {"expected_record_sha256": gate.sha(path.read_bytes()),
                                  "updates": {"annotations": {"fixture_only": True}}})
        attempted = threading.Event()
        finished = threading.Event()
        failures = []
        original_lock = gate.locked
        original_replace = draft_record_update.replace_at

        @contextlib.contextmanager
        def observe_lock(root):
            attempted.set()
            with original_lock(root):
                yield

        def seal():
            try:
                gate.seal(fixture.root, "demo")
            except BaseException as exc:
                failures.append(exc)
            finally:
                finished.set()

        worker = threading.Thread(target=seal, daemon=True)

        def interleave(fd, name, data, expected, api):
            worker.start()
            self.assertTrue(attempted.wait(3), "seal did not attempt the shared lock")
            self.assertFalse(finished.is_set(), "seal completed while draft update held the lock")
            return original_replace(fd, name, data, expected, api)

        with patch.object(gate, "locked", observe_lock), patch.object(draft_record_update, "replace_at", interleave):
            gate.update_draft(fixture.root, "demo")
            self.assertTrue(finished.wait(5), "seal remained blocked after update released the lock")
        worker.join(1)
        self.assertEqual(failures, [])
        final = gate.read_json(path)
        self.assertEqual(final["state"], "sealed")
        self.assertTrue(gate.is_hash(final["payload_sha256"]))
        self.assertEqual(final["annotations"], {"fixture_only": True})
        gate.write_json(proposal, {"expected_record_sha256": gate.sha(path.read_bytes()), "updates": {"claims": []}})
        before = path.read_bytes()
        with self.assertRaisesRegex(gate.GateError, "only draft"):
            gate.update_draft(fixture.root, "demo")
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
