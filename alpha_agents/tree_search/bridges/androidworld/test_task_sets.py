"""Verify the frozen partition reaches each evaluator without score leakage."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from alpha_agents.tree_search.bridges.androidworld import artemis, evaluation, runtime
from alpha_agents.tree_search.bridges.androidworld.bridge import AndroidWorldBridge
from alpha_agents.tree_search.bridges.androidworld.task_sets import (
    load_sets,
    stage_tasks,
)
from alpha_agents.tree_search.core.contracts import Candidate

TASK_FILE = Path(__file__).with_name("tasks.json")


def test_bundled_partition():
    sets = load_sets(TASK_FILE)
    assert {stage: len(tasks) for stage, tasks in sets.items()} == {
        "screen": 5,
        "selection": 20,
        "confirmation": 91,
    }
    assert len(set().union(*map(set, sets.values()))) == 116
    assert sets["screen"] == [
        "ClockStopWatchPausedVerify",
        "SystemBluetoothTurnOff",
        "CameraTakePhoto",
        "OpenAppTaskEval",
        "SystemCopyToClipboard",
    ]
    assert stage_tasks(TASK_FILE, "evaluation") == sets["selection"]


def test_row_manifest_ignores_historical_scores(tmp_path):
    sets = load_sets(TASK_FILE)
    rows = [
        {
            "stage": stage.title(),
            "task": task,
            "artemis_success": 1,
            "artemis_seed": 123,
            "artemis_seconds": 50,
        }
        for stage, tasks in sets.items()
        for task in tasks
    ]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"rows": rows}))
    assert load_sets(path) == sets


@pytest.mark.parametrize(
    "rows",
    [
        [{"stage": "Unknown", "task": "A"}],
        [{"stage": "Screen", "task": "A"}, {"stage": "Screen", "task": "A"}],
        [{"stage": "Screen", "task": "A"}, {"stage": "Confirmation", "task": "A"}],
        [{"stage": "Screen", "task": ""}],
    ],
)
def test_invalid_partition_rejected(tmp_path, rows):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"rows": rows}))
    with pytest.raises(ValueError):
        load_sets(path)


def test_legacy_manifest_retains_explicit_evaluation(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"evaluation": ["LegacyTask"]}))
    assert stage_tasks(path, "evaluation") == ["LegacyTask"]


def config(tmp_path, **overrides):
    source = tmp_path / "harness"
    source.mkdir(exist_ok=True)
    return source, {
        "androidworld_api_url": "http://fake",
        "base_url": "http://model/v1",
        "model": "test",
        "androidworld_devices": ["emulator-5560"],
        **overrides,
    }


def test_default_scoring_is_new_selection(tmp_path):
    source, settings = config(tmp_path)
    bridge = AndroidWorldBridge(source, settings)
    assert bridge.config["score_stage"] == "selection"
    assert len(stage_tasks(bridge.config["task_file"], "selection")) == 20


def test_changed_partition_changes_resume_contract(tmp_path):
    path = tmp_path / "tasks.json"
    sets = load_sets(TASK_FILE)
    path.write_text(json.dumps(sets))
    source, settings = config(tmp_path, task_file=str(path))
    bridge = AndroidWorldBridge(source, settings)
    before = bridge.identity()
    sets["selection"] = list(reversed(sets["selection"]))
    path.write_text(json.dumps(sets))
    assert bridge.identity() != before


def test_confirmation_requires_seed_and_cannot_feed_mutation(tmp_path):
    source, settings = config(tmp_path, score_stage="confirmation")
    with pytest.raises(ValueError, match="explicit fresh"):
        AndroidWorldBridge(source, settings)
    bridge = AndroidWorldBridge(source, {**settings, "seed": 999})
    with bridge.lease() as resource:
        with pytest.raises(ValueError, match="held out"):
            bridge.prepare(
                Candidate("child", "initial", tmp_path, ""),
                Candidate("initial", None, tmp_path, ""),
                resource,
            )


@pytest.mark.parametrize("stage", ["screen", "selection", "confirmation"])
def test_http_queue_dispatches_exact_new_set(tmp_path, monkeypatch, stage):
    expected = stage_tasks(TASK_FILE, stage)
    dispatched = []
    monkeypatch.setattr(
        runtime, "preflight_mobile_agent_http", lambda *a: {"status": "completed"}
    )

    def attempt(worktree, directory, index, task, device, settings, lock_root):
        dispatched.append(task)
        return {
            "task_name": task,
            "device": device,
            "success": True,
            "score": 1,
            "outcome_status": "completed",
            "episode_cleanup": {"status": "released"},
        }

    monkeypatch.setattr(evaluation, "run_attempt", attempt)
    settings = {
        "task_file": str(TASK_FILE),
        "androidworld_devices": ["device"],
        "seed": 42,
        "androidworld_api_url": "http://fake",
        "model": "test",
        "base_url": "http://model/v1",
        "evaluation_task_attempts": 1,
    }
    result = evaluation.run_queue(
        tmp_path, tmp_path / "stage", stage, settings, tmp_path
    )
    assert dispatched == expected
    assert result["planned_denominator"] == len(expected)
    assert result["status"] == "completed"


@pytest.mark.parametrize("stage", ["screen", "selection", "confirmation"])
def test_artemis_dispatches_exact_new_set(tmp_path, monkeypatch, stage):
    expected = stage_tasks(TASK_FILE, stage)
    settings = {
        "python": "python",
        "task_file": str(TASK_FILE),
        "seed": 999,
        "model": "test",
        "base_url": "http://model/v1",
        "adb_path": "/bin/true",
        "androidworld_root": str(tmp_path),
        "androidworld_assigned_device": "device",
        "console_port": 5560,
        "grpc_port": 8560,
        "task_timeout": 10,
        "stage_timeout": 20,
    }

    def execute(command, worktree, artifacts, timeout, env):
        assert command[command.index("--tasks") + 1].split(",") == expected
        manifest = {
            "status": "completed",
            "model": "test",
            "model_base_url": settings["base_url"],
            "seed": 999,
            "tasks": expected,
            "combinations": 1,
            "episodes": [
                {
                    "template": task,
                    "index": 0,
                    "success": 0,
                    "androidworld_reward": 0,
                    "artemis_status": "completed",
                }
                for task in expected
            ],
        }
        (artifacts.parent / "manifest.json").write_text(json.dumps(manifest))
        return {"returncode": 0, "timed_out": False}, ""

    monkeypatch.setattr(artemis, "execute", execute)
    result = artemis.run_stage(tmp_path, tmp_path / "stage", stage, settings, tmp_path)
    assert result["planned_tasks"] == expected
    assert result["status"] == "completed" and result["success_rate_pct"] == 0


def test_legacy_runner_receives_confirmation_as_normalized_evaluation(
    tmp_path, monkeypatch
):
    expected = stage_tasks(TASK_FILE, "confirmation")

    def call(command, cwd, **kwargs):
        assert command[command.index("--subset-split") + 1] == "evaluation"
        path = command[command.index("--task-file") + 1]
        assert json.loads(Path(path).read_text()) == {"evaluation": expected}
        runtime.write_json(tmp_path / "stage/summary.json", {"status": "completed"})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runtime, "call", call)
    settings = {
        "task_file": str(TASK_FILE),
        "seed": 999,
        "base_url": "http://model",
        "model": "test",
        "max_steps": 10,
        "task_timeout": 10,
        "stage_timeout": 20,
        "grpc_port": 8560,
    }
    assert (
        runtime.run_stage(
            tmp_path, tmp_path / "stage", "confirmation", settings, tmp_path
        )["status"]
        == "completed"
    )
