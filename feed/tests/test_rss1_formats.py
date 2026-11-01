"""Offline coverage of journal RSS 1.0 alongside existing feed formats."""
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

HELPER = Path('/home/hermes/.hermes/skills/research/site-specific-feed-extractors/scripts/site_feed_extractors.py')
spec = importlib.util.spec_from_file_location('rss1_extractor', HELPER)
assert spec is not None and spec.loader is not None
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class FeedFormats(unittest.TestCase):
    def test_formats_preserve_metadata_and_limit(self):
        rss_item = '<item><title>Semantic BCI</title><link>https://example.com/paper</link><description>Semantic decoding research.</description><dc:date>2026-11-01</dc:date><dc:creator>Researcher</dc:creator></item>'
        fixtures = {
            'rss1': '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">' + rss_item * 2 + '</rdf:RDF>',
            'rss2': '<rss xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>' + rss_item * 2 + '</channel></rss>',
            'atom': '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Semantic BCI</title><link href="https://example.com/paper"/><summary>Semantic decoding research.</summary><published>2026-11-01</published><author>Researcher</author></entry></feed>',
        }
        with patch.object(helper, 'urlopen_text', side_effect=AssertionError('unexpected network')):
            for name, xml in fixtures.items():
                with self.subTest(format=name):
                    records = helper.parse_rss_items(xml, 'journal', 'Journal', 'https://example.com/feed', limit=1)
                    self.assertEqual(len(records), 1)
                    item = records[0]
                    self.assertEqual(item['title'], 'Semantic BCI')
                    self.assertEqual(item['url'], 'https://example.com/paper')
                    self.assertEqual(item['published_date'], '2026-11-01')
                    self.assertEqual(item['author'], 'Researcher')
                    self.assertEqual(item['summary'], 'Semantic decoding research.')
                    self.assertEqual(item['source'], 'journal')


if __name__ == '__main__':
    unittest.main()
