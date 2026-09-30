"""Durable task queue and restart supervisor for AndroidWorld evaluation.

No remote lease is force-released. Recovery only uses a persisted capability or
a successful fresh acquire/release preflight after the old lease expires.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Propagate the installed package or checkout root to detached evaluator workers.
_PACKAGE_ROOT = str(Path(__file__).resolve().parents[4])


def worker_environment():
    env = os.environ.copy()
    env["PYTHONPATH"] = _PACKAGE_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return env


class ContractMismatch(ValueError):
    """A different experiment must not reuse this experiment's checkpoint."""


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def failure(task, device, error):
    return {
        "task_name": task,
        "device": device,
        "success": False,
        "score": 0.0,
        "steps": 0,
        "duration_sec": 0,
        "outcome_status": "invalid_runtime",
        "status": f"{type(error).__name__}: {error}",
    }


def task_record(raw, task, device):
    return {
        "task_name": task,
        "device": device,
        "authoritative_outcome": raw.get("outcome_status") == "completed",
        "success": bool(raw.get("score")) and raw.get("outcome_status") == "completed",
        "score": float(bool(raw.get("score"))),
        "steps": len(raw.get("step_data", [])),
        "duration_sec": raw.get("elapsed_seconds", 0),
        "outcome_status": "completed"
        if raw.get("outcome_status") == "completed"
        else "invalid_runtime",
        "status": raw.get("error", raw.get("outcome_status", "unknown")),
    }


def run_attempt(worktree, directory, index, task, device, config, lock_root):
    from . import runtime as mobile

    cleanup = {}
    result = None
    record = None
    launched = False
    try:
        directory.mkdir(parents=True, exist_ok=True)
        command = [
            config.get("python", sys.executable),
            "scripts/run_ma35_on_docker.py",
            "--server-url",
            config["androidworld_api_url"],
            "--model-base-url",
            config["base_url"],
            "--model",
            config["model"],
            "--task",
            task,
            "--seed",
            str(config["seed"] + index),
            "--max-steps",
            str(config["max_steps"]),
            "--output-dir",
            str(directory),
        ]
        if device:
            command += ["--device", device]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(worktree) + os.pathsep + env.get("PYTHONPATH", "")
        with mobile.evaluation_lock(lock_root, device, blocking=False) as device_lock:
            with mobile.supervised_episode_cleanup(config, directory, device, cleanup):
                try:
                    launched = True
                    result = mobile.call(
                        command,
                        worktree,
                        timeout=config["task_timeout"],
                        env=env,
                        pass_fds=(device_lock.fileno(),),
                    )
                except subprocess.TimeoutExpired as error:
                    record = failure(task, device, error)
                    record["steps"] = mobile._partial_trajectory_steps(directory)
                    record["original_outcome_status"] = "timed_out"
                    atomic_json(
                        directory / "runner_result.json",
                        mobile._process_evidence(command, error=error),
                    )
            if result is not None:
                atomic_json(
                    directory / "runner_result.json",
                    mobile._process_evidence(command, result=result),
                )
            if record is None:
                raw = load_json(directory / "summary.json")
                record = task_record(raw, task, device)
            if cleanup.get("status") != "released":
                record.update(success=False, outcome_status="invalid_runtime")
            record["episode_cleanup"] = cleanup
            record["runner_returncode"] = None if result is None else result.returncode
    except Exception as error:
        record = failure(task, device, error)
        if isinstance(error, BlockingIOError) and not launched:
            record["resource_unavailable"] = True
        try:
            raw = load_json(directory / "summary.json")
            if raw.get("outcome_status") == "completed":
                # Artifact/cleanup failure invalidates the attempt, but must
                # not turn a known measured miss into another chance to score.
                record["authoritative_outcome"] = True
                record["worker_score"] = raw.get("score")
        except (OSError, ValueError, AttributeError):
            pass
        record["episode_cleanup"] = cleanup
    # The coordinator checkpoints this result even if this task's directory is
    # unwritable. A bad task artifact must not unwind another task's future.
    try:
        atomic_json(directory / "attempt_result.json", record)
    except OSError as error:
        record["artifact_error"] = str(error)
    return record


