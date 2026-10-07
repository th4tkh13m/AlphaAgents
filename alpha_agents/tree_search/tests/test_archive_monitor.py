"""The observer must report the same winner eligibility as the controller."""

import os
from pathlib import Path

import pytest

from alpha_agents.tree_search.core.storage import read_json, write_json
from eval import monitor_archive_transfer as monitor


@pytest.mark.skipif(
    not Path("/proc/self/stat").exists(), reason="Linux process monitor"
)
@pytest.mark.parametrize(
    "scores,expected", [([0.5, 0.8], "child_000001"), ([0.5, 0.5], "initial")]
)
def test_monitor_includes_diagnostic_candidates_and_preserves_archive_ties(
    tmp_path, monkeypatch, scores, expected
):
    stat = Path(f"/proc/{os.getpid()}/stat").read_text().split(") ", 1)[1].split()
    write_json(tmp_path / "launch.json", {"pid": os.getpid(), "start_ticks": stat[19]})
    write_json(
        tmp_path / "attempt_001/state.json",
        {
            "archive": ["initial", "child_000001"],
            "records": {
                "initial": {
                    "status": "completed",
                    "evaluation": {"status": "completed", "score": scores[0]},
                },
                "child_000001": {
                    "status": "completed",
                    "evaluation": {
                        "status": "completed",
                        "score": scores[1],
                        "diagnostic": True,
                    },
                },
            },
        },
    )

    def stop_after_observation(seconds):
        raise RuntimeError("stop-test-observer")

    monkeypatch.setattr(monitor.time, "sleep", stop_after_observation)
    with pytest.raises(RuntimeError, match="stop-test-observer"):
        monitor.main(tmp_path)
    report = read_json(tmp_path / "monitor_status.json")
    assert report["best"][1] == expected
    assert report["controller_alive"]
