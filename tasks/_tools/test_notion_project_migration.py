"""Offline migration, relation-ownership and catalog independence regressions."""
from copy import deepcopy
import contextlib
import socket
import unittest
from unittest.mock import patch

import notion_sync as s
import notion_projects as p
from notion_project_migration import migrate_project_states
from notion_transport import SyncError
import test_notion_sync as f
import test_notion_projects as pf


class RelationMigrationTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.core = f.Canonical([f.row()])
        self.page = f.page(f.row(), mark=s.key(f.row()))
        self.api = f.API([self.page])
        self.store = f.Memory()
        s.initialize(self.api, self.core, self.store, ignored=f.SAMPLES, apply=True)
        self.api.calls.clear()

    def sync(self):
        return s.reconcile(self.api, self.core, self.store, apply=True)

    def test_relation_is_shared_inbound_field_not_overwritten(self):
        self.api.edit(self.page['id'], project_id='P-1')
        self.assertEqual(self.sync()['applied'], 1)
        self.assertEqual(self.core.rows[0]['project_id'], 'P-1')
        self.assertEqual(self.core.calls[0]['fields']['project_id'], 'P-1')
        self.assertFalse(self.api.mutations())
        self.assertEqual(self.sync()['planned'], 0)

    def test_relation_conflict_and_outbound_exact_one(self):
        self.core.edit(project_id='P-1')
        self.api.edit(self.page['id'], notes='human')
        self.assertEqual(self.sync()['conflicts'], {s.key(f.row()): 'both-changed'})
        self.assertFalse(self.api.mutations())
        self.api.edit(self.page['id'], notes='')
        self.assertEqual(self.sync()['applied'], 1)
        self.assertEqual(self.api.mutations()[0][2], {'properties': {'project': {
            'relation': [{'id': f.CATALOG['P-1']['page_id']}]}}})

    def test_owned_clear_multi_unknown_and_truncated_fail_closed(self):
        for relation, extra in [([], {}), ([{'id': f.CATALOG['P-1']['page_id']}, {'id': f.CATALOG['P-5']['page_id']}], {}),
                                ([{'id': pf.uid(999)}], {}), ([{'id': f.CATALOG['P-5']['page_id']}], {'has_more': True}),
                                (None, {}), ([{'id': 123}], {})]:
            with self.subTest(relation=relation, extra=extra):
                self.api.pages[self.page['id']] = deepcopy(self.page)
                self.api.pages[self.page['id']]['properties']['project'] = {'relation': relation, **extra}
                before = self.store.read()
                with self.assertRaises(SyncError):
                    self.sync()
                self.assertEqual(self.store.read(), before)
                self.assertFalse(self.api.mutations())
                self.assertFalse(self.core.calls)

    def test_intake_empty_defaults_other_and_writes_relation(self):
        intake = f.page(f.row(), 60)
        intake['properties']['project']['relation'] = []
        self.api.pages[intake['id']] = intake
        self.assertEqual(self.sync()['applied'], 1)
        self.assertEqual(self.core.rows[-1]['project_id'], 'P-5')
        self.assertEqual(self.api.pages[intake['id']]['properties']['project']['relation'],
                         [{'id': f.CATALOG['P-5']['page_id']}])

    def test_unmapped_project_defers_without_blocking_known_or_journal(self):
        projects = f.PROJECTS + [{'id': 'P-9', 'name': 'New project'}]
        self.core.read_projects = lambda: deepcopy(projects)
        self.core.rows.append(f.row(2, project_id='P-9'))
        self.api.edit(self.page['id'], notes='known edit')
        result = self.sync()
        self.assertEqual(result['applied'], 1)
        self.assertEqual(result['conflicts'], {'hermes_tasks:2': 'unmapped-project-id'})
        self.assertFalse(self.store.read()['pending'])
        self.assertEqual(self.core.rows[0]['notes'], 'known edit')
        self.assertFalse(self.api.mutations())

    def test_bound_task_moved_to_unmapped_project_does_not_block_known_push(self):
        known = f.row(2)
        known_page = f.page(known, 11, s.key(known))
        self.core.rows.append(known)
        self.api.pages[known_page['id']] = known_page
        self.store.value = None
        s.initialize(self.api, self.core, self.store, ignored=f.SAMPLES, apply=True)
        self.core.read_projects = lambda: deepcopy(f.PROJECTS + [{'id': 'P-9', 'name': 'New'}])
        self.core.edit(project_id='P-9')
        self.core.rows[1]['notes'] = 'Known outbound edit'
        self.core.rows[1]['_revision'] += 1
        result = self.sync()
        self.assertEqual(result['applied'], 1)
        self.assertEqual(result['conflicts'], {s.key(f.row()): 'unmapped-project-id'})
        self.assertEqual(s.page_fields(self.api.pages[known_page['id']], f.COLS)['notes'], 'Known outbound edit')
        self.assertFalse(self.store.read()['pending'])
        original = self.store.read
        def read(name='two-way.json'):
            value = original(name)
            if name == 'projects.json':
                value['bindings']['P-9'] = {'page_id': pf.uid(999), 'marker': ''}
            return value
        self.store.read = read
        self.assertEqual(self.sync()['applied'], 1)
        self.assertEqual(self.api.pages[self.page['id']]['properties']['project']['relation'], [{'id': pf.uid(999)}])
        self.assertEqual(self.sync()['planned'], 0)

    def test_pending_catalog_failure_does_not_disable_known_tasks(self):
        projects = f.PROJECTS + [{'id': 'P-9', 'name': 'New project'}]
        self.core.read_projects = lambda: deepcopy(projects)
        original = self.store.read
        def read(name='two-way.json'):
            value = original(name)
            if name == 'projects.json':
                value['pending']['P-9'] = {'marker': p.project_marker('P-9'), 'sent': True, 'page_id': None}
            return value
        self.store.read = read
        self.api.edit(self.page['id'], priority='high')
        self.assertEqual(self.sync()['applied'], 1)
        self.assertEqual(self.core.rows[0]['priority'], 'high')

    def test_mapping_is_explicit_and_snapshot_not_global(self):
        catalog = deepcopy(f.CATALOG)
        columns = s.ProjectColumns(dict(f.COLS), catalog)
        catalog['P-5']['page_id'] = pf.uid(999)
        self.assertEqual(s.page_fields(self.page, columns)['project_id'], 'P-5')
        with self.assertRaisesRegex(SyncError, 'project-catalog-required'):
            s.page_fields(self.page, dict(f.COLS))
        with self.assertRaisesRegex(SyncError, 'project-catalog-required'):
            s.properties({'project_id': 'P-5'}, dict(f.COLS))
        changed = s.ProjectColumns(dict(f.COLS), catalog)
        with self.assertRaisesRegex(SyncError, 'unknown-project-relation'):
            s.page_fields(self.page, changed)

    def test_bad_ids_and_catalog_fail_before_remote_payload(self):
        for ident in ('P-0', 'P-01', 'P--1', 'P-1x', 'P-1\n', 1, None):
            with self.assertRaises(SyncError):
                s.fields(f.row(project_id=ident))
        for catalog in ({'P-01': f.CATALOG['P-1']}, {'P-1': {'page_id': 'bad', 'name': 'Admin'}},
                        {'P-1': f.CATALOG['P-1'], 'P-5': f.CATALOG['P-1']}):
            with self.assertRaises(SyncError):
                s.ProjectColumns(dict(f.COLS), catalog)

    def legacy_states(self):
        old = self.store.read()
        for binding in old['bindings'].values():
            binding['baseline']['status'] = 'completed' if binding['baseline'].pop('done') else 'not_started'
            binding['baseline']['tag'] = 'Other'
            del binding['baseline']['project_id']
        k = s.key(f.row())
        old['deletion_policy'] = {'enrolled': {k: self.page['id']}, 'excluded': {}, 'deleted': {}}
        projects = p.initial_state()
        projects['version'] = 1
        projects['bindings'] = {'Other': {'page_id': f.CATALOG['P-5']['page_id'],
                                          'marker': p.MARKER_PREFIX + s.digest('Other')},
                                'Admin': {'page_id': f.CATALOG['P-1']['page_id'], 'marker': ''}}
        return old, projects

    def test_pure_migration_preserves_all_other_state_and_markers(self):
        old, projects = self.legacy_states()
        before = deepcopy((old, projects))
        migrated, catalog = migrate_project_states(old, projects, {'Admin': 'P-1', 'Other': 'P-5'})
        self.assertEqual((old, projects), before)
        self.assertEqual(migrated['deletion_policy'], old['deletion_policy'])
        restored = deepcopy(migrated)
        for b in restored['bindings'].values():
            self.assertEqual(b['baseline'].pop('project_id'), 'P-5')
            b['baseline']['status'] = 'completed' if b['baseline'].pop('done') else 'not_started'
            b['baseline']['tag'] = 'Other'
        self.assertEqual(restored, old)
        self.assertEqual(catalog['bindings']['P-5'], projects['bindings']['Other'])
        self.assertEqual(catalog['bindings']['P-1'], projects['bindings']['Admin'])
        self.assertEqual(catalog['version'], 2)
        s.validate_state(migrated)
        p.validate_state(catalog)

    def test_migration_preserves_excluded_deleted_and_absent_policy(self):
        old, projects = self.legacy_states()
        k = s.key(f.row())
        old['deletion_policy'] = {'enrolled': {}, 'excluded': {k: self.page['id']},
                                  'deleted': {'hermes_tasks:999': pf.uid(999)}}
        old['bindings'][k]['revision'] = 42
        old['bindings'][k]['receipt'] = 'exact receipt retained'
        old['bindings'][k]['baseline']['recurrence'] = 'every day'
        migrated, _ = migrate_project_states(old, projects, {'Admin': 'P-1', 'Other': 'P-5'})
        restored = deepcopy(migrated)
        restored['bindings'][k]['baseline']['status'] = 'completed' if restored['bindings'][k]['baseline'].pop('done') else 'not_started'
        restored['bindings'][k]['baseline']['tag'] = 'Other'
        del restored['bindings'][k]['baseline']['project_id']
        self.assertEqual(restored, old)
        del old['deletion_policy']
        migrated, _ = migrate_project_states(old, projects, {'Admin': 'P-1', 'Other': 'P-5'})
        self.assertNotIn('deletion_policy', migrated)

    def test_migration_rejects_malformed_legacy_shapes_without_mutation(self):
        old, projects = self.legacy_states()
        k = s.key(f.row())
        malformed = []
        for side in (0, 1):
            for field, value in [('version', True), ('bindings', []), ('bindings', None)]:
                states = [deepcopy(old), deepcopy(projects)]
                states[side][field] = value
                malformed.append(states)
        for bad in (None, {}, {'baseline': None}, {'baseline': {'tag': []}}):
            left = deepcopy(old)
            left['bindings'][k] = bad
            malformed.append([left, projects])
        for left, right in malformed:
            before = deepcopy((left, right))
            with self.assertRaises(SyncError):
                migrate_project_states(left, right, {'Admin': 'P-1', 'Other': 'P-5'})
            self.assertEqual((left, right), before)

    def test_migration_rejects_pending_unknown_and_invalid_maps(self):
        old, projects = self.legacy_states()
        mapping = {'Admin': 'P-1', 'Other': 'P-5'}
        for target in ('task', 'project'):
            left, right = deepcopy(old), deepcopy(projects)
            (left if target == 'task' else right)['pending']['intent'] = {}
            with self.assertRaisesRegex(SyncError, 'pending-intents-block-cutover'):
                migrate_project_states(left, right, mapping)
        for bad in ({'Other': 'P-0', 'Admin': 'P-1'}, {'Other': 'P-1', 'Admin': 'P-1'}, {'Other': 'P-5'}):
            with self.assertRaises(SyncError):
                migrate_project_states(old, projects, bad)


