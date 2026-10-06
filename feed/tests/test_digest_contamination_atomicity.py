from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


FEED_OPS = Path('/home/hermes/feed/_tools/feed_ops.py')


def load_feed_ops():
    spec = importlib.util.spec_from_file_location('feed_ops_contamination_atomicity', FEED_OPS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_contamination_failure_happens_before_canonical_output_promotion(monkeypatch):
    feed_ops = load_feed_ops()
    selected = [
        {
            'candidate_id': f'candidate-{slot}', 'slot': slot,
            'source': f'source-{slot}', 'title': f'Title {slot}',
            'url': f'https://example.com/{slot}', 'summary': 'Summary.',
            'relation_type': 'adjacent_interest' if slot <= 3 else 'exploratory',
            'matched_interest': 'test interest' if slot <= 3 else None,
        }
        for slot in range(1, 6)
    ]
    snapshots = iter([{'protected': {'before': (1, 'a')}}, {'protected': {'after': (1, 'b')}}])
    writes: list[str] = []

    monkeypatch.setattr(feed_ops, 'validate', lambda: [])
    monkeypatch.setattr(feed_ops, 'protected_snapshot', lambda: next(snapshots))
    monkeypatch.setattr(feed_ops, 'collect_signals', lambda: {})
    monkeypatch.setattr(feed_ops, 'build_profile', lambda *args, **kwargs: {'active_interests': [{'topic': 'test interest'}]})
    monkeypatch.setattr(feed_ops, 'fetch_candidates', lambda *args, **kwargs: [])
    monkeypatch.setattr(feed_ops, 'select', lambda *args, **kwargs: selected)
    monkeypatch.setattr(feed_ops, 'current_run_id', lambda: '2026-09-06-1200')
    monkeypatch.setattr(feed_ops, 'load_json', lambda *args, **kwargs: [])
    monkeypatch.setattr(feed_ops, 'prepare_feed_page', lambda history: (object(), '<html></html>'))
    monkeypatch.setattr(feed_ops, 'write_text', lambda path, text: writes.append(str(path)))
    monkeypatch.setattr(feed_ops, 'save_json', lambda path, value: writes.append(str(path)))
    monkeypatch.setattr(feed_ops, 'append_text', lambda path, text: writes.append(str(path)))

    with pytest.raises(RuntimeError, match='ANTI-CONTAMINATION CHECK FAILED'):
        feed_ops.digest(dry_run=False)

    assert writes == []


def test_source_validation_includes_semantic_gate_without_refetching(monkeypatch, capsys):
    feed_ops = load_feed_ops()
    calls = {'fetch': 0}
    source = {'id': 'source', 'name': 'Source', 'enabled': True, 'semantic_role': 'adjacent_interest'}

    monkeypatch.setattr(feed_ops, 'candidate_source_records', lambda: [source])
    monkeypatch.setattr(feed_ops, 'build_profile', lambda save=False: {'active_interests': []})

    def fetch_once(rec, limit=3):
        calls['fetch'] += 1
        calls['limit'] = limit
        return [{'title': 'Weak item', 'summary': 'thin', 'categories': []}]

    monkeypatch.setattr(feed_ops, 'source_candidate_items', fetch_once)
    monkeypatch.setattr(feed_ops, 'semantic_usefulness_report', lambda items, profile, rec: {
        'ok': False, 'errors': ['low_semantic_usefulness'], 'warnings': [], 'metrics': {'items': 1},
    })

    assert feed_ops.validate_source('source', limit=3) is False
    assert calls['fetch'] == 1
    assert calls['limit'] == 5
    output = capsys.readouterr().out
    assert '"structural_ok": false' in output
    assert 'too_few_items:1<2' in output
    assert '"semantic_ok": false' in output
