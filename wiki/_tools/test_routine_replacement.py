"""Offline acceptance checks for the routine publication path."""
import importlib.util
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import ingestion_gate as gate
import wiki_build as build
import wiki_ops as ops


class RoutineReplacement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name in ('src/concepts', 'src/raw/notes', 'public/assets',
                     '_meta/ingestion/records', '.private-sources/novel'):
            (self.root / name).mkdir(parents=True)
        pages = {
            'src/index.md': '# Index\n\n## Concepts\n\n- [[seed|Seed]] — Seed concept.\n- [[concepts/novel|Novel concept]] — Source-backed observation.\n',
            'src/concepts/seed.md': '---\ntitle: Seed\npublished: undated\n---\n# Seed\n\nSource date: undated\n\nSeed concept. [Source](../raw/notes/legacy.md).\n',
            'src/raw/notes/legacy.md': '---\ntitle: Legacy\npublished: undated\n---\nLegacy source.\n',
            'src/concepts/novel.md': '---\ntitle: Novel concept\nupdated: 2026-10-14\n---\n# Novel concept\n\nSource date: 2026-01-02\n\nA small observation. [Source](../raw/notes/novel.md).\n',
            'src/log.md': '# Log\n',
        }
        for rel, text in pages.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        original = self.root / '.private-sources/novel/novel.private'
        original.write_bytes(b'PRIVATE SYNTHETIC ORIGINAL: restricted symbol 47893\n')
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        (self.root / 'src/raw/notes/novel.md').write_text(
            f'---\ntitle: Dated source\nprivate_original: .private-sources/novel/novel.private\nprivate_original_sha256: {digest}\npublished: 2026-01-02\n---\nSource date: 2026-01-02\n\nA small observation from a synthetic source.\n')
        baseline_files = {rel: hashlib.sha256((self.root / rel).read_bytes()).hexdigest()
                          for rel in ('src/index.md', 'src/concepts/seed.md', 'src/raw/notes/legacy.md')}
        baseline = {'version': 1, 'status': 'legacy-unreviewed', 'files': baseline_files,
                    'files_sha256': gate.sha(gate.canonical(baseline_files))}
        (self.root / '_meta/ingestion/baseline.json').write_text(json.dumps(baseline))
        (self.root / 'public/wiki-graph.json').write_text('{}\n')
        semantic = self.root / 'src/_meta/semantic'
        semantic.mkdir(parents=True)
        (semantic / 'graph.json').write_text('{}\n')
        (self.root / 'node_modules').symlink_to(ROOT / 'node_modules', target_is_directory=True)
        self.addCleanup(self.tmp.cleanup)

    def check(self):
        return gate.check(self.root)

    def pin(self, raw, name='new'):
        original = self.root / '.private-sources' / name / 'original.txt'
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(b'Original user text, kept outside public output.\n')
        text = raw.read_text()
        raw.write_text(text.replace('\n---\n',
            f'\nprivate_original: .private-sources/{name}/original.txt\nprivate_original_sha256: {hashlib.sha256(original.read_bytes()).hexdigest()}\n---\n', 1))
        return original

    def test_direct_curated_edit_and_source_backed_concept_without_receipt(self):
        p = self.root / 'src/concepts/seed.md'
        p.write_text(p.read_text() + '\nA corrected claim.\n')
        (self.root / 'src/concepts/new.md').write_text('---\ntitle: New\npublished: undated\n---\n# New\n\nSource date: undated\n\nSource-backed finding. [Source](../raw/notes/new.md).\n')
        (self.root / 'src/raw/notes/new.md').write_text('---\ntitle: New source\npublished: undated\n---\nSource date: undated\n\nPublic metadata only; original kept private.\n')
        self.pin(self.root / 'src/raw/notes/new.md')
        self.assertTrue(self.check()['ok'], self.check())

    def test_legacy_raw_change_or_deletion_fails(self):
        p = self.root / 'src/raw/notes/legacy.md'
        p.write_text(p.read_text() + 'altered\n')
        self.assertFalse(self.check()['ok'])
        p.unlink()
        self.assertFalse(self.check()['ok'])

    def test_restricted_original_cannot_enter_public(self):
        data = (self.root / '.private-sources/novel/novel.private').read_bytes()
        (self.root / 'public/assets/secret.bin').write_bytes(data)
        self.assertFalse(self.check()['ok'])
        (self.root / 'public/assets/secret.bin').unlink()
        (self.root / 'src/concepts/seed.md').write_bytes(data)
        self.assertFalse(self.check()['ok'])

    def test_nested_private_control_json_is_not_public_metadata(self):
        target = self.root / 'public/assets/metadata.json'
        target.write_text(json.dumps({'wrapper': [{'request_sha256': '0' * 64}]}))
        self.assertIn('private control metadata', ' '.join(self.check()['issues']))
        target.write_text(json.dumps({'source': 'A public locator'}))
        self.assertTrue(self.check()['ok'], self.check())

    def test_changed_public_note_cannot_embed_whole_private_text(self):
        raw = self.root / 'src/raw/notes/new.md'
        raw.write_text('---\npublished: undated\n---\nSource date: undated\n')
        original = self.pin(raw)
        text = b'An entire private source passage with details and qualifiers. ' * 4
        original.write_bytes(text)
        raw.write_text(raw.read_text().replace(
            hashlib.sha256(b'Original user text, kept outside public output.\n').hexdigest(),
            hashlib.sha256(text).hexdigest()))
        self.assertTrue(self.check()['ok'], self.check())
        raw.write_text(raw.read_text() + '\n' + text.decode())
        self.assertFalse(self.check()['ok'])
        raw.write_text(raw.read_text().replace(text.decode(), 'Public locator only.'))
        concept = self.root / 'src/concepts/seed.md'
        concept.write_text(concept.read_text() + '\n' + text.decode())
        self.assertFalse(self.check()['ok'])

    def test_approved_explanatory_image_allowed_not_original_pdf(self):
        for suffix in ('.png', '.jpg', '.jpeg', '.webp', '.svg'):
            asset = self.root / 'public/assets' / ('explanation' + suffix)
            asset.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"/>' if suffix == '.svg' else b'illustration bytes')
            self.assertTrue(self.check()['ok'], self.check())
        (self.root / 'public/assets/original.pdf').write_bytes(b'%PDF-1.4 private original')
        self.assertFalse(self.check()['ok'])

    def release(self):
        # Exercise the actual snapshot/admission/manifest/promotion path; only
        # external compiler invocation and unrelated derived files are stubbed.
        for name in ('_tools', '.vitepress'):
            if not (self.root / name).exists():
                shutil.copytree(ROOT / name, self.root / name)
        for name in ('package.json', 'package-lock.json'):
            shutil.copy2(ROOT / name, self.root / name)
        def fake_run(command, root):
            if 'vitepress' in str(command[0]):
                output = root / 'dist'
                output.mkdir()
                (output / 'index.html').write_text('release')
        with patch.object(build, 'WIKI_ROOT', self.root), patch.object(build, 'DIST', self.root / 'dist'), \
             patch.object(build, 'run', side_effect=fake_run), \
             patch.object(build, 'capture_public_asset_aliases', return_value={}), \
             patch.object(build, 'sync_generated'):
            build.build()
        return (self.root / 'dist/release-manifest.json').read_bytes()

    def test_release_pins_originals_across_builds_and_allows_new_source(self):
        first = json.loads(self.release())
        source = '.private-sources/novel/novel.private'
        self.assertEqual(first['original_source_hashes'][source],
                         hashlib.sha256((self.root / source).read_bytes()).hexdigest())
        self.assertFalse((self.root / 'dist/.private-sources').exists())
        second = json.loads(self.release())
        self.assertEqual(second['original_source_hashes'], first['original_source_hashes'])
        new = self.root / 'src/raw/notes/added.md'
        new.write_text('---\npublished: undated\n---\nSource date: undated\n')
        original = self.pin(new, 'added')
        third = json.loads(self.release())
        self.assertEqual(third['original_source_hashes']['.private-sources/added/original.txt'],
                         hashlib.sha256(original.read_bytes()).hexdigest())
        self.assertEqual(third['original_source_hashes'][source], first['original_source_hashes'][source])

    def test_final_recheck_skips_legacy_raw_syntax_and_recognizes_published_v2(self):
        legacy = self.root / 'src/raw/notes/legacy.md'
        legacy.write_bytes(b'---\r\nHistorical raw evidence, not new metadata.\r\n')
        v2 = self.root / 'src/raw/notes/prior-v2.md'
        v2.write_bytes(b'Historical v2 raw body without new locator frontmatter.\n')
        records = self.root / '_meta/ingestion/records'
        records.mkdir(parents=True, exist_ok=True)
        (records / 'prior-v2.json').write_text(json.dumps({
            'id': 'prior-v2', 'version': 2, 'hash_policy': gate.HASH_POLICY,
            'state': 'published', 'applied_at': '2026-01-02T00:00:00Z',
            'files': {'src/raw/notes/prior-v2.md': {'before': None, 'after': gate.sha(v2.read_bytes())}}}))
        dist = self.root / 'dist'
        dist.mkdir()
        (dist / 'release-manifest.json').write_text(json.dumps({
            'input_inventory': {'src/raw/notes/prior-v2.md': gate.sha(v2.read_bytes())}}))
        self.assertEqual(gate.recheck_private_pins(self.root), {
            '.private-sources/novel/novel.private': hashlib.sha256(
                (self.root / '.private-sources/novel/novel.private').read_bytes()).hexdigest()})

    def test_repin_or_removed_original_refuses_next_release_without_touching_dist(self):
        prior = self.release()
        original = self.root / '.private-sources/novel/novel.private'
        note = self.root / 'src/raw/notes/novel.md'
        original.write_bytes(b'silently replaced original')
        note.write_text(note.read_text().replace(
            json.loads(prior)['original_source_hashes']['.private-sources/novel/novel.private'],
            hashlib.sha256(original.read_bytes()).hexdigest()))
        with self.assertRaises(gate.GateError):
            self.release()
        self.assertEqual((self.root / 'dist/release-manifest.json').read_bytes(), prior)
        original.unlink()
        with self.assertRaises(gate.GateError):
            self.release()
        self.assertEqual((self.root / 'dist/release-manifest.json').read_bytes(), prior)

    def test_lost_or_corrupt_prior_original_map_and_symlink_refuse(self):
        prior = self.release()
        manifest = self.root / 'dist/release-manifest.json'
        original = self.root / '.private-sources/novel/novel.private'
        for replacement in ({}, {'original_source_hashes': {}},
                            {'original_source_hashes': {'bad': 'not-a-hash'}}):
            value = json.loads(prior)
            value.pop('original_source_hashes', None)
            value.update(replacement)
            manifest.write_text(json.dumps(value))
            with self.assertRaises(gate.GateError):
                self.release()
        manifest.write_bytes(prior)
        target = self.root / 'other-source'
        target.write_bytes(original.read_bytes())
        original.unlink()
        original.symlink_to(target)
        with self.assertRaises(gate.GateError):
            self.release()
        self.assertEqual(manifest.read_bytes(), prior)

    def test_private_pin_changes_after_final_drift_check_refuse_promotion(self):
        original = self.root / '.private-sources/novel/novel.private'
        old_dist = self.root / 'dist'
        old_dist.mkdir()
        (old_dist / 'index.html').write_text('previous release')
        def fake_run(command, root):
            if 'vitepress' in str(command[0]):
                (root / 'dist').mkdir(exist_ok=True)
        def mutate(_snapshot):
            original.write_bytes(b'changed after validation')
        with patch.object(build, 'WIKI_ROOT', self.root), patch.object(build, 'DIST', old_dist), \
             patch.object(build, 'run', side_effect=fake_run), \
             patch.object(build, 'capture_public_asset_aliases', return_value={}), \
             patch.object(build, 'sync_generated', side_effect=mutate):
            with self.assertRaises(gate.GateError):
                build.build()
        self.assertEqual((old_dist / 'index.html').read_text(), 'previous release')

    def test_private_pin_missing_changed_and_not_snapshotted(self):
        original = self.root / '.private-sources/novel/novel.private'
        with patch.object(build, 'WIKI_ROOT', self.root):
            expected = gate.inventory(self.root, build_inputs=True)
            with build.source_snapshot(expected) as snapshot:
                self.assertFalse((snapshot / '.private-sources').exists())
                self.assertTrue(gate.check(snapshot, private_root=self.root)['ok'])
        original.write_bytes(b'changed')
        self.assertFalse(self.check()['ok'])
        original.unlink()
        self.assertFalse(self.check()['ok'])

    def test_navigation_excluded_and_established_raw_pointer_accepted(self):
        (self.root / 'src/queries').mkdir(exist_ok=True)
        (self.root / 'src/queries/index.md').write_text('# Navigation only\n')
        curated = self.root / 'src/concepts/novel.md'
        curated.write_text('# Novel\n\nAs of 2026-01-02 the source describes a small observation. '
                           '^[raw/notes/novel.md]\n')
        with patch.object(ops, 'ROOT', self.root), patch.object(ops, 'SRC', self.root / 'src'), \
             patch.object(ops, 'INDEX', self.root / 'src/index.md'), \
             patch.object(ops, 'CHRONOLOGY_LEDGER', self.root / 'src/_meta/chronology-audit.json'), \
             patch.object(ops, 'SEMANTIC_DIR', self.root / 'src/_meta/semantic'):
            import contextlib, io
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = ops.validate(__import__('argparse').Namespace())
            self.assertEqual(status, 0, output.getvalue())

    def test_old_lifecycle_absent_and_pending_refused(self):
        for name in ('candidate_bytes', 'stage', 'seal', 'review', 'apply', 'initialize', 'update_draft', 'recover', 'validate_review', 'validate_payload'):
            self.assertFalse(hasattr(gate, name), name)
        journal = self.root / '_meta/ingestion/transactions/example/attempt-0001/journal.json'
        journal.parent.mkdir(parents=True)
        journal.write_text('{"state":"pending"}')
        self.assertFalse(self.check()['ok'])
        self.assertIn('unfinished historical apply transaction', self.check()['issues'][0])

    def test_new_raw_needs_dated_or_undated_status_and_no_binary_original(self):
        p = self.root / 'src/raw/notes/new.md'
        p.write_text('---\ntitle: New\n---\nText\n')
        self.assertFalse(self.check()['ok'])
        p.write_text('---\ntitle: New\npublished: undated\n---\nSource date: undated\n\nText\n')
        self.assertIn('private original', ' '.join(self.check()['issues']))
        original = self.pin(p)
        self.assertTrue(self.check()['ok'], self.check())
        p.write_text(p.read_text().replace('published: undated', 'published: 2026-01-02'))
        self.assertFalse(self.check()['ok'])
        p.write_text(p.read_text().replace('Source date: undated', 'Source date: 2026-01-02'))
        self.assertTrue(self.check()['ok'], self.check())
        original.write_bytes(b'Changed user original')
        self.assertFalse(self.check()['ok'])
        original.write_bytes(b'Original user text, kept outside public output.\n')
        original.unlink()
        self.assertFalse(self.check()['ok'])
        (self.root / 'public/assets/original.pdf').write_bytes(b'%PDF-1.4 example')
        self.assertFalse(self.check()['ok'])

    def test_private_original_path_symlink_refused(self):
        p = self.root / 'src/raw/notes/new.md'
        p.write_text('---\npublished: undated\n---\nSource date: undated\n')
        original = self.pin(p)
        self.assertTrue(self.check()['ok'], self.check())
        target = self.root / 'other-original.txt'
        target.write_bytes(original.read_bytes())
        original.unlink()
        original.symlink_to(target)
        self.assertFalse(self.check()['ok'])

    def test_historical_published_record_pins_raw_without_old_review_api(self):
        raw = self.root / 'src/raw/notes/prior-v2.md'
        raw.write_bytes(b'Published synthetic raw evidence.\n')
        record = {'id': 'prior-v2', 'version': 2, 'hash_policy': gate.HASH_POLICY,
                  'state': 'published', 'applied_at': '2026-01-02T00:00:00Z',
                  'files': {'src/raw/notes/prior-v2.md':
                            {'before': None, 'after': gate.sha(raw.read_bytes())}}}
        path = self.root / '_meta/ingestion/records/prior-v2.json'
        path.write_text(json.dumps(record))
        self.assertTrue(self.check()['ok'], self.check())
        raw.write_bytes(b'Changed synthetic raw evidence.\n')
        self.assertIn('immutable raw evidence', ' '.join(self.check()['issues']))
        record['files']['src/raw/notes/prior-v2.md']['after'] = gate.sha(raw.read_bytes())
        path.write_text(json.dumps(record))
        prior = self.root / 'dist/release-manifest.json'
        (self.root / 'dist').mkdir(exist_ok=True)
        prior.write_text(json.dumps({'input_inventory': {'src/raw/notes/prior-v2.md':
                                                        gate.sha(b'Published synthetic raw evidence.\n')}}))
        self.assertIn('previous release raw pin changed', ' '.join(self.check()['issues']))

    def test_previous_release_pins_historical_record_bytes_and_presence(self):
        raw = self.root / 'src/raw/notes/prior-v2.md'
        raw.write_bytes(b'Synthetic v2 raw.\n')
        record = {'id': 'prior-v2', 'version': 2, 'hash_policy': gate.HASH_POLICY,
                  'state': 'published', 'applied_at': '2026-01-02T00:00:00Z',
                  'files': {'src/raw/notes/prior-v2.md':
                            {'before': None, 'after': gate.sha(raw.read_bytes())}}}
        path = self.root / '_meta/ingestion/records/prior-v2.json'
        path.write_text(json.dumps(record))
        manifest = self.root / 'dist/release-manifest.json'
        manifest.parent.mkdir()
        manifest.write_text(json.dumps({'input_inventory': {
            '_meta/ingestion/records/prior-v2.json': gate.sha(path.read_bytes()),
            'src/raw/notes/prior-v2.md': gate.sha(raw.read_bytes())}}))
        self.assertTrue(self.check()['ok'], self.check())
        path.write_text(json.dumps({**record, 'files': {}}))
        self.assertIn('previous historical record missing or changed', ' '.join(self.check()['issues']))
        path.unlink()
        self.assertIn('missing regular file', ' '.join(self.check()['issues']))

    def test_obsolete_modules_have_no_non_test_imports(self):
        retired = ('draft_record_update', 'evidence_selection', 'exact_inputs',
                   'ingestion_orchestrator', 'ingestion_progress', 'model_routing',
                   'source_work', 'visual_packet', 'visual_evidence', 'publication_content')
        for name in retired:
            self.assertFalse((ROOT / '_tools' / (name + '.py')).exists(), name)
        for path in (ROOT / '_tools').glob('*.py'):
            if path.name.startswith('test_'):
                continue
            text = path.read_text()
            for name in retired:
                self.assertNotIn('import ' + name, text, str(path))

    def test_stale_inputs_and_failed_promotion_preserve_dist(self):
        with patch.object(build, 'WIKI_ROOT', self.root):
            expected = gate.inventory(self.root, build_inputs=True)
            (self.root / 'src/concepts/seed.md').write_text('changed')
            with self.assertRaises(gate.GateError):
                build.require_unchanged(expected)
            (self.root / 'dist').mkdir()
            (self.root / 'dist/index.html').write_text('prior release')
            with patch.object(build, 'run', side_effect=RuntimeError('compiler failed')):
                with self.assertRaises(RuntimeError):
                    build.build()
            self.assertEqual((self.root / 'dist/index.html').read_text(), 'prior release')

    def test_malformed_index_link_and_unsourced_concept_fail(self):
        (self.root / '_tools').mkdir()
        (self.root / '.vitepress').mkdir()
        shutil.copy2(ROOT / '_tools/wiki_ops.py', self.root / '_tools/wiki_ops.py')
        shutil.copy2(ROOT / '_tools/ingestion_gate.py', self.root / '_tools/ingestion_gate.py')
        shutil.copy2(ROOT / '.vitepress/validate-wiki-links.mjs', self.root / '.vitepress/validate-wiki-links.mjs')
        index = self.root / 'src/index.md'
        index.write_text(index.read_text() + '\n- [[missing|Missing]] — Wrong entry.\n')
        result = subprocess.run(['node', '.vitepress/validate-wiki-links.mjs'], cwd=self.root, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('missing', result.stderr + result.stdout)
        result = subprocess.run([sys.executable, '-B', '_tools/wiki_ops.py', 'validate'], cwd=self.root, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('extra index entries', result.stdout)
        index.write_text('# Index\n\n## Concepts\n\n- [[seed|Seed]] — Seed concept.\n- [[concepts/novel|Novel concept]] — Source-backed observation.\n')
        (self.root / 'src/concepts/novel.md').write_text('# Novel\n\nSource date: undated\n\nUncited claim.\n')
        result = subprocess.run([sys.executable, '-B', '_tools/wiki_ops.py', 'validate'], cwd=self.root, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('resolvable raw-source citation', result.stdout)

    def test_build_lock_contention(self):
        with patch.object(build, 'LOCK_FILE', self.root / 'lock'):
            with build.build_lock():
                with self.assertRaises(BlockingIOError):
                    with build.build_lock(nonblocking=True):
                        pass


if __name__ == '__main__':
    unittest.main()