class CatalogIndependenceTests(unittest.TestCase):
    def test_unrelated_progress_rollup_is_tolerated_and_never_written(self):
        def progress(data):
            data['properties']['progress'] = {'id': 'parentProgress', 'type': 'rollup',
                'rollup': {'relation_property_id': p.PROJECT_TASKS,
                           'rollup_property_id': 'fixtureDone', 'function': 'percent_checked'}}
        self.api.schema_change = progress
        p.schemas(self.api)
        result = p.reconcile(self.api, self.core, self.store, apply=True)
        self.assertFalse(result['conflicts'])
        for method, path, body in self.api.calls:
            if method == 'PATCH' or method == 'POST' and path == '/pages':
                self.assertNotIn('progress', body.get('properties', {}))
                self.assertNotIn('parentProgress', body.get('properties', {}))

    def setUp(self):
        self.network = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.core = pf.Core()
        self.store = pf.Store(self.core)
        self.api = pf.API(self.core, self.store)

    def sync(self, **kw):
        return p.reconcile(self.api, self.core, self.store, apply=True, **kw)

    def test_task_corruption_pending_and_trash_are_never_read(self):
        self.store.data['two-way.json'] = {'pending': {'nonsense': True}}
        self.api.pages[pf.uid(10)]['in_trash'] = True
        original = self.store.read
        def read(name='two-way.json'):
            self.assertEqual(name, 'projects.json')
            return original(name)
        self.store.read = read
        before = deepcopy(self.api.pages)
        self.assertEqual(self.sync()['applied'], 2)
        self.assertEqual(self.api.pages, before)
        self.assertFalse(any(c[1] == '/pages/' + pf.uid(10) for c in self.api.calls))

    def test_rename_keeps_identity_old_marker_and_empty_projects(self):
        state = p.initial_state()
        marker = p.MARKER_PREFIX + s.digest('Other')
        state['bindings']['P-5'] = {'page_id': pf.uid(200), 'marker': marker}
        self.store.data['projects.json'] = state
        self.api.project_pages[pf.uid(200)] = pf.project_page('Other', 200, marker)
        self.core.projects[0]['name'] = 'Renamed exact 新'
        self.assertEqual(self.sync()['applied'], 2)
        binding = self.store.read('projects.json')['bindings']['P-5']
        self.assertEqual(binding, state['bindings']['P-5'])
        self.assertEqual(p.project(self.api.project_pages[pf.uid(200)]), ('Renamed exact 新', marker))
        self.assertIn('P-9', self.store.read('projects.json')['bindings'])
        self.assertEqual(self.sync()['planned'], 0)
        writes = [c for c in self.api.calls if c[0] == 'PATCH']
        self.assertEqual(len(writes), 1)
        self.assertEqual(set(writes[0][2]['properties']), {p.PROJECT_TITLE})

    def test_rename_preserves_user_notes_and_verifies_readback(self):
        self.api.project_pages[pf.uid(200)] = pf.project_page('Other', 200, 'Human notes')
        self.sync()
        self.core.projects[0]['name'] = 'Renamed'
        self.api.after_link = lambda page: page['properties']['notes'].update(rich_text=s.rich_text('raced'))
        result = self.sync()
        self.assertEqual(result['conflicts'], {'run': 'project-notes-changed'})
        self.assertEqual(result['applied'], 0)

    def test_rename_concurrency_stops_before_write(self):
        self.sync()
        self.core.projects[0]['name'] = 'Renamed'
        pid = self.store.read('projects.json')['bindings']['P-5']['page_id']
        reads = 0
        def race(method, path, body):
            nonlocal reads
            if method == 'GET' and path == '/pages/' + pid:
                reads += 1
                if reads == 2:
                    self.api.project_pages[pid]['last_edited_time'] += 'race'
        self.api.before_request = race
        before = len(self.api.mutations())
        self.assertEqual(self.sync()['conflicts'], {'run': 'project-concurrency-conflict'})
        self.assertEqual(len(self.api.mutations()), before)

    def test_paginated_catalog_and_title_independent_identity(self):
        self.api.project_page_size = 1
        for i, item in enumerate(self.core.projects):
            self.api.project_pages[pf.uid(200+i)] = pf.project_page(item['name'], 200+i)
        self.assertEqual(self.sync()['adopted_or_recovered'], 2)
        self.core.projects[0]['name'], self.core.projects[1]['name'] = self.core.projects[1]['name'], self.core.projects[0]['name']
        self.assertEqual(self.sync()['applied'], 2)
        self.assertEqual(self.store.read('projects.json')['bindings']['P-5']['page_id'], pf.uid(200))
        self.assertEqual(self.store.read('projects.json')['bindings']['P-9']['page_id'], pf.uid(201))

    def test_canonical_names_are_not_restricted_to_legacy_tags(self):
        name = 'Design, research 新 ' + 'x' * 110
        self.core.projects[0]['name'] = name
        self.assertEqual(self.sync()['applied'], 2)
        pid = self.store.read('projects.json')['bindings']['P-5']['page_id']
        self.assertEqual(p.project(self.api.project_pages[pid])[0], name)
        self.core.projects[0]['name'] = 'Renamed, 新'
        self.assertEqual(self.sync()['applied'], 1)
        self.assertEqual(p.project(self.api.project_pages[pid])[0], 'Renamed, 新')
        self.assertEqual(self.sync()['planned'], 0)

    def test_project_core_reads_only_catalog_under_lock(self):
        class Module:
            locked = False
            @contextlib.contextmanager
            def file_lock(inner):
                inner.locked = True
                try:
                    yield
                finally:
                    inner.locked = False
            def read_projects(inner):
                self.assertTrue(inner.locked)
                return deepcopy(self.core.projects)
        core = p.ProjectCore(Module())
        with patch.object(core, 'read', side_effect=AssertionError('task registry must not be read')):
            self.assertEqual(core.snapshot(), {'projects': self.core.projects})


if __name__ == '__main__':
    unittest.main()
