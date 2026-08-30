import importlib.util
import os
from pathlib import Path

MODULE_PATH = Path(__file__).with_name('journal_ops.py')
spec = importlib.util.spec_from_file_location('journal_ops', MODULE_PATH)
journal_ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(journal_ops)


def configure_tmp_paths(tmp_path):
    root = tmp_path / 'journal'
    (root / '_meta').mkdir(parents=True)
    journal_ops.JOURNAL_ROOT = root
    journal_ops.REGISTRY_PATH = root / '_meta' / 'entry_registry.json'
    journal_ops.SCHEMA_PATH = root / 'SCHEMA.md'
    journal_ops.INDEX_PATH = root / 'index.md'
    journal_ops.LOG_PATH = root / 'log.md'
    journal_ops.LOCK_PATH = root / '_meta' / '.journal_ops.lock'
    return root


def test_journal_lock_rejects_symlink_without_truncating_target(tmp_path):
    configure_tmp_paths(tmp_path)
    target = tmp_path / 'protected.txt'
    target.write_text('preserve me\n', encoding='utf-8')
    journal_ops.LOCK_PATH.symlink_to(target)

    try:
        with journal_ops.file_lock():
            pass
    except OSError:
        pass
    else:
        raise AssertionError('journal lock should reject a symlink')

    assert target.read_text(encoding='utf-8') == 'preserve me\n'


def test_journal_lock_rejects_symlinked_meta_parent_without_creating_external_lock(tmp_path):
    root = configure_tmp_paths(tmp_path)
    external = tmp_path / 'external-meta'
    external.mkdir()
    (root / '_meta').rmdir()
    (root / '_meta').symlink_to(external, target_is_directory=True)

    try:
        with journal_ops.file_lock():
            pass
    except OSError:
        pass
    else:
        raise AssertionError('journal lock should reject a symlinked parent')

    assert not (external / '.journal_ops.lock').exists()


def test_add_rejects_symlinked_tag_parent_before_any_state_change(tmp_path):
    root = configure_tmp_paths(tmp_path)
    journal_ops.write_registry([])
    journal_ops.INDEX_PATH.write_text('original index\n', encoding='utf-8')
    journal_ops.LOG_PATH.write_text('original log\n', encoding='utf-8')
    external = tmp_path / 'external-tag'
    external.mkdir()
    victim = external / 'J1.md'
    victim.write_text('external target\n', encoding='utf-8')
    (root / 'ideas').symlink_to(external, target_is_directory=True)
    before = {
        'registry': journal_ops.REGISTRY_PATH.read_bytes(),
        'index': journal_ops.INDEX_PATH.read_bytes(),
        'log': journal_ops.LOG_PATH.read_bytes(),
        'victim': victim.read_bytes(),
    }

    try:
        journal_ops.add_entry('Unsafe add', 'raw', 'clean', tag='ideas', entry_date='2026-08-29')
    except (OSError, ValueError):
        pass
    else:
        raise AssertionError('symlinked generated-entry parent should be rejected')

    assert journal_ops.REGISTRY_PATH.read_bytes() == before['registry']
    assert journal_ops.INDEX_PATH.read_bytes() == before['index']
    assert journal_ops.LOG_PATH.read_bytes() == before['log']
    assert victim.read_bytes() == before['victim']


def test_add_rejects_invalid_entry_before_registry_log_or_derived_writes(tmp_path):
    root = configure_tmp_paths(tmp_path)
    journal_ops.write_registry([])
    journal_ops.INDEX_PATH.write_text('original index\n', encoding='utf-8')
    journal_ops.LOG_PATH.write_text('original log\n', encoding='utf-8')

    try:
        journal_ops.add_entry('Invalid', 'raw', 'clean', tag='ideas', entry_date='not-a-date')
    except ValueError as exc:
        assert 'refusing invalid journal registry update' in str(exc)
    else:
        raise AssertionError('invalid journal entry was accepted')

    assert journal_ops.read_registry() == []
    assert journal_ops.INDEX_PATH.read_text(encoding='utf-8') == 'original index\n'
    assert journal_ops.LOG_PATH.read_text(encoding='utf-8') == 'original log\n'
    assert not (root / 'ideas').exists()


