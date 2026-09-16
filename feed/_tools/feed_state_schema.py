"""Structural contracts for canonical feed state; no mutation or network I/O.

Accept historical optional fields and extension metadata without coercing values
or rewriting records. Source quality/policy lint remains a separate operation.
"""
import math
from urllib.parse import urlsplit

STATE_FILES = frozenset(('interest_profile.json', 'recommendation_history.json',
                         'source_state.json', 'information_sources.json'))


def validate_state(name, data):
    """Raise a field-addressed ValueError for malformed canonical state."""
    if name not in STATE_FILES:
        return

    def need(condition, path, expected):
        if not condition:
            raise ValueError(f'{name}: {path}: expected {expected}')

    def obj(value, path):
        need(isinstance(value, dict), path, 'object')

    def array(value, path):
        need(isinstance(value, list), path, 'list')

    def text(value, path):
        need(isinstance(value, str) and bool(value.strip()), path, 'nonempty string')

    def strings(value, path):
        array(value, path)
        for i, item in enumerate(value):
            need(isinstance(item, str), f'{path}[{i}]', 'string')

    def optional_strings(row, path, fields):
        for key in fields:
            if key in row and row[key] is not None:
                need(isinstance(row[key], str), f'{path}.{key}', 'string or null')

    def number(value, path):
        need(type(value) in (int, float) and math.isfinite(value), path, 'finite number (not boolean)')

    def url(value, path):
        text(value, path)
        try:
            parsed = urlsplit(value)
            valid = parsed.scheme in ('http', 'https') and bool(parsed.hostname)
        except ValueError:
            valid = False
        need(valid, path, 'http(s) URL with host')

    if name == 'recommendation_history.json':
        array(data, '$')
        for i, row in enumerate(data):
            path = f'$[{i}]'
            obj(row, path)
            for key in ('candidate_id', 'source', 'title', 'run_id', 'date', 'relation_type'):
                text(row.get(key), f'{path}.{key}')
            need(type(row.get('slot')) is int and 1 <= row['slot'] <= 5, f'{path}.slot', 'integer 1..5')
            need(row['relation_type'] in ('direct_interest', 'adjacent_interest', 'exploratory'),
                 f'{path}.relation_type', 'known relation type')
            url(row.get('url') or row.get('hn_url'), f'{path}.url')
            optional_strings(row, path, ('url', 'hn_url', 'generated_at', 'summary', 'pick_id',
                                         'domain', 'matched_interest', 'query_topic', 'why_recommended'))
            if row.get('categories') is not None:
                strings(row['categories'], f'{path}.categories')
            for key in ('score', 'correlation_relevance'):
                if key in row and row[key] is not None:
                    number(row[key], f'{path}.{key}')
        return

    obj(data, '$')
    if name == 'interest_profile.json':
        array(data.get('active_interests'), '$.active_interests')
        for i, row in enumerate(data['active_interests']):
            path = f'$.active_interests[{i}]'
            obj(row, path)
            text(row.get('topic'), f'{path}.topic')
            if 'weight' in row:
                number(row['weight'], f'{path}.weight')
            if row.get('terms') is not None:
                strings(row['terms'], f'{path}.terms')
            for key in ('keywords', 'queries', 'evidence'):
                if key in row:
                    strings(row[key], f'{path}.{key}')
        optional_strings(data, '$', ('updated', 'generated_at'))
        if 'exploration_policy' in data:
            obj(data['exploration_policy'], '$.exploration_policy')
            optional_strings(data['exploration_policy'], '$.exploration_policy', ('mode', 'description'))
        for key in ('notes', 'negative_signals'):
            if key in data:
                array(data[key], f'$.{key}')
    elif name == 'source_state.json':
        for key, row in data.items():
            path = f'$.{key}'
            if key == 'last_fetch_errors':
                array(row, path)
                for i, error in enumerate(row):
                    obj(error, f'{path}[{i}]')
                    text(error.get('source'), f'{path}[{i}].source')
                    need(isinstance(error.get('error'), str), f'{path}[{i}].error', 'string')
                    optional_strings(error, f'{path}[{i}]', ('checked_at', 'fetched_at', 'title'))
                continue
            obj(row, path)
            if 'seen_ids' in row:
                strings(row['seen_ids'], f'{path}.seen_ids')
            optional_strings(row, path, ('last_checked',))
    elif name == 'information_sources.json':
        array(data.get('allowed_candidate_sources'), '$.allowed_candidate_sources')
        seen = set()
        for i, row in enumerate(data['allowed_candidate_sources']):
            path = f'$.allowed_candidate_sources[{i}]'
            obj(row, path)
            text(row.get('name'), f'{path}.name')
            url(row.get('endpoint'), f'{path}.endpoint')
            # Legacy bootstrap rows predate id/connector/enabled; keep accepting
            # their absence, but never silently coerce malformed present values.
            optional_strings(row, path, ('connector', 'kind', 'purpose', 'semantic_role', 'source_role'))
            if 'id' in row:
                text(row['id'], f'{path}.id')
                need(row['id'] not in seen, f'{path}.id', 'unique source id')
                seen.add(row['id'])
            if 'enabled' in row:
                need(type(row['enabled']) is bool, f'{path}.enabled', 'boolean')
            if row.get('expected_topics') is not None:
                strings(row['expected_topics'], f'{path}.expected_topics')
            for key in ('semantic_role', 'source_role'):
                if row.get(key):
                    need(row[key].strip().lower() in ('', 'core_interest', 'adjacent_interest', 'broad_exploratory', 'mixed'),
                         f'{path}.{key}', 'known semantic role')
        optional_strings(data, '$', ('updated', 'channel_id', 'message_id'))
        if 'read_only_interest_sources' in data:
            array(data['read_only_interest_sources'], '$.read_only_interest_sources')
            for i, row in enumerate(data['read_only_interest_sources']):
                path = f'$.read_only_interest_sources[{i}]'
                obj(row, path)
                for key in ('name', 'path', 'purpose'):
                    text(row.get(key), f'{path}.{key}')
        if 'forbidden' in data:
            strings(data['forbidden'], '$.forbidden')
