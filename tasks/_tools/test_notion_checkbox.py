"""Hermetic strict-checkbox and durable local cancellation regressions."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import notion_sync as s
from notion_transport import SyncError
from notion_project_migration import migrate_project_states
import test_notion_sync as f
import test_notion_project_migration as migration


class CheckboxTests(unittest.TestCase):
    def test_strict_bool_both_directions(self):
        for invalid in (None, 0, 1, '', 'false', 'completed', [], {}):
            with self.subTest(value=invalid):
                with self.assertRaises(SyncError):
                    s.fields(f.row(done=invalid))
                with self.assertRaises(SyncError):
                    s.properties({'done': invalid}, f.COLS)
                page = f.page(f.row())
                page['properties']['done']['checkbox'] = invalid
                with self.assertRaises(SyncError):
                    s.page_fields(page, f.COLS)
        for done in (False, True):
            self.assertIs(s.page_fields(f.page(f.row(done=done)), f.COLS)['done'], done)

    def test_dynamic_binding_is_explicit_pinned_and_not_global(self):
        api = f.API()
        with self.assertRaisesRegex(SyncError, 'invalid-checkbox-binding'):
            s.schema(api, f.CATALOG)
        self.assertFalse(api.calls)
        for value in (None, {}, {'source_id': s.SOURCE_ID, 'done_property_id': 'US%5Cb'},
                      {'source_id': 'wrong', 'done_property_id': 'new'},
                      {'source_id': s.SOURCE_ID, 'done_property_id': 'title'}):
            with self.assertRaises(SyncError):
                s.schema(api, f.CATALOG, mapping=value,
                         est_mapping={'source_id': s.SOURCE_ID, 'est_time_property_id': 'fixtureEst'})
        for actual in ('new%3Aid', 'second'):
            api.property_ids['done'] = actual
            mapping = {'source_id': s.SOURCE_ID, 'done_property_id': actual}
            self.assertEqual(s.schema(api, f.CATALOG, mapping=mapping,
                est_mapping={'source_id': s.SOURCE_ID, 'est_time_property_id': 'fixtureEst'})['done'], 'done')
        self.assertNotIn('done', s.PROPERTY_IDS)
        self.assertFalse(api.mutations())

    def test_checked_and_unchecked_round_trip(self):
        core, store = f.Canonical([f.row()]), f.Memory()
        page = f.page(f.row(), mark=s.key(f.row()))
        api = f.API([page])
        s.initialize(api, core, store, ignored=f.SAMPLES, apply=True)
        for done in (True, False):
            api.edit(page['id'], done=done)
            self.assertEqual(s.reconcile(api, core, store, apply=True)['applied'], 1)
            self.assertIs(core.rows[0]['done'], done)
        for done in (True, False):
            core.edit(done=done)
            self.assertEqual(s.reconcile(api, core, store, apply=True)['applied'], 1)
            self.assertIs(s.page_fields(api.pages[page['id']], f.COLS)['done'], done)


class CancellationTests(f.RealCanonicalTests):
    def cancel_bound(self):
        row = self.seed()
        self.ops.cancel_task(row['id'])
        return row, deepcopy(self.ops.read_deletions())

    def test_cancel_trashes_exact_page_preserves_full_archive_and_never_reimports(self):
        row, ledger = self.cancel_bound()
        self.assertEqual(ledger['1']['snapshot'], row)
        self.assertIsNone(ledger['1']['page_id'])
        planned = s.reconcile(self.api, self.core, self.store)
        self.assertEqual(planned['planned'], 1)
        self.assertFalse(self.api.pages[self.pid]['in_trash'])
        result = s.reconcile(self.api, self.core, self.store, apply=True)
        self.assertEqual(result['applied'], 1)
        self.assertEqual(self.api.mutations()[-1][2], {'in_trash': True})
        self.assertEqual(self.ops.read_deletions(), ledger)
        self.assertNotIn(s.key(row), self.store.read()['bindings'])
        self.assertEqual(self.store.read()['deletion_policy']['deleted'], {s.key(row): self.pid})
        # Even manual restoration must not import or recreate a canonical task.
        self.api.pages[self.pid]['in_trash'] = False
        self.assertEqual(s.reconcile(self.api, self.core, self.store, apply=True)['planned'], 0)
        self.assertEqual(self.core.read(), [])

    def test_cancel_lost_patch_ack_recovers_without_second_patch(self):
        _, ledger = self.cancel_bound()
        self.api.after_patch = lambda _: (_ for _ in ()).throw(OSError('lost ack'))
        with self.assertRaises(OSError):
            s.reconcile(self.api, self.core, self.store, apply=True)
        self.assertTrue(self.api.pages[self.pid]['in_trash'])
        self.api.after_patch = None
        before = len(self.api.mutations())
        self.assertEqual(s.reconcile(self.api, self.core, self.store, apply=True)['applied'], 1)
        self.assertEqual(len(self.api.mutations()), before)
        self.assertEqual(self.ops.read_deletions(), ledger)

    def test_cancel_pending_pins_complete_original_receipt(self):
        self.cancel_bound()
        self.api.after_patch = lambda _: (_ for _ in ()).throw(OSError('lost ack'))
        with self.assertRaises(OSError):
            s.reconcile(self.api, self.core, self.store, apply=True)
        self.api.after_patch = None
        ledger = self.ops.read_deletions()
        ledger['1']['snapshot']['reminder'] = {'changed': True}
        with patch.object(self.ops, 'read_deletions', return_value=ledger):
            result = s.reconcile(self.api, self.core, self.store, apply=True)
        self.assertEqual(result['conflicts'], {'hermes_tasks:1': 'cancellation-receipt-changed'})
        self.assertIn('hermes_tasks:1', self.store.read()['bindings'])

    def test_cancel_already_trashed_and_query_absent_recovers(self):
        self.cancel_bound()
        self.api.pages[self.pid]['in_trash'] = True
        self.api.hide.add(self.pid)
        self.assertEqual(s.reconcile(self.api, self.core, self.store, apply=True)['applied'], 1)
        self.assertFalse(self.api.mutations())

    def test_missing_without_receipt_and_invalid_receipt_fail_closed(self):
        self.seed()
        self.ops.write_registry([])
        with self.assertRaisesRegex(SyncError, 'missing-canonical-binding'):
            s.reconcile(self.api, self.core, self.store, apply=True)
        self.assertFalse(self.api.mutations())

    def test_cancel_remote_edit_conflicts_and_ownership_loss_is_fatal(self):
        self.cancel_bound()
        self.api.edit(self.pid, notes='human edit')
        self.assertEqual(s.reconcile(self.api, self.core, self.store, apply=True)['conflicts'],
                         {'hermes_tasks:1': 'cancellation-baseline-conflict'})
        self.api.pages[self.pid]['properties'][s.MARKER]['rich_text'] = []
        with self.assertRaisesRegex(SyncError, 'ownership-changed'):
            s.reconcile(self.api, self.core, self.store, apply=True)
        self.assertFalse(self.api.mutations())

    def test_cancellation_budget_and_unexported_history(self):
        self.cancel_bound()
        self.assertEqual(s.reconcile(self.api, self.core, self.store, apply=True, max_deletes=0)['selected'], 0)
        self.assertFalse(self.api.mutations())
        s.reconcile(self.api, self.core, self.store, apply=True)
        row = self.ops.add_task('Never exported', '2026-05-20')
        self.ops.cancel_task(row['id'])
        before = len(self.api.mutations())
        self.assertEqual(s.reconcile(self.api, self.core, self.store, apply=True)['planned'], 0)
        self.assertEqual(len(self.api.mutations()), before)

    def test_cancel_tampered_snapshot_rejected(self):
        self.cancel_bound()
        ledger = self.ops.read_deletions()
        ledger['1']['snapshot']['_revision'] += 1
        with patch.object(self.ops, 'read_deletions', return_value=ledger):
            with self.assertRaisesRegex(SyncError, 'cancellation-snapshot-mismatch'):
                s.reconcile(self.api, self.core, self.store, apply=True)
        self.assertFalse(self.api.mutations())


class CancelMigrationTests(migration.RelationMigrationTests):
    def test_cancelled_bound_requires_explicit_exact_handled_scope(self):
        old, catalog = self.legacy_states()
        key = s.key(f.row())
        old['bindings'][key]['baseline']['status'] = 'cancelled'
        before = deepcopy(old)
        for scope in (None, {}, {key: f.SAMPLES[0]}):
            with self.assertRaisesRegex(SyncError, 'bound-cancelled-needs-handled-scope'):
                migrate_project_states(old, catalog, {'Admin': 'P-1', 'Other': 'P-5'}, handled_cancelled=scope)
        migrated, _ = migrate_project_states(old, catalog, {'Admin': 'P-1', 'Other': 'P-5'},
                                             handled_cancelled={key: self.page['id']})
        self.assertEqual(old, before)
        self.assertNotIn(key, migrated['bindings'])
        self.assertEqual(migrated['deletion_policy']['deleted'][key], self.page['id'])
