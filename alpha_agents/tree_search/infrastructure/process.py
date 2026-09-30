"""Subprocess evidence and bounded process-tree cleanup."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

from ..core.storage import write_json


def execute(
    command: list[str],
    workspace: Path,
    artifacts: Path,
    timeout: float | None = None,
    env=None,
):
    artifacts.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=workspace,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name == "posix",
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
            process.kill()
        stdout, stderr = process.communicate()
    (artifacts / "stdout.txt").write_text(stdout, encoding="utf-8")
    (artifacts / "stderr.txt").write_text(stderr, encoding="utf-8")
    report = {
        "command": command,
        "returncode": process.returncode,
        "timed_out": timed_out,
        "elapsed_seconds": time.monotonic() - started,
    }
    write_json(artifacts / "process.json", report)
    return report, stdout
