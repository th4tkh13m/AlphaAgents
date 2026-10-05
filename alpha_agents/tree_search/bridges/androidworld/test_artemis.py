"""Bridge contract tests: authoritative scores and usable mutation context."""

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from alpha_agents.tree_search.bridges.androidworld import artemis
from alpha_agents.tree_search.bridges.androidworld.bridge import AndroidWorldBridge
from alpha_agents.tree_search.core.contracts import Candidate
from alpha_agents.tree_search.core.workspace import materialize
from alpha_agents.tree_search.bridges.androidworld.task_outcomes import MissingAgentAnswerError, bounded_agent_failure


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


GraphLimit = type("GraphRecursionError", (Exception,), {"__module__": "langgraph.errors"})


@pytest.mark.parametrize(
    "error,phase",
    [(TimeoutError(), "agent_execution"),
     (TimeoutError("LLM call timed out after 180 seconds."), "agent_execution"),
     (GraphLimit("limit 100"), "agent_execution"),
     (MissingAgentAnswerError("Artemis did not return a non-empty information-retrieval answer"), "answer_submission")],
)
def test_bounded_agent_failure_preserves_other_tasks_and_exception(tmp_path, monkeypatch, error, phase):
    config = settings(tmp_path)
    tasks = ["ClockStopWatchRunning", "SecondTask"]
    Path(config["task_file"]).write_text(json.dumps({"evaluation": tasks}))
    value = manifest(config)
    value["tasks"] = tasks
    failure = {
        "template": tasks[1], "index": 0, "success": 0.0,
        "exception": f"{type(error).__name__}: {error}", "seconds": 180,
        **bounded_agent_failure(error, phase),
    }
    value["episodes"].append(failure)
    summary = artemis.normalize_manifest(value, tasks, config, PROCESS)
    from alpha_agents.tree_search.bridges.androidworld import runtime

    monkeypatch.setattr(runtime, "run_stage", lambda *args: summary)
    bridge = AndroidWorldBridge(tmp_path, config)
    with bridge.lease() as resource:
        result = bridge.evaluate(Candidate("initial", None, tmp_path, ""), resource)
    assert result.status == "completed" and result.score == 0.5
    assert result.metrics["total_submitted_instances"] == 2
    measured = result.metrics["task_results"][1]
    assert measured["score"] == 0 and measured["outcome_status"] == "agent_failed"
    assert measured["exception"] == failure["exception"]
    assert measured["androidworld_reward"] is None
    assert not summary["errors"]


@pytest.mark.parametrize("phase", ["setup", "answer_submission", "grading", "teardown"])
def test_timeout_outside_agent_execution_still_invalidates_evidence(tmp_path, phase):
    error = TimeoutError("LLM call timed out after 180 seconds.")
    assert not bounded_agent_failure(error, phase)
    config = settings(tmp_path)
    value = manifest(config, score=0)
    value["episodes"][0].update(exception=str(error), failure_phase=phase)
    summary = artemis.normalize_manifest(value, value["tasks"], config, PROCESS)
    assert summary["status"] == "invalid_runtime"


@pytest.mark.parametrize("defect", ["nonzero", "wrong_phase", "wrong_error", "fake_reward"])
def test_agent_failure_marker_cannot_hide_invalid_evidence(tmp_path, defect):
    config = settings(tmp_path)
    value = manifest(config, score=0)
    episode = value["episodes"][0]
    error = TimeoutError("LLM call timed out after 180 seconds.")
    episode.update(bounded_agent_failure(error, "agent_execution"), exception=f"TimeoutError: {error}")
    if defect == "nonzero":
        episode["success"] = 1
    elif defect == "wrong_phase":
        episode["failure_phase"] = "grading"
    elif defect == "wrong_error":
        episode["exception"] = "RuntimeError: unavailable"
    else:
        episode["androidworld_reward"] = 1
    assert artemis.normalize_manifest(value, value["tasks"], config, PROCESS)["status"] == "invalid_runtime"


def test_unknown_agent_error_is_not_a_bounded_failure():
    assert not bounded_agent_failure(RuntimeError("broken evaluator integration"), "agent_execution")


@pytest.mark.parametrize("phase", ["setup", "agent_execution", "grading", "teardown"])
def test_missing_answer_error_outside_submission_remains_invalid(phase):
    assert not bounded_agent_failure(MissingAgentAnswerError("missing answer"), phase)


def test_unknown_answer_submission_error_is_not_scored():
    assert not bounded_agent_failure(ValueError("Artemis did not return a non-empty information-retrieval answer"), "answer_submission")
    assert not bounded_agent_failure(RuntimeError("AndroidWorld did not retain the submitted agent answer"), "answer_submission")


def test_runner_can_request_official_emulator_setup(tmp_path):
    config = settings(tmp_path)
    config["task_timeout"] = 30
    config["perform_emulator_setup"] = True
    command = artemis.runner_command(
        tmp_path, tmp_path / "results", ["ClockStopWatchRunning"], config
    )
    assert "--perform-emulator-setup" in command


def test_optional_routes_use_the_assigned_model_including_fallback(tmp_path):
    config = settings(tmp_path)
    output = tmp_path / "runtime"
    output.mkdir()
    env = artemis.execution_environment(tmp_path, output, config)
    llm = json.loads(Path(env["DGM_ARTEMIS_LLM_CONFIG"]).read_text())
    for name in ("planner_validation", "history_analyzer", "output_analyzer"):
        assert llm[name]["provider"] == "openai"
        assert llm[name]["model"] == config["model"]
        assert llm[name]["fallback"] == {"provider": "openai", "model": config["model"]}
    assert env["OPENAI_BASE_URL"] == config["base_url"]


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
    runtime = initial.directory / "stages/selection/runtime"
    (runtime / "images").mkdir(parents=True)
    screenshot = runtime / "images/screen.jpg"
    screenshot.write_bytes(b"saved screenshot")
    with sqlite3.connect(runtime / "data_engine.db") as database:
        database.execute(
            "CREATE TABLE steps (step_number INTEGER, pre_image_name TEXT, action_taken TEXT)"
        )
        database.execute("INSERT INTO steps VALUES (1, 'screen', 'tap')")
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
    copied_runtime = child.workspace / ".dgm_parent_evidence/stages/selection/runtime"
    with sqlite3.connect(
        f"file:{copied_runtime / 'data_engine.db'}?mode=ro", uri=True
    ) as database:
        step, image, action = database.execute("SELECT * FROM steps").fetchone()
    assert (step, action) == (1, "tap")
    assert (
        copied_runtime / "images" / f"{image}.jpg"
    ).read_bytes() == screenshot.read_bytes()
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
    with pytest.raises(ValueError, match="port mapping"):
        AndroidWorldBridge(tmp_path, config)
