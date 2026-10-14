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


def test_html_image_alias_uses_rendered_source_and_exact_bytes(tmp_path):
    import ingestion_gate as gate
    source = tmp_path / 'public/assets/explanation.png'
    alias = tmp_path / 'dist/assets/rendered.png'
    source.parent.mkdir(parents=True)
    alias.parent.mkdir(parents=True)
    source.write_bytes(b'illustration bytes')
    alias.write_bytes(source.read_bytes())
    html = tmp_path / 'dist/index.html'
    html.write_text('<img src="/wiki/assets/rendered.png">')
    expected = {'public/assets/explanation.png': gate.sha(source.read_bytes())}
    aliases = {'assets/rendered.png': {'source': 'public/assets/explanation.png', 'sha256': expected['public/assets/explanation.png']}}
    assert wiki_build.validate_public_asset_aliases(tmp_path, tmp_path / 'dist', expected, aliases) == {
        'assets/rendered.png': expected['public/assets/explanation.png']}
    html.write_text('<p>no rendered image</p>')
    try:
        wiki_build.validate_public_asset_aliases(tmp_path, tmp_path / 'dist', expected, aliases)
    except gate.GateError as exc:
        assert 'not a current rendered image' in str(exc)
    else:
        raise AssertionError('unreferenced asset was admitted')


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


def fixture_baseline(root):
    import json
    import ingestion_gate as gate
    path = root / '_meta/ingestion/baseline.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    files = gate.inventory(root)
    path.write_text(json.dumps({'version': 1, 'status': 'legacy-unreviewed',
                                'files': files, 'files_sha256': gate.sha(gate.canonical(files))}))


def test_snapshot_is_independent_and_input_drift_is_rejected(tmp_path):
    import pytest
    import ingestion_gate as gate
    (tmp_path / "src/concepts").mkdir(parents=True)
    (tmp_path / "src/concepts/a.md").write_text("Reviewed baseline bytes.")
    (tmp_path / ".vitepress/plugins").mkdir(parents=True)
    (tmp_path / ".vitepress/config.ts").write_text("Snapshot config.")
    (tmp_path / "node_modules").mkdir()
    fixture_baseline(tmp_path)
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
    fixture_baseline(tmp_path)


def fake_commands(command, root):
    if command[0] == "node" and command[1].endswith("gen-sidebar.mjs"):
        (root / ".vitepress/_sidebar-generated.mjs").write_text("export const generatedSidebar=[]")
    if command[0].endswith("/vitepress"):
        (root / "dist").mkdir()
        # Deliberately mocked compiler; real native mapping proof is in lifecycle tests.
        (root / "dist/.vite").mkdir()
        (root / "dist/.vite/manifest.json").write_text("{}")
        (root / "dist/index.html").write_text((root / "src/concepts/a.md").read_text())


def test_bad_link_or_unsourced_edit_leaves_previous_site_intact(tmp_path):
    import shutil
    import subprocess
    import pytest
    import ingestion_gate as gate
    for mode in ('link', 'unsourced'):
        root = tmp_path / mode
        chronology_fixture(root)
        shutil.copy2(Path(__file__).parents[1] / '.vitepress/validate-wiki-links.mjs',
                     root / '.vitepress/validate-wiki-links.mjs')
        if mode == 'link':
            index = root / 'src/index.md'
            index.write_text(index.read_text() + '\n- [[missing]] — Broken link.\n')
        else:
            (root / 'src/concepts/a.md').write_text('Uncited material claim.\n')
        assert gate.check(root)['ok']  # No receipt gate for direct edits.
        commands = []
        def run(command, snapshot):
            commands.append(command)
            if 'validate' in command or any(str(arg).endswith('validate-wiki-links.mjs') for arg in command):
                subprocess.run(command, cwd=snapshot, check=True, capture_output=True, text=True, timeout=15)
            else:
                fake_commands(command, snapshot)
        with mock.patch.object(wiki_build, 'WIKI_ROOT', root), mock.patch.object(wiki_build, 'DIST', root / 'dist'), mock.patch.object(wiki_build, 'run', side_effect=run):
            with pytest.raises(subprocess.CalledProcessError):
                wiki_build.build()
        assert not any(command[0].endswith('/vitepress') for command in commands)
        assert (root / 'dist/index.html').read_text() == 'Previous release.'


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
    commands = []
    def recorded(command, root):
        commands.append(command)
        fake_commands(command, root)
    with mock.patch.object(wiki_build, "WIKI_ROOT", tmp_path), mock.patch.object(wiki_build, "DIST", tmp_path / "dist"), mock.patch.object(wiki_build, "run", side_effect=recorded):
        wiki_build.build()
    assert sum(command[-1] == 'validate' for command in commands) == 1
    assert not any('chronology-audit' in command for command in commands)
    for script in ('gen-sidebar.mjs', 'validate-wiki-links.mjs', 'gen-semantic-graph.mjs', 'validate-semantic-relationships.mjs'):
        assert sum(any(arg.endswith(script) for arg in command) for command in commands) == 1
    validation = next(i for i, command in enumerate(commands) if command[-1] == 'validate')
    compilation = next(i for i, command in enumerate(commands) if command[0].endswith('/vitepress'))
    assert validation < compilation
    assert (tmp_path / "dist/index.html").read_text() == "Frozen baseline."
    manifest = json.loads((tmp_path / "dist/release-manifest.json").read_text())
    assert manifest["legacy_unreviewed_files"] == 1
    assert manifest["reviewed_files"] == 0
    assert not (tmp_path / "dist/_meta/ingestion").exists()