def run_queue(worktree, artifact_dir, stage, config, lock_root):
    from . import runtime as mobile

    worktree, artifact_dir, lock_root = map(Path, (worktree, artifact_dir, lock_root))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    lock_root.mkdir(parents=True, exist_ok=True)
    # One owner of a checkpoint, including when a second restart is requested.
    with (artifact_dir / ".continuation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _run_queue(worktree, artifact_dir, stage, config, lock_root, mobile)


def _run_queue(worktree, artifact_dir, stage, config, lock_root, mobile):
    tasks = list(load_json(config["task_file"])[stage])
    devices = list(config.get("androidworld_devices") or [None])
    interval = max(0.01, float(config.get("evaluation_recovery_interval", 15)))
    max_attempts = max(1, int(config.get("evaluation_task_attempts", 3)))
    identity = {
        "worktree": str(worktree.resolve()),
        "stage": stage,
        "tasks": tasks,
        "config": config,
    }
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True, default=str).encode()
    ).hexdigest()
    state_path = artifact_dir / "evaluation_state.json"
    if state_path.exists():
        state = load_json(state_path)
        if state["fingerprint"] != fingerprint:
            raise ContractMismatch(
                "evaluation checkpoint contract changed; use a new artifact directory"
            )
    else:
        state = {
            "version": 1,
            "fingerprint": fingerprint,
            "status": "running",
            "tasks": [
                {"task_name": task, "attempts": [], "result": None} for task in tasks
            ],
            "blocked_devices": {},
        }

    def save():
        # Losing storage must pause dispatch, not lose the queue or spin/crash.
        while True:
            try:
                atomic_json(state_path, state)
                return
            except OSError as error:
                print(
                    f"evaluation checkpoint unavailable; retrying: {error}",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(interval)

    def terminal(entry, record):
        if record.get("resource_unavailable"):
            return False
        runtime_attempts = sum(
            not (a.get("result") or {}).get("resource_unavailable")
            for a in entry["attempts"]
        )
        return (
            record.get("authoritative_outcome")
            or not mobile._is_runtime_failure(record)
            or runtime_attempts >= max_attempts
        )

    # An attempt recorded as running at a prior crash is either recoverable from
    # its result or an invalid runtime attempt. Never overwrite its artifacts.
    for entry in state["tasks"]:
        for attempt in entry["attempts"]:
            if attempt.get("result") is not None:
                continue
            directory = Path(attempt["directory"])
            try:
                record = load_json(directory / "attempt_result.json")
                if not isinstance(record, dict) or "outcome_status" not in record:
                    raise ValueError("malformed attempt result")
            except (OSError, ValueError):
                record = failure(
                    entry["task_name"],
                    attempt["device"],
                    RuntimeError("evaluator interrupted during attempt"),
                )
                # Preserve an authoritative worker outcome if the crash occurred
                # between the worker summary and the supervisor checkpoint.
                try:
                    raw = load_json(directory / "summary.json")
                    if raw.get("outcome_status") == "completed":
                        record = task_record(raw, entry["task_name"], attempt["device"])
                        if raw.get("episode_released") is True:
                            record["episode_cleanup"] = {
                                "status": "released",
                                "confirmed_by": "worker",
                            }
                        else:
                            record.update(
                                success=False,
                                outcome_status="invalid_runtime",
                                status="worker scored but release was not confirmed before interruption",
                            )
                except (OSError, ValueError, AttributeError, TypeError):
                    pass
            attempt["result"] = record
            state["blocked_devices"][str(attempt["device"])] = str(directory)
            if terminal(entry, record):
                entry["result"] = record
    save()
    while any(entry["result"] is None for entry in state["tasks"]):
        ready = []
        state["device_probes"] = {}
        for device in devices:
            blocked = state["blocked_devices"].get(str(device))
            if blocked:
                try:
                    with mobile.evaluation_lock(lock_root, device, blocking=False):
                        mobile.reclaim_timed_out_episode(config, Path(blocked), device)
                except Exception:
                    pass
            # Fresh acquisition is the readiness proof, including recovery after
            # TTL expiry when the old capability was never persisted.
            try:
                preflight = mobile.preflight_mobile_agent_http(
                    {
                        **config,
                        "androidworld_device": device,
                        "evaluation_nonblocking_lock": True,
                    },
                    lock_root,
                )
            except Exception as error:
                preflight = {"status": "invalid_runtime", "error": str(error)}
            state["device_probes"][str(device)] = preflight
            if preflight.get("status") == "completed" and not preflight.get(
                "episode_end_error"
            ):
                ready.append(device)
                state["blocked_devices"].pop(str(device), None)
        if not ready:
            state["status"] = "waiting_for_device"
            save()
            time.sleep(interval)
            continue
        state["status"] = "running"
        # Untouched tasks go first, so one repeatedly broken task cannot starve
        # the rest. No fixed index-to-device assignment survives a failure.
        pending = sorted(
            (i for i, entry in enumerate(state["tasks"]) if entry["result"] is None),
            key=lambda i: (len(state["tasks"][i]["attempts"]), i),
        )
        batch = []
        for device, index in zip(ready, pending):
            entry = state["tasks"][index]
            directory = (
                artifact_dir
                / "tasks"
                / f"{index:03d}_{entry['task_name']}"
                / f"attempt_{len(entry['attempts']) + 1:04d}"
            )
            attempt = {
                "device": device,
                "directory": str(directory.resolve()),
                "result": None,
            }
            entry["attempts"].append(attempt)
            batch.append((index, device, directory, attempt))
        save()  # A durable running record MUST precede remote work.
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(batch)) as executor:
            futures = {
                executor.submit(
                    run_attempt,
                    worktree,
                    directory,
                    index,
                    tasks[index],
                    device,
                    config,
                    lock_root,
                ): (index, device, directory, attempt)
                for index, device, directory, attempt in batch
            }
            for future in concurrent.futures.as_completed(futures):
                index, device, directory, attempt = futures[future]
                try:
                    record = future.result()
                except Exception as error:
                    record = failure(tasks[index], device, error)
                entry = state["tasks"][index]
                attempt["result"] = record
                if record.get("episode_cleanup", {}).get("status") != "released":
                    state["blocked_devices"][str(device)] = str(directory.resolve())
                if terminal(entry, record):
                    entry["result"] = record
                save()
        if any(
            attempt["result"].get("resource_unavailable") for _, _, _, attempt in batch
        ):
            time.sleep(interval)
    results = [
        dict(entry["result"], attempts=entry["attempts"]) for entry in state["tasks"]
    ]
    failures = [record for record in results if mobile._is_runtime_failure(record)]
    successes = sum(bool(record["success"]) for record in results)
    summary = {
        "runtime": "mobile_agent_http",
        "status": "invalid_runtime" if failures else "completed",
        "seed": config["seed"],
        "server_url": config["androidworld_api_url"],
        "model": config["model"],
        "base_url": config["base_url"],
        "planned_tasks": tasks,
        "planned_denominator": len(tasks),
        "successful_tasks": successes,
        "failed_tasks": len(tasks) - successes,
        "success_rate_pct": round(100 * successes / len(tasks), 2) if tasks else 0.0,
        "task_results": results,
        "runtime_failures": failures,
        "status_counts": {
            status: sum(r["outcome_status"] == status for r in results)
            for status in (
                "completed",
                "failed",
                "invalid_runtime",
                "timed_out",
                "interrupted",
            )
        },
    }
    state.update(status="finished", summary=summary)
    save()
    atomic_json(artifact_dir / "summary.json", summary)
    return summary


