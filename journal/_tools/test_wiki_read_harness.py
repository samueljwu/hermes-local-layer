import importlib.util
import json
from pathlib import Path

MODULE_PATH = Path(__file__).with_name('wiki_read_harness.py')
spec = importlib.util.spec_from_file_location('wiki_read_harness', MODULE_PATH)
wiki_read_harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wiki_read_harness)


def test_semantic_plan_reads_prebuilt_artifacts_without_subprocess(tmp_path):
    root = tmp_path / 'wiki'
    semantic = root / 'public' / 'semantic'
    semantic.mkdir(parents=True)
    (semantic / 'index.json').write_text(json.dumps({
        'schemaVersion': 1,
        'generatedAt': '2026-05-10T00:00:00Z',
        'pages': {
            'concepts/optical-test': {
                'path': 'src/concepts/optical-test.md',
                'kind': 'concept',
                'title': 'Optical Test',
                'tags': ['biophotonics'],
                'aliases': [],
                'summary_1line': 'Optical imaging and biophotonics test page.',
                'summary_compact': 'Optical imaging and biophotonics test page.',
                'query_terms': ['optical imaging'],
                'important_edges': [['related_to', 'concepts/neighbor-test', 1.0, 0.9]],
                'degree': {'total': 2},
            },
            'concepts/neighbor-test': {
                'path': 'src/concepts/neighbor-test.md',
                'kind': 'concept',
                'title': 'Neighbor Test',
                'tags': [],
                'aliases': [],
                'summary_1line': 'Neighbor.',
                'summary_compact': 'Neighbor.',
                'query_terms': [],
                'degree': {'total': 1},
            },
        },
    }))
    (semantic / 'graph.json').write_text(json.dumps({
        'schemaVersion': 1,
        'generatedAt': '2026-05-10T00:00:00Z',
        'counts': {'nodes': 2, 'links': 1},
        'relationshipTypes': ['related_to'],
    }))

    plan = wiki_read_harness._load_semantic_plan('optical imaging', root, top=1, hops=1, include_candidates=False)

    assert plan['queryEngine'] == 'read-only-artifact-query'
    assert plan['results'][0]['path'] == 'src/concepts/optical-test.md'
    assert 'src/concepts/neighbor-test.md' in plan['readThesePagesFirst']
    assert not hasattr(wiki_read_harness, 'subprocess')


if __name__ == '__main__':
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        test_semantic_plan_reads_prebuilt_artifacts_without_subprocess(Path(td))
    print('wiki_read_harness tests passed')
