"""Fresh repeated episodes, empirical utility, and strict success qualification."""

import json

import pytest

from alpha_agents.tree_search.bridges.androidworld.staged_evaluator import (
    StagedArtemisEvaluator,
)
from alpha_agents.tree_search.core.mcts.contracts import FixedPUCTConfig, Objective
from alpha_agents.tree_search.core.mcts.ports import StageEvidence
from alpha_agents.tree_search.core.mcts.task_cache import TaskCache, TaskIncomplete
from alpha_agents.tree_search.tests.test_mcts_coordinator import Evaluator
from alpha_agents.tree_search.tests.test_mcts_devices import cache as cache_fixture
from alpha_agents.tree_search.tests.test_mcts_devices import (
    staged_adapter as staged_adapter_fixture,
)
from alpha_agents.tree_search.tests.test_success_stop import (
    RatedGateway,
    search_fixture,
)

cache = cache_fixture
staged_adapter = staged_adapter_fixture


def repeated_adapter(staged_adapter, tmp_path, rewards=(1, 1, 1), *, self_failed=False):
    base, artifact, workspace, calls, failures = staged_adapter
    repeated_cache = TaskCache(tmp_path / 'repeated', FixedPUCTConfig(screen_gate='execution_valid',
        evaluation_task='TaskB'), Objective('single', 'rubric', 'judge'))
    pending = iter(rewards)

    def runner(*args, **kwargs):
        summary = base.runner(*args, **kwargs)
        if summary['status'] == 'completed':
            reward = next(pending)
            episode = summary['task_results'][0]
            episode.update(androidworld_reward=reward, success=reward == 1 and not self_failed)
            output = args[1]
            (output / 'summary.json').write_text(json.dumps(summary))
        return summary

    adapter = StagedArtemisEvaluator(repeated_cache, base.artifacts, base.scheduler, base.config, base.revision,
        {'screen': (), 'selection': ('TaskB',), 'confirmation': ('HeldoutA',)}, 'repeated',
        runner=runner, health_check=base.health_check)
    return adapter, artifact, workspace, calls, failures


def test_three_real_episodes_same_seed_distinct_evidence_and_durable_reuse(staged_adapter, tmp_path):
    adapter, artifact, workspace, calls, _ = repeated_adapter(staged_adapter, tmp_path, self_failed=True)
    result = adapter.evaluate(artifact, workspace, 's0', ('TaskB',))
    assert len(calls) == 3
    assert result.samples == {'TaskB': (True, True, True)}
    assert result.rates == {'TaskB': 1}
    assert len(set(result.evidence)) == 3
    # AndroidWorld pass authority does not depend on the agent's own completion claim.
    assert result.outcomes == {'TaskB': True}
    adapter.verify_stage(result)
    resumed = adapter.for_owner('resumed')
    assert resumed.evaluate(artifact, workspace, 's0', ('TaskB',)) == result
    assert len(calls) == 3
    with adapter.cache.store() as store:
        contracts = [json.loads(x[0]) for x in store.db.execute('SELECT contract FROM task_jobs')]
        assert sorted(x['repetition'] for x in contracts) == [0, 1, 2]
        assert len({x['generated_seed'] for x in contracts}) == 1
        assert store.db.execute('SELECT count(*) FROM task_runs').fetchone()[0] == 3


@pytest.mark.parametrize('rewards, expected', [
    ((0, 1, 1), (False,)),
    ((1, 0, 1), (True, False)),
    ((1, 1, 0), (True, True, False)),
])
def test_first_failure_stops_admission_and_resume(staged_adapter, tmp_path, rewards, expected):
    adapter, artifact, workspace, calls, _ = repeated_adapter(staged_adapter, tmp_path, rewards)
    result = adapter.evaluate(artifact, workspace, 's0', ('TaskB',))
    assert len(calls) == len(expected)
    assert result.samples == {'TaskB': expected}
    assert result.outcomes == {'TaskB': False}
    assert result.rates['TaskB'] == sum(expected) / len(expected)
    adapter.verify_stage(result)
    assert adapter.for_owner('resumed').evaluate(artifact, workspace, 's0', ('TaskB',)) == result
    assert len(calls) == len(expected)
    with adapter.cache.store() as store:
        assert store.db.execute('SELECT count(*) FROM task_jobs').fetchone()[0] == len(expected)
        assert store.db.execute("SELECT count(*) FROM task_jobs WHERE state!='completed'").fetchone()[0] == 0