def test_validate_detects_index_count_mismatch():
    registry = [
        {'id': 'J1', 'title': 'A', 'tag': 'ideas', 'date': '2026-05-07', 'tags': [], 'status': 'active', 'original': 'a', 'entry': 'a', 'related': []},
        {'id': 'J2', 'title': 'B', 'tag': 'ideas', 'date': '2026-05-08', 'tags': [], 'status': 'archived', 'original': 'b', 'entry': 'b', 'related': []},
    ]
    index_text = '> Last updated: 2026-05-08 | Total entries: 2 | Active entries: 2\n'

    issues = journal_ops.validate_registry(registry, index_text=index_text)

    assert 'index active count 2 != registry active count 1' in issues


def test_build_index_separates_active_and_archived_entries():
    registry = [
        {'id': 'J1', 'title': 'Active idea', 'tag': 'ideas', 'date': '2026-05-07', 'tags': [], 'status': 'active', 'original': 'a', 'entry': 'a', 'related': []},
        {'id': 'J2', 'title': 'Old idea', 'tag': 'ideas', 'date': '2026-05-08', 'tags': [], 'status': 'archived', 'original': 'b', 'entry': 'b', 'related': ['J1']},
    ]

    text = journal_ops.build_index(registry, today='2026-05-09')

    assert 'Total entries: 2 | Active entries: 1' in text
    assert '- **J1** (2026-05-07) — Active idea' in text
    assert '- **J2** (2026-05-08) — Old idea — merged/related to J1' in text


def test_render_entry_preserves_original_and_cleaned_layers():
    entry = {'id': 'J4', 'title': 'Short thought', 'tag': 'ideas', 'date': '2026-05-09', 'tags': ['x'], 'status': 'active', 'original': 'raw words', 'entry': 'clean words', 'related': ['J1']}

    text = journal_ops.render_entry_markdown(entry)

    assert '## Original\nraw words' in text
    assert '## Cleaned entry\nclean words' in text
    assert 'Related: J1' in text


def test_regenerate_removes_stale_same_id_entry_in_wrong_folder(tmp_path):
    root = configure_tmp_paths(tmp_path)
    registry = [
        {'id': 'J1', 'title': 'A', 'tag': 'ideas', 'date': '2026-05-07', 'tags': [], 'status': 'active', 'original': 'a', 'entry': 'a', 'related': []},
    ]
    stale = root / 'research' / 'J1.md'
    stale.parent.mkdir(parents=True)
    stale.write_text('stale')

    journal_ops.regenerate_entries(registry, root=root)

    assert (root / 'ideas' / 'J1.md').exists()
    assert not stale.exists()


def test_stale_entry_deletion_survives_tag_parent_swap(tmp_path, monkeypatch):
    root = configure_tmp_paths(tmp_path)
    tag_dir = root / 'research'
    tag_dir.mkdir()
    stale = tag_dir / 'J9.md'
    stale.write_text('stale', encoding='utf-8')
    detached = tmp_path / 'detached-research'
    external = tmp_path / 'external-research'
    external.mkdir()
    victim = external / 'J9.md'
    victim.write_text('preserve me', encoding='utf-8')
    original_scandir = os.scandir
    descriptor_scans = 0

    def swap_before_tag_scan(path):
        nonlocal descriptor_scans
        if isinstance(path, int):
            descriptor_scans += 1
            if descriptor_scans == 2:
                tag_dir.rename(detached)
                tag_dir.symlink_to(external, target_is_directory=True)
        return original_scandir(path)

    monkeypatch.setattr(journal_ops.os, 'scandir', swap_before_tag_scan)
    journal_ops.regenerate_entries([], root=root)

    assert descriptor_scans >= 2
    assert victim.read_text(encoding='utf-8') == 'preserve me'
    assert not (detached / 'J9.md').exists()
    assert tag_dir.is_symlink()


