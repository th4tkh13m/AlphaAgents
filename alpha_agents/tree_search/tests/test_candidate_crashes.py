"""Candidate raise sites are failures; transport and evaluator outages are not."""
import json

import pytest

from alpha_agents.tree_search.bridges.androidworld import artemis
from alpha_agents.tree_search.bridges.androidworld.task_outcomes import (
    bounded_agent_failure,
    is_bounded_agent_failure,
)
from alpha_agents.tree_search.tests.test_mcts_devices import cache as cache_fixture
from alpha_agents.tree_search.tests.test_mcts_devices import (
    staged_adapter as staged_adapter_fixture,
)
from alpha_agents.tree_search.tests.test_pass_hat_k import repeated_adapter

cache = cache_fixture
staged_adapter = staged_adapter_fixture


def crash(tmp_path, statement):
    source = tmp_path / 'candidate/artemis/agents/outputter/answer_review.py'
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(statement)
    try:
        exec(compile(statement, str(source), 'exec'), {})
    except Exception as error:
        return error, tmp_path / 'candidate'
    raise AssertionError('Fixture must raise')


@pytest.mark.parametrize('statement', [
    "class OutputReviewError(RuntimeError): pass\nraise OutputReviewError('bad label')",
    "raise ValueError('bad answer')",
    "raise AttributeError('missing attribute')",
])
def test_candidate_crash_provenance_normalizes_without_fabricating_reward(tmp_path, statement):
    error, root = crash(tmp_path, statement)
    episode = {'template': 'Task', 'index': 0, 'success': 0.0,
               'exception': f'{type(error).__name__}: {error}'}
    episode.update(bounded_agent_failure(error, 'agent_execution', agent_root=root))
    assert episode['failure_kind'] == 'candidate_exception'
    assert is_bounded_agent_failure(episode)
    config = {'model': 'model', 'base_url': 'http://model', 'seed': 42}
    manifest = {'status': 'completed', 'model': 'model', 'model_base_url': 'http://model',
                'seed': 42, 'tasks': ['Task'], 'combinations': 1, 'episodes': [episode]}
    result = artemis.normalize_manifest(manifest, ['Task'], config, {'returncode': 0, 'timed_out': False})
    assert result['status'] == 'completed'
    assert result['task_results'][0]['outcome_status'] == 'agent_failed'
    assert result['task_results'][0]['androidworld_reward'] is None
    assert result['task_results'][0]['success'] is False


@pytest.mark.parametrize('phase', ['setup', 'grading', 'answer_submission', 'teardown'])
def test_evaluator_phase_exceptions_are_not_candidate_failures(tmp_path, phase):
    error, root = crash(tmp_path, "raise RuntimeError('bug')")
    assert bounded_agent_failure(error, phase, agent_root=root) == {}


@pytest.mark.parametrize('statement', [
    "raise ConnectionError('endpoint down')",
    "raise RuntimeError('wrapped transport') from ConnectionError('endpoint down')",
    "raise OSError('device disconnected')",
])
def test_transport_errors_remain_unavailable_even_with_candidate_raise_site(tmp_path, statement):
    error, root = crash(tmp_path, statement)
    assert bounded_agent_failure(error, 'agent_execution', agent_root=root) == {}


def test_external_raise_site_does_not_become_candidate_error(tmp_path):
    try:
        raise RuntimeError('evaluator bug')
    except RuntimeError as error:
        assert bounded_agent_failure(error, 'agent_execution', agent_root=tmp_path) == {}


def test_candidate_crash_stops_repetitions_without_infrastructure_retry(staged_adapter, tmp_path):
    adapter, artifact, workspace, calls, _ = repeated_adapter(staged_adapter, tmp_path)
    original = adapter.runner
    error, root = crash(tmp_path, "raise ValueError('bad answer')")

    def runner(*args, **kwargs):
        summary = original(*args, **kwargs)
        episode = summary['task_results'][0]
        episode.update(bounded_agent_failure(error, 'agent_execution', agent_root=root))
        episode.update(success=False, score=0.0, exception=f'{type(error).__name__}: {error}')
        (args[1] / 'summary.json').write_text(json.dumps(summary))
        return summary

    adapter.runner = runner
    evidence = adapter.evaluate(artifact, workspace, 's0', ('TaskB',))
    assert evidence.samples == {'TaskB': (False,)}
    assert len(calls) == 1
    adapter.verify_stage(evidence)
    with adapter.cache.store() as store:
        assert store.db.execute("SELECT count(*) FROM task_runs WHERE state='infra_failed'").fetchone()[0] == 0
        outcome = json.loads(store.db.execute('SELECT result FROM task_jobs').fetchone()[0])
        assert outcome['raw_reward'] is None
        assert outcome['outcome_status'] == 'agent_failed'
