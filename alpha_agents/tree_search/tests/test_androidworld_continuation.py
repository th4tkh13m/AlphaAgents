import sys

import pytest

if sys.platform == "win32":
    pytest.skip("AndroidWorld runtime requires Linux/WSL", allow_module_level=True)

import json
import subprocess
import sys
from pathlib import Path

import pytest
from alpha_agents.tree_search.bridges.androidworld import evaluation as recovery
from alpha_agents.tree_search.bridges.androidworld import runtime as mobile

REAL_PREFLIGHT = mobile.preflight_mobile_agent_http


@pytest.fixture
def setup_queue(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"selection": ["A", "B", "C"]}))
    config = {
        "task_file": str(manifest),
        "androidworld_devices": ["d0"],
        "androidworld_api_url": "http://fake",
        "base_url": "http://model",
        "model": "test",
        "seed": 42,
        "max_steps": 3,
        "task_timeout": 1,
        "evaluation_recovery_interval": 0.01,
        "evaluation_task_attempts": 3,
    }
    monkeypatch.setattr(
        mobile, "preflight_mobile_agent_http", lambda *a: {"status": "completed"}
    )
    monkeypatch.setattr(
        mobile, "reclaim_timed_out_episode", lambda *a: {"status": "released"}
    )
    return dict(
        worktree=tmp_path,
        artifact_dir=tmp_path / "stage",
        stage="selection",
        config=config,
        lock_root=tmp_path / "locks",
    )


def completed(task, device, success=True, released=True):
    return {
        "task_name": task,
        "device": device,
        "success": success,
        "score": float(success),
        "steps": 1,
        "outcome_status": "completed",
        "status": "completed",
        "episode_cleanup": {"status": "released" if released else "release_failed"},
    }


def test_reassigns_untouched_tasks_to_healthy_device(setup_queue, monkeypatch):
    setup_queue["config"].update(
        androidworld_devices=["d0", "d1"], evaluation_task_attempts=1
    )
    calls = []

    def attempt(worktree, directory, index, task, device, config, lock):
        calls.append((task, device))
        if device == "d0":
            return recovery.failure(task, device, RuntimeError("device lost"))
        return completed(task, device)

    def preflight(config, lock):
        return {
            "status": "invalid_runtime"
            if ("A", "d0") in calls and config["androidworld_device"] == "d0"
            else "completed"
        }

    monkeypatch.setattr(recovery, "run_attempt", attempt)
    monkeypatch.setattr(mobile, "preflight_mobile_agent_http", preflight)
    summary = recovery.run_queue(**setup_queue)
    assert ("C", "d1") in calls
    assert len(calls) == 3
    assert summary["planned_denominator"] == 3
    assert summary["status"] == "invalid_runtime"


def test_busy_device_lock_cannot_block_healthy_device(setup_queue, monkeypatch):
    setup_queue["config"]["androidworld_devices"] = ["d0", "d1"]
    setup_queue["lock_root"].mkdir()
    monkeypatch.setattr(mobile, "preflight_mobile_agent_http", REAL_PREFLIGHT)
    monkeypatch.setattr(
        mobile,
        "_request_json",
        lambda *a, **k: {"status": "success", "episode_id": "test"},
    )
    calls = []

    def attempt(w, d, i, task, device, c, lock_root):
        calls.append(device)
        return completed(task, device)

    monkeypatch.setattr(recovery, "run_attempt", attempt)
    with mobile.evaluation_lock(setup_queue["lock_root"], "d0"):
        summary = recovery.run_queue(**setup_queue)
    assert calls == ["d1", "d1", "d1"]
    assert summary["status"] == "completed"


