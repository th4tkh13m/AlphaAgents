import sys

import pytest

if sys.platform == "win32":
    pytest.skip("AndroidWorld runtime requires Linux/WSL", allow_module_level=True)

import io
import json
from urllib.error import HTTPError

import pytest
from alpha_agents.tree_search.bridges.androidworld import runtime as runner


@pytest.fixture
def owned(tmp_path):
    (tmp_path / "run_metadata.json").write_text(
        json.dumps({"episode_id": "owned", "task": "Example", "task_initialized": True})
    )
    return tmp_path


def test_teardown_failure_still_releases(monkeypatch, owned):
    calls = []

    def request(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("tear_down"):
            raise TimeoutError("teardown stalled")
        return {"status": "success"}

    monkeypatch.setattr(runner, "_request_json", request)
    report = runner.reclaim_timed_out_episode(
        {"androidworld_api_url": "http://gateway"}, owned, "device"
    )
    assert report["status"] == "released"
    assert "task_teardown_error" in report
    assert [url.rsplit("/", 1)[1] for url, _ in calls] == ["tear_down", "end"]
    assert all(
        args["timeout"] == 5 and args["payload"]["episode_id"] == "owned"
        for _, args in calls
    )


@pytest.mark.parametrize(
    "detail,released", [("already released", True), ("different owner", False)]
)
def test_conflict_requires_explicit_release(monkeypatch, owned, detail, released):
    def request(url, **kwargs):
        raise HTTPError(url, 409, "Conflict", {}, io.BytesIO(detail.encode()))

    monkeypatch.setattr(runner, "_request_json", request)
    report = runner.reclaim_timed_out_episode(
        {"androidworld_api_url": "http://gateway", "episode_cleanup_retries": 1},
        owned,
        "device",
    )
    assert (report["status"] == "released") is released


@pytest.mark.parametrize(
    "error",
    [None, TimeoutError("timeout"), RuntimeError("API failure"), KeyboardInterrupt()],
)
def test_cleanup_on_every_exit_preserves_original(monkeypatch, owned, error):
    calls = []

    def cleanup(*args):
        calls.append(args)
        raise RuntimeError("cleanup failure")

    monkeypatch.setattr(runner, "reclaim_timed_out_episode", cleanup)
    report = {}

    def run():
        with runner.supervised_episode_cleanup({}, owned, "device", report):
            if error is not None:
                raise error

    if error is None:
        run()
    else:
        with pytest.raises(type(error)) as caught:
            run()
        assert caught.value is error
    assert len(calls) == 1
    assert report["status"] == "release_failed"
    assert (owned / "episode_cleanup.json").exists()


def test_worker_confirmed_release_needs_no_request(monkeypatch, owned):
    (owned / "summary.json").write_text(
        json.dumps({"episode_id": "owned", "episode_released": True})
    )

    def unexpected(*args, **kwargs):
        pytest.fail("already released episode should not be contacted")

    monkeypatch.setattr(runner, "_request_json", unexpected)
    assert runner.reclaim_timed_out_episode({}, owned, "device")["status"] == "released"


def test_timeout_with_actions_is_not_agent_miss():
    assert runner._is_runtime_failure({"outcome_status": "timed_out", "steps": 25})


@pytest.mark.parametrize("release_ok", [True, False])
def test_stage_timeout_releases_before_next_task(monkeypatch, tmp_path, release_ok):
    import subprocess
    from pathlib import Path

    manifest = tmp_path / "manifest.json"
    (tmp_path / "locks").mkdir()
    manifest.write_text(json.dumps({"selection": ["First", "Second"]}))
    events = []
    monkeypatch.setattr(
        runner, "preflight_mobile_agent_http", lambda *a: {"status": "completed"}
    )

    def request(url, **kwargs):
        events.append("release")
        if not release_ok:
            raise TimeoutError("gateway unavailable")
        return {"status": "success"}

    monkeypatch.setattr(runner, "_request_json", request)

    def call(command, *args, **kwargs):
        task = command[command.index("--task") + 1]
        events.append(task)
        directory = Path(command[command.index("--output-dir") + 1])
        (directory / "run_metadata.json").write_text(json.dumps({"episode_id": task}))
        if task == "First":
            raise subprocess.TimeoutExpired(command, 10)
        (directory / "summary.json").write_text(
            json.dumps({"outcome_status": "completed", "score": True})
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(runner, "call", call)
    summary = runner.run_mobile_agent_http_stage(
        tmp_path,
        tmp_path / "artifacts",
        "selection",
        {
            "task_file": str(manifest),
            "androidworld_devices": ["device"],
            "androidworld_api_url": "http://gateway",
            "base_url": "http://model",
            "model": "test",
            "seed": 0,
            "max_steps": 2,
            "task_timeout": 10,
            "episode_cleanup_retries": 1,
            "evaluation_task_attempts": 1,
        },
        tmp_path / "locks",
    )
    assert summary["status"] == "invalid_runtime"
    # A fresh successful preflight permits reuse even when old-token cleanup
    # failed; unlike the old quarantine, no task is permanently skipped.
    assert events == (
        ["First", "release", "Second", "release"]
        if release_ok
        else ["First", "release", "release", "Second", "release"]
    )
    assert summary["successful_tasks"] == int(release_ok)
