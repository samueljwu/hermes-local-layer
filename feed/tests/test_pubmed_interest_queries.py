import unittest
from unittest.mock import patch
from test_canonical_sources import load_feed_ops


class PubmedInterestQueryTests(unittest.TestCase):
    def test_targeted_queries_survive_lower_interest_rank_and_dedupe(self):
        f = load_feed_ops()
        profile = {'active_interests': [{'topic': t} for t in [
            'AI', 'networks', 'packaging', 'biophotonics and optical imaging',
            'neurotechnology and BCI', 'biophotonics and optical imaging']]}
        queries, topics = f.pubmed_profile_queries(profile, ['AI', 'networks', 'packaging'], {})
        self.assertEqual(len(queries), 5)
        self.assertEqual(queries[:3], ['AI', 'networks', 'packaging'])
        self.assertIn('fNIRS', queries[3])
        self.assertIn('OPM-MEG', queries[4])
        self.assertEqual(topics[queries[3]], 'biophotonics and optical imaging')
        self.assertEqual(f.pubmed_profile_queries({'active_interests': []}, ['fallback'], {}), (['fallback'], {'fallback': None}))

    def test_runtime_dispatch_uses_targeted_queries_without_writes(self):
        f = load_feed_ops()
        profile = {'active_interests': [{'topic': 'biophotonics and optical imaging'}]}
        with patch.object(f, 'candidate_source_records', return_value=[{
            'id': 'medical', 'connector': 'pubmed_api', 'endpoint': 'https://example.com/eutils/'}]), \
             patch.object(f, 'existing_candidate_ids', return_value=set()), \
             patch.object(f, 'fetch_public_blog_candidates', return_value=[]), \
             patch.object(f, 'pubmed_candidates_for_queries', return_value=[]) as fetch, \
             patch.object(f, 'save_json') as save:
            f.fetch_candidates(profile=profile, save=False)
            queries = fetch.call_args.args[0]
            self.assertTrue(any('fNIRS' in q for q in queries))
            self.assertEqual(fetch.call_args.kwargs['source_id'], 'medical')
            self.assertEqual(fetch.call_args.kwargs['max_results'], 5)
            save.assert_not_called()

    def test_paper_title_matches_both_curated_interests(self):
        f = load_feed_ops()
        title = 'Magnetically compatible and fiberless fNIRS enables simultaneous multimodal imaging with optically pumped magnetometer MEG'
        matches = f.semantic_match(title)
        self.assertEqual({topic for _, topic in matches[:2]}, {
            'biophotonics and optical imaging', 'neurotechnology and BCI'})


if __name__ == '__main__':
    unittest.main()