def test_regenerate_rejects_symlinked_tag_directory_without_deleting_external_file(tmp_path):
    root = configure_tmp_paths(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    victim = outside / 'J9.md'
    victim.write_text('preserve me', encoding='utf-8')
    (root / 'linked').symlink_to(outside, target_is_directory=True)

    try:
        journal_ops.regenerate_entries([], root=root)
    except ValueError as exc:
        assert 'symlinked journal output parent' in str(exc)
    else:
        raise AssertionError('symlinked journal tag directory should be rejected')

    assert victim.read_text(encoding='utf-8') == 'preserve me'


def test_validate_detects_invalid_status_and_stale_entry_file(tmp_path):
    root = configure_tmp_paths(tmp_path)
    registry = [
        {'id': 'J1', 'title': 'A', 'tag': 'ideas', 'date': '2026-05-07', 'tags': [], 'status': 'draft', 'original': 'a', 'entry': 'a', 'related': []},
    ]
    stale = root / 'research' / 'J9.md'
    stale.parent.mkdir(parents=True)
    stale.write_text('stale')

    issues = journal_ops.validate_registry(registry, check_entries=True, root=root)

    assert any('unexpected status draft' in issue for issue in issues)
    assert any('stale or misplaced journal entry file' in issue for issue in issues)


def test_validate_detects_derived_entry_and_index_content_drift(tmp_path):
    root = configure_tmp_paths(tmp_path)
    registry = [
        {'id': 'J1', 'title': 'Canonical title', 'tag': 'ideas', 'date': '2026-05-07', 'tags': [], 'status': 'active', 'original': 'raw', 'entry': 'clean', 'related': []},
    ]
    journal_ops.regenerate_entries(registry, root=root)
    expected_index = journal_ops.build_index(registry)
    (root / 'index.md').write_text(expected_index, encoding='utf-8')
    (root / 'ideas' / 'J1.md').write_text('plausible but stale', encoding='utf-8')
    drifted_index = expected_index.replace('Canonical title', 'Stale title')

    issues = journal_ops.validate_registry(registry, index_text=drifted_index, check_entries=True, root=root)

    assert any('derived journal entry content differs from registry' in issue for issue in issues)
    assert 'derived journal index content differs from registry' in issues


def test_unsafe_journal_entry_id_rejected_for_path(tmp_path):
    root = configure_tmp_paths(tmp_path)
    entry = {'id': 'J../evil', 'title': 'Bad', 'tag': 'ideas', 'date': '2026-05-07', 'tags': [], 'status': 'active', 'original': 'a', 'entry': 'a', 'related': []}

    try:
        journal_ops.entry_path(entry, root=root)
    except ValueError as exc:
        assert 'unsafe journal entry id' in str(exc)
    else:
        raise AssertionError('unsafe journal id was not rejected')


def test_noncanonical_journal_root_env_requires_explicit_allow(tmp_path):
    root = tmp_path / 'other-journal'
    root.mkdir()
    old_root = os.environ.get('JOURNAL_ROOT')
    old_allow = os.environ.get(journal_ops.ALLOW_NONCANONICAL_ROOTS_ENV)
    try:
        os.environ['JOURNAL_ROOT'] = str(root)
        os.environ.pop(journal_ops.ALLOW_NONCANONICAL_ROOTS_ENV, None)
        try:
            journal_ops.resolve_journal_root()
        except SystemExit as exc:
            assert 'Refusing non-canonical JOURNAL_ROOT' in str(exc)
        else:
            raise AssertionError('non-canonical JOURNAL_ROOT was not rejected')
        os.environ[journal_ops.ALLOW_NONCANONICAL_ROOTS_ENV] = '1'
        assert journal_ops.resolve_journal_root() == root
    finally:
        if old_root is None:
            os.environ.pop('JOURNAL_ROOT', None)
        else:
            os.environ['JOURNAL_ROOT'] = old_root
        if old_allow is None:
            os.environ.pop(journal_ops.ALLOW_NONCANONICAL_ROOTS_ENV, None)
        else:
            os.environ[journal_ops.ALLOW_NONCANONICAL_ROOTS_ENV] = old_allow


if __name__ == '__main__':
    import subprocess
    raise SystemExit(subprocess.run(['uv', 'run', '--with', 'pytest', 'pytest', str(Path(__file__)), '-q']).returncode)
