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


def test_build_lock_rejects_symlink_without_truncating_target(tmp_path):
    target = tmp_path / "protected.txt"
    target.write_text("preserve me\n", encoding="utf-8")
    lock_path = tmp_path / "wiki.lock"
    lock_path.symlink_to(target)

    with mock.patch.object(wiki_build, "LOCK_FILE", lock_path):
        try:
            with wiki_build.build_lock():
                pass
        except OSError:
            pass
        else:
            raise AssertionError("wiki build lock should reject a symlink")

    assert target.read_text(encoding="utf-8") == "preserve me\n"


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


def test_failed_promotion_keeps_previous_dist(tmp_path):
    dist = tmp_path / "dist"
    staging = Path(tempfile.mkdtemp(prefix=".dist.build.", dir=tmp_path))
    dist.mkdir()
    (dist / "index.html").write_text("old", encoding="utf-8")
    (staging / "index.html").write_text("new", encoding="utf-8")
    with mock.patch.object(wiki_build, "WIKI_ROOT", tmp_path), mock.patch.object(wiki_build, "DIST", dist), mock.patch.object(wiki_build, "exchange_dirs", side_effect=OSError("injected exchange failure")):
        try:
            wiki_build.promote(staging)
        except OSError as exc:
            assert "injected" in str(exc)
        else:
            raise AssertionError("promotion failure was not raised")
    assert (dist / "index.html").read_text(encoding="utf-8") == "old"


def test_build_requires_gate_before_any_generator(tmp_path):
    import pytest
    from ingestion_gate import GateError
    (tmp_path / "src").mkdir()
    with mock.patch.object(wiki_build, "WIKI_ROOT", tmp_path), mock.patch.object(wiki_build, "run") as run:
        with pytest.raises(GateError, match="baseline"):
            wiki_build.build()
    run.assert_not_called()


def test_snapshot_is_independent_and_input_drift_is_rejected(tmp_path):
    import pytest
    import ingestion_gate as gate
    (tmp_path / "src/concepts").mkdir(parents=True)
    (tmp_path / "src/concepts/a.md").write_text("Reviewed baseline bytes.")
    (tmp_path / ".vitepress/plugins").mkdir(parents=True)
    (tmp_path / ".vitepress/config.ts").write_text("Snapshot config.")
    (tmp_path / "node_modules").mkdir()
    gate.initialize(tmp_path)
    with mock.patch.object(wiki_build, "WIKI_ROOT", tmp_path):
        before = gate.inventory(tmp_path, build_inputs=True)
        with wiki_build.source_snapshot(before) as snapshot:
            assert (snapshot / "src/concepts/a.md").read_text() == "Reviewed baseline bytes."
            assert not (snapshot / "src").is_symlink()
            (tmp_path / "src/concepts/a.md").write_text("Concurrent live edit.")
            assert (snapshot / "src/concepts/a.md").read_text() == "Reviewed baseline bytes."
            with pytest.raises(gate.GateError, match="changed"):
                wiki_build.require_unchanged(before)


def build_fixture(tmp_path):
    import ingestion_gate as gate
    (tmp_path / "src/concepts").mkdir(parents=True)
    (tmp_path / "src/concepts/a.md").write_text("Frozen baseline.")
    (tmp_path / "public/assets").mkdir(parents=True)
    (tmp_path / "public/wiki-graph.json").write_text("{}")
    (tmp_path / ".vitepress").mkdir()
    (tmp_path / "node_modules/katex/dist/fonts").mkdir(parents=True)
    (tmp_path / "node_modules/katex/dist/katex.min.css").write_text("css")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist/index.html").write_text("Previous release.")
    gate.initialize(tmp_path)


def fake_commands(command, root):
    if command[0] == "node" and command[1].endswith("gen-sidebar.mjs"):
        (root / ".vitepress/_sidebar-generated.mjs").write_text("export const generatedSidebar=[]")
    if command[0].endswith("/vitepress"):
        (root / "dist").mkdir()
        (root / "dist/index.html").write_text((root / "src/concepts/a.md").read_text())


