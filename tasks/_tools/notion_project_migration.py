"""Pure, offline cutover transformation. No files, credentials or API are opened."""
from copy import deepcopy

import notion_sync as tasks
import notion_projects as projects
from notion_transport import require


def migrate_project_states(two_way, project_state, name_to_id, *, handled_cancelled=None):
    """Return (new_two_way, new_projects); inputs are never modified.

    Baselines change tag->project_id and status->strict done; catalog keys/version change.
    Cancelled bindings require an explicit key->page ID handled scope, proving the
    caller separately archived and trashed them; only those become deletion tombstones.
    Existing page IDs, notes markers, receipts, revisions and deletion_policy survive
    byte-for-byte as JSON values. Pending task OR project intents prohibit cutover.
    Caller must independently verify remote relations against these baselines first.
    """
    require(isinstance(name_to_id, dict) and bool(name_to_id) and
            all(projects.valid_tag(k) and projects.valid_project_id(v) for k, v in name_to_id.items()) and
            len(set(name_to_id.values())) == len(name_to_id), 'invalid-project-migration-map')
    require(isinstance(two_way, dict) and isinstance(project_state, dict) and
            two_way.get('pending') == {} and project_state.get('pending') == {}, 'pending-intents-block-cutover')
    require(type(project_state.get('version')) is int and project_state['version'] == 1,
            'expected-project-state-v1')
    require(type(two_way.get('version')) is int and two_way['version'] == 1,
            'expected-two-way-state-v1')
    require(isinstance(two_way.get('bindings'), dict) and
            isinstance(project_state.get('bindings'), dict), 'invalid-legacy-bindings')
    result, catalog = deepcopy(two_way), deepcopy(project_state)
    handled = handled_cancelled or {}
    require(isinstance(handled, dict), 'invalid-handled-cancelled-scope')
    used = set()
    for task_key, binding in list(result['bindings'].items()):
        require(isinstance(binding, dict), 'invalid-legacy-binding')
        baseline = binding.get('baseline')
        require(isinstance(baseline, dict) and 'tag' in baseline and 'project_id' not in baseline and
                isinstance(baseline['tag'], str) and baseline['tag'] in name_to_id,
                'unknown-baseline-project')
        require('done' not in baseline and baseline.get('status') in
                {'not_started', 'in_progress', 'completed', 'cancelled'}, 'invalid-legacy-status')
        if baseline['status'] == 'cancelled':
            require(handled.get(task_key) == binding.get('page_id'), 'bound-cancelled-needs-handled-scope')
            used.add(task_key)
            policy = result.setdefault('deletion_policy', {'enrolled': {}, 'excluded': {}, 'deleted': {}})
            policy['enrolled'].pop(task_key, None)
            policy['excluded'].pop(task_key, None)
            policy['deleted'][task_key] = binding['page_id']
            del result['bindings'][task_key]
            continue
        baseline['done'] = baseline.pop('status') == 'completed'
        baseline['project_id'] = name_to_id[baseline.pop('tag')]
    require(set(handled) == used, 'unused-handled-cancelled-scope')
    bindings = {}
    for name, binding in catalog['bindings'].items():
        require(name in name_to_id and isinstance(binding, dict) and
                binding.get('marker') in ('', projects.MARKER_PREFIX + tasks.digest(name)),
                'invalid-legacy-project-binding')
        bindings[name_to_id[name]] = binding
    catalog['bindings'] = bindings
    catalog['version'] = 2
    tasks.validate_state(result)
    projects.validate_state(catalog)
    return result, catalog
