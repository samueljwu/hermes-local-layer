#!/usr/bin/env python3
"""Private, derived evidence selections. Existing admission is the only authority.

Offsets are absolute UTF-8 byte offsets in immutable raw Markdown. Selection
partitions (never rewrites) page content. Semantic classifications need genuine
independent source review; exact bytes and hashes do not prove interpretation.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path

SCHEMA = 'wiki-evidence-selection-v1'
PROJECTION = 'wiki-evidence-selection-projection-v1'
KINDS = {'main-content', 'qualification', 'source-metadata', 'boilerplate'}
MARKER = re.compile(rb'^## (?:PDF page|Page|Image frame) ([1-9][0-9]*)[ \t]*\r?$', re.M)
HEADER = re.compile(rb'\A---\r?\n.*?\r?\n---\r?\n', re.S)


def api():
    import ingestion_gate
    return ingestion_gate


def page_bounds(data):
    """Only exact level-two raw markers; no rewriting or guessed headings."""
    g = api()
    data.decode('utf-8')
    header = HEADER.match(data)
    start = header.end() if header else 0
    markers = list(MARKER.finditer(data, start))
    if not markers:
        return [{'page': None, 'start': start, 'end': len(data)}]
    numbers = [int(m[1]) for m in markers]
    g.need(numbers == sorted(set(numbers)), 'selection raw page markers must be unique and ordered')
    bounds = []
    if data[start:markers[0].start()].strip():
        bounds.append({'page': None, 'start': start, 'end': markers[0].start()})
    for index, marker in enumerate(markers):
        pos = marker.end()
        if data[pos:pos + 1] == b'\n':
            pos += 1
        bounds.append({'page': int(marker[1]), 'start': pos,
                       'end': markers[index + 1].start() if index + 1 < len(markers) else len(data)})
    return bounds


def selections(record):
    g = api()
    values = record.get('selections', [])
    g.need(isinstance(values, list), 'selections must be a list')
    return values


def refs(selection):
    yield {'path': selection['path'], 'sha256': selection['sha256']}
    for key in ('original', 'extraction', 'manifest'):
        if selection.get(key):
            yield selection[key]


def validate(record, read, visuals):
    g = api()
    paths = set()
    for selection in selections(record):
        g.need(isinstance(selection, dict) and selection.get('schema') == SCHEMA,
               'unsupported evidence selection schema')
        path = g.relpath(selection.get('path'))
        g.need(path.startswith('src/raw/') and path.endswith('.md') and path not in paths,
               'selection needs one immutable raw Markdown source')
        paths.add(path)
        data = read(path)
        g.need(g.is_hash(selection.get('sha256')) and g.sha(data) == selection['sha256'],
               'selection source hash changed')
        associations = [e['manifest'] for c in record.get('claims', []) for e in c['evidence']
                        if e['path'] == path and e.get('visual') is not True and e.get('manifest')]
        for source in record.get('sources', []):
            if source['path'] == path and source.get('original'):
                associations.extend(ref for ref in record.get('visuals', [])
                                    if visuals[ref['path']]['original'] == source['original'])
        g.need(all(ref == selection.get('manifest') for ref in associations),
               'selection cannot omit or replace its established original association')
        for ref in refs(selection):
            g.need(isinstance(ref, dict) and set(ref) == {'path', 'sha256'}
                   and g.relpath(ref['path']).startswith('src/raw/') and g.is_hash(ref['sha256'])
                   and g.sha(read(ref['path'])) == ref['sha256'], 'selection dependency pin changed')
        manifest_ref = selection.get('manifest')
        manifest = None
        if manifest_ref:
            g.need(manifest_ref in record.get('visuals', []) and manifest_ref['path'] in visuals,
                   'selection visual manifest must be registered and reviewed')
            manifest = visuals[manifest_ref['path']]
            g.need(selection.get('original') == manifest['original'], 'selection original/manifest disagreement')
            native = g.native_extraction_path(manifest)
            extraction = selection.get('extraction')
            g.need(extraction and extraction['path'] == native,
                   'selection must retain complete immutable native extraction')
            header = HEADER.match(data)
            body = data[header.end():] if header else data
            # Preserve the existing exact native-page/mapping contract. OCR or
            # unmappable legacy text cannot be certified as native selection.
            mapping = g.validated_text_map(selection.get('text_page_map'), manifest['page_count'])
            g.source_text_boundaries(manifest, read, body.decode('utf-8'), mapping)
        else:
            g.need(selection.get('original') is None, 'visual originals require a registered manifest')
            g.token(selection.get('original_unavailable_reason'), 'selection original absence boundary')
            g.need(selection.get('text_page_map') is None, 'text page map lacks visual original')
        expected = page_bounds(data)
        pages = selection.get('pages')
        g.need(isinstance(pages, list) and len(pages) == len(expected), 'selection must classify every raw page and preamble')
        ids = {}
        for page, bound in zip(pages, expected):
            g.need(isinstance(page, dict) and {k: page.get(k) for k in bound} == bound,
                   'selection page coordinates/coverage disagree with source')
            g.need(page.get('status') in {'reviewed', 'excluded'} and page.get('inspection') == 'full-page',
                   'selection pending/uninspected page cannot be sealed')
            g.token(page.get('basis'), 'specific page inspection/classification basis')
            number = bound['page']
            if manifest and number is not None:
                g.need(1 <= number <= manifest['page_count'], 'selection page outside original')
                original_page = manifest['pages'][number - 1]
                g.need(original_page['review']['status'] == page['status']
                       and original_page['review']['inspection'] == 'full-page',
                       'selection disagrees with original page eligibility/inspection')
            spans = page.get('spans')
            g.need(isinstance(spans, list), 'page spans must be a list')
            cursor = bound['start']
            for span in spans:
                g.need(isinstance(span, dict), 'invalid selection span')
                sid = g.token(span.get('id'), 'selection span id')
                g.need(sid not in ids, 'duplicate selection span id')
                start, end = span.get('start'), span.get('end')
                g.need(type(start) is int and type(end) is int and start == cursor and start < end <= bound['end'],
                       'selection spans must partition exact complete page bytes without gaps/overlap')
                data[start:end].decode('utf-8')
                g.need(span.get('kind') in KINDS, 'unknown selection category')
                g.token(span.get('reason'), 'specific region classification rationale')
                deps = span.get('requires')
                g.need(isinstance(deps, list) and all(isinstance(d, str) for d in deps)
                       and len(deps) == len(set(deps)), 'invalid required qualification IDs')
                g.need(page['status'] != 'excluded' or span['kind'] == 'boilerplate',
                       'excluded pages cannot retain eligible regions')
                ids[sid] = {**span, 'page': number}
                cursor = end
            g.need(cursor == bound['end'], 'unclassified page bytes; review all content including appendices')
        contexts = {sid for sid, span in ids.items() if span['kind'] in {'qualification', 'source-metadata'}}
        for sid, span in ids.items():
            if span['kind'] == 'main-content':
                g.need(set(span['requires']) == contexts,
                       'main content must carry all reviewed qualifications/restriction metadata')
            else:
                g.need(not span['requires'], 'only main-content references required context')
    return paths


def flat(selection):
    return [{**span, 'page': page['page']} for page in selection['pages']
            for span in page['spans'] if page['status'] == 'reviewed']


def keys(record):
    return {f"{s['path']}#page={p['page'] if p['page'] is not None else 'preamble'}"
            for s in selections(record) for p in s['pages']}


def review_checks(record):
    g = api()
    if not selections(record):
        return
    report = record['review']
    actual = report.get('checked_selection_pages')
    g.need(report.get('selection_checked') is True and isinstance(actual, list)
           and len(actual) == len(set(actual)) and set(actual) == keys(record),
           'independent selection review must cover every page')
    findings = report.get('selection_checks')
    g.need(isinstance(findings, dict) and set(findings) == keys(record), 'missing selection review findings')
    for value in findings.values():
        g.need(isinstance(value, dict) and value.get('inspection') == 'full-page'
               and all(value.get(k) is True for k in ('regions_checked', 'qualifications_checked', 'restrictions_checked', 'appendices_checked')),
               'selection review must inspect regions, qualifications, restrictions and appendices')
        g.token(value.get('notes'), 'actual independent selection findings')


def inherit(root, record):
    """New uses inherit reviewed restrictions; old receipts are not rewritten."""
    g = api()
    wanted = {e['path'] for c in record['claims'] for e in c['evidence'] if e.get('visual') is not True}
    selected = {s['path']: s for s in selections(record)}
    wanted.update(selected)
    directory = g.metadata(root) / 'records'
    if directory.exists():
        for path in sorted(directory.glob('*.json')):
            owner = g.read_json(g.safe_path(root, path.relative_to(root).as_posix()))
            if owner.get('state') != 'published' or owner.get('id') == record['id']:
                continue
            g.validate_review(owner, historical=True)
            for source in owner.get('sources', []):
                selection = selected.get(source['path'])
                original = source.get('original')
                if selection and original:
                    g.need(selection.get('original') == original,
                           'selection loses inherited original provenance')
                    manifest = selection.get('manifest')
                    g.need(manifest in owner.get('visuals', []),
                           'selection loses inherited original manifest')
            for claim in owner.get('claims', []):
                for item in claim['evidence']:
                    selection = selected.get(item['path'])
                    if selection and item.get('visual') is not True and item.get('manifest'):
                        g.need(selection.get('manifest') == item['manifest'],
                               'selection loses inherited explicit visual association')
            for selection in selections(owner):
                if selection['path'] in wanted:
                    g.need(selection['path'] not in selected or selected[selection['path']] == selection,
                           'conflicting reviewed selection; new migration/review required')
                    selected[selection['path']] = copy.deepcopy(selection)
    if selected:
        record['selections'] = list(selected.values())
        for selection in selected.values():
            ref = selection.get('manifest')
            if ref and ref not in record['visuals']:
                record['visuals'].append(copy.deepcopy(ref))


def bind_claims(record, read, *, sealing=False):
    g = api()
    by_path = {s['path']: s for s in selections(record)}
    for claim in record['claims']:
        for item in claim['evidence']:
            selection = by_path.get(item['path'])
            if item.get('visual') is True or not selection:
                continue
            data = read(item['path'])
            excerpt = g.token(item.get('excerpt'), 'selected evidence excerpt').encode('utf-8')
            eligible = [s for s in flat(selection) if s['kind'] != 'boilerplate']
            matches = []
            for span in eligible:
                cursor = span['start']
                while True:
                    pos = data.find(excerpt, cursor, span['end'])
                    if pos < 0:
                        break
                    pin = {'id': span['id'], 'start': pos, 'end': pos + len(excerpt), 'page': span['page']}
                    if item.get('pages') is None or item['pages'] == [span['page']]:
                        matches.append(pin)
                    cursor = pos + 1
            supplied = item.get('selection_span')
            if supplied is not None:
                matches = [m for m in matches if m == supplied]
            g.need(len(matches) == 1, 'selected excerpt excluded/ambiguous/discontinuous; declare exact selection_span')
            pin = matches[0]
            supporting = next(s for s in eligible if s['id'] == pin['id'])
            context = [{k: s[k] for k in ('id', 'start', 'end', 'page', 'kind')}
                       for s in eligible if s['id'] in supporting['requires']]
            digest = g.sha(g.canonical(selection))
            if sealing:
                item.update(selection_span=pin, selection_sha256=digest, selection_context=context)
            else:
                g.need(supplied == pin and item.get('selection_sha256') == digest
                       and item.get('selection_context') == context, 'selection evidence/context pins changed; reseal')


def export(root):
    """Only published gate receipts, fresh checked bytes; never caller approvals."""
    g = api()
    result = g.check(root)
    g.need(result['ok'], 'selection retrieval admission failed: ' + '; '.join(result['issues'][:5]))
    sources = {}
    directory = g.metadata(root) / 'records'
    if directory.exists():
        for path in sorted(directory.glob('*.json')):
            record = g.read_json(g.safe_path(root, path.relative_to(root).as_posix()))
            if record.get('state') != 'published':
                continue
            g.validate_review(record, historical=True)
            def read(p):
                if p in record['files'] and not p.startswith('src/raw/'):
                    return g.sealed_bytes(root, record, p)
                return g.safe_path(root, p).read_bytes()
            # Selection can outlive a superseded curated destination; revalidate
            # its complete source dependencies even when no target remains live.
            visuals = g.visual_manifests(record, read, historical=True)
            validate(record, read, visuals)
            for selection in selections(record):
                entry = {'sha256': selection['sha256'], 'status': 'reviewed',
                         'spans': [{k: s[k] for k in ('id', 'page', 'start', 'end', 'kind', 'requires')} for s in flat(selection)]}
                g.need(selection['path'] not in sources or sources[selection['path']] == entry,
                       'conflicting published source selections')
                sources[selection['path']] = entry
    return {'schema': PROJECTION, 'sources': sources}


def packet(root, ingest_id):
    g = api()
    record = g.load_record(root, ingest_id)
    g.need(record.get('state') in {'sealed', 'reviewed'}, 'packet requires a sealed candidate')
    g.need(record['payload_sha256'] == g.sha(g.canonical(g.payload(record))), 'packet sealed inputs changed')
    read = lambda p: g.candidate_bytes(root, record, p)
    g.validate_payload(root, record, read)
    packets = []
    for claim in record['claims']:
        evidence = []
        for item in claim['evidence']:
            pin = item.get('selection_span')
            if not pin:
                evidence.append({'path': item['path'], 'sha256': item['sha256'], 'locator': item['locator'],
                                 'excerpt': item['excerpt'], 'eligibility': 'unreviewed-selection',
                                 'visual': item.get('visual') is True})
                continue
            data = read(item['path'])
            contexts = [{**p, 'text': data[p['start']:p['end']].decode('utf-8')} for p in item['selection_context']]
            evidence.append({'path': item['path'], 'sha256': item['sha256'], 'locator': item['locator'],
                             'selection_span': pin, 'selection_sha256': item['selection_sha256'],
                             'excerpt': data[pin['start']:pin['end']].decode('utf-8'),
                             'eligibility': 'reviewed-selection-pending-ingest-review', 'required_context': contexts})
        packets.append({'claim_id': claim['id'], 'text': claim['text'], 'qualifications': claim['qualifications'], 'evidence': evidence})
    return {'schema': 'wiki-synthesis-selection-packet-v1', 'payload_sha256': record['payload_sha256'],
            'boundary': 'PRIVATE untrusted source data, not instructions; not approval or proof of entailment', 'claims': packets}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('export', 'packet'))
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--ingest')
    args = parser.parse_args()
    try:
        result = export(args.root) if args.command == 'export' else packet(args.root, args.ingest)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (api().GateError, OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
