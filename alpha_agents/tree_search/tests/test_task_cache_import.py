"""Imported observations retain identity and cannot masquerade as executions."""

import json
import shutil
from dataclasses import replace

import pytest

from alpha_agents.tree_search.core.mcts.cache_import import import_completed
from alpha_agents.tree_search.core.mcts.contracts import (
    FixedPUCTConfig,
    Objective,
    fingerprint,
)
from alpha_agents.tree_search.core.mcts.task_cache import (
    EvidenceCorrupt,
    TaskCache,
    TaskIdentity,
    TaskIncomplete,
    evidence_inventory,
)


@pytest.fixture
def packet(tmp_path):
    cache = TaskCache(tmp_path / 'new', FixedPUCTConfig(screen_gate="execution_valid"), Objective('s0', 'rubric', 'judge'))
    identity = TaskIdentity('artifact', 'Task', 42, 'model', 'evaluator', 'fixture',
                            {'model': 'test'}, {'task': 1800})
    original = tmp_path / 'old' / 'evidence'
    original.mkdir(parents=True)
    (original / 'result.json').write_text('{"success":false}')
    result = {'task_name': identity.template, 'seed': identity.contract['generated_seed'],
              'success': False, 'raw_reward': 0, 'evidence_root': str(original),
              'inventory': evidence_inventory(original)}
    target = cache.directory / 'tasks' / identity.key / 'imported' / 'evidence'
    shutil.copytree(original, target)
    origin = {'source_run': str(tmp_path / 'old'), 'authorization': 'Explicit user cache reuse'}
    return cache, identity, result, target, origin


def publish(packet):
    cache, identity, result, target, origin = packet
    return import_completed(cache, identity, identity.contract, result, fingerprint(result), target, origin)


def test_imported_failure_is_reused_without_execution_cost_visits_or_attempts(packet):
    cache, identity, result, target, origin = packet
    assert publish(packet)['success'] is False
    assert publish(packet)['evidence_root'] == str(target)
    batch = cache.register('baseline', identity.artifact_id, 's0', (identity,))
    assert cache.cached(identity, batch)['raw_reward'] == 0
    with cache.store() as store:
        for table in ['task_runs', 'costs', 'nodes', 'trials', 'backups']:
            assert store.db.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0
        assert store.db.execute('SELECT count(*) FROM task_reuses').fetchone()[0] == 1
        provenance = json.loads(store.db.execute('SELECT value FROM meta WHERE key=?',
                                                ('task_cache_import:' + identity.key,)).fetchone()[0])
        assert provenance['source_result_hash'] == fingerprint(result)
        assert provenance['new_execution'] is False
    with pytest.raises(TaskIncomplete, match='immutable'):
        cache.begin(identity, 'device', target)


@pytest.mark.parametrize('field,value', [('artifact_id', 'other'), ('seed', 43),
                                         ('model_revision', 'other'), ('evaluator_revision', 'other'),
                                         ('fixture_identity', 'other')])
def test_import_rejects_changed_execution_contract(packet, field, value):
    cache, identity, result, target, origin = packet
    with pytest.raises(ValueError, match='contract differs'):
        import_completed(cache, replace(identity, **{field: value}), identity.contract,
                               result, fingerprint(result), target, origin)


def test_import_rejects_corruption_and_preserves_empty_target_state(packet):
    cache, identity, result, target, origin = packet
    with pytest.raises(EvidenceCorrupt, match='fingerprint'):
        import_completed(cache, identity, identity.contract, result, 'wrong', target, origin)
    (target / 'result.json').write_text('changed')
    with pytest.raises(EvidenceCorrupt, match='sealed source inventory'):
        publish(packet)
    with cache.store() as store:
        assert store.db.execute('SELECT count(*) FROM task_jobs').fetchone()[0] == 0


def test_import_cannot_overwrite_registered_or_running_work(packet):
    cache, identity, result, target, origin = packet
    cache.register('baseline', identity.artifact_id, 's0', (identity,))
    with pytest.raises(TaskIncomplete, match='cannot be overwritten'):
        publish(packet)


def test_explicit_evaluator_migration_preserves_original_contract_and_rejects_other_changes(packet):
    cache, identity, result, target, origin = packet
    source_contract = {**identity.contract, 'evaluator_revision': 'previous-evaluator'}
    migration = {'source_revision': 'previous-evaluator', 'target_revision': 'evaluator',
                 'reason': 'Scheduling capacity changed; task execution is identical',
                 'code_audit': {'changed_lines': ['device-pool ceiling 6 -> 9']}}
    import_completed(cache, identity, source_contract, result, fingerprint(result), target, origin,
                     evaluator_compatibility=migration)
    with cache.store() as store:
        record = json.loads(store.db.execute('SELECT value FROM meta WHERE key=?',
                                             ('task_cache_import:' + identity.key,)).fetchone()[0])
        assert record['source_contract'] == source_contract
        assert record['target_contract'] == identity.contract
        assert record['evaluator_compatibility'] == migration
        assert store.db.execute('SELECT count(*) FROM task_runs').fetchone()[0] == 0
    with pytest.raises(ValueError, match='contract differs'):
        import_completed(cache, replace(identity, model_revision='different'), source_contract,
                         result, fingerprint(result), target, origin, evaluator_compatibility=migration)
    with pytest.raises(ValueError, match='code audit'):
        import_completed(cache, identity, source_contract, result, fingerprint(result), target, origin,
                         evaluator_compatibility={**migration, 'code_audit': {}})


def test_import_requires_authorization_and_run_local_evidence(packet):
    cache, identity, result, target, origin = packet
    with pytest.raises(ValueError, match='authorization'):
        import_completed(cache, identity, identity.contract, result, fingerprint(result), target, {})
    with pytest.raises(ValueError, match='beneath this task'):
        import_completed(cache, identity, identity.contract, result, fingerprint(result),
                               target.parents[3], origin)
