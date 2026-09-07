"""Shared canonical task and project vocabulary; explicit, uncached reads."""
import json
from pathlib import Path


from collections.abc import Mapping
import re
from typing import Any

PROJECT_ID_RE = re.compile(r'^P-[1-9][0-9]*$')

def validate_project_registry(projects: list) -> None:
    """Validate the minimal registry, including unique exact names and IDs."""
    if not isinstance(projects, list):
        raise ValueError('project registry must be a flat JSON array')
    ids, names = set(), set()
    for project in projects:
        if not isinstance(project, dict) or set(project) != {'id', 'name'}:
            raise ValueError('project records require only id and name')
        pid, name = project['id'], project['name']
        if not isinstance(pid, str) or not PROJECT_ID_RE.fullmatch(pid):
            raise ValueError('invalid project id; expected positive P-N')
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise ValueError('project name must be a nonblank trimmed string')
        if pid in ids or name in names:
            raise ValueError('duplicate project id or name')
        ids.add(pid)
        names.add(name)


def load_project_registry(root: Path) -> list:
    """Load root/_meta/project_registry.json; missing/corrupt data fails closed."""
    projects = json.loads((Path(root) / '_meta' / 'project_registry.json').read_text(encoding='utf-8'))
    validate_project_registry(projects)
    return projects


def project_name(task: Mapping[str, Any], projects: list) -> str:
    """Resolve one task's foreign key from an already validated snapshot."""
    pid = task.get('project_id')
    if not isinstance(pid, str) or not PROJECT_ID_RE.fullmatch(pid):
        raise ValueError('invalid project_id; expected positive P-N')
    for project in projects:
        if project['id'] == pid:
            return project['name']
    raise ValueError(f'unknown project_id {pid}')


OPEN_STATUSES = frozenset({'not_started', 'in_progress'})
VALID_STATUSES = OPEN_STATUSES | {'completed', 'cancelled'}
REQUIRED_FIELDS = ('id', 'name', 'due_date', 'recurrence', 'priority', 'project_id', 'done', 'notes', 'reminder')
TASK_ID_RE = re.compile(r"^T(?:-(?P<rank>[1-9][0-9]*)-(?P<created>[1-9][0-9]*)|(?P<legacy>[1-9][0-9]*))$")


def is_open(task: Mapping[str, Any]) -> bool:
    """Classify a strict checkbox projection, rejecting stored legacy status.

    Full registry shape validation belongs at the registry boundary.
    """
    if not isinstance(task, Mapping):
        raise ValueError('task must be an object')
    if 'task' in task:
        raise ValueError('forbidden legacy task field; use name')
    if 'status' in task:
        raise ValueError('forbidden stored status; use done')
    if type(task.get('done')) is not bool:
        raise ValueError('done must be a boolean')
    return not task['done']


def validate_task_shape(task: Mapping[str, Any]) -> None:
    """Validate canonical scalar types, without coercing stored or API tokens.

    Date and registry-wide identity semantics belong to the full validator; API
    callers may still supply supported human-readable dates for normalization.
    """
    is_open(task)
    for legacy in ('tag', 'project', 'project_name', 'category'):
        if legacy in task:
            raise ValueError(f'forbidden stored {legacy} field; use project_id')
    for field in REQUIRED_FIELDS:
        if field not in task:
            raise ValueError(f'missing field {field}')
        value = task[field]
        if field == 'done':
            continue  # strict boolean checked by is_open
        nullable = field in {'due_date', 'recurrence', 'reminder'}
        if not isinstance(value, str) and not (nullable and value is None):
            suffix = ' or null' if nullable else ''
            raise ValueError(f'{field} must be a string{suffix}')
    if task.get('start_date') is not None and not isinstance(task['start_date'], str):
        raise ValueError('start_date must be a string or null')
    revision = task.get('_revision', 0)
    if type(revision) is not int or revision < 0:
        raise ValueError('_revision must be a nonnegative integer')
    if '_notion_page_id' in task and (not isinstance(task['_notion_page_id'], str) or not task['_notion_page_id'].strip()):
        raise ValueError('_notion_page_id must be a nonempty string')
    if '_external_receipt' in task:
        receipt = task['_external_receipt']
        if not isinstance(receipt, dict) or set(receipt) != {'operations'} or not isinstance(receipt['operations'], dict):
            raise ValueError('invalid _external_receipt')
        for operation, event in receipt['operations'].items():
            if not isinstance(operation, str) or not operation.strip() or not isinstance(event, dict):
                raise ValueError('invalid external operation receipt')
            if set(event) != {'payload_hash', 'log_entry', 'cleanup_id'}:
                raise ValueError('invalid external event fields')
            if not isinstance(event['payload_hash'], str) or not re.fullmatch('[0-9a-f]{64}', event['payload_hash']):
                raise ValueError('invalid external payload hash')
            if event['log_entry'] is not None:
                import hashlib
                marker = '<!-- task-operation:' + hashlib.sha256(operation.encode()).hexdigest() + ' -->'
                if not isinstance(event['log_entry'], str) or not event['log_entry'].endswith('\n\n' + marker):
                    raise ValueError('invalid external log event')
            if event['cleanup_id'] is not None and (not isinstance(event['cleanup_id'], str) or not TASK_ID_RE.fullmatch(event['cleanup_id'])):
                raise ValueError('invalid external cleanup identity')
    if not PROJECT_ID_RE.fullmatch(task['project_id']):
        raise ValueError('invalid project_id; expected positive P-N')
    if not task['name'].strip():
        raise ValueError('name must be a non-empty string')
    if not TASK_ID_RE.fullmatch(task['id']):
        raise ValueError(f"invalid task id {task['id']!r}; identity components must be positive integers")
