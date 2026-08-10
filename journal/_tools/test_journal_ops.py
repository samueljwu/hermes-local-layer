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
    import tempfile
    test_validate_detects_index_count_mismatch()
    test_build_index_separates_active_and_archived_entries()
    test_render_entry_preserves_original_and_cleaned_layers()
    with tempfile.TemporaryDirectory() as td:
        test_regenerate_removes_stale_same_id_entry_in_wrong_folder(Path(td))
    with tempfile.TemporaryDirectory() as td:
        test_validate_detects_invalid_status_and_stale_entry_file(Path(td))
    with tempfile.TemporaryDirectory() as td:
        test_unsafe_journal_entry_id_rejected_for_path(Path(td))
    print('journal_ops tests passed')
