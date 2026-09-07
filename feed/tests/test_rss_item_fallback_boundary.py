from __future__ import annotations

import importlib.util
import urllib.parse
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


def test_bing_news_wrapper_accepts_only_approved_destination_boundary():
    extractors = load_extractors()
    feed_url = (
        'https://www.bing.com/news/search?'
        'q=site%3Awww.bme.jhu.edu%2Fnews-events%2Fnews+Johns+Hopkins+BME&format=rss'
    )
    target = 'https://www.bme.jhu.edu/news-events/news/approved-story'
    wrapped = 'https://www.bing.com/news/apiclick.aspx?url=' + urllib.parse.quote(target, safe='')

    records = extractors.parse_rss_items(
        f'<?xml version="1.0"?><rss><channel><item><title>Approved story</title><link>{wrapped.replace("&", "&amp;")}</link><description>Useful summary.</description></item></channel></rss>',
        'jhu_bme_news', 'Johns Hopkins BME News', feed_url, limit=1,
    )

    assert [record['url'] for record in records] == [target]


def test_bing_news_wrapper_rejects_off_domain_destination():
    extractors = load_extractors()
    feed_url = 'https://www.bing.com/news/search?q=site%3Awww.bme.jhu.edu%2Fnews-events%2Fnews&format=rss'
    target = 'https://attacker.example/news-events/news/copied-path'
    wrapped = 'https://www.bing.com/news/apiclick.aspx?url=' + urllib.parse.quote(target, safe='')
    xml = f'<?xml version="1.0"?><rss><channel><item><title>Off domain</title><link>{wrapped.replace("&", "&amp;")}</link><description>Do not accept.</description></item></channel></rss>'

    assert extractors.parse_rss_items(xml, 'jhu_bme_news', 'Johns Hopkins BME News', feed_url) == []


def test_bing_news_wrapper_rejects_wrong_approved_host_path():
    extractors = load_extractors()
    feed_url = 'https://www.bing.com/news/search?q=site%3Awww.bme.jhu.edu%2Fnews-events%2Fnews&format=rss'
    target = 'https://www.bme.jhu.edu/news-events/newsletter/not-approved'
    wrapped = 'https://www.bing.com/news/apiclick.aspx?url=' + urllib.parse.quote(target, safe='')
    xml = f'<?xml version="1.0"?><rss><channel><item><title>Wrong path</title><link>{wrapped.replace("&", "&amp;")}</link><description>Do not accept.</description></item></channel></rss>'

    assert extractors.parse_rss_items(xml, 'jhu_bme_news', 'Johns Hopkins BME News', feed_url) == []


def test_bing_news_wrapper_rejects_literal_dot_segment_escape():
    extractors = load_extractors()
    feed_url = 'https://www.bing.com/news/search?q=site%3Aapproved.example%2Fallowed&format=rss'
    target = 'https://approved.example/allowed/../outside'
    wrapped = 'https://www.bing.com/news/apiclick.aspx?url=' + urllib.parse.quote(target, safe='')
    assert extractors.canonicalize_news_redirect_url(wrapped, feed_url) == ''


def test_bing_news_wrapper_rejects_encoded_dot_segment_escape():
    extractors = load_extractors()
    feed_url = 'https://www.bing.com/news/search?q=site%3Aapproved.example%2Fallowed&format=rss'
    target = 'https://approved.example/allowed/%2e%2e/outside'
    wrapped = 'https://www.bing.com/news/apiclick.aspx?url=' + urllib.parse.quote(target, safe='')
    assert extractors.canonicalize_news_redirect_url(wrapped, feed_url) == ''


def test_bing_news_wrapper_rejects_deeply_nested_dot_segment_escape():
    extractors = load_extractors()
    feed_url = 'https://www.bing.com/news/search?q=site%3Aapproved.example%2Fallowed&format=rss'
    segment = '..'
    for _ in range(8):
        segment = urllib.parse.quote(segment, safe='')
    target = f'https://approved.example/allowed/{segment}/outside'
    wrapped = 'https://www.bing.com/news/apiclick.aspx?url=' + urllib.parse.quote(target, safe='')
    assert extractors.canonicalize_news_redirect_url(wrapped, feed_url) == ''
