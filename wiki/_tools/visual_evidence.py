#!/usr/bin/env python3
"""Offline visual evidence preparation, not interpretation or visual review.

All manifest paths are draft-root-relative POSIX paths. ``read(path)`` supplied
by admission callers must return bytes and enforce filesystem no-symlink policy
(or read an already immutable, path-validated bundle). validate() never imports
or depends on the ingestion gate. Only operational callers disable review checks.

PDF coordinates use the displayed, rotated page rectangle in points; image
coordinates use decoded frame pixels, without an explicit EXIF transpose. TIFF
decoder-native orientation is retained. Image DPI is PNG
metadata, not invented source resolution: images are not upsampled. Native PDF
text is preserved by page; empty extraction does NOT mean an empty visual page.
Resource ceilings bound bytes, pages, per-raster pixels and total rendering work;
processing remains one page/frame at a time. No OCR, network or model calls.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
import warnings

VERSION = 1
MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_ASSET_BYTES = 512 * 1024 * 1024
MAX_PAGES = 100_000
MAX_PIXELS = 80_000_000
MAX_TOTAL_PIXELS = 100_000_000_000
MAX_OUTPUT_BYTES = 16 * 1024 * 1024 * 1024
MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 128 * 1024 * 1024
PDF_METHOD = "PyMuPDF.get_pixmap; RGB; opaque"
IMAGE_METHOD = "Pillow frame decode; RGB; opaque; native pixels"
CLASSIFICATIONS = {"prose", "table", "figure", "equation", "scan", "blank", "disclaimer", "mixed", "uncertain"}
EXTENSIONS = {".pdf": "pdf", ".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG",
              ".webp": "WEBP", ".tif": "TIFF", ".tiff": "TIFF", ".bmp": "BMP", ".gif": "GIF"}
SHEET_COLUMNS, SHEET_ROWS = 4, 4
TILE_WIDTH, TILE_HEIGHT, LABEL_HEIGHT, MARGIN = 256, 320, 24, 12
SHEET_SIZE = (SHEET_COLUMNS * (TILE_WIDTH + MARGIN) + MARGIN,
              SHEET_ROWS * (TILE_HEIGHT + LABEL_HEIGHT + MARGIN) + MARGIN)


class VisualError(ValueError):
    """Invalid, unsafe, unrenderable or incompletely reviewed visual evidence."""


def need(condition, message):
    if not condition:
        raise VisualError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def _deps():
    # Deliberately lazy: --help and importing this module need only stdlib.
    try:
        import pymupdf
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise VisualError("requires locally installed pymupdf and Pillow (offline only)") from exc
    return pymupdf, Image, ImageDraw


def _slug(value):
    need(isinstance(value, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", value), "invalid slug")
    return value


def _rel(value):
    need(isinstance(value, str) and value and value.strip() == value, "invalid path")
    p = PurePosixPath(value)
    need(not p.is_absolute() and "\\" not in value and value == p.as_posix()
         and all(part not in {".", ".."} for part in p.parts), "unsafe/noncanonical path")
    need(len(p.parts) >= 5 and p.parts[:3] == ("src", "raw", "assets"), "path outside src/raw/assets")
    _slug(list(p.parts)[3])
    return value


def _root(root):
    p = Path(root).absolute()
    need(".." not in p.parts, "unsafe root")
    for ancestor in [*reversed(p.parents), p]:
        need(not ancestor.is_symlink(), f"symlink root ancestor: {ancestor}")
    need(p.is_dir(), "root must be an existing directory")
    return p


def safe_path(root, rel, *, missing=False):
    """Reject traversal, symlinks (including root ancestors), special files."""
    _rel(rel)
    p = _root(root)
    for part in PurePosixPath(rel).parts:
        p = p / part
        if p.exists() or p.is_symlink():
            mode = p.lstat().st_mode
            need(not stat.S_ISLNK(mode), f"symlink path: {rel}")
            need(stat.S_ISREG(mode) or stat.S_ISDIR(mode), f"nonregular path: {rel}")
    if not missing:
        need(p.is_file(), f"missing regular file: {rel}")
    return p


def _read(root, rel, limit=MAX_ASSET_BYTES):
    p = safe_path(root, rel)
    need(p.stat().st_size <= limit, f"asset byte budget exceeded: {rel}")
    with p.open("rb") as stream:
        data = stream.read(limit + 1)
    need(len(data) <= limit, f"asset byte budget exceeded: {rel}")
    return data


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            need(key not in result, f"duplicate JSON key: {key}")
            result[key] = value
        return result
    try:
        need(isinstance(data, bytes) and len(data) <= MAX_MANIFEST_BYTES, "invalid manifest bytes/budget")
        return json.loads(data, object_pairs_hook=unique,
                          parse_constant=lambda x: (_ for _ in ()).throw(VisualError(f"nonfinite JSON: {x}")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise VisualError(f"invalid manifest JSON: {exc}") from exc


def _serialize(manifest):
    return json.dumps(manifest, indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"


def _keys(value, keys, label):
    need(isinstance(value, dict) and set(value) == set(keys), f"invalid {label} schema")


def _integer(value, label, minimum=0, maximum=None):
    need(type(value) is int and value >= minimum and (maximum is None or value <= maximum), f"invalid {label}")
    return value


def _number(value):
    return type(value) in (float, int) and math.isfinite(value)


def _dpi(value):
    return _integer(value, "dpi", 1, 2400)


def _pixel_budget(width, height):
    _integer(width, "width", 1)
    _integer(height, "height", 1)
    need(width * height <= MAX_PIXELS, "per-raster pixel budget exceeded")


def _bbox(value, bounds):
    need(isinstance(value, (list, tuple)) and len(value) == 4 and all(_number(x) for x in value), "invalid bbox")
    x0, y0, x1, y1 = value
    bx0, by0, bx1, by1 = bounds
    need(bx0 <= x0 < x1 <= bx1 and by0 <= y0 < y1 <= by1, "bbox outside page/frame or empty")
    return list(value)


class _Source:
    def __init__(self, data, original_path):
        pm, Image, _ = _deps()
        need(isinstance(data, bytes) and 0 < len(data) <= MAX_SOURCE_BYTES, "source byte budget/empty file")
        self.pm, self.Image, self.doc, self.image = pm, Image, None, None
        ext = PurePosixPath(original_path).suffix
        need(ext in EXTENSIONS, "unsupported original format")
        try:
            if ext == ".pdf":
                self.kind = "pdf"
                self.doc = pm.open(stream=data, filetype="pdf")
                # Empty-user-password encryption is auto-authenticated by MuPDF,
                # so is_encrypted/needs_pass alone do not detect all encryption.
                need(self.doc.is_pdf and not self.doc.is_encrypted and not self.doc.needs_pass
                     and self.doc.xref_get_key(-1, "Encrypt")[0] == "null",
                     "encrypted/non-PDF source rejected")
                need(not self.doc.is_repaired, "malformed/repaired PDF rejected")
                self.count = self.doc.page_count
            else:
                self.kind = "image"
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    self.image = Image.open(io.BytesIO(data))
                need(self.image.format == EXTENSIONS[ext], "original extension/decoded format mismatch")
                self.count = getattr(self.image, "n_frames", 1)
            _integer(self.count, "source page/frame count", 1, MAX_PAGES)
        except Exception:
            self.close()
            raise

    def close(self):
        if self.doc is not None:
            self.doc.close()
        if self.image is not None:
            self.image.close()

    def info(self, number):
        if self.kind == "pdf":
            assert self.doc is not None
            p = self.doc.load_page(number - 1)
            r = p.rect
            need(all(math.isfinite(x) for x in r) and r.width > 0 and r.height > 0, "invalid PDF page rectangle")
            text = p.get_text("text")
            need(len(text.encode("utf-8")) <= MAX_TEXT_BYTES, "page text budget exceeded")
            images = p.get_images(full=True)
            for image in images:
                _pixel_budget(image[2], image[3])
            triage = {"text_chars": len(text), "embedded_images": len(images),
                      "vector_drawings": len(p.get_drawings())}
            return {"bounds": list(r), "rotation": p.rotation, "text": text, "triage": triage}
        assert self.image is not None
        with warnings.catch_warnings():
            warnings.simplefilter("error", self.Image.DecompressionBombWarning)
            self.image.seek(number - 1)
            _pixel_budget(*self.image.size)
            self.image.load()  # All frames must decode, not just headers.
            # TIFF decoder orientation may swap dimensions during load().
            w, h = self.image.size
            _pixel_budget(w, h)
        return {"bounds": [0, 0, w, h], "rotation": 0, "text": "",
                "triage": {"text_chars": 0, "embedded_images": 1, "vector_drawings": 0}}

    def dimensions(self, number, dpi, bounds):
        if self.kind == "pdf":
            r = self.pm.Rect(bounds) * self.pm.Matrix(dpi / 72, dpi / 72)
            ir = r.irect
            w, h = ir.width, ir.height
        else:
            x0, y0, x1, y1 = bounds
            w, h = math.ceil(x1) - math.floor(x0), math.ceil(y1) - math.floor(y0)
        _pixel_budget(w, h)
        return w, h

    def render(self, number, dpi, bounds):
        self.dimensions(number, dpi, bounds)  # Check before allocating a raster.
        if self.kind == "pdf":
            assert self.doc is not None
            p = self.doc.load_page(number - 1)
            pix = p.get_pixmap(dpi=dpi, colorspace=self.pm.csRGB, alpha=False, clip=self.pm.Rect(bounds))
            return pix.tobytes("png"), pix.width, pix.height
        assert self.image is not None
        self.image.seek(number - 1)
        x0, y0, x1, y1 = bounds
        box = (math.floor(x0), math.floor(y0), math.ceil(x1), math.ceil(y1))
        with self.image.crop(box).convert("RGBA") as rgba:
            with self.Image.new("RGB", rgba.size, "white") as rgb:
                rgb.paste(rgba, mask=rgba.getchannel("A"))
                stream = io.BytesIO()
                rgb.save(stream, format="PNG", dpi=(dpi, dpi))
                return stream.getvalue(), rgb.width, rgb.height


@contextlib.contextmanager
def _source(data, path):
    src = None
    try:
        src = _Source(data, path)
        yield src
    except VisualError:
        raise
    except Exception as exc:
        raise VisualError(f"cannot parse/decode/render source: {exc}") from exc
    finally:
        if src is not None:
            src.close()


def _png(data):
    _, Image, _ = _deps()
    try:
        need(isinstance(data, bytes) and 0 < len(data) <= MAX_ASSET_BYTES, "PNG byte budget")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                need(image.format == "PNG" and getattr(image, "n_frames", 1) == 1, "render must be a single-frame PNG")
                _pixel_budget(*image.size)
                size = image.size
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                need(image.size == size, "unstable PNG dimensions")
        return size
    except VisualError:
        raise
    except Exception as exc:
        raise VisualError(f"cannot decode PNG: {exc}") from exc


def _review(review, require_review, *, legacy_exclusions=False):
    need(isinstance(review, dict), "missing review")
    status = review.get("status")
    need(isinstance(status, str) and status in {"pending", "reviewed", "excluded"}, "invalid review status")
    keys = {"status", "inspection", "classification", "notes"}
    if status == "excluded":
        keys.add("exclusion_reason")
    _keys(review, keys, "review")
    need(isinstance(review["notes"], str), "invalid review notes")
    classes = review["classification"]
    need(isinstance(classes, list) and all(isinstance(c, str) and c in CLASSIFICATIONS for c in classes)
         and len(set(classes)) == len(classes), "invalid review classification")
    if status == "pending":
        need(review["inspection"] is None and classes == [] and review["notes"] == "", "pending review must not imply inspection")
        need(not require_review, "pending visual review blocks admission")
    elif status == "reviewed":
        need(review["inspection"] in ("full-page", "contact-sheet"), "reviewed page needs full-page/contact-sheet inspection")
        need(bool(classes) and bool(review["notes"].strip()), "reviewed page needs classification and notes")
    else:
        if legacy_exclusions:
            need(review["inspection"] is None or review["inspection"] in ("full-page", "contact-sheet"), "invalid excluded inspection")
        else:
            # All exclusions require full-page inspection. This deliberately
            # covers opening/terminal, mixed, uncertain and otherwise high-risk
            # pages without trusting text-layer triage to identify those risks.
            need(review["inspection"] == "full-page" and bool(classes),
                 "excluded page requires full-page inspection and classification")
        need(isinstance(review["exclusion_reason"], str) and review["exclusion_reason"].strip()
             and review["notes"].strip(), "excluded page requires explicit exclusion_reason and notes")


def validate(manifest, read, *, require_review=True, legacy_exclusions=False) -> None:
    """Check schema, pins, source-derived completeness, PNGs and review coverage.

    ``manifest`` is a decoded mapping; ``read`` accepts each root-relative path.
    Hash identity/dimensions are structural checks, NOT proof that rendering is
    faithful or that the reviewer actually looked at pixels. Crop sufficiency,
    central numeric/equation/figure policy and identity inspection belong to the
    admission gate. This function requires a decision for every source page.
    """
    try:
        _validate(manifest, read, require_review, legacy_exclusions)
    except VisualError:
        raise
    except Exception as exc:
        raise VisualError(f"invalid visual manifest/assets: {exc}") from exc


def _validate(manifest, read, require_review, legacy_exclusions=False):
    _keys(manifest, {"version", "original", "kind", "page_count", "render_method", "pages", "crops", "contact_sheets"}, "manifest")
    need(type(manifest["version"]) is int and manifest["version"] == VERSION, "unsupported manifest version")
    _keys(manifest["original"], {"path", "sha256"}, "original")
    original = manifest["original"]["path"]
    _rel(original)
    op = PurePosixPath(original)
    need(len(op.parts) == 5 and op.stem == "original" and op.suffix in EXTENSIONS, "invalid original path")
    base = op.parent / "visual"
    seen = set()
    total_bytes = 0

    def pinned(item, expected=None):
        nonlocal total_bytes
        _keys(item, {"path", "sha256"}, "asset pin")
        path = _rel(item["path"])
        need(expected is None or path == expected, f"unexpected asset path: {path}")
        need(path not in seen, "duplicate asset path")
        seen.add(path)
        need(isinstance(item["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", item["sha256"]), "invalid asset SHA256")
        data = read(path)
        need(isinstance(data, bytes) and len(data) <= MAX_ASSET_BYTES, "asset bytes/budget")
        total_bytes += len(data)
        need(total_bytes <= MAX_OUTPUT_BYTES + MAX_SOURCE_BYTES, "aggregate asset byte budget exceeded")
        need(sha(data) == item["sha256"], f"asset SHA256 mismatch: {path}")
        return data

    original_bytes = pinned(manifest["original"])
    extraction = read(str(base / "extraction.txt"))
    need(isinstance(extraction, bytes) and len(extraction) <= MAX_ASSET_BYTES, "invalid native extraction bytes/budget")
    text_cursor = 0
    _integer(manifest["page_count"], "page_count", 1, MAX_PAGES)
    need(isinstance(manifest["pages"], list) and isinstance(manifest["crops"], list)
         and isinstance(manifest["contact_sheets"], list), "pages/crops/contact_sheets must be lists")
    need(len(manifest["crops"]) <= MAX_PAGES and len(manifest["pages"]) == manifest["page_count"], "invalid page/crop coverage")
    with _source(original_bytes, original) as src:
        need(manifest["kind"] == src.kind, "original/manifest kind mismatch")
        need(manifest["page_count"] == src.count, "actual source page/frame count mismatch")
        need(manifest["render_method"] == (PDF_METHOD if src.kind == "pdf" else IMAGE_METHOD), "invalid render_method")
        source_info, total_pixels = {}, 0
        for number, entry in enumerate(manifest["pages"], 1):
            _keys(entry, {"page", "render", "width", "height", "dpi", "rotation", "text_chars", "triage", "review"}, "page")
            need(type(entry["page"]) is int and entry["page"] == number, "pages must be exactly ordered 1..source count")
            dpi = _dpi(entry["dpi"])
            info = src.info(number)
            native_chunk = _text_chunk(src.kind, number, info["text"])
            text_end = text_cursor + len(native_chunk)
            need(extraction[text_cursor:text_end] == native_chunk,
                 "native extraction page boundaries/content differ from the original")
            text_cursor = text_end
            source_info[number] = info["bounds"]
            size = src.dimensions(number, dpi, info["bounds"])
            for field in ("width", "height"):
                _integer(entry[field], field, 1)
            need(tuple(entry[k] for k in ("width", "height")) == size, "page dimensions disagree with source/DPI")
            need(_number(entry["rotation"]) and entry["rotation"] == info["rotation"], "rotation mismatch")
            _integer(entry["text_chars"], "text_chars")
            need(entry["text_chars"] == len(info["text"]), "native text count mismatch")
            _keys(entry["triage"], {"text_chars", "embedded_images", "vector_drawings"}, "triage")
            for key, value in entry["triage"].items():
                _integer(value, f"triage {key}")
            need(entry["triage"] == info["triage"], "source triage mismatch (triage is not proof)")
            total_pixels += size[0] * size[1]
            need(total_pixels <= MAX_TOTAL_PIXELS, "aggregate pixel budget exceeded")
            need(_png(pinned(entry["render"], str(base / f"page-{number:06d}.png"))) == size, "PNG/page dimensions mismatch")
            _review(entry["review"], require_review, legacy_exclusions=legacy_exclusions)
        need(text_cursor == len(extraction), "native extraction has extra or missing page boundaries/content")
        ids = set()
        for crop in manifest["crops"]:
            _keys(crop, {"id", "page", "bbox", "units", "render", "width", "height", "dpi"}, "crop")
            cid = _slug(crop["id"])
            need(cid not in ids, "duplicate crop id")
            ids.add(cid)
            number = _integer(crop["page"], "crop page", 1, src.count)
            dpi = _dpi(crop["dpi"])
            need(crop["units"] == ("pdf-points" if src.kind == "pdf" else "image-pixels"), "crop units mismatch")
            bbox = _bbox(crop["bbox"], source_info[number])
            size = src.dimensions(number, dpi, bbox)
            _integer(crop["width"], "crop width", 1)
            _integer(crop["height"], "crop height", 1)
            need((crop["width"], crop["height"]) == size, "crop dimensions mismatch")
            total_pixels += size[0] * size[1]
            need(total_pixels <= MAX_TOTAL_PIXELS, "aggregate pixel budget exceeded")
            need(_png(pinned(crop["render"], str(base / f"crop-{cid}.png"))) == size, "crop PNG dimensions mismatch")
        chunk = SHEET_COLUMNS * SHEET_ROWS
        need(len(manifest["contact_sheets"]) == math.ceil(src.count / chunk), "missing/extra contact sheets")
        for index, sheet in enumerate(manifest["contact_sheets"], 1):
            _keys(sheet, {"render", "pages"}, "contact sheet")
            expected = list(range((index - 1) * chunk + 1, min(index * chunk, src.count) + 1))
            need(isinstance(sheet["pages"], list) and all(type(n) is int for n in sheet["pages"])
                 and sheet["pages"] == expected, "contact-sheet page coverage mismatch")
            need(_png(pinned(sheet["render"], str(base / f"contact-{index:04d}.png"))) == SHEET_SIZE,
                 "contact-sheet dimensions mismatch")


def text_page_map(mapping, count):
    """Validate explicit native full-page/UTF-8-span selection, never normalize."""
    _integer(count, "source page/frame count", 1, MAX_PAGES)
    if mapping is None:
        return [{"page": number} for number in range(1, count + 1)]
    need(isinstance(mapping, list) and mapping, "invalid text page map")
    numbers = []
    for entry in mapping:
        need(isinstance(entry, dict) and set(entry) in ({"page"}, {"page", "start", "end"}), "invalid text page map entry")
        numbers.append(_integer(entry["page"], "mapped page", 1, count))
        if "start" in entry:
            _integer(entry["start"], "native UTF-8 span start")
            _integer(entry["end"], "native UTF-8 span end", 1)
            need(entry["start"] < entry["end"], "empty/reversed native UTF-8 span")
    need(numbers == sorted(set(numbers)), "duplicate/unordered text page map")
    return [dict(entry) for entry in mapping]


def _text_chunk(kind, number, text):
    heading = f"## {'PDF page' if kind == 'pdf' else 'Image frame'} {number}\n"
    body = text if text else "[No native text extracted; visual content not inferred.]\n"
    return (heading + body + "\n").encode("utf-8")


def native_page_chunks(original, read, mapping=None):
    """Yield exact original-derived native chunks for a validated selection.

    Optional start/end are UTF-8 byte offsets into the original page's native
    text, not the Markdown or synthetic headings. No OCR/transcription guessing.
    A caller supplies the same safe immutable read callback as validate().
    """
    _keys(original, {"path", "sha256"}, "original")
    _rel(original["path"])
    data = read(original["path"])
    need(sha(data) == original["sha256"], "original hash mismatch")
    with _source(data, original["path"]) as src:
        total = 0
        for entry in text_page_map(mapping, src.count):
            number = entry["page"]
            text = src.info(number)["text"] if src.kind == "pdf" else ""
            if "start" in entry:
                native = text.encode("utf-8")
                need(entry["end"] <= len(native), "native UTF-8 span outside original page text")
                try:
                    text = native[entry["start"]:entry["end"]].decode("utf-8")
                except UnicodeError as exc:
                    raise VisualError("native span splits a UTF-8 character") from exc
            chunk = _text_chunk(src.kind, number, text)
            total += len(chunk)
            need(total <= MAX_ASSET_BYTES, "native extraction byte budget exceeded")
            yield number, chunk


def _new_file(root, rel, data):
    path = safe_path(root, rel, missing=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return {"path": rel, "sha256": sha(data)}


def _result(root, rel, manifest, extra=None):
    result = {"manifest": rel, "manifest_sha256": sha(_read(root, rel, MAX_MANIFEST_BYTES)),
              "page_count": manifest["page_count"], "render_count": len(manifest["pages"]),
              "crop_count": len(manifest["crops"]), "contact_sheet_count": len(manifest["contact_sheets"]),
              "pending_review_count": sum(p["review"]["status"] == "pending" for p in manifest["pages"]),
              "note": "Rendering is preparation, not reading. Pending pages require actual visual review before admission."}
    if extra:
        result.update(extra)
    return result


def prepare(root, original, output, dpi=150):
    """Create a new visual directory; never replace original or existing output."""
    root = _root(root)
    need(not (root / "_meta/ingestion/baseline.json").exists(), "visual preparation requires scratch/draft root, not an admitted canonical wiki")
    _rel(original)
    _rel(output)
    _dpi(dpi)
    op = PurePosixPath(original)
    need(len(op.parts) == 5 and op.stem == "original" and op.suffix in EXTENSIONS, "original must be src/raw/assets/<slug>/original.<supported extension>")
    need(output == str(op.parent / "visual"), "output must be the same asset slug's visual directory")
    destination = safe_path(root, output, missing=True)
    need(not destination.exists(), "output already exists; refusing overwrite")
    original_bytes = _read(root, original, MAX_SOURCE_BYTES)
    original_hash = sha(original_bytes)
    created = False
    try:
        with _source(original_bytes, original) as src:
            destination.mkdir()  # Exclusive; existing even empty directories are rejected.
            created = True
            _, Image, ImageDraw = _deps()
            manifest = {"version": VERSION, "original": {"path": original, "sha256": original_hash},
                        "kind": src.kind, "page_count": src.count,
                        "render_method": PDF_METHOD if src.kind == "pdf" else IMAGE_METHOD,
                        "pages": [], "crops": [], "contact_sheets": []}
            total_pixels, total_output = 0, 0
            text_rel = str(PurePosixPath(output) / "extraction.txt")
            # Stream native extraction, rather than retaining an entire book's text.
            text_path = safe_path(root, text_rel, missing=True)
            with text_path.open("xb") as extraction:
                sheet = None
                try:
                    for number in range(1, src.count + 1):
                        info = src.info(number)
                        size = src.dimensions(number, dpi, info["bounds"])
                        total_pixels += size[0] * size[1]
                        need(total_pixels <= MAX_TOTAL_PIXELS, "aggregate pixel budget exceeded")
                        data, w, h = src.render(number, dpi, info["bounds"])
                        total_output += len(data)
                        need(total_output <= MAX_OUTPUT_BYTES, "output byte budget exceeded")
                        pin = _new_file(root, str(PurePosixPath(output) / f"page-{number:06d}.png"), data)
                        manifest["pages"].append({"page": number, "render": pin, "width": w, "height": h,
                                                  "dpi": dpi, "rotation": info["rotation"], "text_chars": len(info["text"]),
                                                  "triage": info["triage"], "review": {"status": "pending", "inspection": None,
                                                                                       "classification": [], "notes": ""}})
                        # Marker describes extraction state ONLY, not page content.
                        chunk_text = _text_chunk(src.kind, number, info["text"])
                        extraction.write(chunk_text)
                        total_output += len(chunk_text)
                        need(total_output <= MAX_OUTPUT_BYTES, "output byte budget exceeded")
                        offset = (number - 1) % (SHEET_COLUMNS * SHEET_ROWS)
                        if offset == 0:
                            sheet = Image.new("RGB", SHEET_SIZE, "white")
                        assert sheet is not None
                        with Image.open(io.BytesIO(data)) as im:
                            im.thumbnail((TILE_WIDTH, TILE_HEIGHT))
                            x = MARGIN + (offset % SHEET_COLUMNS) * (TILE_WIDTH + MARGIN)
                            y = MARGIN + (offset // SHEET_COLUMNS) * (TILE_HEIGHT + LABEL_HEIGHT + MARGIN)
                            ImageDraw.Draw(sheet).text((x, y), f"Page {number}" if src.kind == "pdf" else f"Frame {number}", fill="black")
                            sheet.paste(im, (x, y + LABEL_HEIGHT))
                        if offset == SHEET_COLUMNS * SHEET_ROWS - 1 or number == src.count:
                            index = len(manifest["contact_sheets"]) + 1
                            stream = io.BytesIO()
                            sheet.save(stream, format="PNG")
                            sheet_bytes = stream.getvalue()
                            total_output += len(sheet_bytes)
                            need(total_output <= MAX_OUTPUT_BYTES, "output byte budget exceeded")
                            pin = _new_file(root, str(PurePosixPath(output) / f"contact-{index:04d}.png"), sheet_bytes)
                            start = (index - 1) * SHEET_COLUMNS * SHEET_ROWS + 1
                            manifest["contact_sheets"].append({"render": pin, "pages": list(range(start, number + 1))})
                            sheet.close()
                            sheet = None
                    extraction.flush()
                    os.fsync(extraction.fileno())
                finally:
                    if sheet is not None:
                        sheet.close()
        need(sha(_read(root, original, MAX_SOURCE_BYTES)) == original_hash, "original changed during preparation")
        validate(manifest, lambda p: _read(root, p), require_review=False)
        manifest_rel = str(PurePosixPath(output) / "manifest.json")
        _new_file(root, manifest_rel, _serialize(manifest))
        # Read exact target back: exercise serialized schema and pins too.
        validate(_json(_read(root, manifest_rel, MAX_MANIFEST_BYTES)), lambda p: _read(root, p), require_review=False)
        need(sha(_read(root, original, MAX_SOURCE_BYTES)) == original_hash, "original changed during preparation")
        return _result(root, manifest_rel, manifest, {"original_sha256_before": original_hash,
                       "original_sha256_after": original_hash,
                       "extraction": {"path": text_rel, "sha256": sha(_read(root, text_rel))}})
    except Exception as exc:
        if created:
            shutil.rmtree(destination)
        if isinstance(exc, VisualError):
            raise
        raise VisualError(f"preparation failed: {exc}") from exc


def _manifest_location(path):
    path = Path(path).absolute()
    need(".." not in path.parts and len(path.parts) >= 7, "unsafe manifest path")
    rel = PurePosixPath(*path.parts[-6:]).as_posix()
    _rel(rel)
    parts = PurePosixPath(rel).parts
    need(parts[4:] == ("visual", "manifest.json"), "manifest must be src/raw/assets/<slug>/visual/manifest.json")
    root = _root(path.parents[5])
    safe_path(root, rel)
    return root, rel


def crop(manifest_path, page, bbox, crop_id, dpi=300):
    """Append a crop PNG and atomically update only the existing manifest.

    Previously created PNGs, extraction and original remain byte-identical.
    Failed operations remove only their newly created crop. Existing files and
    crop IDs are never overwritten. Fractional image bounds cover whole pixels
    with floor(left/top), ceil(right/bottom); PDF clipping is at requested DPI.
    """
    root, rel = _manifest_location(manifest_path)
    need(not (root / "_meta/ingestion/baseline.json").exists(), "cropping requires scratch/draft root; published raw manifests are immutable")
    _slug(crop_id)
    _dpi(dpi)
    before_manifest = _read(root, rel, MAX_MANIFEST_BYTES)
    manifest = _json(before_manifest)
    validate(manifest, lambda p: _read(root, p), require_review=False)
    number = _integer(page, "crop page", 1, manifest["page_count"])
    need(all(c["id"] != crop_id for c in manifest["crops"]), "crop id already exists")
    original = manifest["original"]
    original_bytes = _read(root, original["path"], MAX_SOURCE_BYTES)
    need(sha(original_bytes) == original["sha256"], "original hash mismatch")
    with _source(original_bytes, original["path"]) as src:
        info = src.info(number)
        bounds = _bbox(bbox, info["bounds"])
        data, w, h = src.render(number, dpi, bounds)
        units = "pdf-points" if src.kind == "pdf" else "image-pixels"
    crop_rel = str(PurePosixPath(rel).parent / f"crop-{crop_id}.png")
    target = safe_path(root, crop_rel, missing=True)
    need(not target.exists(), "crop output already exists")
    pin = _new_file(root, crop_rel, data)
    temp_name = None
    committed = False
    try:
        manifest["crops"].append({"id": crop_id, "page": number, "bbox": bounds, "units": units,
                                  "render": pin, "width": w, "height": h, "dpi": dpi})
        validate(manifest, lambda p: _read(root, p), require_review=False)
        need(_read(root, rel, MAX_MANIFEST_BYTES) == before_manifest, "manifest changed concurrently")
        need(sha(_read(root, original["path"], MAX_SOURCE_BYTES)) == original["sha256"], "original changed during cropping")
        fd, temp_name = tempfile.mkstemp(prefix=".visual-", dir=safe_path(root, rel).parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write(_serialize(manifest))
            stream.flush()
            os.fsync(stream.fileno())
        safe_path(root, rel)
        os.replace(temp_name, root / rel)
        temp_name = None
        committed = True
        validate(_json(_read(root, rel, MAX_MANIFEST_BYTES)), lambda p: _read(root, p), require_review=False)
        need(sha(_read(root, original["path"], MAX_SOURCE_BYTES)) == original["sha256"], "original changed during cropping")
        return _result(root, rel, manifest, {"crop": manifest["crops"][-1],
                       "original_sha256_before": original["sha256"], "original_sha256_after": original["sha256"]})
    except Exception as exc:
        if not committed:
            target.unlink()
        if isinstance(exc, VisualError):
            raise
        raise VisualError(f"crop failed: {exc}") from exc
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def prepare_packet(manifest_path, output, regions, **limits):
    from visual_packet import prepare_packet as prepare
    return prepare(manifest_path, output, regions, **limits)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare", help="render all pages/frames, initially pending visual review")
    p.add_argument("--root", required=True)
    p.add_argument("--original", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--dpi", type=int, default=150)
    c = commands.add_parser("crop", help="append a bounded crop without overwriting existing images")
    c.add_argument("--manifest", required=True)
    c.add_argument("--page", required=True, type=int)
    c.add_argument("--bbox", required=True, nargs=4, type=float)
    c.add_argument("--id", required=True, dest="crop_id")
    c.add_argument("--dpi", type=int, default=300)
    v = commands.add_parser("validate", help="validate source/pins and require completed reviews")
    v.add_argument("--manifest", required=True)
    v.add_argument("--allow-pending", action="store_true", help="operational check only; not admission")
    packet = commands.add_parser("packet", help="private context/detail payloads; never transport attestation")
    packet.add_argument("--manifest", required=True)
    packet.add_argument("--output", required=True)
    packet.add_argument("--regions", required=True, help="JSON region array in stored-page raster pixels")
    packet.add_argument("--max-edge", type=int, default=1568)
    packet.add_argument("--overlap", type=int, default=64)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.root, args.original, args.output, args.dpi)
        elif args.command == "crop":
            result = crop(args.manifest, args.page, args.bbox, args.crop_id, args.dpi)
        elif args.command == "packet":
            regions = _json(Path(args.regions).read_bytes())
            result = prepare_packet(args.manifest, args.output, regions,
                                    max_edge=args.max_edge, overlap=args.overlap)
        else:
            root, rel = _manifest_location(args.manifest)
            manifest = _json(_read(root, rel, MAX_MANIFEST_BYTES))
            validate(manifest, lambda p: _read(root, p), require_review=not args.allow_pending)
            result = {"valid": True, "admission_review_required": not args.allow_pending,
                      "manifest": rel, "manifest_sha256": sha(_read(root, rel, MAX_MANIFEST_BYTES))}
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (VisualError, OSError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
