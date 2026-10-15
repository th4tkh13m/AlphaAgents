"""Short judge references must remain bound to exact sealed evidence."""
import hashlib
import re

import pytest

from alpha_agents.tree_search.core.mcts.responses import (
    _evidence,
    canonical_file_evidence,
)
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.prompts import (
    RoleRequest,
)
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.responses import (
    output_schema,
)


def binding(tmp_path):
    root = tmp_path / 'tasks' / ('a' * 64) / 'execution' / 'evidence'
    root.mkdir(parents=True)
    path = root / 'trajectory.json'
    path.write_bytes(b'{"success":false}')
    item = {'reference_id': 's0-file-0001', 'path': path.name,
            'absolute_file': str(path), 'size': path.stat().st_size,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    request = RoleRequest(str(tmp_path), 'call', 'judge_execution', 'artifact', 'model', 'low',
        {'rubric': 'rubric', 'canonical_evidence': {'snapshot_id': 'sealed-stage',
            'evidence': [str(root)], 'file_catalog': [{'evidence_root_path': str(root), 'files': [item]}]}})
    return request, path


def test_bound_reference_normalization_preserves_raw_response(tmp_path):
    request, path = binding(tmp_path)
    raw = {'dimensions': {'decisions': {'evidence': ['s0-file-0001']}}}
    _evidence(['s0-file-0001'], request, tmp_path / 'workspace')
    canonical, mappings = canonical_file_evidence(raw, request)
    assert raw['dimensions']['decisions']['evidence'] == ['s0-file-0001']
    assert canonical['dimensions']['decisions']['evidence'] == [str(path)]
    assert mappings[0]['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    _evidence([str(path)], request, tmp_path / 'workspace')


def test_judge_narrative_citations_remain_usable_by_proposer(tmp_path):
    request, path = binding(tmp_path)
    raw = {'findings': 'Observed failure (s0-file-0001).',
           'coverage': {'inspected': ['Screenshot: `s0-file-0001`']}}
    canonical, mappings = canonical_file_evidence(raw, request)
    assert raw['findings'] == 'Observed failure (s0-file-0001).'
    assert canonical['findings'] == f'Observed failure ({path}).'
    assert canonical['coverage']['inspected'] == [f'Screenshot: `{path}`']
    assert len(mappings) == 1
    proposer = RoleRequest(str(tmp_path), 'next-call', 'proposer', 'artifact', 'model', 'low',
                           {'canonical_evidence': {'s0': {'evidence': [str(path.parent)]},
                                                   'judge': canonical}})
    _evidence([str(path)], proposer, tmp_path / 'workspace')
    with pytest.raises(ValueError, match='Unknown canonical file reference'):
        _evidence(['s0-file-0001'], proposer, tmp_path / 'workspace')


def test_narrative_reference_requires_unchanged_sealed_content(tmp_path):
    request, path = binding(tmp_path)
    path.write_text('modified after sealing')
    with pytest.raises(ValueError, match='sealed content'):
        canonical_file_evidence({'findings': 'Inspect s0-file-0001.'}, request)


def test_narrative_reference_does_not_resolve_unknown_or_embedded_ids(tmp_path):
    request, _ = binding(tmp_path)
    raw = {'findings': 's0-file-9999; prefix-s0-file-0001; dir/s0-file-0001/file'}
    canonical, mappings = canonical_file_evidence(raw, request)
    assert canonical == raw
    assert mappings == []


@pytest.mark.parametrize('corruption', ['modified', 'deleted', 'symlink', 'wrong_root', 'duplicate'])
def test_reference_rejects_broken_binding(tmp_path, corruption):
    request, path = binding(tmp_path)
    bundle = request.context['canonical_evidence']['file_catalog'][0]
    if corruption == 'modified':
        path.write_text('changed')
    elif corruption == 'deleted':
        path.unlink()
    elif corruption == 'symlink':
        outside = tmp_path / 'outside.json'
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
    elif corruption == 'wrong_root':
        bundle['evidence_root_path'] = str(tmp_path)
    else:
        bundle['files'].append(dict(bundle['files'][0]))
    with pytest.raises(ValueError):
        _evidence(['s0-file-0001'], request, tmp_path / 'workspace')


def test_unknown_reference_and_invented_absolute_path_rejected(tmp_path):
    request, path = binding(tmp_path)
    for reference in ['s0-file-9999', str(path).replace('/execution/', '/invented/')]:
        with pytest.raises(ValueError):
            _evidence([reference], request, tmp_path / 'workspace')


def test_schema_shares_large_enum_and_excludes_absolute_paths(tmp_path):
    request, path = binding(tmp_path)
    files = request.context['canonical_evidence']['file_catalog'][0]['files']
    files[:] = [{**files[0], 'reference_id': f's0-file-{i:04d}'} for i in range(1, 382)]
    schema = output_schema(request)
    assert len(schema['$defs']['file_reference']['enum']) == 381
    assert sum(len(s) for s in schema['$defs']['file_reference']['enum']) < 15000
    items = schema['properties']['dimensions']['properties']['decisions']['properties']['evidence']['items']
    def allowed(reference):
        for option in items['anyOf']:
            if '$ref' in option:
                if reference in schema['$defs']['file_reference']['enum']:
                    return True
            elif 'enum' in option:
                if reference in option['enum']:
                    return True
            elif re.fullmatch(option['pattern'], reference):
                return True
        return False
    assert allowed('s0-file-0001')
    assert allowed('artemis/agents/validator/execution_loop.py:42')
    assert allowed('code:controller.py:42:3')
    assert not allowed(str(path))
    assert not allowed('s0-file-9999')


def test_catalog_ids_are_stable_and_do_not_change_stage_identity(tmp_path):
    from alpha_agents.tree_search.core.mcts.controller import (
        StageEvidence,
        canonical_judge_context,
    )
    request, path = binding(tmp_path)
    stage = StageEvidence('artifact', 's0', {'task': False}, (str(path.parent),))
    identity = stage.identity
    first = canonical_judge_context(stage, ('task',))
    second = canonical_judge_context(stage, ('task',))
    assert first == second
    assert stage.identity == identity == first['snapshot_id']
    assert first['file_catalog'][0]['files'][0]['reference_id'] == 's0-file-0001'


def test_value_continuation_task_file_and_bundle_ids_share_permission_contract(tmp_path):
    from alpha_agents.tree_search.core.mcts.plan_archive import freeze_continuation_evidence
    from alpha_agents.tree_search.core.mcts.responses import validate_response
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.permissions import role_permissions

    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    task_root = tmp_path / 'tasks' / ('c' * 64) / 'execution' / 'evidence'
    task_root.mkdir(parents=True)
    summary = task_root / 'summary.json'
    summary.write_text('{"success": false}')
    nested_id = 'd' * 64
    payload = {'snapshot_id': 'capacity', 'reachable_evidence': {
        'child': {'evaluations': [{'snapshot_id': nested_id, 'payload': {'evidence': [str(task_root)]}}]}}}
    frozen = freeze_continuation_evidence(tmp_path, payload)
    context = {'score_snapshot_id': 'score', 'capacity_snapshot_id': 'capacity', 'objective_id': 'objective',
               'discount_factor': .9, 'continuation_policy_id': 'policy',
               'continuation_evidence': {**frozen, 'capacity_snapshot_id': 'capacity', 'canonical_evidence': [str(task_root)]}}
    request = RoleRequest(str(tmp_path), 'value', 'value', 'artifact', 'model', 'low', context)
    profile = role_permissions(request, workspace)
    assert profile['filesystem'][str(task_root)] == 'read'
    response = {'artifact_id': 'artifact', 'status': 'completed', 'value': .1,
                **{key: context[key] for key in ('score_snapshot_id', 'capacity_snapshot_id', 'objective_id',
                                                'discount_factor', 'continuation_policy_id')},
                'rationale': 'Inspected task and reachable node evidence', 'uncertainty': 'One task only',
                'evidence': [str(summary), nested_id]}
    assert validate_response(response, request, workspace) == response
    with pytest.raises(ValueError, match='rather than directories'):
        validate_response({**response, 'evidence': [str(task_root)]}, request, workspace)
    sibling = task_root.parent.parent.parent / ('e' * 64) / 'execution/evidence/summary.json'
    sibling.parent.mkdir(parents=True)
    sibling.write_text('{}')
    with pytest.raises(ValueError, match='out-of-scope'):
        validate_response({**response, 'evidence': [str(sibling)]}, request, workspace)
    with pytest.raises(ValueError):
        validate_response({**response, 'evidence': ['f' * 64]}, request, workspace)
    # A checksum mismatch rejects the declaration even if the cited file exists.
    bad_context = {**context, 'continuation_evidence': {**context['continuation_evidence'], 'evidence_bundle_hash': '0' * 64}}
    bad_request = RoleRequest(str(tmp_path), 'other', 'value', 'artifact', 'model', 'low', bad_context)
    with pytest.raises(ValueError, match='hash mismatch'):
        validate_response(response, bad_request, workspace)
    with pytest.raises(ValueError, match='hash mismatch'):
        role_permissions(bad_request, workspace)
