from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path('/home/hermes/.hermes/skills/research/site-specific-feed-extractors/scripts/site_feed_extractors.py')


def load_extractors():
    spec = importlib.util.spec_from_file_location('site_feed_extractors_for_test', SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_rss_item_fallback_rejects_private_cross_domain_url(monkeypatch):
    extractors = load_extractors()
    calls: list[str] = []

    def fake_urlopen_text(url: str, timeout: int = 25) -> str:
        calls.append(url)
        return '<html><meta name="description" content="should not be fetched"></html>'

    monkeypatch.setattr(extractors, 'urlopen_text', fake_urlopen_text)
    feed = '''<?xml version="1.0"?><rss><channel><item><title>Boundary test item</title><link>http://127.0.0.1:1/private</link></item></channel></rss>'''

    records = extractors.parse_rss_items(feed, 'test_source', 'Test Source', 'https://approved.example/feed.xml', limit=1)

    assert len(records) == 1
    assert calls == []
    assert records[0]['summary'] == ''


def test_rss_item_fallback_allows_public_same_host_url(monkeypatch):
    extractors = load_extractors()
    calls: list[str] = []

    monkeypatch.setattr(extractors, '_host_is_public', lambda host: True)

    def fake_urlopen_text(url: str, timeout: int = 25) -> str:
        calls.append(url)
        return '<html><meta name="description" content="Public same-host summary."></html>'

    monkeypatch.setattr(extractors, 'urlopen_text', fake_urlopen_text)
    feed = '''<?xml version="1.0"?><rss><channel><item><title>Same host item</title><link>https://approved.example/item</link></item></channel></rss>'''

    records = extractors.parse_rss_items(feed, 'test_source', 'Test Source', 'https://approved.example/feed.xml', limit=1)

    assert calls == ['https://approved.example/item']
    assert records[0]['summary'] == 'Public same-host summary.'