def test_infrastructure_retry_is_not_an_additional_vote(staged_adapter, tmp_path):
    adapter, artifact, workspace, calls, failures = repeated_adapter(staged_adapter, tmp_path)
    failures['TaskB'] = 1
    result = adapter.evaluate(artifact, workspace, 's0', ('TaskB',))
    assert len(calls) == 4 and len(result.samples['TaskB']) == 3
    with adapter.cache.store() as store:
        states = [x[0] for x in store.db.execute('SELECT state FROM task_runs')]
    assert states.count('infra_failed') == 1 and states.count('completed') == 3


def test_resume_runs_only_missing_repetition(staged_adapter, tmp_path, monkeypatch):
    adapter, artifact, workspace, calls, _ = repeated_adapter(staged_adapter, tmp_path)
    original = adapter._submit

    def unavailable(identity, stage):
        if identity.repetition == 1:
            raise TaskIncomplete('Injected admission interruption')
        return original(identity, stage)

    monkeypatch.setattr(adapter, '_submit', unavailable)
    with pytest.raises(TaskIncomplete, match='interruption'):
        adapter.evaluate(artifact, workspace, 's0', ('TaskB',))
    assert len(calls) == 1
    monkeypatch.setattr(adapter, '_submit', original)
    result = adapter.evaluate(artifact, workspace, 's0', ('TaskB',))
    assert len(calls) == 3 and sum(result.samples['TaskB']) == 3


class RepeatedEvaluator(Evaluator):
    def evaluate(self, artifact, workspace, stage, tasks):
        self.calls.append((artifact, stage, tasks))
        value = int((workspace / 'controller.py').read_text().split('=')[1])
        values = (False,) if value == 0 else ((True, True, False) if value == 1 else (True, True, True))
        return StageEvidence(artifact, stage, {tasks[0]: all(values)},
            tuple(f'trace:{artifact}:{i}' for i in range(len(values))), {tasks[0]: values})


def test_empirical_score_two_of_three_cannot_stop_then_three_of_three_can(tmp_path):
    search = search_fixture(tmp_path, evaluator=RepeatedEvaluator(), evaluation_repetitions=3)
    try:
        final = search.run()['final_selection']
        assert final['completed_rollouts'] == 2
        root = next(x for x in final['evaluated'] if x['node'] == 'root')
        assert root['evaluations'] == 1 and root['samples'] == {'Task': [False]}
        assert final['winner']['passes'] == final['winner']['evaluations'] == 3
        intermediate = next(x for x in final['evaluated'] if x['u'] == 2 / 3)
        assert intermediate['passes'] == 2 and intermediate['outcomes'] == {'Task': False}
        from alpha_agents.tree_search.core.mcts.reporting import score_breakdowns
        breakdowns = score_breakdowns(search.store, {'tasks': [{'task': 'Task', 'weight': {'display': 1.0}}]})
        row = next(x for x in breakdowns if x['node_id'] == intermediate['node'])
        assert row['weighted_tasks'][0]['contribution'] == 2 / 3
        assert row['weighted_tasks'][0]['passes'] == 2
        assert final['winner']['j'] > final['success_stop']['root_score']['j']
        before = len(search.evaluator.calls)
        assert search.run(resume=True)['final_selection'] == final
        assert len(search.evaluator.calls) == before
    finally:
        search.close()


def test_equal_judge_never_triggers_child_success_stop(tmp_path):
    search = search_fixture(tmp_path, gateway=RatedGateway({0: 3, 1: 3, 2: 3, 3: 3}),
                            evaluator=RepeatedEvaluator(), evaluation_repetitions=3)
    try:
        final = search.run()['final_selection']
        assert final['completed_rollouts'] == 3 and final['success_stop'] is None
    finally:
        search.close()


