import copy
import json
from pathlib import Path

import pytest

from alpha_agents.tree_search.bridges.androidworld import benchmark
from alpha_agents.tree_search.bridges.androidworld.bridge import AndroidWorldBridge
from alpha_agents.tree_search.core.contracts import Candidate, RequiredEvaluationError
from alpha_agents.tree_search.core.storage import write_json


def manifest(tasks, config, reward=1.0):
    return {
        "status": "completed", "model": config["model"],
        "model_base_url": config["base_url"], "seed": config["seed"],
        "tasks": tasks, "combinations": 1,
        "episodes": [{"template": task, "index": 0, "success": reward,
                      "androidworld_reward": reward, "artemis_status": "completed",
                      "exception": None} for task in tasks],
    }


@pytest.fixture
def setup(tmp_path, monkeypatch):
    task_file = tmp_path / "tasks.json"
    write_json(task_file, {"screen": ["s1", "s2"], "selection": ["v1", "v2", "v3"], "confirmation": ["held_out"]})
    config = {"model": "model", "base_url": "http://model", "seed": 42,
              "confirmation_seed": 1042, "task_file": str(task_file)}
    configs = [dict(config, androidworld_assigned_device=f"emulator-{5554 + 2*i}") for i in range(2)]
    candidate = Candidate("initial", None, tmp_path / "run/initial", "base")
    calls = []

    def runner(workspace, output, stage, config, lock_root, *, tasks=None):
        calls.append((stage, config["androidworld_assigned_device"], tasks, config["seed"]))
        m = manifest(tasks, config)
        process = {"returncode": 0, "timed_out": False}
        write_json(output / "manifest.json", m)
        write_json(output / "runner/process.json", process)
        return benchmark.artemis.normalize_manifest(m, tasks, config, process)

    monkeypatch.setattr(benchmark.artemis, "run_stage", runner)
    return candidate, configs, tmp_path / "cache", calls


def test_full_cache_reuses_all_stages_and_stays_outside_parent_evidence(setup):
    candidate, configs, cache, calls = setup
    selection, report, output = benchmark.full_benchmark(candidate, configs, cache, {"source": "one"})
    assert selection["planned_tasks"] == ["v1", "v2", "v3"]
    assert report["tasks"] == 6 and report["success_rate_pct"] == 100
    assert not output.is_relative_to(candidate.directory)
    assert set(t for _, _, group, _ in calls for t in group) == {"s1", "s2", "v1", "v2", "v3", "held_out"}
    assert all(seed == (1042 if stage == "confirmation" else 42) for stage, _, _, seed in calls)
    before = len(calls)
    benchmark.full_benchmark(candidate, list(reversed(configs)), cache, {"source": "one"})
    assert len(calls) == before
    benchmark.full_benchmark(candidate, configs, cache, {"source": "changed"})
    assert len(calls) == before * 2


def test_cache_rechecks_manifests_and_reruns_corrupt_stage(setup):
    candidate, configs, cache, calls = setup
    _, _, output = benchmark.full_benchmark(candidate, configs, cache, {"source": "one"})
    m = json.loads((output / "screen/worker_00/manifest.json").read_text())
    m["episodes"] = []
    write_json(output / "screen/worker_00/manifest.json", m)
    calls.clear()
    benchmark.full_benchmark(candidate, configs, cache, {"source": "one"})
    assert [stage for stage, _, _, _ in calls] == ["screen", "screen"]
    assert list(output.glob("screen.incomplete.*"))


def test_parallel_merge_rejects_missing_duplicate_and_invalid_scores():
    config = {"model": "m", "base_url": "u", "seed": 42}
    good = {"status": "completed", "task_results": [{"task_name": "x", "score": 0.5, "success": False}]}
    assert benchmark.merge_summaries([good], ["x"], config)["status"] == "completed"
    assert benchmark.merge_summaries([good, good], ["x"], config)["status"] == "invalid_runtime"
    assert benchmark.merge_summaries([good], ["x", "y"], config)["status"] == "invalid_runtime"
    bad = copy.deepcopy(good); bad["task_results"][0]["score"] = None
    assert benchmark.merge_summaries([bad], ["x"], config)["status"] == "invalid_runtime"


def test_parallel_batches_really_overlap(setup, monkeypatch):
    from threading import Barrier

    candidate, configs, cache, calls = setup
    runner = benchmark.artemis.run_stage
    barrier = Barrier(2, timeout=3)

    def simultaneous(*args, **kwargs):
        barrier.wait()
        return runner(*args, **kwargs)

    monkeypatch.setattr(benchmark.artemis, "run_stage", simultaneous)
    summary = benchmark.run_parallel_stage(candidate.workspace, cache / "test", "selection", configs, cache)
    assert summary["status"] == "completed"
    assert len({device for _, device, _, _ in calls}) == 2
    assigned = [task for _, _, tasks, _ in calls for task in tasks]
    assert sorted(assigned) == ["v1", "v2", "v3"]


