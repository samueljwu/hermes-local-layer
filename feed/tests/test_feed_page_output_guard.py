from __future__ import annotations

import importlib.util
from pathlib import Path


RENDERER = Path('/home/hermes/feed/_tools/render_feed_page.py')


def load_renderer():
    spec = importlib.util.spec_from_file_location('render_feed_page_guard_test', RENDERER)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_render_rejects_noncanonical_output_path(tmp_path):
    renderer = load_renderer()
    output = tmp_path / 'index.html'

    try:
        renderer.render_to_file(output_path=output, locked=True)
    except RuntimeError as exc:
        assert 'outside canonical output' in str(exc)
    else:  # pragma: no cover
        raise AssertionError('renderer should reject noncanonical output path')
    assert not output.exists()


def test_render_rejects_symlinked_canonical_output_file(monkeypatch, tmp_path):
    renderer = load_renderer()
    canonical_dir = tmp_path / 'feed'
    canonical_dir.mkdir()
    canonical_file = canonical_dir / 'index.html'
    target = tmp_path / 'target.html'
    target.write_text('do not overwrite', encoding='utf-8')
    canonical_file.symlink_to(target)

    monkeypatch.setattr(renderer, 'OUTPUT_PATH', canonical_file)
    monkeypatch.setattr(renderer, 'HISTORY_PATH', tmp_path / 'history.json')

    try:
        renderer.render_to_file(history_path=renderer.HISTORY_PATH, output_path=canonical_file, locked=True)
    except RuntimeError as exc:
        assert 'symlinked feed output file' in str(exc)
    else:  # pragma: no cover
        raise AssertionError('renderer should reject symlinked canonical output file')
    assert target.read_text(encoding='utf-8') == 'do not overwrite'


def test_render_rejects_symlinked_ancestor_without_creating_output_dir(monkeypatch, tmp_path):
    renderer = load_renderer()
    real_target = tmp_path / 'escaped-target'
    real_target.mkdir()
    symlinked_dist = tmp_path / 'dist'
    symlinked_dist.symlink_to(real_target, target_is_directory=True)
    canonical_file = symlinked_dist / 'feed' / 'index.html'

    monkeypatch.setattr(renderer, 'OUTPUT_PATH', canonical_file)
    monkeypatch.setattr(renderer, 'HISTORY_PATH', tmp_path / 'history.json')

    try:
        renderer.render_to_file(history_path=renderer.HISTORY_PATH, output_path=canonical_file, locked=True)
    except RuntimeError as exc:
        assert 'symlinked output ancestor' in str(exc) or 'non-canonical feed output ancestor' in str(exc)
    else:  # pragma: no cover
        raise AssertionError('renderer should reject symlinked output ancestor')
    assert not (real_target / 'feed').exists()