def chronology_fixture(root, fault=None):
    import json
    import shutil
    import ingestion_gate as gate
    build_fixture(root)
    tools = root / '_tools'
    tools.mkdir()
    for name in ('wiki_ops.py', 'ingestion_gate.py'):
        shutil.copy2(Path(__file__).with_name(name), tools / name)
    (root / 'src/index.md').write_text('## Concepts\n- [[concepts/a]] — TEST-only model.\n')
    semantic = root / 'src/_meta/semantic'
    semantic.mkdir(parents=True)
    (semantic / 'graph.json').write_text('{}')
    raw = root / 'src/raw/notes/source.md'
    raw.parent.mkdir(parents=True)
    original = root / '.private-sources/source/original.txt'
    original.parent.mkdir(parents=True)
    original.write_text('Original source supplied by user.\n')
    raw.write_text('---\npublished: undated\nprivate_original: .private-sources/source/original.txt\nprivate_original_sha256: '
                   + gate.sha(original.read_bytes()) + '\n---\nSource date: undated\nTEST-only source.\n')
    (root / 'src/concepts/a.md').write_text('TEST-only model. [Source](../raw/notes/source.md).\n')
    ledger = {'curated': {'concepts/a.md': gate.sha((root / 'src/concepts/a.md').read_bytes())},
              'grandfathered_raw': {}, 'reviewed_raw': {'raw/notes/source.md': {'sha256': gate.sha(raw.read_bytes()), 'ingest_id': 'TEST-only'}}}
    if fault == 'absent':
        ledger['curated'] = {}
    elif fault == 'stale':
        ledger['curated']['concepts/a.md'] = '0' * 64

    (root / 'src/_meta/chronology-audit.json').write_text(json.dumps(ledger))
    (root / '_meta/ingestion/baseline.json').unlink()
    fixture_baseline(root)
    if fault == 'missing-raw':
        raw.unlink()
    elif fault == 'moved-raw':
        raw.rename(raw.with_name('moved.md'))
    elif fault == 'raw-status':
        raw.write_text(raw.read_text().replace('published: undated', 'published: 2026-01-02'))


def test_actual_chronology_validation_and_build_boundary(tmp_path):
    import subprocess
    import sys
    import pytest
    import ingestion_gate as gate
    for fault in (None, 'absent', 'stale', 'missing-raw', 'moved-raw', 'raw-status'):
        root = tmp_path / (fault or 'valid')
        chronology_fixture(root, fault)
        ledger = (root / 'src/_meta/chronology-audit.json').read_bytes()
        validation = subprocess.run([sys.executable, '-B', str(root / '_tools/wiki_ops.py'), 'validate'],
                                    capture_output=True, text=True, timeout=15)
        if fault in ('absent', 'stale'):
            assert validation.returncode == 0, validation.stdout + validation.stderr
        if fault is None:
            assert validation.returncode == 0, validation.stdout + validation.stderr
        commands = []
        def run(command, snapshot):
            commands.append(command)
            if 'validate' in command:
                # Real copied validator uses snapshot-local ROOT and every derived global.
                subprocess.run(command, cwd=snapshot, check=True, capture_output=True, text=True, timeout=15)
            else:
                fake_commands(command, snapshot)
        with mock.patch.object(wiki_build, 'WIKI_ROOT', root), mock.patch.object(wiki_build, 'DIST', root / 'dist'), mock.patch.object(wiki_build, 'run', side_effect=run), mock.patch.object(wiki_build, 'promote', wraps=wiki_build.promote) as promote:
            if fault in ('missing-raw', 'moved-raw', 'raw-status'):
                with pytest.raises(gate.GateError, match='immutable raw evidence'):
                    wiki_build.build()
                promote.assert_not_called()
                assert not any(command[0].endswith('/vitepress') for command in commands)
                assert (root / 'dist/index.html').read_text() == 'Previous release.'
            else:
                wiki_build.build()
                promote.assert_called_once()
                assert (root / 'dist/index.html').read_text() == (root / 'src/concepts/a.md').read_text()
        assert (root / 'src/_meta/chronology-audit.json').read_bytes() == ledger


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
