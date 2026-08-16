from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path('/home/hermes/.hermes/scripts/local_ops.py')


def load_local_ops():
    spec = importlib.util.spec_from_file_location('local_ops_tested', SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class LocalOpsTests(unittest.TestCase):
    def test_tasks_lock_rejects_symlink_without_truncating_target(self):
        local_ops = load_local_ops()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'tasks'
            (root / '_meta').mkdir(parents=True)
            target = Path(td) / 'protected.txt'
            target.write_text('preserve me\n', encoding='utf-8')
            (root / '_meta' / '.task_ops.lock').symlink_to(target)

            with self.assertRaises(OSError):
                with local_ops.tasks_lock(root):
                    pass

            self.assertEqual(target.read_text(encoding='utf-8'), 'preserve me\n')
    def test_resolve_tasks_root_defaults_to_canonical_root(self):
        local_ops = load_local_ops()
        old_tasks_root = os.environ.pop('TASKS_ROOT', None)
        old_allow = os.environ.pop('HERMES_ALLOW_NONCANONICAL_LOCAL_ROOTS', None)
        try:
            self.assertEqual(local_ops.resolve_tasks_root(), Path('/home/hermes/tasks').resolve())
        finally:
            if old_tasks_root is not None:
                os.environ['TASKS_ROOT'] = old_tasks_root
            if old_allow is not None:
                os.environ['HERMES_ALLOW_NONCANONICAL_LOCAL_ROOTS'] = old_allow

    def test_resolve_tasks_root_rejects_noncanonical_without_explicit_opt_in(self):
        local_ops = load_local_ops()
        old_tasks_root = os.environ.get('TASKS_ROOT')
        old_allow = os.environ.pop('HERMES_ALLOW_NONCANONICAL_LOCAL_ROOTS', None)
        os.environ['TASKS_ROOT'] = '/tmp/not-hermes-tasks'
        try:
            with self.assertRaisesRegex(RuntimeError, 'Refusing non-canonical TASKS_ROOT'):
                local_ops.resolve_tasks_root()
        finally:
            if old_tasks_root is None:
                os.environ.pop('TASKS_ROOT', None)
            else:
                os.environ['TASKS_ROOT'] = old_tasks_root
            if old_allow is not None:
                os.environ['HERMES_ALLOW_NONCANONICAL_LOCAL_ROOTS'] = old_allow

    def test_resolve_tasks_root_allows_noncanonical_only_for_tests(self):
        local_ops = load_local_ops()
        old_tasks_root = os.environ.get('TASKS_ROOT')
        old_allow = os.environ.get('HERMES_ALLOW_NONCANONICAL_LOCAL_ROOTS')
        os.environ['TASKS_ROOT'] = '/tmp/not-hermes-tasks'
        os.environ['HERMES_ALLOW_NONCANONICAL_LOCAL_ROOTS'] = '1'
        try:
            self.assertEqual(local_ops.resolve_tasks_root(), Path('/tmp/not-hermes-tasks').resolve())
        finally:
            if old_tasks_root is None:
                os.environ.pop('TASKS_ROOT', None)
            else:
                os.environ['TASKS_ROOT'] = old_tasks_root
            if old_allow is None:
                os.environ.pop('HERMES_ALLOW_NONCANONICAL_LOCAL_ROOTS', None)
            else:
                os.environ['HERMES_ALLOW_NONCANONICAL_LOCAL_ROOTS'] = old_allow


if __name__ == '__main__':
    unittest.main()