def supervise(request_path):
    """Restart a crashed evaluator, killing its remaining process group first."""
    request_path = Path(request_path).resolve()
    while True:
        try:
            request = load_json(request_path)
            break
        except (OSError, ValueError) as error:
            print(
                f"evaluation request unavailable; retrying: {error}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(15)
    interval = max(
        0.01, float(request["config"].get("evaluation_recovery_interval", 15))
    )
    while True:
        process = None
        code = None
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "alpha_agents.tree_search.bridges.androidworld.evaluation",
                    "worker",
                    str(request_path),
                ],
                start_new_session=True,
                env=worker_environment(),
            )
            code = process.wait()
        except OSError as error:
            print(
                f"evaluator launch/wait failed; retrying: {error}",
                file=sys.stderr,
                flush=True,
            )
        finally:
            if process is not None:
                # Kill orphan task runners before a new worker can touch devices.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        if code == 0:
            try:
                return load_json(Path(request["artifact_dir"]) / "summary.json")
            except (OSError, ValueError) as error:
                print(
                    f"evaluation summary unavailable: {error}",
                    file=sys.stderr,
                    flush=True,
                )
        if code == 78:
            raise ContractMismatch(
                "worker rejected changed evaluation contract; use a new artifact directory"
            )
        print(
            f"evaluator exited {code}; resuming checkpoint in {interval}s",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(interval)


def supervised_stage(worktree, artifact_dir, stage, config, lock_root):
    artifact_dir = Path(artifact_dir).resolve()
    request_path = artifact_dir / "evaluation_request.json"
    request = {
        "worktree": str(Path(worktree).resolve()),
        "artifact_dir": str(artifact_dir),
        "stage": stage,
        "config": config,
        "lock_root": str(Path(lock_root).resolve()),
    }
    interval = max(0.01, float(config.get("evaluation_recovery_interval", 15)))
    while True:
        try:
            atomic_json(request_path, request)
            break
        except OSError as error:
            print(
                f"evaluation request storage unavailable: {error}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(interval)
    return supervise(request_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["worker", "supervise"])
    parser.add_argument("request")
    args = parser.parse_args()

    # Service-manager stops unwind supervision and reap workers. SIGKILL needs
    # an external service manager to restart this supervisor.
    def terminate(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    if args.mode == "worker":
        try:
            run_queue(**load_json(args.request))
        except ContractMismatch as error:
            print(error, file=sys.stderr)
            raise SystemExit(78)
    else:
        supervise(args.request)


if __name__ == "__main__":
    main()
