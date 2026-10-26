"""Single-task success stops admission, drains work, and survives recovery."""

import json
import threading
from dataclasses import asdict

import pytest

from alpha_agents.tree_search.core.mcts.contracts import FixedPUCTConfig, Objective
from alpha_agents.tree_search.core.mcts.coordinator import FixedPUCTCoordinator
from alpha_agents.tree_search.core.mcts.ports import RoleSettings
from alpha_agents.tree_search.core.mcts.storage import CapacityError
from alpha_agents.tree_search.core.mcts.success_stop import (
    qualifying_success,
    stop_receipt,
)
from alpha_agents.tree_search.tests.judge_fixtures import review_payload
from alpha_agents.tree_search.tests.test_mcts_coordinator import Evaluator, Gateway


class RatedGateway(Gateway):
    def __init__(self, ratings=None, barrier=None, manipulated=(), child_ratings=()):
        super().__init__()
        self.ratings = {0: 2, **(ratings or {})}
        self.barrier = barrier
        self.manipulated = manipulated
        self.child_ratings = iter(child_ratings)

    def invoke(self, request, workspace):
        if request.role == 'judge_review':
            with self.lock:
                self.calls.append(request)
            value = int((workspace / 'controller.py').read_text().split('=')[1])
            result = review_payload(request.artifact_id,
                request.context['canonical_evidence']['snapshot_id'], request.context['rubric'],
                score=next(self.child_ratings, self.ratings.get(value, 3)) if value else self.ratings.get(value, 3))
            if value != 0 and (self.manipulated is True or value in self.manipulated):
                result['findings'] = [{'id': 'integrity-defect', 'kind': 'defect', 'title': 'Evaluator shortcut',
                    'severity': 'critical', 'confidence': 'high', 'evidence_basis': 'observed',
                    'source_evidence': ['controller.py'], 'execution_evidence': [],
                    'trigger': 'Canonical evaluation', 'mechanism': 'Injected manipulation fixture',
                    'consequence': 'Invalid shortcut', 'affected_criteria': ['G01'], 'uncertainty': 'None in fixture'}]
                result['integrity'].update(manipulation_assessment='confirmed',
                    controller_flag='confirmed_manipulation', finding_ids=['integrity-defect'])
            return result
        result = super().invoke(request, workspace)
        if request.role == 'implementer' and self.barrier:
            self.barrier.wait(timeout=15)
        return result


def search_fixture(tmp_path, *, value=0, gateway=None, evaluator=None, **settings):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'controller.py').write_text(f'value = {value}\n')
    config = FixedPUCTConfig(**{'screen_gate': 'execution_valid', 'evaluation_task': 'Task', 'evaluation_repetitions': 1,
        'max_trials': 3, 'trial_workers': 1, 'max_new_attempts_per_trial': 1, **settings})
    return FixedPUCTCoordinator(source, tmp_path / 'tree', config, Objective('single', 'rubric', 'judge'),
        gateway or RatedGateway(), evaluator or Evaluator(), RoleSettings('model', 'low'),
        (), {'Task': 1.0}, ('Task',), {})


@pytest.mark.parametrize('child_rating', [3, 4])
def test_success_above_original_root_judge_stops_and_reuses_finalization(tmp_path, child_rating):
    gateway = RatedGateway({1: child_rating})
    search = search_fixture(tmp_path, gateway=gateway)
    try:
        result = search.run()
        assert result['errors'] == []
        final = result['final_selection']
        assert final['selection_mode'] == 'qualified-single-task-success'
        assert final['stopped_early'] and final['completed_rollouts'] == 1
        assert final['configured_rollouts'] == 3
        assert final['winner']['u'] == 1 and final['winner']['j'] >= final['success_stop']['root_score']['j']
        assert len(final['selected_actions']) == 1
        assert search.store.budget()['reserved'] == 0
        assert not [call for call in gateway.calls if call.role == 'value']
        backup = json.loads(search.store.db.execute('SELECT payload FROM backups').fetchone()[0])
        assert backup['value_id'] is None and backup['capacity']['success_terminal']
        assert len(backup['path']) == 1
        root_capacity = search.store.capacity('root')
        assert root_capacity['exhausted']
        assert root_capacity['success_terminal']['node'] == final['success_stop']['node']
        with pytest.raises(CapacityError, match='success'):
            search.store.reserve_trial()
        calls = len(gateway.calls), len(search.evaluator.calls)
        assert search.run(resume=True)['final_selection'] == final
        assert calls == (len(gateway.calls), len(search.evaluator.calls))
        assert (search.directory / 'winner/controller.py').read_text() == 'value = 1\n'
    finally:
        search.close()


