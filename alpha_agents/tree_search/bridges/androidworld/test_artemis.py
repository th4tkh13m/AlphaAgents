"""Bridge contract tests: authoritative scores and usable mutation context."""

import json
import shutil
from pathlib import Path

import pytest

from alpha_agents.tree_search.bridges.androidworld import artemis
from alpha_agents.tree_search.bridges.androidworld.bridge import AndroidWorldBridge
from alpha_agents.tree_search.core.contracts import Candidate
from alpha_agents.tree_search.core.workspace import materialize


def settings(tmp_path):
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps({"evaluation": ["ClockStopWatchRunning"]}))
    return {
        "evaluation_runner": "artemis_local",
        "base_url": "http://model/v1",
        "model": "qwen",
        "seed": 42,
        "task_file": str(tasks),
        "androidworld_root": str(tmp_path),
        "adb_path": "/bin/true",
        "console_port": 5560,
        "grpc_port": 8560,
        "androidworld_devices": ["emulator-5560"],
        "python": "/bin/python",
        "androidworld_assigned_device": "emulator-5560",
    }


def manifest(config, score=1):
    return {
        "status": "completed",
        "model": config["model"],
        "model_base_url": config["base_url"],
        "seed": 42,
        "tasks": ["ClockStopWatchRunning"],
        "combinations": 1,
        "episodes": [
            {
                "template": "ClockStopWatchRunning",
                "index": 0,
                "artemis_status": "completed",
                "success": score,
                "androidworld_reward": score,
                "exception": None,
                "seconds": 10,
            }
        ],
    }


PROCESS = {"returncode": 0, "timed_out": False}


@pytest.mark.parametrize("score", [0, 1])
def test_authoritative_reward_maps_to_controller_score(tmp_path, monkeypatch, score):
    config = settings(tmp_path)
    bridge = AndroidWorldBridge(tmp_path, config)
    summary = artemis.normalize_manifest(
        manifest(config, score), ["ClockStopWatchRunning"], config, PROCESS
    )
    from alpha_agents.tree_search.bridges.androidworld import runtime

    monkeypatch.setattr(runtime, "run_stage", lambda *args: summary)
    with bridge.lease() as resource:
        result = bridge.evaluate(
            Candidate("initial", None, tmp_path / "initial", ""), resource
        )
    assert result.status == "completed" and result.score == score
    assert result.metrics["task_results"][0]["androidworld_reward"] == score


@pytest.mark.parametrize(
    "defect",
    [
        "partial",
        "duplicate",
        "wrong_model",
        "exception",
        "nan",
        "wrong_reward",
        "timeout",
    ],
)
def test_invalid_evidence_never_becomes_score(tmp_path, monkeypatch, defect):
    config = settings(tmp_path)
    value = manifest(config)
    process = dict(PROCESS)
    if defect == "partial":
        value["episodes"] = []
    if defect == "duplicate":
        value["episodes"] *= 2
    if defect == "wrong_model":
        value["model"] = "other"
    if defect == "exception":
        value["episodes"][0]["exception"] = "RuntimeError"
    if defect == "nan":
        value["episodes"][0]["success"] = float("nan")
    if defect == "wrong_reward":
        value["episodes"][0]["androidworld_reward"] = 0
    if defect == "timeout":
        process["timed_out"] = True
    summary = artemis.normalize_manifest(
        value, ["ClockStopWatchRunning"], config, process
    )
    from alpha_agents.tree_search.bridges.androidworld import runtime

    monkeypatch.setattr(runtime, "run_stage", lambda *args: summary)
    with AndroidWorldBridge(tmp_path, config).lease() as resource:
        result = AndroidWorldBridge(tmp_path, config).evaluate(
            Candidate("initial", None, tmp_path, ""), resource
        )
    assert result.status == "failed" and result.score is None and result.diagnostic
    from dataclasses import asdict

    json.dumps(asdict(result), allow_nan=False)


def test_mutator_gets_parent_results_candidate_command_and_assigned_device(tmp_path):
    config = settings(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "agent.py").write_text("value = 0\n")
    bridge = AndroidWorldBridge(source, config)
    runs = tmp_path / "runs"
    runs.mkdir()
    shutil.copytree(bridge.source, runs / "source")
    initial = materialize(bridge.source, runs, "initial", None)
    (initial.directory / "candidate.json").write_text(
        json.dumps(
            {
                "id": "initial",
                "parent_id": None,
                "base_commit": initial.base_commit,
                "evaluation": {
                    "status": "completed",
                    "score": 0,
                    "metrics": {"task_results": [{"success": False}]},
                },
            }
        )
    )
    child = materialize(bridge.source, runs, "child", initial)
    with bridge.lease() as resource:
        context = bridge.prepare(child, initial, resource)
    assert context.evidence["parent_evaluation"]["score"] == 0
    assert context.evidence["resource"] == "emulator-5560"
    assert context.evidence["experiment_environment"]["DGM_ARTEMIS_WORKTREE"] == str(
        child.workspace
    )
    assert (
        str(Path(artemis.__file__).with_name("artemis_runner.py"))
        in context.evidence["experiment_command"]
    )
    assert (child.workspace / ".dgm_parent_evidence/candidate.json").is_file()
    assert not (source / "venv").exists()


def test_output_modules_survive_core_snapshot_and_candidate_patches(tmp_path):
    source = tmp_path / "harness"
    (source / "artemis/config").mkdir(parents=True)
    module = source / "artemis/config/output.py"
    module.write_text("VALUE = 1\n")
    (source / "artemis/agents/outputter").mkdir(parents=True)
    (source / "artemis/agents/outputter/outputter.md").write_text("Original prompt")
    bridge = AndroidWorldBridge(source, settings(tmp_path))
    runs = tmp_path / "runs"
    runs.mkdir()
    # Emulate the core bootstrap exclusions as well as materialization.
    shutil.copytree(
        bridge.source, runs / "source", ignore=shutil.ignore_patterns("output*")
    )
    candidate = materialize(runs / "source", runs, "initial", None)
    with bridge.lease() as resource:
        bridge.prepare(candidate, None, resource)
    assert (
        candidate.workspace / "artemis/config/output.py"
    ).read_text() == module.read_text()
    assert (
        candidate.workspace / "artemis/agents/outputter/outputter.md"
    ).read_text() == "Original prompt"
    from alpha_agents.tree_search.core.workspace import recover_patch

    assert not recover_patch(candidate)
    (candidate.workspace / "artemis/config/output.py").write_text("VALUE = 2\n")
    assert recover_patch(candidate)
    assert ".dgm_preserved_modules/" in candidate.patch.read_text()
    assert module.read_text() == "VALUE = 1\n"


def test_local_bridge_rejects_unmapped_multiple_devices(tmp_path):
    config = settings(tmp_path)
    config["androidworld_devices"] = ["emulator-5560", "emulator-5562"]
    with pytest.raises(ValueError, match="exactly one"):
        AndroidWorldBridge(tmp_path, config)
