import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest import mock

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


def test_noncanonical_wiki_env_requires_explicit_allow(tmp_path):
    root = tmp_path / 'wiki'
    src = root / 'src'
    src.mkdir(parents=True)
    old_root = os.environ.get('WIKI_ROOT')
    old_src = os.environ.get('WIKI_SRC')
    old_allow = os.environ.get(wiki_read_harness.ALLOW_NONCANONICAL_ROOTS_ENV)
    try:
        os.environ['WIKI_ROOT'] = str(root)
        os.environ['WIKI_SRC'] = str(src)
        os.environ.pop(wiki_read_harness.ALLOW_NONCANONICAL_ROOTS_ENV, None)
        try:
            wiki_read_harness._resolve_default_wiki_paths()
        except SystemExit as exc:
            assert 'Refusing non-canonical WIKI_ROOT' in str(exc)
        else:
            raise AssertionError('non-canonical WIKI_ROOT was not rejected')
        os.environ[wiki_read_harness.ALLOW_NONCANONICAL_ROOTS_ENV] = '1'
        assert wiki_read_harness._resolve_default_wiki_paths() == (root, src)
    finally:
        if old_root is None:
            os.environ.pop('WIKI_ROOT', None)
        else:
            os.environ['WIKI_ROOT'] = old_root
        if old_src is None:
            os.environ.pop('WIKI_SRC', None)
        else:
            os.environ['WIKI_SRC'] = old_src
        if old_allow is None:
            os.environ.pop(wiki_read_harness.ALLOW_NONCANONICAL_ROOTS_ENV, None)
        else:
            os.environ[wiki_read_harness.ALLOW_NONCANONICAL_ROOTS_ENV] = old_allow


def test_journal_session_lookup_uses_shared_wiki_common_guard(tmp_path):
    hooks_root = Path('/home/hermes/.hermes/hooks')
    if str(hooks_root) not in sys.path:
        sys.path.insert(0, str(hooks_root))
    handler_path = hooks_root / 'wiki-autobuild' / 'handler.py'
    handler_spec = importlib.util.spec_from_file_location('journal_wiki_autobuild_probe', handler_path)
    assert handler_spec and handler_spec.loader
    handler = importlib.util.module_from_spec(handler_spec)
    handler_spec.loader.exec_module(handler)
    wiki_common = sys.modules['wiki_common']
    marker = tmp_path / 'autobuild-ran'
    script = tmp_path / 'would-run.py'
    script.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran', encoding='utf-8')\n",
        encoding='utf-8',
    )

    with mock.patch.object(wiki_common, 'get_chat_id_from_session', return_value=wiki_common.get_channel_id('journal')):
        with mock.patch.object(handler, 'SCRIPT', script):
            with mock.patch.object(handler.subprocess, 'run') as run:
                handler.handle('agent:end', {'platform': 'discord', 'session_id': 'journal-session'})

    run.assert_not_called()
    assert not marker.exists()


if __name__ == '__main__':
    import subprocess
    raise SystemExit(subprocess.run(['uv', 'run', '--with', 'pytest', 'pytest', str(Path(__file__)), '-q']).returncode)
