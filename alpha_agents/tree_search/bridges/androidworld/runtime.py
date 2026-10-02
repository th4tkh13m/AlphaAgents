"""AndroidWorld adapter: DGM mutates in parallel, evaluations hold one device lock."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .task_sets import STAGES as STAGES
from .task_sets import stage_tasks


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8"
    )


def performance(summary, stage):
    planned = summary.get("planned_tasks", [])
    results = {x.get("task_name"): x for x in summary.get("task_results", [])}
    resolved = [x for x in planned if results.get(x, {}).get("success")]
    evaluation_status = (
        summary.get("status")
        if summary.get("status") in {"invalid_setup", "invalid_runtime"}
        else "completed"
    )
    return {
        "accuracy_score": 0.0
        if evaluation_status != "completed"
        else summary.get("success_rate_pct", 0.0) / 100,
        "total_resolved_instances": len(resolved),
        "total_submitted_instances": summary.get("planned_denominator", len(planned)),
        "total_resolved_ids": resolved,
        "total_unresolved_ids": [x for x in planned if x not in resolved],
        "total_emptypatch_ids": [],
        "evaluation_stage": stage,
        "evaluation_status": evaluation_status,
    }


def call(command, cwd, **kwargs):
    return subprocess.run(
        command, cwd=cwd, text=True, capture_output=True, check=False, **kwargs
    )


def _process_evidence(command, *, result=None, error=None, stage=None):
    """Serialize completed, timed-out, and crashed subprocess evidence uniformly."""
    record = {"command": [str(item) for item in command], "stage": stage}
    if result is not None:
        record.update(
            returncode=result.returncode, stdout=result.stdout, stderr=result.stderr
        )
    if error is not None:
        record.update(
            status="timed_out"
            if isinstance(error, subprocess.TimeoutExpired)
            else "crashed",
            error=f"{type(error).__name__}: {error}",
            stdout=getattr(error, "stdout", None) or getattr(error, "output", None),
            stderr=getattr(error, "stderr", None),
        )
    return record


@contextlib.contextmanager
def evaluation_lock(root, device=None, *, blocking=True):
    suffix = f".{device}" if device else ""
    with (Path(root) / f".androidworld-evaluation.lock{suffix}").open("w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield lock
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _request_json(url, *, method="GET", timeout=15, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {} if data is None else {"content-type": "application/json"}
    with urlopen(
        Request(url, data=data, headers=headers, method=method), timeout=timeout
    ) as response:
        return json.loads(response.read())


def preflight_mobile_agent_http(config, lock_root):
    """Prove the shared controller can answer a harmless reset before scoring."""
    base_url = config["androidworld_api_url"].rstrip("/")
    device = config.get("androidworld_device")
    record = {"server_url": base_url, "device": device, "checks": []}
    episode_id = None
    with evaluation_lock(
        lock_root, device, blocking=not config.get("evaluation_nonblocking_lock", False)
    ):
        try:
            health = _request_json(f"{base_url}/health")
            record["checks"].append({"name": "health", "response": health})
            healthy = health.get("status") == "success" or health.get("ok") is True
            if device and "device_status" in health:
                # The gateway-wide ``ok`` is false when *any* emulator is
                # unavailable. A leased child only needs its own device, so
                # do not reject a ready device because another lease is down.
                healthy = health["device_status"].get(device) is True
            if not healthy:
                raise RuntimeError(
                    "health response did not mark the requested device ready"
                )
            if device:
                episode = _request_json(
                    f"{base_url}/episode/start",
                    method="POST",
                    payload={"device": device},
                )
                episode_id = episode.get("episode_id")
                record["checks"].append({"name": "episode_start", "response": episode})
                if not episode_id:
                    raise RuntimeError("episode/start returned no episode_id")
            reset_params = {"go_home": "false"}
            if device:
                reset_params["device"] = device
            if episode_id:
                reset_params["episode_id"] = episode_id
            reset = _request_json(
                f"{base_url}/reset?{urlencode(reset_params)}", method="POST"
            )
            record["checks"].append({"name": "reset", "response": reset})
            if reset.get("status") != "success":
                raise RuntimeError("reset response was not success")
        except (OSError, URLError, ValueError, RuntimeError) as error:
            record.update(
                status="invalid_runtime",
                error=f"AndroidWorld preflight failed: {type(error).__name__}: {error}",
            )
        else:
            record["status"] = "completed"
        finally:
            if episode_id:
                try:
                    released = _request_json(
                        f"{base_url}/episode/end",
                        method="POST",
                        payload={"device": device, "episode_id": episode_id},
                    )
                    record["checks"].append(
                        {"name": "episode_end", "response": released}
                    )
                except (OSError, URLError, ValueError) as error:
                    record["episode_end_error"] = f"{type(error).__name__}: {error}"
    return record


def reclaim_timed_out_episode(config, task_dir, device):
    """Release an episode whose runner was killed before its ``finally`` ran.

    The runner writes its opaque episode capability immediately after acquire.
    The outer supervisor owns timeout handling, so it must consume that durable
    record rather than relying on a killed process to execute cleanup.
    """
    task_dir = Path(task_dir)
    if not device:
        return {
            "status": "released",
            "confirmed_by": "no_gateway_lease",
            "attempts": [],
        }
    try:
        ownership = _read_if_present(task_dir / "run_metadata.json") or {}
    except (OSError, ValueError) as error:
        return {"status": "ownership_unavailable", "error": str(error), "attempts": []}
    episode_id = ownership.get("episode_id")
    report = {
        "device": device,
        "episode_id": episode_id,
        "status": "not_attempted",
        "attempts": [],
    }
    if not device or not episode_id:
        report.update(
            status="ownership_unavailable",
            error="timed-out runner did not persist an episode_id",
        )
        return report
    timeout = config.get("episode_cleanup_timeout", 5)
    # New runners record successful cleanup. Older snapshots remain supported
    # through their persisted episode capability and idempotent release.
    try:
        terminal = _read_if_present(task_dir / "summary.json") or {}
    except (OSError, ValueError):
        terminal = {}
    if (
        terminal.get("episode_released") is True
        and terminal.get("episode_id") == episode_id
    ):
        report.update(status="released", confirmed_by="worker")
        return report
    if ownership.get("task_initialized") and ownership.get("task"):
        try:
            report["task_teardown"] = _request_json(
                f"{config['androidworld_api_url'].rstrip('/')}/task/tear_down",
                method="POST",
                timeout=timeout,
                payload={
                    "device": device,
                    "episode_id": episode_id,
                    "task_type": ownership["task"],
                    "task_idx": 0,
                },
            )
        except Exception as error:
            report["task_teardown_error"] = f"{type(error).__name__}: {error}"
    retries = max(1, int(config.get("episode_cleanup_retries", 3)))
    for attempt in range(1, retries + 1):
        try:
            response = _request_json(
                f"{config['androidworld_api_url'].rstrip('/')}/episode/end",
                method="POST",
                timeout=timeout,
                payload={"device": device, "episode_id": episode_id},
            )
            report["attempts"].append({"attempt": attempt, "response": response})
            report["status"] = "released"
            return report
        except Exception as error:
            if isinstance(error, HTTPError) and error.code == 409:
                try:
                    detail = error.read(4096).decode("utf-8", errors="replace")
                except Exception:
                    detail = ""
                # A generic conflict may refer to another owner; never clear it.
                if any(
                    marker in detail.lower()
                    for marker in (
                        "already released",
                        "already ended",
                        "no active episode",
                    )
                ):
                    report.update(
                        status="released",
                        confirmed_by="already_released",
                        detail=detail,
                    )
                    return report
            report["attempts"].append(
                {"attempt": attempt, "error": f"{type(error).__name__}: {error}"}
            )
            if attempt < retries:
                time.sleep(min(2 ** (attempt - 1), 4))
    report.update(
        status="release_failed", error="supervisor could not confirm episode release"
    )
    return report


@contextlib.contextmanager
def supervised_episode_cleanup(config, task_dir, device, report):
    """Run while holding the device lock, after the subprocess has been reaped."""
    try:
        yield
    finally:
        try:
            report.update(reclaim_timed_out_episode(config, task_dir, device))
        except Exception as error:
            report.update(
                status="release_failed", error=f"{type(error).__name__}: {error}"
            )
        # Cleanup evidence must never replace the worker's original exception.
        try:
            write_json(Path(task_dir) / "episode_cleanup.json", report)
        except OSError:
            pass


def _is_runtime_failure(record):
    """Separate controller/transport failures from a completed task miss."""
    outcome = record.get("outcome_status")
    text = str(record.get("status", ""))
    steps = record.get("steps", 0)
    transport_markers = (
        "HTTPError",
        "HTTP ",
        "Server Error",
        "ConnectionError",
        "RemoteDisconnected",
        "timed out",
        "runner exited without summary",
    )
    # A killed evaluation has no authoritative outcome, even after actions.
    if outcome in {"timed_out", "interrupted", "invalid_runtime"}:
        return True
    return any(marker in text for marker in transport_markers) or (
        outcome == "failed" and steps == 0
    )


def _partial_trajectory_steps(task_dir):
    """Recover flushed action records when the task subprocess is killed."""
    return sum(
        len(path.read_text(encoding="utf-8").splitlines())
        for path in task_dir.glob("trajectories/**/action.jsonl")
    )


def run_stage(worktree, artifact_dir, stage, config, lock_root):
    if config.get("evaluation_runner") == "artemis_local":
        from .artemis import run_stage as run_artemis_stage

        return run_artemis_stage(worktree, artifact_dir, stage, config, lock_root)
    if config.get("evaluation_runner") == "mobile_agent_http":
        from .evaluation import supervised_stage

        return supervised_stage(worktree, artifact_dir, stage, config, lock_root)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    external_stage = "evaluation" if stage == "confirmation" else stage
    normalized_manifest = artifact_dir / "task_sets.json"
    write_json(
        normalized_manifest, {external_stage: stage_tasks(config["task_file"], stage)}
    )
    command = [
        config.get("python", sys.executable),
        "scripts/evaluate_androidworld.py",
        "--task-file",
        str(normalized_manifest),
        "--subset-split",
        external_stage,
        "--seed",
        str(config["seed"]),
        "--output-dir",
        str(artifact_dir),
        "--base-url",
        config["base_url"],
        "--model",
        config["model"],
        "--api-key",
        "EMPTY",
        "--max-steps",
        str(config["max_steps"]),
        "--task-timeout",
        str(config["task_timeout"]),
        "--grpc-port",
        str(config["grpc_port"]),
    ]
    if config.get("device_name"):
        command += ["--device-name", config["device_name"]]
    artifact_dir.mkdir(parents=True, exist_ok=True)
    try:
        with evaluation_lock(lock_root):
            result = call(
                command,
                worktree,
                timeout=config["stage_timeout"],
                env=os.environ.copy(),
            )
        write_json(
            artifact_dir / "runner_result.json",
            _process_evidence(command, result=result, stage=f"{stage}_runner"),
        )
    except subprocess.TimeoutExpired as error:
        write_json(
            artifact_dir / "runner_result.json",
            _process_evidence(command, error=error, stage=f"{stage}_runner"),
        )
    summary = artifact_dir / "summary.json"
    return (
        read_json(summary)
        if summary.exists()
        else {
            "status": "invalid_setup",
            "error": "evaluator exited without summary.json",
        }
    )


def run_mobile_agent_http_stage(worktree, artifact_dir, stage, config, lock_root):
    """Checkpointed evaluator; direct entrypoint for offline tests and workers."""
    from .evaluation import run_queue

    return run_queue(worktree, artifact_dir, stage, config, lock_root)


def _read_if_present(path):
    try:
        return read_json(path) if Path(path).exists() else None
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return {"evidence_read_error": f"{type(error).__name__}: {error}"}


def expose_agent_venv(worktree, venv_source=None):
    """Make the DGM interpreter available as ``./venv`` without versioning it.

    Candidate copies deliberately omit environments.  Codex nevertheless needs
    a stable repository-local interpreter for focused tests and live harness
    experiments.  A symlink avoids copying the large environment and Git's
    local exclude prevents it from becoming part of a model patch.
    """
    worktree = Path(worktree)
    source = (
        Path(venv_source) if venv_source else Path(__file__).resolve().parent / ".venv"
    )
    source = source.resolve()
    target = worktree / "venv"
    if not source.is_dir():
        return None
    if target.is_symlink() or target.exists():
        return target
    target.symlink_to(source, target_is_directory=True)
    result = call(["git", "rev-parse", "--git-dir"], worktree)
    if result.returncode == 0:
        git_dir = Path(result.stdout.strip())
        if not git_dir.is_absolute():
            git_dir = worktree / git_dir
        exclude = git_dir / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if "venv" not in existing.splitlines():
            with exclude.open("a", encoding="utf-8") as handle:
                if existing and not existing.endswith("\n"):
                    handle.write("\n")
                handle.write("venv\n")
    return target