def test_successful_root_stops_without_any_search_trials(tmp_path):
    search = search_fixture(tmp_path, value=1)
    try:
        final = search.run()['final_selection']
        assert final['completed_rollouts'] == 0 and final['selected_actions'] == []
        assert final['winner']['depth'] == 0 and final['stopped_early']
        assert all(call.role == 'judge_review' for call in search.gateway.calls)
    finally:
        search.close()


def test_benchmark_pass_with_lower_judge_keeps_searching_until_qualified(tmp_path):
    # The second attempt improves on a lower-quality successful parent but is
    # still below the ORIGINAL root; neither may stop the search.
    gateway = RatedGateway({0: 3}, child_ratings=(1, 2, 4))
    search = search_fixture(tmp_path, gateway=gateway)
    try:
        result = search.run()
        assert result['errors'] == []
        final = result['final_selection']
        assert gateway.implementations == 3
        assert len([call for call in gateway.calls if call.role == 'value']) == 2
        assert final['winner']['j'] == 1
        assert final['success_stop']['root_score']['j'] == .75
        assert final['completed_rollouts'] == 3
    finally:
        search.close()


def test_confirmed_manipulation_never_qualifies_even_with_zero_root_judge(tmp_path):
    gateway = RatedGateway({0: 0}, manipulated=True)
    search = search_fixture(tmp_path, gateway=gateway)
    try:
        final = search.run()['final_selection']
        assert final['completed_rollouts'] == 3
        assert final['success_stop'] is None and not final['stopped_early']
        for node in search.store.db.execute('SELECT id FROM nodes'):
            assert qualifying_success(search.store, node[0]) is None
    finally:
        search.close()


def test_admitted_peer_finishes_and_is_backed_up_before_export(tmp_path):
    gateway = RatedGateway({2: 2}, barrier=threading.Barrier(2))
    search = search_fixture(tmp_path, gateway=gateway, trial_workers=2)
    try:
        result = search.run()
        assert result['errors'] == []
        final = result['final_selection']
        assert gateway.implementations == 2
        assert final['completed_rollouts'] == 2
        assert final['winner']['j'] == .75
        assert search.store.status()['backups'] == 2
        assert search.store.budget()['reserved'] == 0
        # A peer that froze its frontier before the successful commit may
        # finish that forecast. No fresh forecast is needed after the stop.
        assert len([call for call in gateway.calls if call.role == 'value']) <= 1
        for row in search.store.db.execute('SELECT id FROM nodes'):
            assert search.store.capacity(row[0])['success_terminal'] == final['success_stop']
    finally:
        search.close()


def test_resume_after_success_commit_settles_path_without_reimplementation(tmp_path, monkeypatch):
    from alpha_agents.tree_search.core.mcts.storage import TreeStore

    search = search_fixture(tmp_path)
    original = TreeStore.append_transition
    once = []
    def interrupted(store, *args):
        if stop_receipt(store) and not once:
            once.append(True)
            raise RuntimeError('Injected crash after success publication')
        return original(store, *args)
    monkeypatch.setattr(TreeStore, 'append_transition', interrupted)
    try:
        result = search.run()
        assert result['errors'] and 'final_selection' not in result
        assert stop_receipt(search.store)
        calls = len(search.gateway.calls), len(search.evaluator.calls)
        result = search.run(resume=True)
        assert result['errors'] == []
        assert result['final_selection']['completed_rollouts'] == 1
        assert search.store.status()['backups'] == 1
        assert calls == (len(search.gateway.calls), len(search.evaluator.calls))
    finally:
        search.close()


