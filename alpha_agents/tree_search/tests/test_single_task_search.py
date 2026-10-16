"""Single-task rollouts, observed winner selection, and durable task handoff."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from alpha_agents.tree_search.core.mcts.contracts import FixedPUCTConfig, Objective
from alpha_agents.tree_search.core.mcts.coordinator import FixedPUCTCoordinator
from alpha_agents.tree_search.core.mcts.ports import RoleSettings
from alpha_agents.tree_search.core.mcts.task_sequence import TaskSequence
from alpha_agents.tree_search.tests.test_mcts_coordinator import Evaluator, Gateway


def test_single_task_thirty_rollouts_reach_depth_eight_without_screen_or_promotion(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'controller.py').write_text('value = 0\n')
    task = 'OnlyTask'
    config = FixedPUCTConfig(screen_gate='execution_valid', evaluation_task=task,
                             max_trials=30, max_depth=8, max_new_attempts_per_trial=8,
                             max_attempts=240, trial_workers=4, device_limit=4)
    evaluator, gateway = Evaluator(), Gateway()
    subset = {'tasks': [{'task': task, 'weight': {'display': 1.0}}], 'coverage': {'selected_tasks': 1}}
    search = FixedPUCTCoordinator(source, tmp_path / 'tree', config, Objective('single', 'rubric', 'judge'),
        gateway, evaluator, RoleSettings('configured-model', 'low'), (), {task: 1.0}, (task,), subset)
    try:
        result = search.run()
        assert result['errors'] == []
        assert result['status']['trials'] == {'completed': 30}
        assert result['status']['backups'] == 30
        assert search.store.db.execute('SELECT max(depth) FROM nodes').fetchone()[0] == 8
        assert search.store.db.execute('SELECT max(length) FROM (SELECT count(*) length FROM path GROUP BY trial)').fetchone()[0] == 8
        assert all(stage == 's0' and tasks == (task,) for _, stage, tasks in evaluator.calls)
        assert not search.store.db.execute('SELECT 1 FROM promotions').fetchone()
        assert all(request.execution_mode == 'goal' for request in gateway.calls if request.role == 'implementer')
        assert all(request.execution_mode == 'plan' for request in gateway.calls if request.role == 'proposer')
        final = result['final_selection']
        assert final['completed_rollouts'] == 30 and final['winner']['utility'] == 1
        assert final['winner']['f'] == max(c['f'] for c in final['evaluated'])
        assert len(final['selected_actions']) == final['winner']['depth']
        assert (search.directory / 'winner/controller.py').is_file()
        calls = len(gateway.calls), len(evaluator.calls)
        again = search.run(resume=True)
        assert again['final_selection'] == final
        assert calls == (len(gateway.calls), len(evaluator.calls))
        assert search.store.budget()['reserved'] == 0
    finally:
        search.close()


def test_single_task_depth_allowance_does_not_change_multitask_limits():
    base = FixedPUCTConfig(screen_gate='execution_valid')
    with pytest.raises(ValueError):
        replace(base, max_new_attempts_per_trial=8)
    single = replace(base, evaluation_task='Task', max_new_attempts_per_trial=8, max_attempts=240)
    assert single.max_new_attempts_per_trial == 8
    with pytest.raises(ValueError):
        replace(single, max_depth=9)


class Application:
    def __init__(self, source, directory, task, seen, pending=False):
        self.source, self.directory, self.task, self.seen = source, directory, task, seen
        self.pending = pending
        self.stop = False

    def run(self, **kwargs):
        self.seen.append((self.task, (self.source / 'controller.py').read_text()))
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / 'run_configuration.json').write_text('{}')
        if self.pending:
            return {'errors': [{'error': 'value unavailable'}]}
        winner = self.directory / 'winner'
        winner.mkdir(exist_ok=True)
        (winner / 'controller.py').write_text(self.task)
        return {'final_selection': {'selection_status': 'completed', 'winner': {'artifact_id': self.task},
                                   'selected_actions': [{'plan': self.task}], 'completed_rollouts': 30}}

    def request_stop(self):
        self.stop = True

    def close(self):
        pass


def test_sequence_hands_exact_winner_forward_and_resume_skips_completed_tasks(tmp_path):
    source = tmp_path / 'root'
    source.mkdir()
    (source / 'controller.py').write_text('root')
    seen = []
    def factory(task, index, harness, directory):
        return Application(harness, directory, task, seen)
    sequence = TaskSequence(tmp_path / 'sequence', source, ['TaskA', 'TaskB'], {'seed': 42}, factory)
    state = sequence.run()
    assert state['state'] == 'completed'
    assert seen == [('TaskA', 'root'), ('TaskB', 'TaskA')]
    assert Path(state['final_harness']).joinpath('controller.py').read_text() == 'TaskB'
    assert sequence.run(resume=True) == state
    assert len(seen) == 2
    Path(state['final_harness']).joinpath('controller.py').write_text('changed')
    with pytest.raises(ValueError, match='handoff artifact'):
        sequence.run(resume=True)


def test_sequence_never_advances_from_pending_rollout_and_can_resume(tmp_path):
    source = tmp_path / 'root'
    source.mkdir()
    (source / 'controller.py').write_text('root')
    seen, blocked = [], [True]
    def factory(task, index, harness, directory):
        return Application(harness, directory, task, seen, pending=blocked[0])
    sequence = TaskSequence(tmp_path / 'sequence', source, ['TaskA', 'TaskB'], {}, factory)
    assert sequence.run()['state'] == 'recovery_pending'
    assert seen == [('TaskA', 'root')]
    blocked[0] = False
    assert sequence.run(resume=True)['state'] == 'completed'
    assert seen == [('TaskA', 'root'), ('TaskA', 'root'), ('TaskB', 'TaskA')]
    assert (sequence.directory / 'MONITOR.md').is_file()


def test_live_sequence_monitor_reports_scores_and_device_failures(tmp_path):
    from alpha_agents.tree_search.core.mcts.contracts import CombinedScore
    from alpha_agents.tree_search.core.mcts.storage import TreeStore
    from alpha_agents.tree_search.infrastructure.sequence_monitor import SequenceMonitor

    tree = tmp_path / 'tasks/01-Task'
    store = TreeStore(tree, FixedPUCTConfig(screen_gate='execution_valid', evaluation_task='Task'),
                      Objective('task', 'rubric', 'judge'))
    try:
        store.add_root('root', CombinedScore('artifact', 's0', 'judge', store.objective, 0, .5))
        store.db.execute("INSERT INTO device_faults VALUES('fault','emulator-5580','job','a11y unavailable',1)")
        (tmp_path / 'sequence.json').write_text(json.dumps({'state': 'running', 'steps': [
            {'task': 'Task', 'state': 'running', 'directory': 'tasks/01-Task'}]}))
        monitor = SequenceMonitor(tmp_path)
        monitor.update()
        report = (tmp_path / 'LIVE_MONITOR.md').read_text()
        assert 'a11y unavailable' in report and 'artifact' in report and '0.0500' in report
        assert 'a11y unavailable' in (tmp_path / 'INCIDENTS.md').read_text()
        previous = (tmp_path / 'INCIDENTS.md').read_text()
        monitor.update()
        assert previous == (tmp_path / 'INCIDENTS.md').read_text()
    finally:
        store.close()


def test_single_task_winner_verifies_cached_evidence_before_freezing_choice(tmp_path):
    from alpha_agents.tree_search.core.mcts.artifacts import ArtifactStore
    from alpha_agents.tree_search.core.mcts.contracts import CombinedScore
    from alpha_agents.tree_search.core.mcts.finalization import Finalizer
    from alpha_agents.tree_search.core.mcts.ports import StageEvidence
    from alpha_agents.tree_search.core.mcts.storage import TreeStore
    from dataclasses import asdict

    source = tmp_path / 'source'
    source.mkdir()
    (source / 'controller.py').write_text('value = 1\n')
    artifacts = ArtifactStore(tmp_path / 'artifacts')
    artifact = artifacts.freeze(source)
    config = FixedPUCTConfig(screen_gate='execution_valid', evaluation_task='Task', max_trials=2)
    store = TreeStore(tmp_path / 'tree', config, Objective('task', 'rubric', 'judge'))
    class CheckingEvaluator(Evaluator):
        invalid = True
        def verify_stage(self, evidence):
            assert evidence.outcomes == {'Task': True}
            if self.invalid:
                raise RuntimeError('sealed evidence unavailable')
        def evaluate(self, *args):
            raise AssertionError('Finalization must reuse the observed single-task outcome')
    evaluator = CheckingEvaluator()
    try:
        evidence = StageEvidence(artifact, 's0', {'Task': True}, ('sealed-task',))
        store.save_evaluation(evidence.identity, artifact, 's0', asdict(evidence))
        store.add_root('root', CombinedScore(artifact, evidence.identity, 'judge', store.objective, 1, .5))
        finalizer = Finalizer(store, artifacts, evaluator, ('Task',), {})
        with pytest.raises(ValueError, match='all configured rollouts'):
            finalizer.run()
        for _ in range(2):
            trial = store.reserve_trial()
            store.stop_trial(trial, 'test_empty_path')
            store.backup(trial)
        with pytest.raises(RuntimeError, match='sealed evidence'):
            finalizer.run()
        assert not store.db.execute('SELECT 1 FROM finalizations').fetchone()
        evaluator.invalid = False
        final = finalizer.run()
        assert final['winner']['artifact_id'] == artifact and final['selected_actions'] == []
        assert finalizer.run() == final
    finally:
        store.close()


@pytest.mark.parametrize('reuse', [None, True, False])
def test_sequence_can_use_cached_failed_task_list_without_importing_old_outcomes(tmp_path, monkeypatch, reuse):
    from types import SimpleNamespace
    from alpha_agents.tree_search import composition
    from alpha_agents.tree_search.bridges.androidworld import sequence_cache
    from alpha_agents.tree_search.core.mcts.artifacts import manifest
    from alpha_agents.tree_search.core.mcts.contracts import fingerprint

    source = tmp_path / 'source'
    source.mkdir()
    (source / 'harness.py').write_text('unchanged = True\n')
    task_manifest = tmp_path / 'manifest.json'
    task_manifest.write_text(json.dumps({'rows': [{'task': 'TaskA', 'stage': 'selection'}]}))
    index = {'failed_tasks': ['TaskA'], 'artifact_id': fingerprint(manifest(source)), 'receipts': {}}
    imported, constructed = [], []
    monkeypatch.setattr(sequence_cache, 'root_receipts', lambda _: index)
    monkeypatch.setattr(sequence_cache, 'import_root_receipt', lambda *args: imported.append(args))

    def make_application(settings, directory):
        directory.mkdir(parents=True, exist_ok=True)
        constructed.append(settings)
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(composition, 'build', make_application)
    sequence_config = {'cached_root_run': str(tmp_path / 'cached')}
    if reuse is not None:
        sequence_config['reuse_cached_root_results'] = reuse
    settings = {'sequence': sequence_config, 'controller': {},
                'harness': {'source': str(source), 'config': {'androidworld_devices': ['a', 'b', 'c', 'd']}},
                'evaluation': {'manifest': str(task_manifest)}}
    output = tmp_path / 'sequence'
    sequence = composition.build_sequence(settings, output)
    assert sequence.tasks == ['TaskA']
    directory = output / 'tasks/01-TaskA'
    sequence.application_factory('TaskA', 0, source, directory)
    assert len(imported) == (0 if reuse is False else 1)
    assert constructed[0]['controller']['evaluation_task'] == 'TaskA'
    assert constructed[0]['controller']['max_trials'] == 30
    assert constructed[0]['controller']['max_depth'] == 8
    context = json.loads((directory / 'task_sequence_context.json').read_text())
    assert context['reuse_cached_root_results'] is (reuse is not False)


def test_sequence_rejects_non_boolean_cache_reuse_policy(tmp_path):
    from alpha_agents.tree_search.composition import build_sequence
    with pytest.raises(ValueError, match='reuse_cached_root_results must be a boolean'):
        build_sequence({'sequence': {'reuse_cached_root_results': 'false'}}, tmp_path)
