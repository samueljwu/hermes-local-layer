from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


FEED_OPS = Path('/home/hermes/feed/_tools/feed_ops.py')


def load_feed_ops():
    spec = importlib.util.spec_from_file_location('feed_ops_selection_contract', FEED_OPS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def candidate(candidate_id: str, relevance: float) -> dict:
    return {
        'candidate_id': candidate_id,
        'source': candidate_id,
        'title': f'Candidate {candidate_id}',
        'summary': 'A sufficiently detailed candidate summary for deterministic selection tests.',
        'url': f'https://{candidate_id}.example/article',
        '_test_relevance': relevance,
    }


def test_select_refuses_to_force_zero_relevance_items_into_core_picks(monkeypatch):
    feed_ops = load_feed_ops()
    monkeypatch.setattr(feed_ops, 'relevance', lambda item, profile: (item['_test_relevance'], 'test interest'))
    candidates = [candidate('related-a', 1.2), candidate('related-b', 0.9)] + [candidate(f'unrelated-{n}', 0.0) for n in range(4)]

    with pytest.raises(RuntimeError, match=r'only 2 candidates meet.*minimum Core Pick relevance'):
        feed_ops.select(candidates, {'active_interests': [{'topic': 'test interest'}]})


def test_select_returns_exactly_three_genuinely_related_core_picks(monkeypatch):
    feed_ops = load_feed_ops()
    monkeypatch.setattr(feed_ops, 'relevance', lambda item, profile: (item['_test_relevance'], 'test interest'))
    candidates = [
        candidate('core-a', 2.2), candidate('core-b', 1.4), candidate('core-c', 0.8),
        candidate('explore-a', 0.0), candidate('explore-b', 0.1),
    ]

    selected = feed_ops.select(candidates, {'active_interests': [{'topic': 'test interest'}]})

    assert len(selected) == 5
    assert [item['relation_type'] for item in selected[:3]] == ['direct_interest', 'adjacent_interest', 'adjacent_interest']
    assert all(item['_test_relevance'] >= 0.8 for item in selected[:3])
