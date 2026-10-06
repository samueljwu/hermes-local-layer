"""Offline regression fixtures; never publish or fetch live feed data."""
import json
import os
from pathlib import Path

import pytest

from test_validate_readonly import load_feed_ops
from test_feed_page_output_guard import load_renderer


@pytest.fixture
def ops(tmp_path):
    root = tmp_path / 'feed'
    root.mkdir()
    module = load_feed_ops(root)
    module.ensure_layout()
    for name in ('SCHEMA.md', 'index.md', 'log.md'):
        (root / name).write_text('fixture\n')
    return module


@pytest.mark.parametrize('name,bad', [
    ('interest_profile', []),
    ('interest_profile', {'active_interests': {}}),
    ('interest_profile', {'active_interests': [{'topic': 'x', 'weight': True}]}),
    ('interest_profile', {'active_interests': [{'topic': 'x', 'weight': '1'}]}),
    ('recommendation_history', {}),
    ('recommendation_history', [None]),
    ('recommendation_history', [{'slot': True, 'candidate_id': 'x'}]),
    ('source_state', []),
    ('source_state', {'rss': {'seen_ids': 'not a list', 'last_checked': None}}),
    ('source_state', {'last_fetch_errors': ['oops']}),
    ('source_state', {'last_fetch_errors': [{'source': 'rss', 'error': 'timeout', 'checked_at': 1}]}),
    ('source_state', {'rss': {'seen_ids': [3], 'last_checked': None}}),
    ('interest_profile', {'active_interests': [{'topic': 'x', 'weight': float('nan')}]}),
    ('interest_profile', {'active_interests': [{'topic': 'x', 'weight': 1, 'queries': 'not a list'}]}),
    ('information_sources', {'allowed_candidate_sources': [], 'read_only_interest_sources': [False]}),
    ('information_sources', []),
    ('information_sources', {'allowed_candidate_sources': [None]}),
    ('information_sources', {'allowed_candidate_sources': [{'name': 'x', 'endpoint': 'https://example.org', 'enabled': 'false'}]}),
])
def test_malformed_valid_json_is_reported_readonly(ops, name, bad):
    path = ops.META / (name + '.json')
    path.write_text(json.dumps(bad))
    before = {p: p.read_bytes() for p in ops.BASE.rglob('*') if p.is_file()}
    errors = ops.validate()
    assert any(name + '.json' in error for error in errors), errors
    assert before == {p: p.read_bytes() for p in ops.BASE.rglob('*') if p.is_file()}


def test_bootstrap_defaults_and_legacy_optional_fields_validate(ops):
    assert ops.validate() == []
    sources = ops.load_json(ops.META / 'information_sources.json', None)
    # Historical rows may lack id/connector/enabled; extensions remain intact.
    sources['allowed_candidate_sources'] = [{
        'name': 'Legacy fixture', 'endpoint': 'https://example.org/feed',
        'kind': 'rss', 'purpose': 'Offline compatibility fixture',
    }]
    sources['extension_metadata'] = {'preserved': True}
    ops.save_json(ops.META / 'information_sources.json', sources)
    assert ops.validate() == []


def test_invalid_proposed_state_does_not_publish(ops):
    path = ops.META / 'source_state.json'
    before = path.read_bytes()
    with pytest.raises(ValueError):
        ops.save_json(path, {'rss': {'seen_ids': False}})
    assert path.read_bytes() == before


