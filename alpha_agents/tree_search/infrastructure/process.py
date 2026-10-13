"""Subprocess evidence and bounded process-tree cleanup."""

from __future__ import annotations

import json
import logging
import math
import os
import signal
import subprocess
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

from ..core.shared.storage import write_json

logger = logging.getLogger(__name__)


def process_birth(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _wait_phased(process, started, timeout, phase_path, limits):
    """Observe trusted atomic phase records; phase transitions cannot reset time."""
    phase, entered, seen = "startup", started, []
    sequence = ["startup", "task", "cleanup", "finished"]
    while True:
        now = time.monotonic()
        if phase_path.exists():
            try:
                events = json.loads(phase_path.read_text())["events"]
                if not isinstance(events, list) or events[:len(seen)] != seen or len(events) > 4:
                    raise ValueError("Phase history changed or is malformed")
                for index, event in enumerate(events[len(seen):], len(seen)):
                    when = event["entered"]
                    next_phase = event["phase"]
                    if next_phase != sequence[index] or isinstance(when, bool) or not math.isfinite(when) or when < entered or when > now + .01:
                        raise ValueError("Phase order or monotonic timestamp is invalid")
                    if when - entered > limits[phase]:
                        return True, phase, None
                    if index > 0 and next_phase != "finished":
                        phase, entered = next_phase, when
                    # Initial startup marker does not reset the launch deadline.
                seen = events
            except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
                return True, "protocol", str(error)
        if now - entered >= limits[phase] or (timeout is not None and now - started >= timeout):
            return True, phase if now - entered >= limits[phase] else "outer", None
        if process.poll() is not None:
            return False, None, None
        deadline = entered + limits[phase]
        if timeout is not None:
            deadline = min(deadline, started + timeout)
        try:
            process.wait(timeout=min(.1, max(.001, deadline - now)))
        except subprocess.TimeoutExpired:
            pass


def _kill(process):
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
        process.kill()


def execute(
    command: list[str],
    workspace: Path,
    artifacts: Path,
    timeout: float | None = None,
    env=None,
    stdin_path: Path | None = None,
    phase_path: Path | None = None,
    phase_limits: dict[str, float] | None = None,
):
    if (phase_path is None) != (phase_limits is None):
        raise ValueError("Phase watchdog needs both its trusted record and limits")
    if phase_limits is not None:
        if set(phase_limits) != {"startup", "task", "cleanup"} or any(
            isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or v <= 0 for v in phase_limits.values()
        ):
            raise ValueError("Phase limits must be finite positive startup/task/cleanup durations")
        if phase_path.exists():
            raise ValueError("Phase record must be fresh for this execution")
    artifacts.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    launch_id = uuid.uuid4().hex
    child_env = dict(os.environ if env is None else env)
    child_env["ALPHA_PROCESS_LAUNCH_ID"] = launch_id
    stdout_path, stderr_path = artifacts / "stdout.txt", artifacts / "stderr.txt"
    with stdout_path.open("w") as stdout_file, stderr_path.open("w") as stderr_file, ExitStack() as inputs:
        stdin_file = inputs.enter_context(stdin_path.open("rb")) if stdin_path is not None else None
        write_json(artifacts / "process.json", {"command": command, "pid": None, "status": "launching",
                                               "launch_id": launch_id, "started_at": time.time(), "timeout_seconds": timeout})
        process = subprocess.Popen(
            command,
            cwd=workspace,
            env=child_env,
            stdin=stdin_file,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=os.name == "posix",
        )
        birth = process_birth(process.pid)
        write_json(
            artifacts / "process.json",
            {
                "command": command,
                "pid": process.pid,
                "process_birth": birth,
                "launch_id": launch_id,
                "status": "running",
                "started_at": time.time(),
                "timeout_seconds": timeout,
            },
        )
        logger.info("Process started pid=%s artifacts=%s", process.pid, artifacts)
        timed_out = False
        timeout_phase, phase_error = None, None
        interrupted = False
        try:
            if phase_limits is None:
                process.wait(timeout=timeout)
            else:
                timed_out, timeout_phase, phase_error = _wait_phased(process, started, timeout, phase_path, phase_limits)
                if timed_out:
                    _kill(process)
                    process.wait()
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill(process)
            process.wait()
        except BaseException:
            interrupted = True
            _kill(process)
            process.wait()
            raise
        finally:
            write_json(
                artifacts / "process.json",
                {
                    "command": command,
                    "pid": process.pid,
                    "process_birth": birth,
                    "launch_id": launch_id,
                    "status": "interrupted" if interrupted else "finished",
                    "returncode": process.returncode,
                    "timed_out": timed_out,
                    "timeout_phase": timeout_phase,
                    "phase_error": phase_error,
                    "elapsed_seconds": time.monotonic() - started,
                },
            )
    stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
    report = {
        "command": command,
        "pid": process.pid,
        "process_birth": birth,
        "launch_id": launch_id,
        "status": "finished",
        "returncode": process.returncode,
        "timed_out": timed_out,
        "timeout_phase": timeout_phase,
        "phase_error": phase_error,
        "elapsed_seconds": time.monotonic() - started,
    }
    write_json(artifacts / "process.json", report)
    logger.info(
        "Process finished pid=%s returncode=%s timed_out=%s elapsed=%.1fs",
        process.pid,
        process.returncode,
        timed_out,
        report["elapsed_seconds"],
    )
    return report, stdout