def test_waits_for_all_devices_and_keeps_every_task(setup_queue, monkeypatch):
    probes, calls = [], []

    def preflight(*args):
        probes.append(True)
        return {"status": "completed" if len(probes) > 2 else "invalid_runtime"}

    def sleep(seconds):
        state = recovery.load_json(
            setup_queue["artifact_dir"] / "evaluation_state.json"
        )
        assert state["status"] == "waiting_for_device"
        assert all(not entry["attempts"] for entry in state["tasks"])

    monkeypatch.setattr(mobile, "preflight_mobile_agent_http", preflight)
    monkeypatch.setattr(recovery.time, "sleep", sleep)

    def attempt(w, d, i, task, device, c, lock_root):
        calls.append(task)
        return completed(task, device)

    monkeypatch.setattr(recovery, "run_attempt", attempt)
    assert recovery.run_queue(**setup_queue)["status"] == "completed"
    assert calls == ["A", "B", "C"]


def test_crash_resume_preserves_completed_and_attempt_history(setup_queue, monkeypatch):
    calls = []

    class Crash(BaseException):
        pass

    def attempt(w, d, i, task, device, c, lock_root):
        calls.append(task)
        if calls == ["A", "B"]:
            raise Crash()
        return completed(task, device)

    monkeypatch.setattr(recovery, "run_attempt", attempt)
    with pytest.raises(Crash):
        recovery.run_queue(**setup_queue)
    summary = recovery.run_queue(**setup_queue)
    assert calls == ["A", "B", "C", "B"]
    assert summary["status"] == "completed"
    assert (
        summary["task_results"][1]["attempts"][0]["result"]["outcome_status"]
        == "invalid_runtime"
    )
    assert recovery.run_queue(**setup_queue) == summary
    assert calls == ["A", "B", "C", "B"]


def test_retry_budget_is_fixed_and_does_not_retry_scored_misses(
    setup_queue, monkeypatch
):
    calls = []

    def attempt(w, d, i, task, device, c, lock_root):
        calls.append(task)
        if task == "A":
            return recovery.failure(task, device, RuntimeError("API broken"))
        return completed(task, device, success=False)

    monkeypatch.setattr(recovery, "run_attempt", attempt)
    summary = recovery.run_queue(**setup_queue)
    assert calls == ["A", "B", "C", "A", "A"]
    assert summary["status"] == "invalid_runtime"
    assert len(summary["runtime_failures"]) == 1


def test_dispatch_lock_race_does_not_exhaust_task_budget(setup_queue, monkeypatch):
    setup_queue["config"]["evaluation_task_attempts"] = 1
    calls = []

    def attempt(w, d, i, task, device, c, lock_root):
        calls.append(task)
        if calls.count(task) == 1:
            return dict(
                recovery.failure(task, device, BlockingIOError("busy")),
                resource_unavailable=True,
            )
        return completed(task, device)

    monkeypatch.setattr(recovery, "run_attempt", attempt)
    monkeypatch.setattr(recovery.time, "sleep", lambda seconds: None)
    summary = recovery.run_queue(**setup_queue)
    assert summary["successful_tasks"] == 3
    assert calls == ["A", "B", "C", "A", "B", "C"]


def test_transient_checkpoint_error_pauses_dispatch(setup_queue, monkeypatch):
    original = recovery.atomic_json
    writes, calls = [], []

    def write(path, value):
        writes.append(str(path))
        if len(writes) == 1:
            raise OSError("disk temporarily unavailable")
        original(path, value)

    monkeypatch.setattr(recovery, "atomic_json", write)
    monkeypatch.setattr(
        recovery.time, "sleep", lambda seconds: calls.append("storage_wait")
    )

    def attempt(w, d, i, task, device, c, lock_root):
        calls.append(task)
        return completed(task, device)

    monkeypatch.setattr(recovery, "run_attempt", attempt)
    assert recovery.run_queue(**setup_queue)["status"] == "completed"
    assert calls == ["storage_wait", "A", "B", "C"]


