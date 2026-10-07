from eval import report_archive_transfer as report


def test_report_outcomes_distinguish_partial_invalid_and_pass():
    base = {
        "success": 1.0,
        "androidworld_reward": 1.0,
        "artemis_status": "completed",
        "exception": None,
    }
    assert report.outcome(base) == "pass"
    assert (
        report.outcome({**base, "success": 0.5, "androidworld_reward": 0.5})
        == "partial"
    )
    assert report.outcome({**base, "success": 0, "androidworld_reward": 0}) == "zero"
    assert (
        report.outcome({**base, "success": None, "exception": "LLMPermanentError"})
        == "invalid"
    )
    assert report.outcome({**base, "success": 1, "androidworld_reward": 0}) == "invalid"


def test_report_does_not_rank_failed_evaluation_or_expose_confirmation(tmp_path):
    import json

    run = tmp_path / "attempt_001"
    run.mkdir()
    (run / "state.json").write_text(
        json.dumps(
            {
                "records": {
                    "initial": {
                        "status": "evaluation_failed",
                        "parent_id": None,
                        "evaluation": {"status": "failed", "score": 0.6},
                    }
                },
                "pending": [{"id": "child_000001", "parent_id": "initial"}],
            }
        )
    )
    metadata = tmp_path / "metadata.json"
    metadata.write_text(
        json.dumps(
            {
                "rows": [
                    {"stage": "Selection", "task": "A", "difficulty": "easy"},
                    {"stage": "Confirmation", "task": "SECRET", "difficulty": "hard"},
                ]
            }
        )
    )
    data = report.collect(tmp_path, metadata)
    assert data["nodes"][0]["score"] is None
    assert data["nodes"][1]["status"] == "running"
    assert [t["task"] for t in data["tasks"]] == ["A"]
    report.reports(tmp_path, data)
    assert (
        "SECRET"
        not in (tmp_path / "reports/node_task_difficulty_analysis.json").read_text()
    )
    assert (
        "Pending or not yet evaluated: A"
        in (tmp_path / "reports/node_task_difficulty_analysis.md").read_text()
    )
