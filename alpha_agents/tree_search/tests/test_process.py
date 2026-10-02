import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from alpha_agents.tree_search.infrastructure.process import execute


def test_output_and_pid_are_visible_before_exit(tmp_path):
    artifacts = tmp_path / "logs"
    result = []
    worker = threading.Thread(
        target=lambda: result.append(
            execute(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    "import time; print('started'); time.sleep(0.8); print('done')",
                ],
                tmp_path,
                artifacts,
                5,
            )
        )
    )
    worker.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if (artifacts / "process.json").exists() and (
            artifacts / "stdout.txt"
        ).read_text():
            break
        time.sleep(0.01)
    running = json.loads((artifacts / "process.json").read_text())
    assert running["status"] == "running"
    assert running["pid"] > 0
    assert "started" in (artifacts / "stdout.txt").read_text()
    worker.join(5)
    assert not worker.is_alive()
    report, stdout = result[0]
    assert report["returncode"] == 0
    assert stdout == "started\ndone\n"
    assert json.loads((artifacts / "process.json").read_text())["status"] == "finished"


def test_timeout_retains_partial_output(tmp_path):
    report, stdout = execute(
        [sys.executable, "-u", "-c", "import time; print('partial'); time.sleep(60)"],
        tmp_path,
        tmp_path / "logs",
        0.3,
    )
    assert report["timed_out"]
    assert report["returncode"] != 0
    assert stdout == "partial\n"


def test_cli_logging_allows_a_fresh_search(tmp_path):
    config = Path(__file__).parents[1] / "examples" / "command.json"
    output = tmp_path / "search"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alpha_agents.tree_search",
            "--config",
            str(config),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    state = json.loads((output / "state.json").read_text())
    assert state["completed_children"] == 1
    assert state["records"]["child_000001"]["evaluation"]["score"] == 1.0
    logs = (output / "search.log").read_text()
    assert "phase=mutation" in logs
    assert "phase=evaluation" in logs
    assert "Process started pid=" in logs
