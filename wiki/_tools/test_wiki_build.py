from __future__ import annotations

import fcntl
import importlib.util
import os
import tempfile
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).with_name("wiki_build.py")
SPEC = importlib.util.spec_from_file_location("wiki_build_tested", SCRIPT)
assert SPEC and SPEC.loader
wiki_build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wiki_build)


def test_nonblocking_build_lock_rejects_concurrent_owner(tmp_path):
    lock_path = tmp_path / "wiki.lock"
    lock_path.touch()
    with lock_path.open("w") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX)
        with mock.patch.object(wiki_build, "LOCK_FILE", lock_path):
            try:
                with wiki_build.build_lock(nonblocking=True):
                    raise AssertionError("concurrent lock was acquired")
            except BlockingIOError:
                pass


def test_promote_replaces_complete_dist_and_removes_backup(tmp_path):
    dist = tmp_path / "dist"
    staging = Path(tempfile.mkdtemp(prefix=".dist.build.", dir=tmp_path))
    dist.mkdir()
    (dist / "index.html").write_text("old", encoding="utf-8")
    (staging / "index.html").write_text("new", encoding="utf-8")

    with mock.patch.object(wiki_build, "WIKI_ROOT", tmp_path), mock.patch.object(wiki_build, "DIST", dist):
        wiki_build.promote(staging)

    assert (dist / "index.html").read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(".dist.previous.*"))


def test_failed_promotion_rolls_back_previous_dist(tmp_path):
    dist = tmp_path / "dist"
    staging = Path(tempfile.mkdtemp(prefix=".dist.build.", dir=tmp_path))
    dist.mkdir()
    (dist / "index.html").write_text("old", encoding="utf-8")
    (staging / "index.html").write_text("new", encoding="utf-8")
    real_replace = os.replace
    calls = 0

    def fail_staging_promotion(src, dst):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected promotion failure")
        return real_replace(src, dst)

    with mock.patch.object(wiki_build, "WIKI_ROOT", tmp_path), mock.patch.object(wiki_build, "DIST", dist), mock.patch.object(wiki_build.os, "replace", side_effect=fail_staging_promotion):
        try:
            wiki_build.promote(staging)
        except OSError as exc:
            assert "injected" in str(exc)
        else:
            raise AssertionError("promotion failure was not raised")

    assert (dist / "index.html").read_text(encoding="utf-8") == "old"