def test_cache_identity_changes_with_agent_evaluator_model_seed_and_runtime(tmp_path, monkeypatch):
    workspace = tmp_path / "agent"; workspace.mkdir()
    evaluator = tmp_path / "evaluator"; evaluator.mkdir()
    (workspace / "agent.py").write_text("agent version 1")
    (evaluator / "task.py").write_text("grader version 1")
    task_file = tmp_path / "tasks.json"
    write_json(task_file, {"screen": ["s"], "selection": ["v"], "confirmation": ["c"]})
    monkeypatch.setattr(benchmark, "git", lambda *a: "agent.py\0")

    def command(args, **kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(stdout="task.py\0" if args[0] == "git" else '[["openai", "1.0"]]')

    monkeypatch.setattr(benchmark.subprocess, "run", command)
    config = {"androidworld_root": str(evaluator), "python": "python", "task_file": str(task_file), "model": "one", "seed": 42, "confirmation_seed": 1042, "llm_timeout": 180}
    identity = benchmark.cache_identity(workspace, config)
    for key, value in [("model", "two"), ("seed", 43), ("confirmation_seed", 1043), ("llm_timeout", 181)]:
        assert benchmark.cache_identity(workspace, dict(config, **{key: value})) != identity
    assert benchmark.cache_identity(workspace, dict(config, evaluation_workers=4, console_port=5564)) == identity
    (workspace / "agent.py").write_text("agent version 2")
    assert benchmark.cache_identity(workspace, config) != identity
    (workspace / "agent.py").write_text("agent version 1")
    (evaluator / "task.py").write_text("grader version 2")
    assert benchmark.cache_identity(workspace, config) != identity


def test_root_public_evidence_contains_only_selection(setup, monkeypatch):
    candidate, configs, cache, calls = setup
    bridge = object.__new__(AndroidWorldBridge)
    config = dict(configs[0], evaluation_workers=2, full_benchmark=True,
                  evaluation_runner="artemis_local", score_stage="selection", benchmark_cache_dir=str(cache))
    bridge.config = config
    from contextlib import contextmanager

    @contextmanager
    def resources(resource):
        yield [dict(config, androidworld_assigned_device=c["androidworld_assigned_device"]) for c in configs]

    monkeypatch.setattr(bridge, "evaluation_resources", resources)
    monkeypatch.setattr(benchmark, "cache_identity", lambda *a: {"source": "one"})
    evaluation = bridge.evaluate(candidate, config)
    assert evaluation.score == 1.0
    public = candidate.directory / "stages"
    assert [p.name for p in public.iterdir()] == ["selection"]
    assert "held_out" not in json.dumps(evaluation.metrics)
    assert not (candidate.directory / "root_full_benchmark.json").exists()
    assert (candidate.directory.parent / "root_full_benchmark.json").exists()


@pytest.mark.parametrize("workers", [1, 3])
@pytest.mark.parametrize("screen_outcome", ["passed", "failed", "invalid"])
def test_child_screen_gate_precedes_selection_and_never_supplies_rank_score(
    setup, monkeypatch, workers, screen_outcome
):
    root, configs, cache, calls = setup
    candidate = Candidate("child", "initial", root.directory.parent / "child", "base")
    config = dict(configs[0], evaluation_workers=workers, full_benchmark=True,
                  evaluation_runner="artemis_local", score_stage="selection")
    bridge = object.__new__(AndroidWorldBridge)
    bridge.config = config
    from contextlib import contextmanager
    from alpha_agents.tree_search.bridges.androidworld.task_sets import stage_tasks

    @contextmanager
    def resources(resource):
        yield [dict(config, androidworld_assigned_device=f"emulator-{5554 + 2*i}")
               for i in range(workers)]

    def runner(workspace, output, stage, resource, lock_root, *, tasks=None):
        tasks = stage_tasks(resource["task_file"], stage) if tasks is None else tasks
        calls.append((stage, resource["androidworld_assigned_device"], tasks, resource["seed"]))
        m = manifest(tasks, resource)
        for episode in m["episodes"]:
            if (stage == "screen" and screen_outcome == "failed") or episode["template"] == "v1":
                episode.update(success=0.0, androidworld_reward=0.0)
        process = {"returncode": int(stage == "screen" and screen_outcome == "invalid"), "timed_out": False}
        write_json(output / "manifest.json", m)
        write_json(output / "runner/process.json", process)
        return benchmark.artemis.normalize_manifest(m, tasks, resource, process)

    monkeypatch.setattr(bridge, "evaluation_resources", resources)
    monkeypatch.setattr(benchmark.artemis, "run_stage", runner)
    result = bridge.evaluate(candidate, config)
    stages = [stage for stage, _, _, _ in calls]
    assert stages[0] == "screen"
    assert "confirmation" not in stages
    if screen_outcome == "passed":
        assert result.status == "completed" and result.score == pytest.approx(2 / 3)
        assert max(i for i, s in enumerate(stages) if s == "screen") < min(i for i, s in enumerate(stages) if s == "selection")
        assert result.metrics["evaluation_stage"] == "selection"
        assert (candidate.directory / "stages/screen").exists()
    else:
        assert set(stages) == {"screen"}
        assert result.status == "failed" and result.score is None and result.diagnostic
        assert not (candidate.directory / "stages/selection").exists()
        if screen_outcome == "failed":
            assert result.metrics["screen_gate"] == "failed"
            assert {r["task_name"] for r in result.metrics["task_results"]} == {"s1", "s2"}
