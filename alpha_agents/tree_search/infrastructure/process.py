"""Subprocess evidence and bounded process-tree cleanup."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from pathlib import Path

from ..core.storage import write_json

logger = logging.getLogger(__name__)


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
):
    artifacts.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    stdout_path, stderr_path = artifacts / "stdout.txt", artifacts / "stderr.txt"
    with stdout_path.open("w") as stdout_file, stderr_path.open("w") as stderr_file:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            env=env,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=os.name == "posix",
        )
        write_json(
            artifacts / "process.json",
            {
                "command": command,
                "pid": process.pid,
                "status": "running",
                "started_at": time.time(),
                "timeout_seconds": timeout,
            },
        )
        logger.info("Process started pid=%s artifacts=%s", process.pid, artifacts)
        timed_out = False
        interrupted = False
        try:
            process.wait(timeout=timeout)
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
                    "status": "interrupted" if interrupted else "finished",
                    "returncode": process.returncode,
                    "timed_out": timed_out,
                    "elapsed_seconds": time.monotonic() - started,
                },
            )
    stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
    report = {
        "command": command,
        "pid": process.pid,
        "status": "finished",
        "returncode": process.returncode,
        "timed_out": timed_out,
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
