"""All-development curriculum ordering and configurable 3x2/depth-four setup."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from alpha_agents.tree_search import composition
from alpha_agents.tree_search.core.mcts.contracts import FixedPUCTConfig, Objective
from alpha_agents.tree_search.core.mcts.coordinator import FixedPUCTCoordinator
from alpha_agents.tree_search.core.mcts.ports import RoleSettings, StageEvidence
from alpha_agents.tree_search.tests.test_mcts_coordinator import Evaluator, Gateway


def settings(tmp_path, rows=None):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'controller.py').write_text('value = 0\n')
    manifest = tmp_path / 'manifest.json'
    if rows is None:
        rows = [
            {'task': 'Hard', 'stage': 'Selection', 'difficulty': 'hard', 'optimal_human_steps': 1},
            {'task': 'EasyLong', 'stage': 'Selection', 'difficulty': 'easy', 'optimal_human_steps': 9},
            {'task': 'EasyShort', 'stage': 'Screen', 'difficulty': 'easy', 'optimal_human_steps': 1},
            {'task': 'Medium', 'stage': 'Selection', 'difficulty': 'medium', 'optimal_human_steps': 3},
            {'task': 'Hidden', 'stage': 'Confirmation'},
        ]
    manifest.write_text(json.dumps({'rows': rows}))
    return {'controller': {'type': 'task_sequence', 'screen_gate': 'execution_valid',
                'max_depth': 4, 'max_plans': 3, 'max_implementations': 2,
                'max_trials': 30, 'trial_workers': 4},
            'harness': {'source': str(source), 'config': {'androidworld_devices': ['a', 'b', 'c', 'd']}},
            'evaluation': {'manifest': str(manifest)},
            'sequence': {'task_scope': 'all_development', 'order': 'difficulty_then_human_steps',
                'reuse_cached_root_results': False, 'skip_successful_roots': True}}


def test_order_and_factory_honor_limits_without_cached_root(tmp_path, monkeypatch):
    config = settings(tmp_path)
    built = []
    def build(current, directory):
        built.append(copy.deepcopy(current))
        directory.mkdir(parents=True)
        return SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(composition, 'build', build)
    sequence = composition.build_sequence(config, tmp_path / 'run')
    assert sequence.tasks == ['EasyShort', 'EasyLong', 'Medium', 'Hard']
    assert not (tmp_path / 'run/cached_root_index.json').exists()
    for position, task in enumerate(sequence.tasks):
        sequence.application_factory(task, position, sequence.source, tmp_path / 'run/tasks' / task)
    for current in built:
        controller = current['controller']
        assert (controller['max_plans'], controller['max_implementations'], controller['max_depth']) == (3, 2, 4)
        assert controller['max_new_attempts_per_trial'] == 4
        assert controller['max_attempts'] == 120 and controller['max_trials'] == 30
        assert FixedPUCTConfig.from_mapping({key: value for key, value in controller.items() if key != 'type'})
    order = json.loads((tmp_path / 'run/task_order.json').read_text())
    assert [row['task'] for row in order['tasks']] == sequence.tasks
    assert 'Hidden' not in sequence.tasks


@pytest.mark.parametrize('corruption', ['difficulty', 'steps', 'duplicate', 'count', 'reuse', 'order'])
def test_invalid_curriculum_fails_before_constructing_runtime(tmp_path, monkeypatch, corruption):
    config = settings(tmp_path)
    path = Path(config['evaluation']['manifest'])
    data = json.loads(path.read_text())
    if corruption == 'difficulty':
        data['rows'][0]['difficulty'] = 'unknown'
    elif corruption == 'steps':
        data['rows'][0]['optimal_human_steps'] = True
    elif corruption == 'duplicate':
        data['rows'].append(data['rows'][0])
    elif corruption == 'count':
        config['sequence']['expected_task_count'] = 25
    elif corruption == 'reuse':
        config['sequence']['reuse_cached_root_results'] = True
    else:
        config['sequence']['order'] = 'alphabetical'
    path.write_text(json.dumps(data))
    monkeypatch.setattr(composition, 'build', lambda *_: pytest.fail('Runtime must not be constructed'))
    with pytest.raises(ValueError):
        composition.build_sequence(config, tmp_path / 'run')


def test_changed_frozen_order_rejected_on_rebuild(tmp_path):
    config = settings(tmp_path)
    composition.build_sequence(config, tmp_path / 'run')
    path = Path(config['evaluation']['manifest'])
    data = json.loads(path.read_text())
    data['rows'][0]['optimal_human_steps'] = 2
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='order changed'):
        composition.build_sequence(config, tmp_path / 'run')


def test_actual_twenty_five_task_manifest_is_ordered_and_handed_forward(tmp_path, monkeypatch):
    manifest = Path(__file__).resolve().parents[3] / 'androidworld_sets_manifest.json'
    rows = json.loads(manifest.read_text())['rows']
    config = settings(tmp_path, rows)
    config['sequence']['expected_task_count'] = 25
    config['controller'].update(max_trials=2, trial_workers=1)
    rank = {'easy': 0, 'medium': 1, 'hard': 2}
    expected = sorted((row for row in rows if row['stage'].lower() in {'screen', 'selection'}),
        key=lambda row: (rank[row['difficulty']], row['optimal_human_steps'], row['task']))
    observed, applications = [], []
    def build(current, directory):
        task = current['controller']['evaluation_task']
        position = [row['task'] for row in expected].index(task)
        class TaskEvaluator(Evaluator):
            def evaluate(self, artifact, workspace, stage, tasks):
                value = int((workspace / 'controller.py').read_text().split('=')[1])
                observed.append((task, value, stage))
                return StageEvidence(artifact, stage, {task: value >= position // 5 + 1}, (f'trace:{artifact}',))
        search = FixedPUCTCoordinator(Path(current['harness']['source']), directory,
            FixedPUCTConfig.from_mapping({key: value for key, value in current['controller'].items() if key != 'type'}),
            Objective(task, 'rubric', 'judge'), Gateway(), TaskEvaluator(), RoleSettings('model', 'low'),
            (), {task: 1.0}, (task,), {})
        applications.append(search)
        return search
    monkeypatch.setattr(composition, 'build', build)
    sequence = composition.build_sequence(config, tmp_path / 'run')
    assert len(sequence.tasks) == 25
    assert sequence.tasks == [row['task'] for row in expected]
    result = sequence.run()
    assert result['state'] == 'completed'
    assert len(applications) == 25 and len(result['steps']) == 25
    for position, (step, app) in enumerate(zip(result['steps'], applications)):
        expected_attempts = 1 if position % 5 == 0 else 0
        assert app.gateway.implementations == expected_attempts
        assert step['completed_rollouts'] == expected_attempts
        assert app.config.max_implementations == 2 and app.config.max_depth == 4
        if position:
            assert app.source == applications[position - 1].directory / 'winner'
        roots = [value for task, value, _ in observed if task == step['task']]
        assert roots[0] == (position - 1) // 5 + 1 if position else roots[0] == 0
    assert all(stage == 's0' for _, _, stage in observed)
    calls = len(observed)
    assert sequence.run(resume=True) == result
    assert len(observed) == calls


def test_two_implementation_slots_are_enforced_by_actual_tree(tmp_path):
    config = settings(tmp_path)
    search = FixedPUCTCoordinator(Path(config['harness']['source']), tmp_path / 'tree',
        FixedPUCTConfig(screen_gate='execution_valid', evaluation_task='Task', max_plans=3,
            max_implementations=2, max_depth=4, max_trials=8, max_new_attempts_per_trial=4,
            max_attempts=32, trial_workers=1, stop_on_task_success=False),
        Objective('task', 'rubric', 'judge'), Gateway(), Evaluator(), RoleSettings('model', 'low'),
        (), {'Task': 1.0}, ('Task',), {})
    try:
        result = search.run()
        assert result['errors'] == [] and result['final_selection']['completed_rollouts'] == 8
        groups = list(search.store.db.execute('SELECT plan,count(*) n,max(slot) last FROM attempts GROUP BY plan'))
        assert groups and max(row['n'] for row in groups) == 2
        assert max(row['last'] for row in groups) == 2
        assert search.store.db.execute('SELECT max(depth) FROM nodes').fetchone()[0] == 4
        assert search.store.continuation_policy()['max_implementations'] == 2
    finally:
        search.close()
