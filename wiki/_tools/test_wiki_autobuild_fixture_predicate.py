"""Deterministic tests of the actual descendant-stop fixture, without setup.

Only /proc reads are controlled; no hook is loaded and no process is spawned.
"""
from pathlib import Path
import errno
import importlib.util
from unittest import mock

import pytest

spec = importlib.util.spec_from_file_location(
    'predicate_behavioral', Path(__file__).with_name('test_wiki_autobuild_hook.py'))
assert spec and spec.loader
behavioral = importlib.util.module_from_spec(spec)
spec.loader.exec_module(behavioral)

ROWS = [{'kind': 'child', 'pid': 424241}, {'kind': 'leaf', 'pid': 424242}]


def fixture(rows=ROWS):
    # Initialize unittest assertions, but deliberately do not run real setUp.
    instance = behavioral.HookTests('test_timeout_kills_term_resistant_descendants')
    instance.events = lambda: rows
    return instance


@pytest.mark.parametrize('method', ['tree_stopped', 'assert_tree_stopped'])
@pytest.mark.parametrize('outcome', [
    'missing', 'lookup', 'R', 'S', 'Z', 'permission', 'io_error',
])
def test_direct_read_contract(method, outcome):
    errors = {
        'missing': FileNotFoundError(errno.ENOENT, 'disappeared at read'),
        'lookup': ProcessLookupError(errno.ESRCH, 'disappeared at read'),
        'permission': PermissionError(errno.EACCES, 'read denied'),
        'io_error': OSError(errno.EIO, 'unrelated read failure'),
    }
    error = errors.get(outcome)
    paths = []

    def read(path, *args, **kwargs):
        paths.append(str(path))
        if error is not None:
            raise error
        return f'{path.parent.name} (fixture process) {outcome} 1 2 3\n'

    instance = fixture()
    with mock.patch.object(Path, 'read_text', autospec=True, side_effect=read), \
            mock.patch.object(Path, 'exists', return_value=True) as exists:
        call = getattr(instance, method)
        if outcome in ('permission', 'io_error'):
            with pytest.raises(type(error)) as caught:
                call()
            assert caught.value is error
        elif outcome in ('R', 'S'):
            if method == 'tree_stopped':
                assert call() is False
            else:
                with pytest.raises(AssertionError):
                    call()
        elif method == 'tree_stopped':
            assert call() is True
        else:
            assert call() is None
        exists.assert_not_called()
    expected = ['/proc/424241/stat']
    if outcome in ('missing', 'lookup', 'Z'):
        expected.append('/proc/424242/stat')
    assert paths == expected


@pytest.mark.parametrize('method', ['tree_stopped', 'assert_tree_stopped'])
@pytest.mark.parametrize('rows', [[], ROWS[:1], ROWS[1:]], ids=['neither', 'child-only', 'leaf-only'])
def test_missing_descendant_events_still_fail(method, rows):
    instance = fixture(rows)
    with mock.patch.object(Path, 'read_text') as read, \
            mock.patch.object(Path, 'exists') as exists:
        if method == 'tree_stopped':
            assert instance.tree_stopped() is False
        else:
            with pytest.raises(AssertionError):
                instance.assert_tree_stopped()
        read.assert_not_called()
        exists.assert_not_called()
