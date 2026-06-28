from __future__ import annotations

import importlib.util
from pathlib import Path


EXTRACTOR = Path('/home/hermes/.hermes/skills/research/site-specific-feed-extractors/scripts/site_feed_extractors.py')
FEED_OPS = Path('/home/hermes/feed/_tools/feed_ops.py')


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_extractor_rejects_private_discovery_url_before_network(monkeypatch):
    extractors = load_module(EXTRACTOR, 'site_feed_extractors_boundary_test')

    def fail_urlopen(*args, **kwargs):  # pragma: no cover - should not be reached
        raise AssertionError('network fetch should not be attempted for private host')

    monkeypatch.setattr(extractors.urllib.request, 'urlopen', fail_urlopen)
    result = extractors.discover_feed_url('http://127.0.0.1:9/', limit=1)

    assert result['ok'] is False
    assert result.get('feed_url') is None
    assert any('refusing non-public URL host' in err for err in result['errors'])


def test_extractor_rejects_private_rss_url_before_network(monkeypatch):
    extractors = load_module(EXTRACTOR, 'site_feed_extractors_rss_boundary_test')

    def fail_urlopen(*args, **kwargs):  # pragma: no cover - should not be reached
        raise AssertionError('network fetch should not be attempted for private host')

    monkeypatch.setattr(extractors.urllib.request, 'urlopen', fail_urlopen)

    try:
        extractors.fetch_rss_feed('http://169.254.169.254/latest/meta-data/', 'test', 'Test')
    except ValueError as exc:
        assert 'refusing non-public URL host' in str(exc)
    else:  # pragma: no cover
        raise AssertionError('private metadata host should be rejected')


def test_feed_ops_rejects_cgnat_and_multicast_literals_as_non_public():
    feed_ops = load_module(FEED_OPS, 'feed_ops_cgnat_boundary_test')

    assert feed_ops._host_is_public('100.64.0.1') is False
    assert feed_ops._host_is_public('100.127.255.254') is False
    assert feed_ops._host_is_public('224.0.0.1') is False
    assert feed_ops._host_is_public('ff02::1') is False


def test_extractor_rejects_cgnat_and_multicast_literals_as_non_public():
    extractors = load_module(EXTRACTOR, 'site_feed_extractors_cgnat_boundary_test')

    assert extractors._host_is_public('100.64.0.1') is False
    assert extractors._host_is_public('100.127.255.254') is False
    assert extractors._host_is_public('224.0.0.1') is False
    assert extractors._host_is_public('ff02::1') is False


def test_feed_ops_rejects_dns_name_resolving_to_cgnat_or_multicast(monkeypatch):
    feed_ops = load_module(FEED_OPS, 'feed_ops_dns_cgnat_boundary_test')

    monkeypatch.setattr(
        feed_ops.socket,
        'getaddrinfo',
        lambda *args, **kwargs: [
            (feed_ops.socket.AF_INET, feed_ops.socket.SOCK_STREAM, 6, '', ('100.64.0.1', 0)),
            (feed_ops.socket.AF_INET, feed_ops.socket.SOCK_STREAM, 6, '', ('224.0.0.1', 0)),
        ],
    )

    assert feed_ops._host_is_public('feed.example') is False


def test_feed_ops_rejects_private_url_before_network(monkeypatch):
    feed_ops = load_module(FEED_OPS, 'feed_ops_boundary_test')

    def fail_urlopen(*args, **kwargs):  # pragma: no cover - should not be reached
        raise AssertionError('network fetch should not be attempted for private host')

    monkeypatch.setattr(feed_ops, '_urlopen_no_redirect', fail_urlopen)

    try:
        feed_ops.urlopen_text('http://localhost/private')
    except ValueError as exc:
        assert 'refusing non-public URL host' in str(exc)
    else:  # pragma: no cover
        raise AssertionError('localhost should be rejected')


def test_feed_ops_rejects_private_redirect_before_following(monkeypatch):
    feed_ops = load_module(FEED_OPS, 'feed_ops_redirect_boundary_test')
    calls = []
    monkeypatch.setattr(feed_ops, '_host_is_public', lambda host: host == 'example.com')

    def redirect_once(req, timeout):
        calls.append(req.full_url)
        raise feed_ops._ValidatedRedirect('http://169.254.169.254/latest/meta-data/')

    monkeypatch.setattr(feed_ops, '_urlopen_no_redirect', redirect_once)

    try:
        feed_ops.urlopen_text('https://example.com/feed')
    except ValueError as exc:
        assert 'refusing non-public URL host' in str(exc)
    else:  # pragma: no cover
        raise AssertionError('metadata redirect target should be rejected')
    assert calls == ['https://example.com/feed']


def test_extractor_rejects_private_redirect_before_following(monkeypatch):
    extractors = load_module(EXTRACTOR, 'site_feed_extractors_redirect_boundary_test')
    calls = []
    monkeypatch.setattr(extractors, '_host_is_public', lambda host: host == 'example.com')

    def redirect_once(req, timeout):
        calls.append(req.full_url)
        raise extractors._ValidatedRedirect('http://127.0.0.1/private')

    monkeypatch.setattr(extractors, '_urlopen_no_redirect', redirect_once)

    try:
        extractors.urlopen_text('https://example.com/feed')
    except ValueError as exc:
        assert 'refusing non-public URL host' in str(exc)
    else:  # pragma: no cover
        raise AssertionError('private redirect target should be rejected')
    assert calls == ['https://example.com/feed']


def test_extractor_rejects_oversized_response(monkeypatch):
    extractors = load_module(EXTRACTOR, 'site_feed_extractors_size_boundary_test')
    monkeypatch.setattr(extractors, '_host_is_public', lambda host: True)

    class Headers:
        def get_content_charset(self):
            return 'utf-8'

    class Response:
        url = 'https://example.com/feed'
        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, n):
            return b'x' * n

    monkeypatch.setattr(extractors, '_urlopen_no_redirect', lambda *args, **kwargs: Response())

    try:
        extractors.urlopen_text('https://example.com/feed', max_bytes=10)
    except ValueError as exc:
        assert 'larger than 10 bytes' in str(exc)
    else:  # pragma: no cover
        raise AssertionError('oversized response should be rejected')