@pytest.mark.parametrize('operation', ['write', 'append', 'lock', 'layout', 'page', 'page_lock'])
def test_symlink_parent_is_rejected(ops, tmp_path, monkeypatch, operation):
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = ops.BASE / 'linked'
    link.symlink_to(outside, target_is_directory=True)
    renderer = load_renderer()
    monkeypatch.setattr(renderer, 'OUTPUT_PATH', link / 'index.html')
    monkeypatch.setattr(renderer, 'LOCK_PATH', link / '.feed_ops.lock')
    monkeypatch.setattr(ops, 'LOCK_PATH', link / '.feed_ops.lock')
    with pytest.raises((OSError, RuntimeError)):
        if operation == 'write': ops.write_text(link / 'new' / 'state', 'bad')
        elif operation == 'append': ops.append_text(link / 'log', 'bad')
        elif operation == 'layout':
            monkeypatch.setattr(ops, 'RUNS', link / 'runs')
            ops.ensure_layout()
        elif operation == 'page': renderer.atomic_write_text(renderer.OUTPUT_PATH, 'bad')
        else:
            with (renderer if operation == 'page_lock' else ops).feed_lock(): pass
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize('operation', ['write', 'append', 'lock', 'page', 'page_lock'])
def test_parent_swap_stays_bound_to_open_directory(ops, tmp_path, monkeypatch, operation):
    parent = ops.BASE / 'target'
    parent.mkdir()
    moved = ops.BASE / 'moved'
    outside = tmp_path / 'outside'
    outside.mkdir()
    renderer = load_renderer()
    monkeypatch.setattr(renderer, 'OUTPUT_PATH', parent / 'index.html')
    monkeypatch.setattr(renderer, 'LOCK_PATH', parent / '.feed_ops.lock')
    monkeypatch.setattr(ops, 'LOCK_PATH', parent / '.feed_ops.lock')
    real_open = os.open
    swapped = False

    def swap_after_open(path, flags, *args, **kwargs):
        nonlocal swapped
        fd = real_open(path, flags, *args, **kwargs)
        if flags & os.O_DIRECTORY and not swapped and os.fstat(fd).st_ino == parent.stat().st_ino:
            parent.rename(moved)
            parent.symlink_to(outside, target_is_directory=True)
            swapped = True
        return fd

    monkeypatch.setattr(os, 'open', swap_after_open)
    if operation == 'write': ops.write_text(parent / 'state', 'safe')
    elif operation == 'append': ops.append_text(parent / 'state', 'safe')
    elif operation == 'page': renderer.atomic_write_text(renderer.OUTPUT_PATH, 'safe')
    else:
        with (renderer if operation == 'page_lock' else ops).feed_lock(): pass
    assert swapped, 'operation did not bind the destination parent'
    assert list(outside.iterdir()) == []
    name = 'index.html' if operation == 'page' else '.feed_ops.lock' if 'lock' in operation else 'state'
    assert (moved / name).is_file()
    assert not list(moved.glob('*.tmp'))


@pytest.mark.parametrize('operation', ['write', 'append', 'lock'])
def test_nonregular_leaf_is_rejected_without_blocking(ops, monkeypatch, operation):
    path = ops.BASE / 'fifo'
    os.mkfifo(path)
    monkeypatch.setattr(ops, 'LOCK_PATH', path)
    with pytest.raises((OSError, RuntimeError)):
        if operation == 'write': ops.write_text(path, 'unsafe')
        elif operation == 'append': ops.append_text(path, 'unsafe')
        else:
            with ops.feed_lock(): pass


def test_failed_replace_cleans_bound_temporary_file(ops, monkeypatch):
    parent = ops.BASE / 'target'
    parent.mkdir()
    path = parent / 'state'
    path.write_text('old')
    moved = ops.BASE / 'moved'
    outside = ops.BASE / 'outside'
    outside.mkdir()

    def fail_replace(*args, **kwargs):
        parent.rename(moved)
        parent.symlink_to(outside, target_is_directory=True)
        raise OSError('injected replace failure')

    monkeypatch.setattr(os, 'replace', fail_replace)
    with pytest.raises(OSError, match='injected'):
        ops.write_text(path, 'new')
    assert (moved / 'state').read_text() == 'old'
    assert sorted(p.name for p in moved.iterdir()) == ['state']
    assert list(outside.iterdir()) == []


def test_valid_synthetic_history_and_fetch_telemetry(ops):
    history = [{'slot': 4, 'candidate_id': 'rss:1', 'source': 'rss',
                'title': 'Fixture', 'url': 'https://example.org/article',
                'relation_type': 'exploratory', 'run_id': '2026-01-01',
                'date': '2026-01-01', 'categories': None, 'hn_url': None,
                'score': 1.2, 'extension_metadata': {'preserved': True}}]
    state = {'rss': {'last_checked': None, 'seen_ids': ['rss:1']},
             'last_fetch_errors': [{'source': 'rss', 'error': 'timeout',
                                    'checked_at': '2026-01-01T00:00:00Z'}]}
    ops.save_json(ops.META / 'recommendation_history.json', history)
    ops.save_json(ops.META / 'source_state.json', state)
    assert ops.validate() == []
    assert ops.load_json(ops.META / 'recommendation_history.json', None) == history


def test_lexical_traversal_is_rejected(ops):
    with pytest.raises(RuntimeError):
        ops.write_text(ops.BASE / '..' / 'escaped', 'unsafe')
