"""Future-only policy gates, real canonical deletion and offline crash recovery."""
from copy import deepcopy
from unittest.mock import patch
import unittest
import uuid

import notion_sync as s
import test_notion_sync as fixtures


class FutureChangesTests(unittest.TestCase):
    setUp = fixtures.RealCanonicalTests.setUp
    seed = fixtures.RealCanonicalTests.seed

    def enable(self):
        return s.enable_deletions(self.api, self.core, self.store, apply=True)

    def trash(self):
        self.api.pages[self.pid]['in_trash'] = True
        self.api.pages[self.pid]['last_edited_time'] += 'trash'
        self.api.hide.add(self.pid)

    def test_cutover_is_read_only_and_preserves_legacy_blank(self):
        r = self.seed()
        rows = self.core.read()
        rows[0]['start_date'] = None
        self.ops.write_registry(rows)
        self.api.edit(self.pid, start_date=None)
        self.store.value['bindings'][s.key(r)]['baseline']['start_date'] = None
        before = self.ops.REGISTRY_PATH.read_bytes()
        result = s.enable_deletions(self.api, self.core, self.store)
        self.assertEqual(result['planned'], 1)
        self.assertNotIn('deletion_policy', self.store.value)
        self.enable()
        self.assertEqual(self.ops.REGISTRY_PATH.read_bytes(), before)
        self.assertEqual(s.reconcile(self.api, self.core, self.store, apply=True)['planned'], 0)
        self.assertEqual(self.api.mutations(), [])
        with self.assertRaises(s.SyncError):
            self.enable()

    def test_cutover_ignores_changing_request_envelope(self):
        self.seed()
        original = self.api.request
        calls = []
        def response(method, path, body=None):
            value = original(method, path, body)
            value['request_id'] = str(uuid.uuid4())
            calls.append(value['request_id'])
            return value
        with patch.object(self.api, 'request', side_effect=response):
            self.assertEqual(self.enable()['applied'], 1)
        self.assertEqual(len(calls), len(set(calls)))
        self.assertEqual(self.api.mutations(), [])

    def test_cutover_rejects_relevant_page_change(self):
        self.seed()
        original = self.api.request
        reads = 0
        def response(method, path, body=None):
            nonlocal reads
            value = original(method, path, body)
            if path == '/pages/' + self.pid:
                reads += 1
                if reads == 2:
                    value['last_edited_time'] += 'changed'
            return value
        with patch.object(self.api, 'request', side_effect=response):
            with self.assertRaisesRegex(s.SyncError, 'remote-changed-during-cutover'):
                self.enable()
        self.assertNotIn('deletion_policy', self.store.value)
        self.assertEqual(len(self.core.read()), 1)

    def test_old_trash_quarantined_until_equal_restoration(self):
        r = self.seed()
        self.trash()
        self.assertEqual(self.enable()['excluded'], 1)
        for trash in (True,):
            self.api.pages[self.pid]['in_trash'] = trash
            result = s.reconcile(self.api, self.core, self.store, apply=True)
            self.assertEqual(result['planned'], 0)
            self.assertEqual(result['conflicts'][s.key(r)], 'pre-cutover-trash-excluded')
            self.assertEqual(len(self.core.read()), 1)
        self.api.pages[self.pid]['in_trash'] = False
        result = s.reconcile(self.api, self.core, self.store, apply=True)
        self.assertEqual(result['applied'], 1)
        self.assertIn(s.key(r), self.store.value['deletion_policy']['enrolled'])
        self.assertEqual(self.api.mutations(), [])
        self.trash()
        self.assertEqual(s.reconcile(self.api, self.core, self.store, apply=True)['applied'], 1)
        self.assertEqual(self.core.read(), [])

    def test_delete_removes_notes_index_no_recurrence_and_no_suffix_reuse(self):
        r = self.seed(recurrence='weekly')
        self.enable()
        self.trash()
        self.assertEqual(s.reconcile(self.api, self.core, self.store)['selected'], 1)
        self.assertEqual(len(self.core.read()), 1)
        self.assertEqual(s.reconcile(self.api, self.core, self.store, apply=True)['applied'], 1)
        self.assertEqual(self.core.read(), [])
        self.assertEqual(list((self.root / 'other').glob('T*.md')), [])
        self.assertNotIn('Fixture work', self.ops.INDEX_PATH.read_text())
        self.assertNotIn('completed occurrence', self.ops.LOG_PATH.read_text())
        self.assertEqual(self.ops.LOG_PATH.read_text().count('deleted via Notion'), 1)
        self.assertEqual(self.api.mutations(), [])
        self.api.pages[self.pid]['in_trash'] = False
        self.api.hide.clear()
        self.assertEqual(s.reconcile(self.api, self.core, self.store, apply=True)['planned'], 0)
        new = self.ops.add_task('next', '2026-06-01')
        self.assertGreater(self.ops.creation_order(new), self.ops.creation_order(r))

    def test_404_access_unknown_parent_and_identity_never_delete(self):
        self.seed()
        self.enable()
        original = self.api.request
        for mode in ('404', '403', 'db-trash', 'source-trash', 'marker', 'parent', 'missing-trash'):
            def guarded(method, path, body=None):
                if path == '/pages/' + self.pid and mode in ('404', '403'):
                    raise s.SyncError('http-' + mode)
                value = original(method, path, body)
                if path == '/databases/' + s.DB_ID and mode == 'db-trash':
                    value['in_trash'] = True
                if path == '/data_sources/' + s.SOURCE_ID and mode == 'source-trash':
                    value['in_trash'] = True
                if path == '/pages/' + self.pid:
                    if mode == 'marker':
                        value['properties'][s.MARKER]['rich_text'] = s.rich_text('hermes_tasks:999')
                    if mode == 'parent':
                        value['parent']['data_source_id'] = str(uuid.uuid4())
                    if mode == 'missing-trash':
                        value.pop('in_trash')
                return value
            with patch.object(self.api, 'request', side_effect=guarded):
                with self.assertRaises(s.SyncError):
                    s.reconcile(self.api, self.core, self.store, apply=True)
            self.assertEqual(len(self.core.read()), 1)
            self.assertEqual(self.ops.read_deletions(), {})

    def test_baseline_revision_and_remote_change_conflict(self):
        r = self.seed()
        self.enable()
        self.ops.amend_task(r['id'], notes='local')
        self.trash()
        result = s.reconcile(self.api, self.core, self.store, apply=True)
        self.assertEqual(result['conflicts'][s.key(r)], 'deletion-baseline-conflict')
        self.assertEqual(len(self.core.read()), 1)

    def test_revision_race_before_delete_stops_under_lock(self):
        r = self.seed()
        self.enable()
        self.trash()
        original = self.core.delete_external_task
        def raced(**kw):
            self.ops.amend_task(r['id'], notes='concurrent')
            return original(**kw)
        with patch.object(self.core, 'delete_external_task', side_effect=raced):
            result = s.reconcile(self.api, self.core, self.store, apply=True)
        self.assertEqual(result['applied'], 0)
        self.assertEqual(self.ops.read_deletions(), {})
        self.assertEqual(self.core.read()[0]['notes'], 'concurrent')

    def prepare_raced_delete(self):
        self.test_revision_race_before_delete_stops_under_lock()
        op = next(iter(self.store.value['pending'].values()))
        self.api.pages[self.pid]['in_trash'] = False
        self.api.hide.clear()
        self.api.edit(self.pid, notes='concurrent')
        row = self.core.read()[0]
        return dict(key_value=s.key(row), expected_operation_id=op['operation_id'],
                    expected_revision=s.revision(row))

    def test_prepared_delete_equal_recovery_and_future_sync(self):
        args = self.prepare_raced_delete()
        before = self.ops.REGISTRY_PATH.read_bytes()
        state = deepcopy(self.store.value)
        self.assertEqual(s.reconcile(self.api, self.core, self.store, apply=True)['applied'], 0)
        self.assertEqual(s.resolve_equal(self.api, self.core, self.store, **args)['applied'], 0)
        self.assertEqual(self.store.value, state)
        self.assertEqual(s.resolve_equal(self.api, self.core, self.store, **args, apply=True)['applied'], 1)
        self.assertEqual(self.ops.REGISTRY_PATH.read_bytes(), before)
        self.assertEqual(self.ops.read_deletions(), {})
        self.assertEqual(self.api.mutations(), [])
        self.assertEqual(self.store.value['pending'], {})
        self.assertEqual(self.store.value['bindings'][args['key_value']]['revision'], args['expected_revision'])
        self.api.edit(self.pid, notes='subsequent')
        self.assertEqual(s.reconcile(self.api, self.core, self.store, apply=True)['applied'], 1)
        self.assertEqual(self.core.read()[0]['notes'], 'subsequent')

    def test_prepared_delete_recovery_rejects_invalid_guards(self):
        args = self.prepare_raced_delete()
        state = deepcopy(self.store.value)
        before = self.ops.REGISTRY_PATH.read_bytes()
        for changes in ({'expected_operation_id': str(uuid.uuid4())},
                        {'expected_revision': args['expected_revision'] - 1}):
            with self.assertRaises(s.SyncError):
                s.resolve_equal(self.api, self.core, self.store, **(args | changes), apply=True)
        for mode in ('trash', 'unequal', 'db-trash', 'source-trash'):
            original = self.api.request
            def response(method, path, body=None):
                value = original(method, path, body)
                if ((mode == 'trash' and path == '/pages/' + self.pid) or
                    (mode == 'db-trash' and path == '/databases/' + s.DB_ID) or
                    (mode == 'source-trash' and path == '/data_sources/' + s.SOURCE_ID)):
                    value['in_trash'] = True
                if mode == 'unequal' and path == '/pages/' + self.pid:
                    value['properties']['notes']['rich_text'] = s.rich_text('different')
                return value
            with patch.object(self.api, 'request', side_effect=response):
                with self.assertRaises(s.SyncError):
                    s.resolve_equal(self.api, self.core, self.store, **args, apply=True)
        # Check both receipt snapshots, including publication during remote reads.
        receipt = {args['key_value'].split(':')[1]: {'operation_id': args['expected_operation_id']}}
        for snapshots in ([receipt], [{}, receipt]):
            with patch.object(self.ops, 'read_deletions', side_effect=snapshots):
                with self.assertRaisesRegex(s.SyncError, 'resolution-deletion-receipt-present'):
                    s.resolve_equal(self.api, self.core, self.store, **args, apply=True)
        self.assertEqual(self.store.value, state)
        self.assertEqual(self.ops.REGISTRY_PATH.read_bytes(), before)
        self.assertEqual(self.api.mutations(), [])

    def test_crash_repair_before_and_after_registry_commit(self):
        self.seed(recurrence='weekly')
        self.enable()
        self.trash()
        with patch.object(self.ops, 'write_registry', side_effect=OSError('crash')):
            with self.assertRaises(OSError):
                s.reconcile(self.api, self.core, self.store, apply=True)
        self.assertEqual(len(self.ops.read_deletions()), 1)
        op = next(iter(self.store.value['pending'].values()))
        self.api.pages[self.pid]['in_trash'] = False
        self.api.hide.clear()
        with self.assertRaisesRegex(s.SyncError, 'resolution-deletion-receipt-present'):
            s.resolve_equal(self.api, self.core, self.store, key_value=op['marker'],
                expected_operation_id=op['operation_id'],
                expected_revision=s.revision(self.core.read()[0]), apply=True)
        self.assertIn(op['marker'], self.store.value['pending'])
        with self.assertRaises(ValueError):
            self.ops.add_task('blocked until repair')
        with patch.object(self.ops, 'regenerate_notes', side_effect=OSError('cache crash')):
            with self.assertRaises(OSError):
                s.reconcile(self.api, self.core, self.store, apply=True)
        self.assertEqual(self.core.read(), [])
        # A restoration after the durable receipt cannot undo canonical deletion.
        self.api.pages[self.pid]['in_trash'] = False
        with patch.object(self.api, 'request', side_effect=AssertionError('recovery must not access remote')):
            result = s.reconcile(self.api, self.core, self.store, apply=True)
        self.assertEqual(result['applied'], 1)
        self.assertEqual(self.ops.LOG_PATH.read_text().count('deleted via Notion'), 1)
        self.assertEqual(s.reconcile(self.api, self.core, self.store, apply=True)['planned'], 0)

    def test_deleted_origin_and_old_receipts_cannot_reimport(self):
        r = self.seed()
        r = self.ops.apply_external_change(r['id'], {}, 1, 'old-operation')
        self.enable()
        self.trash()
        s.reconcile(self.api, self.core, self.store, apply=True)
        with self.assertRaises((ValueError, KeyError)):
            self.ops.apply_external_change(r['id'], {}, 1, 'old-operation')
        with self.assertRaises(ValueError):
            self.ops.apply_external_change(None, s.fields(r), None, 'new-operation', self.pid)

    def test_cap_and_zero_budget(self):
        self.seed()
        self.enable()
        self.trash()
        result = s.reconcile(self.api, self.core, self.store, apply=True, max_deletes=0)
        self.assertEqual((result['planned'], result['selected']), (1, 0))
        with self.assertRaises(s.SyncError):
            s.reconcile(self.api, self.core, self.store, max_deletes=4)

    def test_intake_default_start_is_actual_remote_write_and_preserved(self):
        p = fixtures.page(fixtures.row(start_date=None), 50)
        self.pid = p['id']
        self.api.pages[self.pid] = p
        s.initialize(self.api, self.core, self.store, ignored=fixtures.SAMPLES, apply=True)
        self.enable()
        s.reconcile(self.api, self.core, self.store, apply=True)
        r = self.core.read()[0]
        self.assertEqual(r['start_date'], '2026-05-20')
        self.assertEqual(s.page_fields(self.api.pages[self.pid], fixtures.COLS)['start_date'], r['start_date'])
        self.api.edit(self.pid, due_date='2026-05-25')
        s.reconcile(self.api, self.core, self.store, apply=True)
        self.assertEqual(self.core.read()[0]['start_date'], '2026-05-20')
        self.api.edit(self.pid, start_date=None)
        s.reconcile(self.api, self.core, self.store, apply=True)
        self.assertEqual(self.core.read()[0]['start_date'], '2026-05-25')
        self.assertEqual(s.page_fields(self.api.pages[self.pid], fixtures.COLS)['start_date'], '2026-05-25')


if __name__ == '__main__':
    unittest.main()