@pytest.mark.parametrize('corruption', ['missing_judge', 'partial_judge', 'canonical_mismatch'])
def test_unavailable_or_contradictory_records_cannot_trigger_success(tmp_path, corruption):
    search = search_fixture(tmp_path, value=1)
    try:
        search.assess_root()
        root = search.store.score('root')
        with search.store.transaction():
            if corruption == 'missing_judge':
                search.store.db.execute('DELETE FROM judge_snapshots WHERE id=?', (root.j_snapshot_id,))
            elif corruption == 'partial_judge':
                row = search.store.db.execute('SELECT payload FROM judge_snapshots WHERE id=?', (root.j_snapshot_id,)).fetchone()
                payload = json.loads(row[0])
                payload['status'] = 'partial'
                search.store.db.execute('UPDATE judge_snapshots SET payload=? WHERE id=?', (json.dumps(payload), root.j_snapshot_id))
            else:
                row = search.store.db.execute('SELECT payload FROM evaluation_snapshots WHERE id=?', (root.u_snapshot_id,)).fetchone()
                payload = json.loads(row[0])
                payload['outcomes']['Task'] = False
                search.store.db.execute('UPDATE evaluation_snapshots SET payload=? WHERE id=?', (json.dumps(payload), root.u_snapshot_id))
        assert qualifying_success(search.store, 'root') is None
        assert not search.store.capacity('root')['exhausted']
    finally:
        search.close()


def test_stop_policy_is_frozen_and_boolean(tmp_path):
    with pytest.raises(ValueError, match='boolean'):
        FixedPUCTConfig(screen_gate='execution_valid', stop_on_task_success='yes')
    search = search_fixture(tmp_path, value=1, stop_on_task_success=False)
    try:
        search.assess_root()
        assert not search.store.continuation_policy()['success_stop']['enabled']
        assert qualifying_success(search.store, 'root') is None
        assert not search.store.capacity('root')['exhausted']
        assert asdict(search.config)['stop_on_task_success'] is False
    finally:
        search.close()


def test_task_failure_with_high_judge_uses_full_budget(tmp_path):
    from alpha_agents.tree_search.core.mcts.ports import StageEvidence

    class FailedEvaluator(Evaluator):
        def evaluate(self, artifact, workspace, stage, tasks):
            self.calls.append((artifact, stage, tasks))
            return StageEvidence(artifact, stage, {task: False for task in tasks}, (f'trace:{artifact}',))

    search = search_fixture(tmp_path, evaluator=FailedEvaluator())
    try:
        final = search.run()['final_selection']
        assert final['completed_rollouts'] == 3
        assert final['success_stop'] is None
        assert search.gateway.implementations == 3
    finally:
        search.close()


def test_early_handoff_rechecks_evidence_before_freezing_winner(tmp_path):
    class CheckingEvaluator(Evaluator):
        invalid = False
        def verify_stage(self, evidence):
            if self.invalid:
                raise RuntimeError('Canonical evidence is unavailable')

    evaluator = CheckingEvaluator()
    search = search_fixture(tmp_path, evaluator=evaluator)
    try:
        result = search.run(finalize=False)
        assert result['errors'] == [] and stop_receipt(search.store)
        evaluator.invalid = True
        with pytest.raises(RuntimeError, match='Canonical evidence'):
            search.finalize()
        assert not (search.directory / 'winner').exists()
        assert not search.store.db.execute('SELECT 1 FROM finalizations').fetchone()
        evaluator.invalid = False
        final = search.run(resume=True)['final_selection']
        assert final['stopped_early'] and search.gateway.implementations == 1
    finally:
        search.close()


