"""The Neuralink listing can append newly published posts after old ones."""
import html
import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

HELPER = Path('/home/hermes/.hermes/skills/research/site-specific-feed-extractors/scripts/site_feed_extractors.py')
spec = importlib.util.spec_from_file_location('neuralink_extractor_test', HELPER)
assert spec is not None and spec.loader is not None
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class NeuralinkUpdatesOrder(unittest.TestCase):
    def test_recent_appended_post_survives_limit(self):
        posts = [
            {'slug': f'older-{n}', 'name': f'Older {n}',
             'fields': {'release_date': f'2025-01-{n + 1:02d}', 'summary': 'Older update.'}}
            for n in range(12)
        ]
        posts += [
            {'slug': '72-hour-voice-hackathon', 'name': 'Voice hackathon',
             'fields': {'release_date': '2026-10-29T00:00:00', 'summary': 'Thought to speech.'}},
            {'slug': 'unpublished', 'name': 'Draft', 'status': 'draft',
             'fields': {'release_date': '2027-01-01'}},
            {'slug': 'fallback-date', 'name': 'Fallback date',
             'published': '2026-10-01T12:00:00Z', 'fields': {'summary': 'Pretraining.'}},
            {'slug': 'undated', 'name': 'Undated', 'fields': {'summary': 'No date.'}},
        ]
        payload = html.escape(json.dumps({'pageProps': {'blogPosts': posts}}))
        listing = f'<script id="vite-plugin-ssr_pageContext">{payload}</script>'
        with patch.object(helper, 'urlopen_text', return_value=listing):
            recent = helper.fetch_neuralink_updates(limit=2)
            all_posts = helper.fetch_neuralink_updates()
        self.assertEqual([r['url'].split('/')[-1] for r in recent],
                         ['72-hour-voice-hackathon', 'fallback-date'])
        self.assertEqual([r['published_date'] for r in recent], ['2026-10-29', '2026-10-01'])
        self.assertEqual(len(all_posts), 15)
        self.assertEqual(all_posts[-1]['url'].split('/')[-1], 'undated')


if __name__ == '__main__':
    unittest.main()