def test_unreviewed_build_leaves_previous_site_intact(tmp_path):
    import pytest
    import ingestion_gate as gate
    build_fixture(tmp_path)
    (tmp_path / "src/concepts/a.md").write_text("Unreviewed edit.")
    with mock.patch.object(wiki_build, "WIKI_ROOT", tmp_path), mock.patch.object(wiki_build, "DIST", tmp_path / "dist"), mock.patch.object(wiki_build, "run") as run:
        with pytest.raises(gate.GateError, match="unreviewed"):
            wiki_build.build()
    run.assert_not_called()
    assert (tmp_path / "dist/index.html").read_text() == "Previous release."


def test_changes_during_generation_never_leak_into_release(tmp_path):
    import pytest
    import ingestion_gate as gate
    build_fixture(tmp_path)
    def changing_command(command, root):
        fake_commands(command, root)
        if command[0] == "node" and command[1].endswith("gen-semantic-graph.mjs"):
            (tmp_path / "src/concepts/a.md").write_text("Concurrent live edit.")
    with mock.patch.object(wiki_build, "WIKI_ROOT", tmp_path), mock.patch.object(wiki_build, "DIST", tmp_path / "dist"), mock.patch.object(wiki_build, "run", side_effect=changing_command):
        with pytest.raises(gate.GateError, match="changed"):
            wiki_build.build()
    assert (tmp_path / "dist/index.html").read_text() == "Previous release."


def test_snapshot_mutation_and_asset_copy_drift_abort(tmp_path):
    import pytest
    import ingestion_gate as gate
    build_fixture(tmp_path)
    original_copy = wiki_build.copy_public_artifacts
    def changed_copy(staging, root):
        original_copy(staging, root)
        (root / "src/concepts/a.md").write_text("Unexpected generator mutation.")
    with mock.patch.object(wiki_build, "WIKI_ROOT", tmp_path), mock.patch.object(wiki_build, "DIST", tmp_path / "dist"), mock.patch.object(wiki_build, "run", side_effect=fake_commands), mock.patch.object(wiki_build, "copy_public_artifacts", side_effect=changed_copy):
        with pytest.raises(gate.GateError, match="frozen"):
            wiki_build.build()
    assert (tmp_path / "dist/index.html").read_text() == "Previous release."


def test_successful_build_publishes_only_snapshot_and_manifest(tmp_path):
    import json
    build_fixture(tmp_path)
    with mock.patch.object(wiki_build, "WIKI_ROOT", tmp_path), mock.patch.object(wiki_build, "DIST", tmp_path / "dist"), mock.patch.object(wiki_build, "run", side_effect=fake_commands):
        wiki_build.build()
    assert (tmp_path / "dist/index.html").read_text() == "Frozen baseline."
    manifest = json.loads((tmp_path / "dist/release-manifest.json").read_text())
    assert manifest["legacy_unreviewed_files"] == 1
    assert manifest["reviewed_files"] == 0
    assert not (tmp_path / "dist/_meta/ingestion").exists()


