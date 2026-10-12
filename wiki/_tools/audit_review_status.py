#!/usr/bin/env python3
"""Read-only semantic-review reuse plan; never approves claims or publication."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plan(root=ROOT):
    ledger = json.loads((root / '_meta/audits/reviewed.json').read_text())
    source = root / 'src'
    certificates = dict(ledger['certificates'])
    entries = {entry['path']: entry for entry in ledger['files']}
    assert len(entries) == len(ledger['files']), 'Duplicate reviewed paths'
    resolutions = {}
    resolution_reports = []
    for reference in ledger.get('remediation_ledgers', []):
        overlay_path = root / reference['path']
        assert digest(overlay_path) == reference['sha256'], 'Remediation ledger drift'
        overlay = json.loads(overlay_path.read_text())
        for key, value in overlay['certificates'].items():
            assert key not in certificates, 'Duplicate review certificate'
            certificates[key] = value
        for entry in overlay['files']:
            previous = entries.get(entry['path'])
            if previous is not None:
                assert previous['sha256'] == entry['previous_sha256'], 'Broken review lineage'
            entries[entry['path']] = entry
        resolutions.update(overlay['finding_dispositions'])
        resolution_reports.append(str(overlay_path))
    valid = {
        pid: all(Path(item['path']).is_file()
                 and digest(Path(item['path'])) == item['sha256']
                 for item in items.values())
        for pid, items in certificates.items()
    }
    output = {'reusable_review': [], 'needs_review': [], 'missing_reviewed_files': [],
              'invalid_certificates': sorted(pid for pid, ok in valid.items() if not ok),
              'prior_report': ledger['report_markdown'],
              'historical_claim_status_counts': ledger['claim_status_counts'],
              'finding_dispositions': resolutions,
              'resolution_reports': resolution_reports,
              'note': 'Reuse semantic reading only. Carry forward findings/unresolved claims; '
                      'review dependency-affected claims and rerun structural checks. '
                      'Non-Markdown assets are not certified.'}
    present = set()
    for path in sorted(source.rglob('*.md')):
        relative = path.relative_to(source).as_posix()
        present.add(relative)
        entry = entries.get(relative)
        if entry is None:
            reason = 'not_previously_reviewed'
        elif path.is_symlink() or any(p.is_symlink() for p in path.parents if p != source.parent):
            reason = 'symlink_requires_review'
        elif digest(path) != entry['sha256']:
            reason = 'changed_since_review'
        elif not all(valid.get(r['partition_id'], False) for r in entry['ranges']):
            reason = 'review_certificate_missing_or_changed'
        else:
            output['reusable_review'].append(relative)
            continue
        output['needs_review'].append({'path': relative, 'reason': reason})
    output['missing_reviewed_files'] = sorted(set(entries) - present)
    output['counts'] = {key: len(output[key]) for key in
                        ['reusable_review', 'needs_review', 'missing_reviewed_files']}
    return output


if __name__ == '__main__':
    print(json.dumps(plan(), indent=2))