def test_root_skip_cannot_bypass_confirmed_manipulation(tmp_path):
    search = search_fixture(tmp_path, value=1, gateway=RatedGateway(manipulated=True))
    try:
        root = search.assess_root()
        assert root['u'] == 1 and root['j'] == 0
        assert root['success_stop_qualified'] is False
        with pytest.raises(ValueError, match='without confirmed manipulation'):
            search.retain_successful_root()
        assert not (search.directory / 'winner').exists()
    finally:
        search.close()


def test_sequence_hands_early_winner_to_next_task_and_measures_new_root(tmp_path):
    from alpha_agents.tree_search.core.mcts.task_sequence import TaskSequence

    source = tmp_path / 'source'
    source.mkdir()
    (source / 'controller.py').write_text('value = 0\n')
    searches = []
    def factory(task, index, parent, directory):
        config = FixedPUCTConfig(screen_gate='execution_valid', evaluation_task=task, evaluation_repetitions=1,
            max_trials=3, trial_workers=1, max_new_attempts_per_trial=1)
        search = FixedPUCTCoordinator(parent, directory, config, Objective(task, 'rubric', 'judge'),
            RatedGateway(), Evaluator(), RoleSettings('model', 'low'), (), {task: 1.0}, (task,), {})
        searches.append(search)
        return search
    sequence = TaskSequence(tmp_path / 'sequence', source, ['TaskA', 'TaskB'], {}, factory)
    try:
        result = sequence.run()
        assert result['state'] == 'completed'
        assert [step['completed_rollouts'] for step in result['steps']] == [1, 0]
        assert all(step['stopped_early'] for step in result['steps'])
        assert result['steps'][1]['selected_actions'] == []
        assert searches[1].source == searches[0].directory / 'winner'
        assert len(searches[1].evaluator.calls) == 1
        calls = sum(len(search.gateway.calls) for search in searches)
        assert sequence.run(resume=True) == result
        assert sum(len(search.gateway.calls) for search in searches) == calls
    finally:
        sequence.close()


def test_forecast_frozen_before_peer_success_keeps_original_value(tmp_path, monkeypatch):
    from alpha_agents.tree_search.tests.value_fixtures import forecast_payload

    forecast_started, success_published = threading.Event(), threading.Event()
    class OrderedGateway(RatedGateway):
        def invoke(self, request, workspace):
            if request.role == 'judge_review':
                value = int((workspace / 'controller.py').read_text().split('=')[1])
                if value == 2:
                    assert forecast_started.wait(timeout=15)
            if request.role == 'value':
                assert not request.context['capacity']['success_terminal']
                forecast_started.set()
                assert success_published.wait(timeout=15)
                super().invoke(request, workspace)
                return forecast_payload(request.context, request.artifact_id, .01)
            return super().invoke(request, workspace)

    gateway = OrderedGateway({1: 2}, barrier=threading.Barrier(2))
    search = search_fixture(tmp_path, gateway=gateway, trial_workers=2)
    original = search._executor
    def executor(store, owner):
        worker = original(store, owner)
        callback = worker.on_score
        def scored(node):
            callback(node)
            if stop_receipt(store):
                success_published.set()
        worker.on_score = scored
        return worker
    monkeypatch.setattr(search, '_executor', executor)
    try:
        result = search.run()
        assert result['errors'] == []
        assert result['final_selection']['completed_rollouts'] == 2
        values = list(search.store.db.execute('SELECT * FROM values_assessed'))
        assert len(values) == 1 and values[0]['value'] == .01
        assert not json.loads(values[0]['capacity'])['success_terminal']
        backup = json.loads(search.store.db.execute('SELECT payload FROM backups WHERE trial=?', (values[0]['trial'],)).fetchone()[0])
        assert backup['value_id'] == values[0]['id']
        assert backup['capacity_at_backup']['success_terminal']
        assert not backup['capacity']['success_terminal']
        assert search.store.budget()['reserved'] == 0
    finally:
        search.close()