def test_malformed_task_summary_does_not_abort_other_tasks(setup_queue, monkeypatch):
    setup_queue["config"]["evaluation_task_attempts"] = 1
    calls = []

    def call(command, *args, **kwargs):
        task = command[command.index("--task") + 1]
        directory = Path(command[command.index("--output-dir") + 1])
        calls.append((task, command[command.index("--seed") + 1]))
        (directory / "summary.json").write_text(
            "broken"
            if task == "A"
            else json.dumps({"outcome_status": "completed", "score": False})
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(mobile, "call", call)
    summary = recovery.run_queue(**setup_queue)
    assert calls == [("A", "42"), ("B", "43"), ("C", "44")]
    assert len(summary["runtime_failures"]) == 1


def test_task_artifact_error_is_contained(setup_queue, monkeypatch):
    setup_queue["config"]["evaluation_task_attempts"] = 1
    original = recovery.atomic_json

    def write(path, value):
        if "000_A" in str(path):
            raise PermissionError("one artifact directory is unwritable")
        return original(path, value)

    monkeypatch.setattr(recovery, "atomic_json", write)

    def call(command, *args, **kwargs):
        directory = Path(command[command.index("--output-dir") + 1])
        (directory / "summary.json").write_text(
            json.dumps({"outcome_status": "completed", "score": True})
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(mobile, "call", call)
    summary = recovery.run_queue(**setup_queue)
    assert summary["successful_tasks"] == 2
    assert "artifact_error" in summary["task_results"][0]


def test_changed_contract_cannot_reuse_checkpoint(setup_queue, monkeypatch):
    monkeypatch.setattr(
        recovery,
        "run_attempt",
        lambda w, d, i, t, device, c, lock_root: completed(t, device),
    )
    recovery.run_queue(**setup_queue)
    setup_queue["config"]["seed"] += 1
    with pytest.raises(ValueError, match="contract changed"):
        recovery.run_queue(**setup_queue)


def test_supervisor_restarts_real_sigkill_process(tmp_path, monkeypatch):
    request = tmp_path / "request.json"
    recovery.atomic_json(
        request,
        {
            "config": {"evaluation_recovery_interval": 0.01},
            "artifact_dir": str(tmp_path),
        },
    )
    original = subprocess.Popen
    processes = []

    def spawn(command, **kwargs):
        script = (
            "import os,signal; os.kill(os.getpid(),signal.SIGKILL)"
            if not processes
            else "import pathlib; pathlib.Path("
            + repr(str(tmp_path / "summary.json"))
            + ').write_text(\'{"status":"completed"}\')'
        )
        process = original([sys.executable, "-c", script], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(recovery.subprocess, "Popen", spawn)
    assert recovery.supervise(request) == {"status": "completed"}
    assert len(processes) == 2
    assert processes[0].returncode == -9


def test_supervisor_recovers_request_read_and_spawn_errors(tmp_path, monkeypatch):
    request = tmp_path / "request.json"
    recovery.atomic_json(
        request,
        {
            "config": {"evaluation_recovery_interval": 0.01},
            "artifact_dir": str(tmp_path),
        },
    )
    recovery.atomic_json(tmp_path / "summary.json", {"status": "completed"})
    original_load = recovery.load_json
    reads, spawns, sleeps = [], [], []

    def load(path):
        reads.append(path)
        if len(reads) == 1:
            raise OSError("temporary request read failure")
        return original_load(path)

    class Process:
        pid = 999999999

        def wait(self):
            return 0

    def spawn(*a, **k):
        spawns.append(True)
        if len(spawns) == 1:
            raise OSError("temporary process limit")
        return Process()

    monkeypatch.setattr(recovery, "load_json", load)
    monkeypatch.setattr(recovery.subprocess, "Popen", spawn)
    monkeypatch.setattr(recovery.time, "sleep", sleeps.append)
    assert recovery.supervise(request)["status"] == "completed"
    assert len(spawns) == 2
    assert len(sleeps) == 2


def test_sigkill_restarts_actual_queue_and_kills_orphan(setup_queue, monkeypatch):
    import os
    import time

    request = setup_queue["worktree"] / "request.json"
    recovery.atomic_json(request, setup_queue)
    events = setup_queue["worktree"] / "events"
    orphan = setup_queue["worktree"] / "orphan"
    # Use the real queue and real subprocess boundaries, but no remote gateway
    # or model. The second task kills the evaluator, leaving a sleeping child.
    script = r"""
import json, os, signal, subprocess, sys
from pathlib import Path
from alpha_agents.tree_search.bridges.androidworld import evaluation as recovery
from alpha_agents.tree_search.bridges.androidworld import runtime as mobile
request = recovery.load_json(sys.argv[1])
root = Path(request['worktree'])
mobile.preflight_mobile_agent_http = lambda *a: {'status': 'completed'}
mobile.reclaim_timed_out_episode = lambda *a: {'status': 'released'}
def call(command, *args, **kwargs):
    task = command[command.index('--task') + 1]
    directory = Path(command[command.index('--output-dir') + 1])
    with (root / 'events').open('a') as handle:
        handle.write(task + '\n')
        handle.flush()
        os.fsync(handle.fileno())
    if task == 'B' and not (root / 'orphan').exists():
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        (root / 'orphan').write_text(str(child.pid))
        os.kill(os.getpid(), signal.SIGKILL)
    recovery.atomic_json(directory / 'summary.json', {'outcome_status': 'completed', 'score': True})
    return subprocess.CompletedProcess(command, 0, '', '')
mobile.call = call
recovery.run_queue(**request)
"""
    original = subprocess.Popen
    workers = []

    def spawn(command, **kwargs):
        worker = original([sys.executable, "-c", script, str(request)], **kwargs)
        workers.append(worker)
        return worker

    monkeypatch.setattr(recovery.subprocess, "Popen", spawn)
    summary = recovery.supervise(request)
    assert len(workers) == 2
    assert workers[0].returncode == -9
    assert summary["successful_tasks"] == 3
    assert events.read_text().splitlines() == ["A", "B", "C", "B"]
    assert (
        summary["task_results"][1]["attempts"][0]["result"]["outcome_status"]
        == "invalid_runtime"
    )
    pid = int(orphan.read_text())
    # A killed orphan may briefly remain a zombie until the host reaps it.
    for _ in range(100):
        stat = Path(f"/proc/{pid}/stat")
        if not stat.exists() or stat.read_text().split()[2] == "Z":
            break
        time.sleep(0.01)
    else:
        os.kill(pid, 9)
        pytest.fail("orphan survived evaluator restart")


def test_crash_after_worker_summary_keeps_scored_miss(setup_queue, monkeypatch):
    calls = []

    class Crash(BaseException):
        pass

    def attempt(w, directory, i, task, device, c, lock_root):
        calls.append(task)
        if task == "A":
            recovery.atomic_json(
                directory / "summary.json",
                {
                    "outcome_status": "completed",
                    "score": False,
                    "episode_released": True,
                },
            )
            raise Crash()
        return completed(task, device)

    monkeypatch.setattr(recovery, "run_attempt", attempt)
    with pytest.raises(Crash):
        recovery.run_queue(**setup_queue)
    summary = recovery.run_queue(**setup_queue)
    assert calls == ["A", "B", "C"]
    assert summary["successful_tasks"] == 2
    assert summary["status"] == "completed"


def test_cleanup_failure_never_retries_a_scored_miss(setup_queue, monkeypatch):
    calls = []

    def call(command, *args, **kwargs):
        task = command[command.index("--task") + 1]
        directory = Path(command[command.index("--output-dir") + 1])
        calls.append(task)
        recovery.atomic_json(
            directory / "summary.json", {"outcome_status": "completed", "score": False}
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(mobile, "call", call)
    monkeypatch.setattr(
        mobile, "reclaim_timed_out_episode", lambda *a: {"status": "release_failed"}
    )
    summary = recovery.run_queue(**setup_queue)
    assert calls == ["A", "B", "C"]
    assert summary["status"] == "invalid_runtime"
    assert all(len(result["attempts"]) == 1 for result in summary["task_results"])