def test_autobuild_watches_receipts_and_ignores_generated_churn(tmp_path):
    script = Path("/home/hermes/.hermes/scripts/wiki_autobuild.py")
    spec = importlib.util.spec_from_file_location("autobuild_tested", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("src/concepts", "public/semantic", "_meta/ingestion/records", ".vitepress"):
        (tmp_path / name).mkdir(parents=True)
    (tmp_path / "src/concepts/a.md").write_text("Baseline.")
    (tmp_path / "public/semantic/graph.json").write_text("{}")
    paths = [tmp_path / "src", tmp_path / "public", tmp_path / "_meta/ingestion", tmp_path / ".vitepress"]
    with mock.patch.object(module, "WIKI", tmp_path), mock.patch.object(module, "WATCH_PATHS", paths):
        original = module.snapshot()["digest"]
        (tmp_path / "public/semantic/graph.json").write_text('{"generated": 2}')
        (tmp_path / ".vitepress/_sidebar-generated.mjs").write_text("generated")
        assert module.snapshot()["digest"] == original
        (tmp_path / "_meta/ingestion/records/a.json").write_text('{"state": "reviewed"}')
        assert module.snapshot()["digest"] != original
        assert not any(".ingest-work" in p["path"] for p in module.snapshot()["files"])
        for rel in ("public/semantic/extra.json", "src/__pycache__/hidden.md", "public/assets/fonts/extra.html",
                    "src/cache/evidence.md", "src/.cache/evidence.md", "src/raw/assets/evidence.tmp", "src/concepts/evidence.md~"):
            path = tmp_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            previous = module.snapshot()["digest"]
            path.write_text("Must trigger admission and build checks.")
            assert module.snapshot()["digest"] != previous


def test_postcommit_fsync_or_cleanup_failure_reports_published(tmp_path, capsys):
    import pytest
    for operation in ("fsync_directory", "cleanup"):
        root = tmp_path / operation
        root.mkdir()
        dist, staging = root / "dist", root / "staged"
        dist.mkdir()
        staging.mkdir()
        (dist / "index.html").write_text("Old release")
        (staging / "index.html").write_text("New release")
        target = mock.patch.object(wiki_build, "fsync_directory", side_effect=OSError("injected durability failure")) if operation == "fsync_directory" else mock.patch.object(wiki_build.shutil, "rmtree", side_effect=OSError("injected cleanup failure"))
        with mock.patch.object(wiki_build, "DIST", dist), target:
            with pytest.raises(wiki_build.PublishedWarning, match="new release is live"):
                wiki_build.promote(staging)
        assert (dist / "index.html").read_text() == "New release"
    with mock.patch.object(wiki_build, "LOCK_FILE", tmp_path / "lock"), mock.patch.object(wiki_build, "build", side_effect=wiki_build.PublishedWarning("new release is live")):
        assert wiki_build.main([]) == 2
    error = capsys.readouterr().err
    assert "Published with warning" in error
    assert "Refused publication" not in error


def test_snapshot_preserves_source_cache_named_evidence(tmp_path):
    import ingestion_gate as gate
    build_fixture(tmp_path)
    hidden = tmp_path / "src/__pycache__/source.md"
    hidden.parent.mkdir()
    hidden.write_text("Historical evidence, not Python bytecode.")
    # Build a new isolated fixture baseline, never production state.
    (tmp_path / "_meta/ingestion/baseline.json").unlink()
    gate.initialize(tmp_path)
    with mock.patch.object(wiki_build, "WIKI_ROOT", tmp_path):
        with wiki_build.source_snapshot(gate.inventory(tmp_path, build_inputs=True)) as snapshot:
            assert (snapshot / "src/__pycache__/source.md").read_bytes() == hidden.read_bytes()


def test_autobuild_warns_without_claiming_publication_failed(tmp_path, capsys):
    script = Path("/home/hermes/.hermes/scripts/wiki_autobuild.py")
    spec = importlib.util.spec_from_file_location("warning_autobuild", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    snap = {"digest": "fixture", "file_count": 1}
    with mock.patch.object(module, "WIKI", tmp_path), mock.patch.object(module, "snapshot", return_value=snap), mock.patch.object(module, "load_state", return_value={}), mock.patch.object(module, "run_build", return_value=2), mock.patch.object(module, "save_state") as save, mock.patch("sys.argv", ["wiki_autobuild.py"]):
        assert module.main() == 2
        save.assert_called_once_with(snap, "published-warning")
    output = capsys.readouterr()
    assert "Published with durability/cleanup warning" in output.err
    assert "Build failed" not in output.err
