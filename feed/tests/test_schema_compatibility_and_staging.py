"""Synthetic consumer-contract and validation-before-publication regressions."""
import copy
import json
import pytest

from test_state_and_descriptor_safety import ops
from test_feed_page_output_guard import load_renderer


@pytest.mark.parametrize('metadata', [
    {'purpose': '', 'semantic_role': ''},
    {'purpose': None, 'semantic_role': None, 'source_role': None},
    {'semantic_role': ' MIXED ', 'source_role': ''},
    {'connector': '', 'kind': None, 'expected_topics': None},
])
def test_registry_consumer_defaults_are_preserved(ops, metadata):
    data = {'allowed_candidate_sources': [dict(
        name='Fixture', endpoint='https://example.org/feed', **metadata)]}
    ops.save_json(ops.META / 'information_sources.json', data)
    assert ops.validate() == []
    assert ops.load_json(ops.META / 'information_sources.json', {}) == data


@pytest.mark.parametrize('name,data', [
    ('interest_profile', {'active_interests': [{'topic': 'fixture', 'terms': None}]}),
    ('source_state', {'rss': {'last_checked': None}}),
    ('source_state', {'last_fetch_errors': [{'source': 'rss', 'error': ''}]}),
])
def test_supported_consumer_defaults_validate(ops, name, data):
    ops.save_json(ops.META / (name + '.json'), data)
    assert ops.validate() == []


@pytest.mark.parametrize('terms', ['not a list', [42], {}, False])
def test_profile_terms_are_checked_before_publication(ops, terms):
    path = ops.META / 'interest_profile.json'
    before = path.read_bytes()
    with pytest.raises(ValueError, match='terms'):
        ops.save_json(path, {'active_interests': [{'topic': 'fixture', 'weight': 1, 'terms': terms}]})
    assert path.read_bytes() == before


@pytest.mark.parametrize('metadata', [
    {'purpose': False}, {'semantic_role': []}, {'semantic_role': 'invalid'},
    {'connector': 42}, {'expected_topics': False},
])
def test_optional_registry_values_still_reject_bad_types(ops, metadata):
    with pytest.raises(ValueError):
        ops.save_json(ops.META / 'information_sources.json', {
            'allowed_candidate_sources': [dict(name='Fixture', endpoint='https://example.org/feed', **metadata)]})


@pytest.mark.parametrize('failure', ['history', 'contamination', 'profile', 'source_state', None])
def test_digest_validates_all_staged_state_before_any_write(ops, monkeypatch, failure):
    before = {p: p.read_bytes() for p in ops.BASE.rglob('*') if p.is_file()}
    snapshots = iter([{}, {'changed': {'file': 'hash'}} if failure == 'contamination' else {}])
    monkeypatch.setattr(ops, 'protected_snapshot', lambda: next(snapshots))
    monkeypatch.setattr(ops, 'collect_signals', lambda: {'signals': []})
    real_build = ops.build_profile
    def build(*args, **kwargs):
        profile = real_build(*args, **kwargs)
        if failure == 'profile': profile['active_interests'] = [False]
        return profile
    monkeypatch.setattr(ops, 'build_profile', build)
    # Exercise the real fetch staging path, with no network or protected reads.
    monkeypatch.setattr(ops, 'candidate_source_records', lambda: [])
    monkeypatch.setattr(ops, 'fetch_public_blog_candidates', lambda **kwargs: [])
    if failure == 'source_state':
        (ops.META / 'source_state.json').write_text('{"rss": {"seen_ids": false}}')
        before = {p: p.read_bytes() for p in ops.BASE.rglob('*') if p.is_file()}
        monkeypatch.setattr(ops, 'validate', lambda: [])
    selected = [dict(candidate_id=f'rss:{i}', source='rss', title='Fixture',
                     url='https://example.org/item', slot=i,
                     relation_type='adjacent_interest' if i < 4 else 'exploratory')
                for i in range(1, 6)]
    if failure == 'history': selected[-1]['source'] = None
    monkeypatch.setattr(ops, 'select', lambda *args: copy.deepcopy(selected))
    class Page:
        OUTPUT_PATH = ops.BASE / 'page.html'
        atomic_write_text = staticmethod(ops.write_text)
    monkeypatch.setattr(ops, 'prepare_feed_page', lambda history: (Page, '<html>fixture</html>'))
    if failure:
        with pytest.raises((ValueError, RuntimeError)):
            ops.digest()
        assert before == {p: p.read_bytes() for p in ops.BASE.rglob('*') if p.is_file()}
    else:
        ops.digest()
        assert ops.validate() == []
        assert (ops.BASE / 'page.html').read_text() == '<html>fixture</html>'
        assert len(ops.load_json(ops.META / 'recommendation_history.json', [])) == 5
        assert ops.load_json(ops.META / 'candidates.json', None) == []


@pytest.mark.parametrize('operation', ['page', 'report'])
def test_derived_outputs_reject_malformed_inputs_before_write(ops, monkeypatch, operation):
    output = ops.BASE / 'projection'
    output.write_text('original')
    if operation == 'page':
        renderer = load_renderer()
        monkeypatch.setattr(renderer, 'OUTPUT_PATH', output)
        history = ops.META / 'recommendation_history.json'
        history.write_text(json.dumps([False]))
        call = lambda: renderer.render_to_file(history, output, locked=True)
    else:
        (ops.META / 'information_sources.json').write_text(json.dumps({
            'allowed_candidate_sources': [{'name': 'Fixture', 'endpoint': 'https://example.org', 'enabled': 'false'}]}))
        call = lambda: ops.render_source_report(output)
    with pytest.raises(ValueError):
        call()
    assert output.read_text() == 'original'


def test_fetch_no_save_profile_fallback_remains_readonly(ops, monkeypatch):
    (ops.META / 'interest_profile.json').unlink()
    monkeypatch.setattr(ops, 'collect_signals', lambda: {'signals': []})
    monkeypatch.setattr(ops, 'candidate_source_records', lambda: [])
    monkeypatch.setattr(ops, 'fetch_public_blog_candidates', lambda **kwargs: [])
    before = {p: p.read_bytes() for p in ops.BASE.rglob('*') if p.is_file()}
    assert ops.fetch_candidates(save=False) == []
    assert before == {p: p.read_bytes() for p in ops.BASE.rglob('*') if p.is_file()}


def test_fetch_invalid_state_does_not_publish_fallback_profile(ops, monkeypatch):
    (ops.META / 'interest_profile.json').unlink()
    (ops.META / 'source_state.json').write_text('{"rss": {"seen_ids": false}}')
    monkeypatch.setattr(ops, 'collect_signals', lambda: {'signals': []})
    monkeypatch.setattr(ops, 'candidate_source_records', lambda: [])
    monkeypatch.setattr(ops, 'fetch_public_blog_candidates', lambda **kwargs: [])
    before = {p: p.read_bytes() for p in ops.BASE.rglob('*') if p.is_file()}
    with pytest.raises(ValueError):
        ops.fetch_candidates()
    assert before == {p: p.read_bytes() for p in ops.BASE.rglob('*') if p.is_file()}
