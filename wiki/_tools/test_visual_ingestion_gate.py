"""Scratch-only integration tests; all review receipts here are synthetic fixtures."""
from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

import pymupdf
from PIL import Image

import ingestion_gate as gate
import visual_evidence as visual


class VisualAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="visual-gate-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / "src/concepts/test.md"
        self.target.parent.mkdir(parents=True)
        self.target.write_text("Legacy fixture.\n")
        gate.initialize(self.root)
        self.ingest = "visual-test"
        self.draft = self.root / ".ingest-work" / self.ingest
        self.original = "src/raw/assets/visual-test/original.pdf"
        self.manifest_path = "src/raw/assets/visual-test/visual/manifest.json"
        self.raw = "src/raw/notes/visual-test.md"

    def prepare(self, reviewed=True, crop=True, text_only=False, image=False, repeat=False, middle=False):
        if image:
            self.original = "src/raw/assets/visual-test/original.png"
            with Image.new("RGB", (150, 100), "white") as im:
                from PIL import ImageDraw
                ImageDraw.Draw(im).text((5, 5), "Fixture: 10 -> 25", fill="black")
                stream = io.BytesIO()
                im.save(stream, format="PNG")
                original = stream.getvalue()
        else:
            with pymupdf.open() as pdf:
                page = pdf.new_page(width=144, height=216)
                page.insert_text((12, 20), "Fixture source", fontsize=8)
                page.insert_text((12, 40), "Dose    Response", fontsize=8)
                page.insert_text((12, 55), "10       25", fontsize=8)
                page.draw_rect(pymupdf.Rect(10, 30, 130, 65))
                if middle:
                    pdf.new_page(width=144, height=216).insert_text((12, 20), "Middle evidence", fontsize=8)
                final_page = pdf.new_page(width=144, height=216)
                if repeat:
                    final_page.insert_text((12, 20), "Fixture source", fontsize=8)
                original = pdf.tobytes()
        path = self.draft / self.original
        path.parent.mkdir(parents=True)
        path.write_bytes(original)
        result = visual.prepare(self.draft, self.original, "src/raw/assets/visual-test/visual")
        if crop:
            visual.crop(self.draft / self.manifest_path, 1, [0, 0, 140, 90] if image else [8, 25, 140, 90], "table-1", dpi=300)
        manifest = gate.read_json(self.draft / self.manifest_path)
        if reviewed:
            for entry in manifest["pages"]:
                entry["review"] = {
                    "status": "reviewed", "inspection": "full-page",
                    "classification": ["table"] if entry["page"] == 1 else (["prose"] if repeat or (middle and entry["page"] == 2) else ["blank"]),
                    "notes": "Synthetic test fixture only; not a production visual approval.",
                }
        gate.write_json(self.draft / self.manifest_path, manifest)
        body = (self.draft / result["extraction"]["path"]).read_bytes()
        raw = self.draft / self.raw
        raw.parent.mkdir(parents=True)
        raw.write_bytes(b"---\npublished: undated\nhash_policy: " + gate.HASH_POLICY.encode()
                        + b"\nsha256: " + gate.sha(body).encode() + b"\n---\n" + body)
        text = "The fixture table pairs dose 10 with response 25."
        if text_only:
            text = "The fixture document is labeled Fixture source."
        target = self.draft / "src/concepts/test.md"
        target.parent.mkdir(parents=True)
        target.write_text(text + "\n")
        gate.stage(self.root, self.ingest, "synthetic-fixture-writer")
        record = gate.load_record(self.root, self.ingest)
        ref = {"path": self.manifest_path, "sha256": gate.sha((self.draft / self.manifest_path).read_bytes())}
        record["visuals"] = [ref]
        record["sources"] = [{
            "path": self.raw, "body_sha256": gate.sha(body), "method": "Synthetic fixture native page-bounded extraction.",
            "date_basis": "Synthetic undated fixture.", "normalizations": [],
            "original": manifest["original"], "extraction": result["extraction"],
        }]
        if text_only:
            evidence = {"path": self.raw, "sha256": gate.sha(raw.read_bytes()),
                        "locator": "## PDF page 1", "excerpt": "Fixture source"}
        else:
            render = manifest["crops"][0]["render"] if crop else manifest["pages"][0]["render"]
            evidence = {**render, "visual": True, "manifest": ref, "page": 1,
                        "features": ["values"], "locator": "Page 1 fixture table, headers and values.",
                        "excerpt": "Dose 10, Response 25; no units supplied in synthetic fixture."}
            if crop:
                evidence["crop"] = "table-1"
        record["claims"] = [{"id": "C1", "text": text, "kind": "source-statement",
                             "qualifications": "Synthetic fixture only; no units supplied.",
                             "risks": [] if text_only else ["quantitative"], "risk_basis": "TEST fixture title or numeric table.",
                             "destinations": ["src/concepts/test.md"], "evidence": [evidence]}]
        gate.save_record(self.root, record)
        return record

    def report(self, record):
        checked = {key: "not-applicable" for key in gate.VISUAL_REVIEW_CHECKS}
        checked["transcription"] = checked["values"] = "checked"
        visual_claims = [c["id"] for c in record["claims"] if any(e.get("visual") is True for e in c["evidence"])]
        risky = {}
        for claim in record["claims"]:
            risks = gate.claim_risks(claim)
            if risks:
                required = {"transcription"} | set().union(*(gate.RISK_CHECKS[r] for r in risks))
                risky[claim["id"]] = {"checks": {key: "checked" if key in required else "not-applicable"
                    for key in gate.VISUAL_REVIEW_CHECKS}, "pixel_detail_checked": True,
                    "notes": "Synthetic TEST-only risk checks, including absent units/denominator.",
                    "limitations": "Synthetic fixture only; never source or transport approval."}
        return {
            "payload_sha256": record["payload_sha256"], "reviewer": "synthetic-fixture-reviewer",
            "verdict": "pass", "required_fixes": [], "checked_files": sorted(record["files"]),
            "checked_claims": ["C1"], "checked_evidence": sorted(gate.evidence_paths(record)),
            "all_material_claims_checked": True, "chronology_checked": True,
            "scope": "Synthetic TEST-only approval; never a source or production review.",
            "risk_classification_checked": True, "checked_risk_claims": sorted(risky), "risk_claim_checks": risky,
            "checked_excluded_pages": sorted(record.get("visual_exclusions", {})),
            "exclusion_checks": {key: {"inspection": "full-page", "scope_checked": True,
                "notes": "Synthetic TEST-only exclusion check."} for key in record.get("visual_exclusions", {})},
            "visuals_checked": True, "checked_visual_pages": sorted(gate.visual_page_keys(record)),
            "checked_visual_claims": visual_claims,
            "visual_claim_checks": {cid: {"checks": checked, "notes": "Synthetic fixture only.",
                                           "limitations": "No units in fixture."} for cid in visual_claims},
        }

    def approve(self):
        gate.seal(self.root, self.ingest)
        record = gate.load_record(self.root, self.ingest)
        gate.review(self.root, self.ingest, self.report(record))
        return gate.load_record(self.root, self.ingest)

    def change_manifest(self, record, change):
        path = self.draft / self.manifest_path
        manifest = gate.read_json(path)
        change(manifest)
        gate.write_json(path, manifest)
        new_ref = {"path": self.manifest_path, "sha256": gate.sha(path.read_bytes())}
        record["visuals"] = [new_ref]
        for claim in record["claims"]:
            for evidence in claim["evidence"]:
                if evidence.get("visual"):
                    evidence["manifest"] = new_ref
        gate.save_record(self.root, record)

    def test_pdf_visual_review_apply_and_historical_readmission(self):
        self.prepare()
        record = self.approve()
        self.assertIn(self.original, gate.evidence_paths(record))
        self.assertIn(self.manifest_path + "#page=2", gate.visual_page_keys(record))
        gate.apply(self.root, self.ingest)
        self.assertTrue(gate.check(self.root)["ok"])
        self.assertEqual((self.root / self.original).read_bytes(), (self.draft / self.original).read_bytes())
        # Published validation must not depend on the transient draft folder.
        import shutil
        shutil.rmtree(self.draft)
        self.assertTrue(gate.check(self.root)["ok"])

    def test_standalone_image_visual_review_apply(self):
        self.prepare(image=True)
        self.approve()
        gate.apply(self.root, self.ingest)
        self.assertTrue(gate.check(self.root)["ok"])

    def test_pending_visual_review_blocks_seal(self):
        self.prepare(reviewed=False)
        with self.assertRaisesRegex(gate.GateError, "pending visual review"):
            gate.seal(self.root, self.ingest)
        self.assertTrue(gate.check(self.root)["ok"])
        self.assertEqual(self.target.read_text(), "Legacy fixture.\n")

    def test_text_only_pdf_claims_still_require_visual_review(self):
        record = self.prepare(text_only=True)
        record["visuals"] = []
        gate.save_record(self.root, record)
        with self.assertRaisesRegex(gate.GateError, "original needs complete visual review"):
            gate.seal(self.root, self.ingest)

    def test_reviewed_text_only_pdf_accepts_full_document_coverage(self):
        self.prepare(text_only=True)
        self.approve()
        gate.apply(self.root, self.ingest)
        self.assertTrue(gate.check(self.root)["ok"])

    def test_excluded_page_cannot_support_text_claim(self):
        record = self.prepare(text_only=True)
        self.change_manifest(record, lambda m: m["pages"][0].update(review={
            "status": "excluded", "inspection": "full-page", "classification": ["table"],
            "notes": "Synthetic out-of-scope page.", "exclusion_reason": "Explicit test scope."}))
        with self.assertRaisesRegex(gate.GateError, "excluded"):
            self.approve()

    def test_wrong_page_text_locator_cannot_borrow_excerpt_elsewhere(self):
        record = self.prepare(text_only=True)
        record["claims"][0]["evidence"][0]["locator"] = "## PDF page 2"
        gate.save_record(self.root, record)
        with self.assertRaisesRegex(gate.GateError, "excerpt.*page"):
            gate.seal(self.root, self.ingest)

    def test_text_page_declarations_are_strict_and_bounded(self):
        record = self.prepare(text_only=True)
        for pages in ([0], [99], [True], [1, 1], [], [2], [1, 2]):
            candidate = copy.deepcopy(record)
            candidate["claims"][0]["evidence"][0]["pages"] = pages
            gate.save_record(self.root, candidate)
            with self.subTest(pages=pages), self.assertRaises(gate.GateError):
                gate.seal(self.root, self.ingest)

    def test_inherited_exclusion_applies_to_text_in_a_later_ingest(self):
        record = self.prepare()
        self.change_manifest(record, lambda m: m["pages"][1].update(review={
            "status": "excluded", "inspection": "full-page", "classification": ["blank"],
            "notes": "Synthetic final page intentionally omitted.", "exclusion_reason": "Explicit test scope."}))
        self.approve()
        gate.apply(self.root, self.ingest)
        draft = self.root / ".ingest-work/revision/src/concepts"
        draft.mkdir(parents=True)
        text = "The fixture contains no native text on its final page."
        (draft / "test.md").write_text(text + "\n")
        record = gate.stage(self.root, "revision", "synthetic-second-writer")
        record["claims"] = [{"id": "C1", "text": text, "kind": "source-statement",
                             "qualifications": "Synthetic test only; extraction state is not content absence.",
                             "risks": [], "risk_basis": "TEST-only qualitative extraction-state claim.",
                             "destinations": ["src/concepts/test.md"],
                             "evidence": [{"path": self.raw, "sha256": gate.sha((self.root / self.raw).read_bytes()),
                                           "locator": "## PDF page 2", "excerpt": "No native text extracted"}]}]
        gate.save_record(self.root, record)
        with self.assertRaisesRegex(gate.GateError, "excluded"):
            gate.seal(self.root, "revision")

    def test_seal_pins_text_page_context_without_changing_raw_bytes(self):
        self.prepare(text_only=True)
        before = (self.draft / self.raw).read_bytes()
        self.approve()
        record = gate.load_record(self.root, self.ingest)
        evidence = record["claims"][0]["evidence"][0]
        self.assertEqual(evidence["pages"], [1])
        self.assertEqual(evidence["manifest"], record["visuals"][0])
        self.assertGreaterEqual(len(evidence["page_assets"]), 2)
        self.assertEqual((self.draft / self.raw).read_bytes(), before)

    def test_ambiguous_repeated_excerpt_needs_explicit_page(self):
        record = self.prepare(text_only=True, repeat=True)
        record["claims"][0]["evidence"][0]["locator"] = "body"
        gate.save_record(self.root, record)
        with self.assertRaisesRegex(gate.GateError, "ambiguous excerpt page coverage"):
            gate.seal(self.root, self.ingest)
        record["claims"][0]["evidence"][0]["pages"] = [1]
        gate.save_record(self.root, record)
        self.approve()
        gate.apply(self.root, self.ingest)
        self.assertTrue(gate.check(self.root)["ok"])

    def test_cross_page_excerpt_checks_every_intersecting_page(self):
        record = self.prepare(text_only=True)
        extraction = self.draft / record["sources"][0]["extraction"]["path"]
        item = record["claims"][0]["evidence"][0]
        item["locator"] = "body"
        item["excerpt"] = extraction.read_text().rstrip("\n")
        self.change_manifest(record, lambda m: m["pages"][1].update(review={
            "status": "excluded", "inspection": "full-page", "classification": ["blank"],
            "notes": "Synthetic excluded final page.", "exclusion_reason": "Explicit test scope."}))
        with self.assertRaisesRegex(gate.GateError, "excluded"):
            gate.seal(self.root, self.ingest)

    def replace_body(self, record, body, extraction=None):
        source = record["sources"][0]
        path = extraction or source["extraction"]["path"]
        (self.draft / path).write_bytes(body)
        source["extraction"] = {"path": path, "sha256": gate.sha(body)}
        source["body_sha256"] = gate.sha(body)
        raw = self.draft / self.raw
        raw.write_bytes(b"---\npublished: undated\nhash_policy: " + gate.HASH_POLICY.encode()
                        + b"\nsha256: " + gate.sha(body).encode() + b"\n---\n" + body)
        record["claims"][0]["evidence"][0]["sha256"] = gate.sha(raw.read_bytes())
        if path not in record["files"]:
            record["files"][path] = {"before": None, "after": gate.sha(body)}
        gate.save_record(self.root, record)

    def test_native_page_boundary_loss_or_relocation_is_rejected(self):
        record = self.prepare(text_only=True)
        body = (self.draft / record["sources"][0]["extraction"]["path"]).read_bytes()
        first, second = body.split(b"## PDF page 2\n", 1)
        for bad in (body.replace(b"## PDF page 2\n", b""),
                    first + second + b"## PDF page 2\n\n"):
            self.replace_body(record, bad)
            with self.subTest(body=bad), self.assertRaisesRegex(gate.GateError, "boundaries|native extraction"):
                gate.seal(self.root, self.ingest)

    def test_native_partial_page_selection_needs_source_bound_map(self):
        record = self.prepare(text_only=True)
        body = (self.draft / record["sources"][0]["extraction"]["path"]).read_bytes()
        selected = body.split(b"## PDF page 2\n", 1)[0]
        path = "src/raw/assets/visual-test/selected-extraction.txt"
        self.replace_body(record, selected, extraction=path)
        with self.assertRaisesRegex(gate.GateError, "boundaries|omitted"):
            gate.seal(self.root, self.ingest)
        record["sources"][0]["text_page_map"] = [{"page": 1}]
        gate.save_record(self.root, record)
        self.approve()
        gate.apply(self.root, self.ingest)
        self.assertTrue(gate.check(self.root)["ok"])
        self.assertEqual((self.root / "src/raw/assets/visual-test/visual/extraction.txt").read_bytes(), body)
        self.assertEqual(gate.load_record(self.root, self.ingest)["claims"][0]["evidence"][0]["text_page_map"], [{"page": 1}])

    def test_native_fragment_map_is_checked_against_original(self):
        record = self.prepare(text_only=True)
        original = (self.draft / self.original).read_bytes()
        with pymupdf.open(stream=original, filetype="pdf") as pdf:
            native = str(pdf[0].get_text("text")).encode("utf-8")
        fragment = b"Fixture source"
        start = native.index(fragment)
        mapping = [{"page": 1, "start": start, "end": start + len(fragment)}]
        record["sources"][0]["text_page_map"] = mapping
        selected = b"## PDF page 1\n" + fragment + b"\n"
        self.replace_body(record, selected, extraction="src/raw/assets/visual-test/selected-extraction.txt")
        wrong = copy.deepcopy(record)
        wrong["sources"][0]["text_page_map"][0]["start"] += 1
        gate.save_record(self.root, wrong)
        with self.assertRaises(gate.GateError):
            gate.seal(self.root, self.ingest)
        gate.save_record(self.root, record)
        self.approve()
        gate.apply(self.root, self.ingest)
        self.assertTrue(gate.check(self.root)["ok"])

    def test_legacy_explicit_association_survives_later_omission(self):
        import shutil
        self.prepare(text_only=True)
        root = self.root / "legacy-case"
        original = root / self.original
        original.parent.mkdir(parents=True)
        original.write_bytes((self.draft / self.original).read_bytes())
        raw = root / self.raw
        raw.parent.mkdir(parents=True)
        raw.write_bytes((self.draft / self.raw).read_bytes())
        target = root / "src/concepts/test.md"
        target.parent.mkdir(parents=True)
        target.write_text("Synthetic legacy target.\n")
        gate.initialize(root)
        draft = root / ".ingest-work/legacy-association"
        visual_dir = draft / "src/raw/assets/visual-test/visual"
        shutil.copytree(self.draft / "src/raw/assets/visual-test/visual", visual_dir)
        manifest = gate.read_json(draft / self.manifest_path)
        manifest["pages"][1]["review"] = {"status": "excluded", "inspection": "full-page",
            "classification": ["blank"], "notes": "Synthetic exclusion.", "exclusion_reason": "Synthetic scope."}
        gate.write_json(draft / self.manifest_path, manifest)
        draft_target = draft / "src/concepts/test.md"
        draft_target.parent.mkdir(parents=True)
        draft_target.write_text("The fixture is labeled Fixture source.\n")
        record = gate.stage(root, "legacy-association", "synthetic-first-writer")
        ref = {"path": self.manifest_path, "sha256": gate.sha((draft / self.manifest_path).read_bytes())}
        record["visuals"] = [ref]
        record["claims"] = [{"id": "C1", "text": draft_target.read_text().rstrip("\n"),
            "risks": [], "risk_basis": "TEST-only qualitative title claim.",
            "kind": "source-statement", "qualifications": "Synthetic fixture only.",
            "destinations": ["src/concepts/test.md"], "evidence": [{"path": self.raw,
            "sha256": gate.sha(raw.read_bytes()), "manifest": ref,
            "locator": "## PDF page 1", "excerpt": "Fixture source"}]}]
        gate.save_record(root, record)
        gate.seal(root, "legacy-association")
        gate.review(root, "legacy-association", self.report(gate.load_record(root, "legacy-association")))
        gate.apply(root, "legacy-association")
        shutil.rmtree(draft)
        revision = root / ".ingest-work/revision/src/concepts/test.md"
        revision.parent.mkdir(parents=True)
        revision.write_text("Synthetic later claim.\n")
        record = gate.stage(root, "revision", "synthetic-second-writer")
        record["claims"] = [{"id": "C1", "text": revision.read_text().rstrip("\n"),
            "risks": [], "risk_basis": "TEST-only qualitative claim.",
            "kind": "source-statement", "qualifications": "Synthetic fixture only.",
            "destinations": ["src/concepts/test.md"], "evidence": [{"path": self.raw,
            "sha256": gate.sha(raw.read_bytes()), "locator": "## PDF page 2",
            "excerpt": "No native text extracted"}]}]
        gate.save_record(root, record)
        with self.assertRaisesRegex(gate.GateError, "excluded"):
            gate.seal(root, "revision")
        item = record["claims"][0]["evidence"][0]
        item.update(locator="## PDF page 1", excerpt="Fixture source")
        gate.save_record(root, record)
        gate.seal(root, "revision")
        gate.review(root, "revision", self.report(gate.load_record(root, "revision")))
        gate.apply(root, "revision")
        self.assertTrue(gate.check(root)["ok"])

    def test_low_dpi_numeric_evidence_requires_detail(self):
        self.prepare(crop=False)
        with self.assertRaisesRegex(gate.GateError, "300 DPI"):
            gate.seal(self.root, self.ingest)

    def test_text_claim_supporting_interior_page_requires_full_pixel_reading(self):
        record = self.prepare(text_only=True, middle=True)
        claim = record["claims"][0]
        claim["text"] = "The fixture contains Middle evidence."
        (self.draft / "src/concepts/test.md").write_text(claim["text"] + "\n")
        claim["evidence"][0].update(locator="## PDF page 2", excerpt="Middle evidence")
        self.change_manifest(record, lambda m: m["pages"][1]["review"].update(inspection="contact-sheet"))
        with self.assertRaisesRegex(gate.GateError, "full-page pixel reading"):
            gate.seal(self.root, self.ingest)

    def test_invalid_native_mapping_is_not_normalized(self):
        record = self.prepare(text_only=True)
        for mapping in ([], [{"page": True}], [{"page": 1.0}], [{"page": 0}],
                        [{"page": 1, "start": -1, "end": 2}], [{"page": 1, "start": 0}],
                        [{"page": 1, "start": 0, "end": 999999}], [{"page": 1}, {"page": 1}]):
            candidate = copy.deepcopy(record)
            candidate["sources"][0]["text_page_map"] = mapping
            gate.save_record(self.root, candidate)
            with self.subTest(mapping=mapping), self.assertRaises(gate.GateError):
                gate.seal(self.root, self.ingest)

    def test_contact_sheet_cannot_approve_high_risk_page(self):
        record = self.prepare()
        self.change_manifest(record, lambda m: m["pages"][0]["review"].update(inspection="contact-sheet"))
        with self.assertRaisesRegex(gate.GateError, "full-page review"):
            gate.seal(self.root, self.ingest)

    def test_excluded_page_cannot_support_claim(self):
        record = self.prepare()
        self.change_manifest(record, lambda m: m["pages"][0].update(review={
            "status": "excluded", "inspection": "full-page", "classification": ["table"],
            "notes": "Synthetic exclusion.", "exclusion_reason": "Explicit user scope excludes this page."}))
        with self.assertRaisesRegex(gate.GateError, "excluded or pending page"):
            gate.seal(self.root, self.ingest)

    def test_bad_page_or_crop_or_render_locator_rejected(self):
        record = self.prepare()
        original = copy.deepcopy(record)
        for field, value in (("page", 99), ("crop", "unknown"), ("features", []), ("path", self.original)):
            candidate = copy.deepcopy(original)
            evidence = candidate["claims"][0]["evidence"][0]
            evidence[field] = value
            if field == "path":
                evidence["sha256"] = gate.sha((self.draft / self.original).read_bytes())
            gate.save_record(self.root, candidate)
            with self.subTest(field=field), self.assertRaises(gate.GateError):
                gate.seal(self.root, self.ingest)

    def test_missing_pages_or_original_page_count_lie_blocks(self):
        record = self.prepare()
        self.change_manifest(record, lambda m: (m.update(page_count=1), m["pages"].pop()))
        with self.assertRaisesRegex(gate.GateError, "source page/frame count"):
            gate.seal(self.root, self.ingest)

    def test_review_requires_actual_all_page_claim_asset_coverage_and_features(self):
        self.prepare()
        gate.seal(self.root, self.ingest)
        record = gate.load_record(self.root, self.ingest)
        original = self.report(record)
        for field, value in (("visuals_checked", False), ("checked_visual_pages", []),
                             ("checked_visual_claims", []), ("checked_evidence", [self.manifest_path]),
                             ("visual_claim_checks", {})):
            report = copy.deepcopy(original)
            report[field] = value
            with self.subTest(field=field), self.assertRaises(gate.GateError):
                gate.review(self.root, self.ingest, report)
        report = copy.deepcopy(original)
        report["visual_claim_checks"]["C1"]["checks"]["values"] = "not-applicable"
        with self.assertRaisesRegex(gate.GateError, "declared visual features"):
            gate.review(self.root, self.ingest, report)
        gate.review(self.root, self.ingest, original)

    def test_late_manifest_or_render_edit_invalidates_review(self):
        self.prepare()
        self.approve()
        path = self.draft / self.manifest_path
        manifest = gate.read_json(path)
        manifest["pages"][0]["review"]["notes"] += "Late edit."
        gate.write_json(path, manifest)
        with self.assertRaises(gate.GateError):
            gate.apply(self.root, self.ingest)
        self.assertEqual(self.target.read_text(), "Legacy fixture.\n")

    def test_published_visual_render_drift_blocks_build_admission(self):
        self.prepare()
        record = self.approve()
        gate.apply(self.root, self.ingest)
        crop = record["claims"][0]["evidence"][0]["path"]
        (self.root / crop).write_bytes(b"Corrupt or substituted evidence")
        self.assertFalse(gate.check(self.root)["ok"])

    def test_new_visual_attachment_without_manifest_fails_closed(self):
        self.prepare()
        record = gate.load_record(self.root, self.ingest)
        extra = "src/raw/assets/visual-test/extra.png"
        (self.draft / extra).write_bytes((self.draft / record["claims"][0]["evidence"][0]["path"]).read_bytes())
        record["files"][extra] = {"before": None, "after": gate.sha((self.draft / extra).read_bytes())}
        gate.save_record(self.root, record)
        with self.assertRaisesRegex(gate.GateError, "assets lack visual coverage"):
            gate.seal(self.root, self.ingest)

    def test_unsupported_visual_format_is_not_a_text_only_bypass(self):
        self.prepare()
        record = gate.load_record(self.root, self.ingest)
        extra = "src/raw/assets/visual-test/figure.svg"
        data = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>'
        (self.draft / extra).write_bytes(data)
        record["files"][extra] = {"before": None, "after": gate.sha(data)}
        gate.save_record(self.root, record)
        with self.assertRaisesRegex(gate.GateError, "unsupported visual original format"):
            gate.seal(self.root, self.ingest)


if __name__ == "__main__":
    unittest.main()
