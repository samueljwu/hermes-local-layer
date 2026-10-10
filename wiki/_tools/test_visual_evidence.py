#!/usr/bin/env python3
"""Scratch-only actual PDF/image regression fixtures; no canonical evidence edits.

Run: PYTHONPATH=_tools python -B -m unittest discover -s _tools \
     -p test_visual_evidence.py -v
Review notes below are synthetic TEST assertions, never production approval.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import pymupdf
from PIL import Image

import visual_evidence as ve

SCRATCH = Path(os.environ.get("TMPDIR", "/home/hermes/.hermes/cache/scratch"))
MODULE = Path(ve.__file__).absolute()


def digest(data):
    return hashlib.sha256(data).hexdigest()


class VisualEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="visual-test-", dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.parent = self.root / "src/raw/assets/test-source"
        self.parent.mkdir(parents=True)
        self.output = "src/raw/assets/test-source/visual"
        self.manifest_path = self.root / self.output / "manifest.json"

    def original(self, extension, data):
        rel = f"src/raw/assets/test-source/original.{extension}"
        (self.root / rel).write_bytes(data)
        return rel

    def native_pdf(self, pages=3, rotation=0):
        with pymupdf.open() as doc:
            for n in range(pages):
                page = doc.new_page(width=144, height=216)
                if n < pages - 1:
                    page.insert_text((12, 20), f"Fixture native page {n + 1}", fontsize=8)
                    page.insert_text((12, 40), "Dose   Response", fontsize=8)
                    page.insert_text((12, 55), "10     25", fontsize=8)
                    page.draw_rect(pymupdf.Rect(10, 30, 125, 65))
                    page.draw_line((10, 45), (125, 45))
                    page.draw_line((65, 30), (65, 65))
                page.set_rotation(rotation)
            return doc.tobytes()

    def image_bytes(self, format="PNG", size=(80, 50), color=(12, 50, 200), mode="RGB"):
        with Image.new(mode, size, color) as image:
            stream = io.BytesIO()
            image.save(stream, format=format)
            return stream.getvalue()

    def prepare_pdf(self, **kwargs):
        rel = self.original("pdf", self.native_pdf(**kwargs))
        result = ve.prepare(self.root, rel, self.output)
        return rel, result, json.loads(self.manifest_path.read_bytes())

    def prepare_image(self, extension="png", data=None):
        rel = self.original(extension, data if data is not None else self.image_bytes())
        result = ve.prepare(self.root, rel, self.output)
        return rel, result, json.loads(self.manifest_path.read_bytes())

    def read(self, path):
        return ve.safe_path(self.root, path).read_bytes()

    def valid(self, manifest, reviewed=False):
        ve.validate(manifest, self.read, require_review=reviewed)

    def approve_fixture(self, manifest, inspection="full-page"):
        for page in manifest["pages"]:
            page["review"] = {"status": "reviewed", "inspection": inspection,
                              "classification": ["mixed"], "notes": "TEST-only synthetic fixture review."}
        return manifest

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): digest(p.read_bytes())
                for p in self.root.rglob("*") if p.is_file()}

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", str(MODULE), *map(str, args)],
                              capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def test_native_pdf_all_pages_text_vector_and_blank_final(self):
        rel = self.original("pdf", self.native_pdf())
        original = self.read(rel)
        result = ve.prepare(self.root, rel, self.output)
        manifest = json.loads(self.manifest_path.read_bytes())
        self.assertEqual(result["page_count"], 3)
        self.assertEqual(result["render_count"], 3)
        self.assertEqual([p["page"] for p in manifest["pages"]], [1, 2, 3])
        self.assertEqual([p["review"]["status"] for p in manifest["pages"]], ["pending"] * 3)
        self.assertGreater(manifest["pages"][0]["text_chars"], 0)
        self.assertGreater(manifest["pages"][0]["triage"]["vector_drawings"], 0)
        self.assertEqual(manifest["pages"][-1]["text_chars"], 0)
        self.assertEqual(manifest["pages"][-1]["review"]["classification"], [])
        self.assertEqual(self.read(rel), original)
        self.assertEqual(result["original_sha256_before"], result["original_sha256_after"])
        self.assertEqual(result["manifest_sha256"], digest(self.manifest_path.read_bytes()))
        self.assertEqual(result["extraction"]["sha256"], digest(self.read(result["extraction"]["path"])))
        text = self.read(result["extraction"]["path"]).decode()
        self.assertIn("## PDF page 1\n", text)
        self.assertIn("Dose", text)
        self.assertIn("## PDF page 3\n[No native text extracted; visual content not inferred.]", text)
        self.valid(manifest)
        with self.assertRaisesRegex(ve.VisualError, "pending visual review"):
            self.valid(manifest, reviewed=True)

    def test_scanned_image_pdf_keeps_visual_content_without_fake_ocr(self):
        with pymupdf.open() as doc:
            page = doc.new_page(width=144, height=216)
            page.insert_image(page.rect, stream=self.image_bytes())
            data = doc.tobytes()
        rel = self.original("pdf", data)
        result = ve.prepare(self.root, rel, self.output)
        manifest = json.loads(self.manifest_path.read_bytes())
        page = manifest["pages"][0]
        self.assertEqual(page["text_chars"], 0)
        self.assertEqual(page["triage"]["embedded_images"], 1)
        self.assertEqual(page["review"]["classification"], [])
        with Image.open(io.BytesIO(self.read(page["render"]["path"]))) as image:
            self.assertNotEqual(image.getpixel((image.width // 2, image.height // 2)), (255, 255, 255))
        self.assertIn("not inferred", self.read(result["extraction"]["path"]).decode())
        self.valid(manifest)

    def test_standalone_png_native_size_and_exact_original_bytes(self):
        data = self.image_bytes()
        rel, _, manifest = self.prepare_image(data=data)
        self.assertEqual(manifest["kind"], "image")
        self.assertEqual(manifest["page_count"], 1)
        self.assertEqual((manifest["pages"][0]["width"], manifest["pages"][0]["height"]), (80, 50))
        self.assertEqual(self.read(rel), data)
        self.valid(manifest)

    def test_transparent_png_composited_on_white(self):
        _, _, manifest = self.prepare_image(data=self.image_bytes(mode="RGBA", color=(255, 0, 0, 0)))
        with Image.open(io.BytesIO(self.read(manifest["pages"][0]["render"]["path"]))) as image:
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
        self.valid(manifest)

    def test_jpeg_and_webp_supported_decoders(self):
        for extension, format in (("jpeg", "JPEG"), ("webp", "WEBP"), ("bmp", "BMP")):
            with self.subTest(format=format):
                rel = self.original(extension, self.image_bytes(format=format))
                result = ve.prepare(self.root, rel, self.output)
                self.assertEqual(result["page_count"], 1)
                self.valid(json.loads(self.manifest_path.read_bytes()))
                import shutil
                shutil.rmtree(self.root / self.output)

    def test_multipage_tiff_all_frames_with_distinct_dimensions(self):
        images = [Image.new("RGB", size, color) for size, color in
                  [((32, 40), "red"), ((45, 25), "green"), ((20, 21), "blue")]]
        try:
            stream = io.BytesIO()
            images[0].save(stream, format="TIFF", save_all=True, append_images=images[1:])
        finally:
            for image in images:
                image.close()
        _, result, manifest = self.prepare_image("tif", stream.getvalue())
        self.assertEqual(result["render_count"], 3)
        self.assertEqual([(p["width"], p["height"]) for p in manifest["pages"]], [(32, 40), (45, 25), (20, 21)])
        self.assertEqual(manifest["contact_sheets"][0]["pages"], [1, 2, 3])
        self.valid(manifest)
        result = ve.crop(self.manifest_path, 2, [0, 0, 10, 20], "frame-two")
        self.assertEqual(result["crop"]["units"], "image-pixels")
        self.assertEqual((result["crop"]["width"], result["crop"]["height"]), (10, 20))

    def test_tiff_decoder_orientation_and_crop_use_loaded_frame_dimensions(self):
        with Image.new("RGB", (30, 20), "red") as image:
            stream = io.BytesIO()
            image.save(stream, format="TIFF", tiffinfo={274: 6})
        _, _, manifest = self.prepare_image("tif", stream.getvalue())
        with Image.open(io.BytesIO(stream.getvalue())) as source:
            source.load()
            expected = source.size
        self.assertEqual((manifest["pages"][0]["width"], manifest["pages"][0]["height"]), expected)
        self.valid(manifest)
        result = ve.crop(self.manifest_path, 1, [0, 0, expected[0], expected[1]], "whole-frame")
        self.assertEqual((result["crop"]["width"], result["crop"]["height"]), expected)
        self.valid(json.loads(self.manifest_path.read_bytes()))

    def test_truncated_final_tiff_frame_not_silently_omitted(self):
        images = [Image.new("RGB", (30, 20), color) for color in ("red", "green", "blue")]
        stream = io.BytesIO()
        try:
            images[0].save(stream, format="TIFF", save_all=True, append_images=images[1:], compression="raw")
        finally:
            for image in images:
                image.close()
        truncated = stream.getvalue()[:-16]
        with Image.open(io.BytesIO(truncated)) as source:
            self.assertEqual(getattr(source, "n_frames", 1), 3)
        rel = self.original("tif", truncated)
        with self.assertRaises(ve.VisualError):
            ve.prepare(self.root, rel, self.output)
        self.assertFalse((self.root / self.output).exists())
        self.assertEqual(self.read(rel), truncated)

    def test_prepare_and_crop_refuse_admitted_canonical_roots(self):
        rel, _, _ = self.prepare_pdf()
        marker = self.root / "_meta/ingestion/baseline.json"
        marker.parent.mkdir(parents=True)
        marker.write_text('{"synthetic_fixture": true}')
        before = self.snapshot()
        with self.assertRaisesRegex(ve.VisualError, "canonical wiki"):
            ve.prepare(self.root, rel, self.output)
        with self.assertRaisesRegex(ve.VisualError, "published raw manifests are immutable"):
            ve.crop(self.manifest_path, 1, [10, 10, 100, 100], "new-crop")
        self.assertEqual(self.snapshot(), before)

    def test_animated_gif_all_frames(self):
        images = [Image.new("RGB", (20, 20), color) for color in ("red", "blue")]
        try:
            stream = io.BytesIO()
            images[0].save(stream, format="GIF", save_all=True, append_images=images[1:], duration=100, loop=0)
        finally:
            for image in images:
                image.close()
        _, result, manifest = self.prepare_image("gif", stream.getvalue())
        self.assertEqual(result["page_count"], 2)
        self.assertEqual([entry["page"] for entry in manifest["pages"]], [1, 2])
        self.valid(manifest)

    def test_animated_webp_all_frames(self):
        images = [Image.new("RGB", (20, 20), color) for color in ("red", "blue")]
        try:
            stream = io.BytesIO()
            images[0].save(stream, format="WEBP", save_all=True, append_images=images[1:], duration=100, loop=0)
        finally:
            for image in images:
                image.close()
        _, result, manifest = self.prepare_image("webp", stream.getvalue())
        self.assertEqual(result["page_count"], 2)
        self.valid(manifest)

    def test_numbered_contact_sheets_cover_more_than_one_batch(self):
        _, result, manifest = self.prepare_pdf(pages=18)
        self.assertEqual(result["contact_sheet_count"], 2)
        self.assertEqual(manifest["contact_sheets"][0]["pages"], list(range(1, 17)))
        self.assertEqual(manifest["contact_sheets"][1]["pages"], [17, 18])
        self.assertTrue(manifest["contact_sheets"][1]["render"]["path"].endswith("contact-0002.png"))
        self.valid(manifest)

    def test_encrypted_pdf_rejected_without_output(self):
        with pymupdf.open(stream=self.native_pdf(), filetype="pdf") as doc:
            data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256,
                               user_pw="fixture-test-only", owner_pw="fixture-owner-only")
        rel = self.original("pdf", data)
        before = self.read(rel)
        with self.assertRaisesRegex(ve.VisualError, "encrypted"):
            ve.prepare(self.root, rel, self.output)
        self.assertFalse((self.root / self.output).exists())
        self.assertEqual(self.read(rel), before)

    def test_owner_only_encryption_also_rejected(self):
        with pymupdf.open(stream=self.native_pdf(), filetype="pdf") as doc:
            data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="fixture-owner-only")
        rel = self.original("pdf", data)
        # PyMuPDF can automatically authenticate empty user passwords. Encryption
        # must still be rejected even when needs_pass/is_encrypted are false.
        with self.assertRaisesRegex(ve.VisualError, "encrypted"):
            ve.prepare(self.root, rel, self.output)

    def test_malformed_original_formats_fail_closed(self):
        for extension, data in (("pdf", b"%PDF-1.7 broken"), ("png", b"not PNG"), ("pdf", b"")):
            with self.subTest(extension=extension, data=data):
                rel = self.original(extension, data)
                with self.assertRaises(ve.VisualError):
                    ve.prepare(self.root, rel, self.output)
                self.assertFalse((self.root / self.output).exists())
                self.assertEqual(self.read(rel), data)

    def test_extension_mismatch_rejected(self):
        rel = self.original("jpeg", self.image_bytes())
        with self.assertRaisesRegex(ve.VisualError, "format mismatch"):
            ve.prepare(self.root, rel, self.output)

    def test_prepare_never_overwrites_any_existing_directory_or_original(self):
        rel, _, _ = self.prepare_pdf()
        before = self.snapshot()
        with self.assertRaisesRegex(ve.VisualError, "already exists"):
            ve.prepare(self.root, rel, self.output)
        self.assertEqual(self.snapshot(), before)
        with self.assertRaisesRegex(ve.VisualError, "output must"):
            ve.prepare(self.root, rel, rel)
        self.assertEqual(self.snapshot(), before)

    def test_empty_existing_output_also_refused(self):
        rel = self.original("png", self.image_bytes())
        (self.root / self.output).mkdir()
        with self.assertRaisesRegex(ve.VisualError, "already exists"):
            ve.prepare(self.root, rel, self.output)
        self.assertEqual(list((self.root / self.output).iterdir()), [])

    def test_unsafe_paths_and_wrong_slug_rejected(self):
        rel = self.original("png", self.image_bytes())
        for bad in ("/src/raw/assets/test-source/original.png", "src/raw/assets/test-source/../original.png",
                    "src/raw/assets/test-source//original.png", "src/raw/assets/test-source/./original.png",
                    "src/raw/assets/test-source\\original.png", "src/raw/other/original.png"):
            with self.subTest(path=bad), self.assertRaises(ve.VisualError):
                ve.prepare(self.root, bad, self.output)
        with self.assertRaises(ve.VisualError):
            ve.prepare(self.root, rel, "src/raw/assets/other/visual")
        with self.assertRaises(ve.VisualError):
            ve.prepare(self.root, rel, "src/raw/assets/test-source/other")

    def test_symlink_original_and_root_ancestors_rejected(self):
        data = self.image_bytes()
        external = self.root / "outside.png"
        external.write_bytes(data)
        rel = "src/raw/assets/test-source/original.png"
        (self.root / rel).symlink_to(external)
        with self.assertRaisesRegex(ve.VisualError, "symlink"):
            ve.prepare(self.root, rel, self.output)
        (self.root / rel).unlink()
        self.original("png", data)
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ve.VisualError, "symlink"):
            ve.prepare(alias, rel, self.output)
        with self.assertRaisesRegex(ve.VisualError, "symlink"):
            ve.prepare(alias / "src", rel, self.output)

    def test_symlink_output_refused_and_target_untouched(self):
        rel = self.original("png", self.image_bytes())
        external = self.root / "untouched"
        external.mkdir()
        (external / "sentinel").write_bytes(b"KEEP")
        (self.root / self.output).symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ve.VisualError, "symlink"):
            ve.prepare(self.root, rel, self.output)
        self.assertEqual((external / "sentinel").read_bytes(), b"KEEP")

    def test_crop_pdf_preserves_all_previous_assets_and_original(self):
        _, _, manifest = self.prepare_pdf()
        before = self.snapshot()
        result = ve.crop(self.manifest_path, 1, [10, 30, 125, 65], "table", 300)
        c = result["crop"]
        self.assertEqual(c["bbox"], [10, 30, 125, 65])
        self.assertEqual(c["units"], "pdf-points")
        after = self.snapshot()
        manifest_rel = self.manifest_path.relative_to(self.root).as_posix()
        self.assertEqual(set(after) - set(before), {c["render"]["path"]})
        for path in set(before) - {manifest_rel}:
            self.assertEqual(before[path], after[path], path)
        updated = json.loads(self.manifest_path.read_bytes())
        self.assertEqual(updated["pages"], manifest["pages"])
        self.assertEqual(updated["contact_sheets"], manifest["contact_sheets"])
        self.valid(updated)
        second = ve.crop(self.manifest_path, 2, [0, 0, 20, 20], "second")
        self.assertEqual(second["crop_count"], 2)
        self.assertEqual(after[c["render"]["path"]], digest(self.read(c["render"]["path"])))

    def test_crop_image_pixels_no_invented_upsampling_fractional_bounds(self):
        self.prepare_image()
        result = ve.crop(self.manifest_path, 1, [1.2, 2.2, 20.2, 30.8], "detail", 300)
        self.assertEqual(result["crop"]["units"], "image-pixels")
        self.assertEqual((result["crop"]["width"], result["crop"]["height"]), (20, 29))
        self.valid(json.loads(self.manifest_path.read_bytes()))

    def test_rotated_pdf_full_pages_and_crop_displayed_points(self):
        _, _, manifest = self.prepare_pdf(rotation=90)
        self.assertEqual(manifest["pages"][0]["rotation"], 90)
        self.assertEqual((manifest["pages"][0]["width"], manifest["pages"][0]["height"]), (450, 300))
        result = ve.crop(self.manifest_path, 1, [100, 10, 200, 70], "rotated")
        self.assertEqual(result["crop"]["units"], "pdf-points")
        self.valid(json.loads(self.manifest_path.read_bytes()))

    def test_pdf_crops_match_full_page_pixels_in_rotated_display_space(self):
        # At 72 DPI integer point bounds map exactly to pixels; verify content,
        # not just plausible dimensions. Rotated clipping uses display space.
        from PIL import ImageChops
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation):
                with ve._source(self.native_pdf(rotation=rotation), "src/raw/assets/test-source/original.pdf") as src:
                    info = src.info(1)
                    full_bytes, _, _ = src.render(1, 72, info["bounds"])
                    bbox = (10, 10, 130, 100)
                    crop_bytes, _, _ = src.render(1, 72, bbox)
                with Image.open(io.BytesIO(full_bytes)) as full, Image.open(io.BytesIO(crop_bytes)) as clipped:
                    expected = full.crop(bbox)
                    self.assertIsNone(ImageChops.difference(expected, clipped).getbbox())
                    expected.close()

    def test_repaired_pdf_is_not_silently_accepted(self):
        data = self.native_pdf()
        # Preserve page objects but truncate xref/trailer, which MuPDF repairs.
        offset = data.rfind(b"\nxref\n")
        self.assertGreater(offset, 0)
        truncated = data[:offset]
        with pymupdf.open(stream=truncated, filetype="pdf") as doc:
            self.assertTrue(doc.is_repaired)
        rel = self.original("pdf", truncated)
        with self.assertRaisesRegex(ve.VisualError, "malformed/repaired"):
            ve.prepare(self.root, rel, self.output)
        self.assertFalse((self.root / self.output).exists())

    def test_embedded_image_pixel_budget_prevents_hidden_large_decode(self):
        with pymupdf.open() as doc:
            page = doc.new_page(width=10, height=10)
            page.insert_image(page.rect, stream=self.image_bytes(size=(100, 100)))
            data = doc.tobytes()
        rel = self.original("pdf", data)
        with mock.patch.object(ve, "MAX_PIXELS", 5000):
            with self.assertRaisesRegex(ve.VisualError, "pixel budget"):
                ve.prepare(self.root, rel, self.output)
        self.assertFalse((self.root / self.output).exists())

    def test_crop_invalid_bounds_pages_ids_or_dpi_leave_files_unchanged(self):
        self.prepare_pdf()
        before = self.snapshot()
        cases = [(1, [-1, 0, 10, 10], "bad", 300), (1, [0, 0, 145, 10], "bad", 300),
                 (1, [0, 0, 10, 217], "bad", 300), (1, [10, 0, 10, 10], "bad", 300),
                 (1, [20, 0, 10, 10], "bad", 300), (1, [0, 0, math.nan, 10], "bad", 300),
                 (0, [0, 0, 10, 10], "bad", 300), (4, [0, 0, 10, 10], "bad", 300),
                 (True, [0, 0, 10, 10], "bad", 300), (1, [0, 0, 10, 10], "../escape", 300),
                 (1, [0, 0, 10, 10], "bad", 0)]
        for page, bounds, cid, dpi in cases:
            with self.subTest(page=page, bounds=bounds, cid=cid, dpi=dpi), self.assertRaises(ve.VisualError):
                ve.crop(self.manifest_path, page, bounds, cid, dpi)
            self.assertEqual(self.snapshot(), before)

    def test_crop_duplicate_id_and_existing_png_never_overwritten(self):
        self.prepare_image()
        ve.crop(self.manifest_path, 1, [0, 0, 10, 10], "first")
        before = self.snapshot()
        with self.assertRaisesRegex(ve.VisualError, "already exists"):
            ve.crop(self.manifest_path, 1, [0, 0, 20, 20], "first")
        self.assertEqual(before, self.snapshot())
        target = self.root / self.output / "crop-collision.png"
        target.write_bytes(b"KEEP")
        before = self.snapshot()
        with self.assertRaisesRegex(ve.VisualError, "already exists"):
            ve.crop(self.manifest_path, 1, [0, 0, 10, 10], "collision")
        self.assertEqual(before, self.snapshot())

    def test_crop_on_symlink_manifest_or_render_refused(self):
        _, _, manifest = self.prepare_image()
        original_manifest = self.manifest_path.read_bytes()
        target = self.root / "manifest-copy.json"
        target.write_bytes(original_manifest)
        self.manifest_path.unlink()
        self.manifest_path.symlink_to(target)
        with self.assertRaisesRegex(ve.VisualError, "symlink"):
            ve.crop(self.manifest_path, 1, [0, 0, 10, 10], "bad")
        self.manifest_path.unlink()
        self.manifest_path.write_bytes(original_manifest)
        page = self.root / manifest["pages"][0]["render"]["path"]
        data = page.read_bytes()
        external = self.root / "render.png"
        external.write_bytes(data)
        page.unlink()
        page.symlink_to(external)
        with self.assertRaisesRegex(ve.VisualError, "symlink"):
            ve.crop(self.manifest_path, 1, [0, 0, 10, 10], "bad")
        self.assertFalse((self.root / self.output / "crop-bad.png").exists())

    def test_validator_pending_blocks_admission_but_operational_check_succeeds(self):
        _, _, manifest = self.prepare_image()
        self.valid(manifest)
        with self.assertRaisesRegex(ve.VisualError, "pending"):
            ve.validate(manifest, self.read)
        self.valid(self.approve_fixture(manifest), reviewed=True)

    def test_full_page_or_contact_sheet_review_with_notes_and_classifications(self):
        _, _, manifest = self.prepare_pdf()
        for inspection in ("full-page", "contact-sheet"):
            checked = self.approve_fixture(copy.deepcopy(manifest), inspection)
            self.valid(checked, reviewed=True)
        checked = self.approve_fixture(copy.deepcopy(manifest))
        for field, value in (("inspection", "crop"), ("notes", " "), ("classification", []),
                             ("classification", ["invented"]), ("classification", ["table", "table"]),
                             ("status", "approved")):
            with self.subTest(field=field, value=value):
                bad = copy.deepcopy(checked)
                bad["pages"][0]["review"][field] = value
                with self.assertRaises(ve.VisualError):
                    self.valid(bad, reviewed=True)

    def test_explicit_excluded_pages_require_reason_and_notes(self):
        _, _, manifest = self.prepare_pdf()
        self.approve_fixture(manifest)
        manifest["pages"][-1]["review"] = {"status": "excluded", "inspection": "full-page", "classification": ["blank"],
                                           "notes": "TEST-only explicit final page exclusion.",
                                           "exclusion_reason": "TEST-only final fixture page is out of scope."}
        self.valid(manifest, reviewed=True)
        for key in ("notes", "exclusion_reason"):
            bad = copy.deepcopy(manifest)
            bad["pages"][-1]["review"][key] = ""
            with self.assertRaises(ve.VisualError):
                self.valid(bad, reviewed=True)
        bad = copy.deepcopy(manifest)
        del bad["pages"][-1]["review"]["exclusion_reason"]
        with self.assertRaises(ve.VisualError):
            self.valid(bad, reviewed=True)

    def test_pending_must_not_claim_classification_or_inspection(self):
        _, _, manifest = self.prepare_image()
        for key, value in (("classification", ["scan"]), ("inspection", "full-page"), ("notes", "read")):
            bad = copy.deepcopy(manifest)
            bad["pages"][0]["review"][key] = value
            with self.assertRaises(ve.VisualError):
                self.valid(bad)

    def test_original_hash_mismatch(self):
        rel, _, manifest = self.prepare_pdf()
        (self.root / rel).write_bytes(self.read(rel) + b"changed")
        with self.assertRaisesRegex(ve.VisualError, "SHA256 mismatch"):
            self.valid(manifest)
        with self.assertRaises(ve.VisualError):
            ve.crop(self.manifest_path, 1, [0, 0, 10, 10], "blocked")

    def test_original_rehashed_still_parsed_and_actual_count_enforced(self):
        rel, _, manifest = self.prepare_pdf()
        data = self.native_pdf(pages=4)
        (self.root / rel).write_bytes(data)
        manifest["original"]["sha256"] = digest(data)
        with self.assertRaisesRegex(ve.VisualError, "actual source page/frame count"):
            self.valid(manifest)

    def test_rehashed_invalid_original_rejected(self):
        rel, _, manifest = self.prepare_image()
        data = b"not a source image"
        (self.root / rel).write_bytes(data)
        manifest["original"]["sha256"] = digest(data)
        with self.assertRaisesRegex(ve.VisualError, "parse"):
            self.valid(manifest)

    def test_missing_duplicate_out_of_order_or_false_page_numbers(self):
        _, _, manifest = self.prepare_pdf()
        changes = [lambda m: m["pages"].pop(),
                   lambda m: m["pages"].__setitem__(1, copy.deepcopy(m["pages"][0])),
                   lambda m: m["pages"].reverse(),
                   lambda m: m["pages"][0].__setitem__("page", True)]
        for change in changes:
            with self.subTest(change=change):
                bad = copy.deepcopy(manifest)
                change(bad)
                with self.assertRaises(ve.VisualError):
                    self.valid(bad)
        bad = copy.deepcopy(manifest)
        bad["pages"].pop()
        bad["page_count"] = 2
        with self.assertRaisesRegex(ve.VisualError, "actual source page/frame count"):
            self.valid(bad)

    def test_actual_tiff_count_cannot_hide_final_frame(self):
        images = [Image.new("RGB", (10, 10), color) for color in ("red", "blue")]
        stream = io.BytesIO()
        try:
            images[0].save(stream, format="TIFF", save_all=True, append_images=images[1:])
        finally:
            for image in images:
                image.close()
        _, _, manifest = self.prepare_image("tif", stream.getvalue())
        manifest["pages"].pop()
        manifest["page_count"] = 1
        with self.assertRaisesRegex(ve.VisualError, "actual source page/frame count"):
            self.valid(manifest)

    def test_corrupt_png_rehashed_still_rejected(self):
        _, _, manifest = self.prepare_image()
        pin = manifest["pages"][0]["render"]
        data = b"corrupt PNG"
        (self.root / pin["path"]).write_bytes(data)
        pin["sha256"] = digest(data)
        with self.assertRaisesRegex(ve.VisualError, "PNG"):
            self.valid(manifest)

    def test_truncated_png_with_valid_header_rejected(self):
        _, _, manifest = self.prepare_image()
        pin = manifest["pages"][0]["render"]
        data = self.read(pin["path"])[:60]
        (self.root / pin["path"]).write_bytes(data)
        pin["sha256"] = digest(data)
        with self.assertRaises(ve.VisualError):
            self.valid(manifest)

    def test_png_real_dimensions_must_match_source_not_only_manifest(self):
        _, _, manifest = self.prepare_image()
        pin = manifest["pages"][0]["render"]
        data = self.image_bytes(size=(10, 10))
        (self.root / pin["path"]).write_bytes(data)
        pin["sha256"] = digest(data)
        with self.assertRaisesRegex(ve.VisualError, "PNG/page dimensions mismatch"):
            self.valid(manifest)
        manifest["pages"][0]["width"] = 10
        manifest["pages"][0]["height"] = 10
        with self.assertRaisesRegex(ve.VisualError, "dimensions disagree"):
            self.valid(manifest)

    def test_all_asset_hashes_pinned_including_contact_sheets_and_crops(self):
        _, _, manifest = self.prepare_pdf()
        ve.crop(self.manifest_path, 1, [0, 0, 20, 20], "detail")
        manifest = json.loads(self.manifest_path.read_bytes())
        pins = [manifest["pages"][0]["render"], manifest["crops"][0]["render"],
                manifest["contact_sheets"][0]["render"]]
        for pin in pins:
            with self.subTest(path=pin["path"]):
                path = self.root / pin["path"]
                before = path.read_bytes()
                path.write_bytes(before + b"CHANGED")
                with self.assertRaisesRegex(ve.VisualError, "SHA256 mismatch"):
                    self.valid(manifest)
                path.write_bytes(before)
        self.valid(manifest)

    def test_missing_contact_sheet_or_false_page_coverage(self):
        _, _, manifest = self.prepare_pdf()
        bad = copy.deepcopy(manifest)
        bad["contact_sheets"] = []
        with self.assertRaisesRegex(ve.VisualError, "contact sheets"):
            self.valid(bad)
        bad = copy.deepcopy(manifest)
        bad["contact_sheets"][0]["pages"] = [1, 1, 3]
        with self.assertRaisesRegex(ve.VisualError, "page coverage"):
            self.valid(bad)
        bad = copy.deepcopy(manifest)
        bad["contact_sheets"][0]["pages"] = [True, 2, 3]
        with self.assertRaises(ve.VisualError):
            self.valid(bad)

    def test_crop_manifest_page_bounds_units_and_size_validated(self):
        self.prepare_pdf()
        ve.crop(self.manifest_path, 1, [0, 0, 20, 20], "detail")
        manifest = json.loads(self.manifest_path.read_bytes())
        for key, value in (("page", 4), ("bbox", [-1, 0, 20, 20]), ("bbox", [0, 0, 200, 20]),
                           ("units", "image-pixels"), ("width", 1), ("dpi", False), ("id", "../escape")):
            with self.subTest(key=key):
                bad = copy.deepcopy(manifest)
                bad["crops"][0][key] = value
                with self.assertRaises(ve.VisualError):
                    self.valid(bad)
        bad = copy.deepcopy(manifest)
        bad["crops"].append(copy.deepcopy(bad["crops"][0]))
        with self.assertRaisesRegex(ve.VisualError, "duplicate crop"):
            self.valid(bad)

    def test_manifest_schema_types_hashes_paths_triage_and_rotation(self):
        _, _, manifest = self.prepare_pdf()
        cases = [lambda m: m.__setitem__("version", True),
                 lambda m: m.__setitem__("page_count", 3.0),
                 lambda m: m.__setitem__("kind", "image"),
                 lambda m: m.__setitem__("render_method", "pretend"),
                 lambda m: m.__setitem__("extra", "not fixed schema"),
                 lambda m: m["original"].__setitem__("sha256", "x" * 64),
                 lambda m: m["pages"][0].__setitem__("rotation", 180),
                 lambda m: m["pages"][0].__setitem__("width", True),
                 lambda m: m["pages"][0].__setitem__("text_chars", 0),
                 lambda m: m["pages"][0]["triage"].__setitem__("embedded_images", -1),
                 lambda m: m["pages"][0]["triage"].__setitem__("vector_drawings", 0),
                 lambda m: m["pages"][0]["render"].__setitem__("path", "src/raw/assets/other/visual/page-000001.png"),
                 lambda m: m["pages"][0]["render"].__setitem__("path", "src/raw/assets/test-source/../outside.png")]
        for change in cases:
            with self.subTest(change=change):
                bad = copy.deepcopy(manifest)
                change(bad)
                with self.assertRaises(ve.VisualError):
                    self.valid(bad)
        with self.assertRaises(ve.VisualError):
            ve.validate([], self.read)

    def test_callback_exception_is_visual_error_and_callback_used_for_source(self):
        _, _, manifest = self.prepare_image()
        calls = []
        def read(path):
            calls.append(path)
            return self.read(path)
        ve.validate(manifest, read, require_review=False)
        self.assertEqual(calls[0], manifest["original"]["path"])
        def unavailable(path):
            raise FileNotFoundError(path)
        with self.assertRaises(ve.VisualError):
            ve.validate(manifest, unavailable, require_review=False)

    def test_duplicate_json_keys_rejected_by_cli_loader(self):
        self.prepare_image()
        data = self.manifest_path.read_bytes().replace(b'"version": 1,', b'"version": 1, "version": 1,', 1)
        self.manifest_path.write_bytes(data)
        result = self.cli("validate", "--manifest", self.manifest_path, "--allow-pending")
        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate JSON key", result.stderr)

    def test_cli_prepare_crop_and_review_admission_exit_codes(self):
        rel = self.original("pdf", self.native_pdf())
        prepare = self.cli("prepare", "--root", self.root, "--original", rel, "--output", self.output, "--dpi", 150)
        self.assertEqual(prepare.returncode, 0, prepare.stderr)
        result = json.loads(prepare.stdout)
        self.assertEqual(result["page_count"], 3)
        self.assertEqual(result["pending_review_count"], 3)
        crop = self.cli("crop", "--manifest", self.manifest_path, "--page", 1,
                        "--bbox", 0, 0, 20, 20, "--id", "detail", "--dpi", 300)
        self.assertEqual(crop.returncode, 0, crop.stderr)
        self.assertEqual(json.loads(crop.stdout)["crop_count"], 1)
        pending = self.cli("validate", "--manifest", self.manifest_path)
        self.assertEqual(pending.returncode, 2)
        self.assertIn("pending", pending.stderr)
        operational = self.cli("validate", "--manifest", self.manifest_path, "--allow-pending")
        self.assertEqual(operational.returncode, 0, operational.stderr)
        manifest = self.approve_fixture(json.loads(self.manifest_path.read_bytes()))
        self.manifest_path.write_bytes(json.dumps(manifest).encode())
        reviewed = self.cli("validate", "--manifest", self.manifest_path)
        self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
        self.assertTrue(json.loads(reviewed.stdout)["valid"])

    def test_import_and_help_are_lazy_without_render_dependencies(self):
        code = "\n".join([
            "import builtins,runpy,sys",
            "original = builtins.__import__",
            "def reject(name, *a, **kw):",
            "    if name.split('.')[0] in {'pymupdf', 'fitz', 'PIL'}: raise ImportError('blocked-test')",
            "    return original(name, *a, **kw)",
            "builtins.__import__ = reject",
            f"sys.argv = [{str(MODULE)!r}, '--help']",
            f"runpy.run_path({str(MODULE)!r}, run_name='__main__')",
        ])
        result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("prepare", result.stdout)

    def test_resource_limits_reject_before_large_raster_and_cleanup(self):
        rel = self.original("pdf", self.native_pdf())
        before = self.read(rel)
        with mock.patch.object(ve, "MAX_PIXELS", 100):
            with self.assertRaisesRegex(ve.VisualError, "pixel budget"):
                ve.prepare(self.root, rel, self.output)
        self.assertFalse((self.root / self.output).exists())
        self.assertEqual(self.read(rel), before)
        with mock.patch.object(ve, "MAX_SOURCE_BYTES", 10):
            with self.assertRaisesRegex(ve.VisualError, "byte budget"):
                ve.prepare(self.root, rel, self.output)
        with mock.patch.object(ve, "MAX_PAGES", 2):
            with self.assertRaisesRegex(ve.VisualError, "page/frame count"):
                ve.prepare(self.root, rel, self.output)

    def test_total_pixel_and_output_budgets(self):
        rel = self.original("pdf", self.native_pdf())
        for key, value, message in (("MAX_TOTAL_PIXELS", 200000, "aggregate pixel"),
                                    ("MAX_OUTPUT_BYTES", 20, "output byte")):
            with self.subTest(budget=key), mock.patch.object(ve, key, value):
                with self.assertRaisesRegex(ve.VisualError, message):
                    ve.prepare(self.root, rel, self.output)
            self.assertFalse((self.root / self.output).exists())

    def test_unrenderable_page_is_error_and_cleanup_not_fake_success(self):
        rel = self.original("pdf", self.native_pdf())
        original_render = ve._Source.render
        def fail_second(source, number, dpi, bounds):
            if number == 2:
                raise RuntimeError("fixture renderer failure")
            return original_render(source, number, dpi, bounds)
        with mock.patch.object(ve._Source, "render", fail_second):
            with self.assertRaisesRegex(ve.VisualError, "renderer failure"):
                ve.prepare(self.root, rel, self.output)
        self.assertFalse((self.root / self.output).exists())

    def test_original_mutation_detected_before_manifest_commit(self):
        rel = self.original("pdf", self.native_pdf())
        original_render = ve._Source.render
        def mutate(source, number, dpi, bounds):
            data = original_render(source, number, dpi, bounds)
            if number == 1:
                (self.root / rel).write_bytes(b"fixture concurrent external edit")
            return data
        with mock.patch.object(ve._Source, "render", mutate):
            with self.assertRaisesRegex(ve.VisualError, "original changed"):
                ve.prepare(self.root, rel, self.output)
        self.assertFalse((self.root / self.output).exists())
        self.assertEqual(self.read(rel), b"fixture concurrent external edit")

    def test_crop_validation_failure_rolls_back_only_new_crop(self):
        self.prepare_image()
        before = self.snapshot()
        original_validate = ve.validate
        calls = 0
        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ve.VisualError("fixture validation failure")
            return original_validate(*args, **kwargs)
        with mock.patch.object(ve, "validate", fail_second):
            with self.assertRaisesRegex(ve.VisualError, "fixture validation"):
                ve.crop(self.manifest_path, 1, [0, 0, 10, 10], "rolled-back")
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