def test_root_requires_all_three_passes_and_only_one_review(tmp_path):
    class Passing(RepeatedEvaluator):
        def evaluate(self, artifact, workspace, stage, tasks):
            return StageEvidence(artifact, stage, {'Task': True},
                tuple(f'trace:{artifact}:{i}' for i in range(3)), {'Task': (True, True, True)})
    search = search_fixture(tmp_path, evaluator=Passing(), evaluation_repetitions=3)
    try:
        final = search.run()['final_selection']
        assert final['completed_rollouts'] == 0
        assert sum(c.role == 'judge_review' for c in search.gateway.calls) == 1
        assert final['winner']['evaluations'] == 3
    finally:
        search.close()


@pytest.mark.parametrize('count', [0, 4, True, 1.5])
def test_repetition_count_is_validated(count):
    with pytest.raises(ValueError, match='evaluation_repetitions'):
        FixedPUCTConfig(screen_gate='execution_valid', evaluation_repetitions=count)


def test_three_copies_of_one_evidence_root_cannot_qualify():
    stage = StageEvidence('artifact', 's0', {'Task': True}, ('same',) * 3,
                          {'Task': (True, True, True)})
    with pytest.raises(ValueError, match='distinct'):
        stage.validate_repetitions(3)


def test_targeted_diagnostic_cannot_fill_canonical_repetition(staged_adapter, tmp_path):
    adapter, artifact, workspace, calls, _ = repeated_adapter(staged_adapter, tmp_path, (1, 1, 1, 1))
    diagnostic = adapter.evaluate(artifact, workspace, 'targeted', ('TaskB',))
    assert diagnostic.samples is None and len(calls) == 1
    canonical = adapter.evaluate(artifact, workspace, 's0', ('TaskB',))
    assert len(calls) == 4 and len(canonical.samples['TaskB']) == 3
    assert set(diagnostic.evidence).isdisjoint(canonical.evidence)


@pytest.mark.parametrize('passed', [0, 1, 2, 3])
def test_empirical_rate_and_all_pass_flag_are_distinct(passed):
    values = (True,) * passed + (False,) * (3 - passed)
    stage = StageEvidence('artifact', 's0', {'Task': passed == 3},
                          ('run0', 'run1', 'run2'), {'Task': values})
    stage.validate_repetitions(3)
    assert stage.rates['Task'] == passed / 3
    assert stage.outcomes['Task'] is (passed == 3)


def test_bounded_agent_failures_do_not_become_infrastructure_retries(staged_adapter, tmp_path):
    adapter, artifact, workspace, calls, _ = repeated_adapter(staged_adapter, tmp_path, (None, None, None))
    result = adapter.evaluate(artifact, workspace, 's0', ('TaskB',))
    assert len(calls) == 1 and result.samples == {'TaskB': (False,)}
    assert result.rates['TaskB'] == 0
    with adapter.cache.store() as store:
        assert store.db.execute("SELECT count(*) FROM task_runs WHERE state='infra_failed'").fetchone()[0] == 0

@pytest.mark.parametrize('values', [(True,), (True, True), (False, False), (False, True)])
def test_incomplete_prefix_requires_its_first_failure_at_the_end(values):
    stage = StageEvidence('artifact', 's0', {'Task': all(values)},
                          tuple(f'run{i}' for i in range(len(values))), {'Task': values})
    with pytest.raises(ValueError, match='required'):
        stage.validate_repetitions(3)


def test_infrastructure_failure_then_task_failure_stops_without_extra_votes(staged_adapter, tmp_path):
    adapter, artifact, workspace, calls, failures = repeated_adapter(staged_adapter, tmp_path, (0, 1, 1))
    failures['TaskB'] = 1
    result = adapter.evaluate(artifact, workspace, 's0', ('TaskB',))
    assert len(calls) == 2
    assert result.samples == {'TaskB': (False,)}
    adapter.verify_stage(result)
    with adapter.cache.store() as store:
        assert store.db.execute('SELECT count(*) FROM task_jobs').fetchone()[0] == 1
        assert store.db.execute("SELECT count(*) FROM task_runs WHERE state='infra_failed'").fetchone()[0] == 1
