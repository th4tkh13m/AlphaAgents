import json
import time
from datetime import datetime
from types import SimpleNamespace

from alpha_agents.tree_search.mutators.coding_agents.codex.rollout_watch import (
    LAUNCH_FAILURE,
    LaunchFailureDetector,
    LiveRolloutWatcher,
)
from alpha_agents.tree_search.mutators.coding_agents.codex.worker import (
    _resume_goal_operation,
)


def failure(call_id, timestamp):
    return {
        "timestamp": timestamp,
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call_output",
            "call_id": call_id,
            "output": [
                {"type": "input_text", "text": f"exec_command failed: {LAUNCH_FAILURE}"}
            ],
        },
    }


def launch(started_at_ms):
    return {
        "timestamp": "2026-09-21T16:35:40Z",
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "started_at_ms": started_at_ms,
            "item": {"type": "CommandExecution", "process_id": 123},
        },
    }


def test_detector_requires_three_distinct_failures_without_a_fresh_launch():
    detector = LaunchFailureDetector()
    t = "2026-09-21T16:35:30Z"
    assert detector.feed(failure("a", t), 1) is None
    assert detector.feed(failure("a", t), 2) is None
    # A command that started before the first failure proves nothing.
    assert detector.feed(launch(1790000000000), 3) is None
    assert detector.feed(failure("b", "2026-09-21T16:35:35Z"), 4) is None
    incident = detector.feed(failure("c", "2026-09-21T16:35:38Z"), 5)
    assert incident["call_ids"] == ["a", "b", "c"]


def test_fresh_command_launch_resets_streak():
    detector = LaunchFailureDetector()
    detector.feed(failure("a", "2026-09-21T16:35:30Z"), 1)
    detector.feed(
        launch(
            int(datetime.fromisoformat("2026-09-21T16:35:31+00:00").timestamp() * 1000)
        ),
        2,
    )
    assert not detector.failures
    assert detector.feed(failure("b", "2026-09-21T16:35:32Z"), 3) is None


def test_watcher_tails_only_complete_lines_and_skips_prior_attempt(tmp_path):
    path = tmp_path / "rollout.jsonl"
    previous = "".join(
        json.dumps(failure(f"prév{i}", "2026-09-21T16:35:30Z"), ensure_ascii=False)
        + "\n"
        for i in range(3)
    )
    path.write_text(previous)
    incidents = []
    watcher = LiveRolloutWatcher(
        path, incidents.append, poll_seconds=0.01, start_offset=len(previous.encode())
    )
    watcher.start()
    try:
        with path.open("a") as output:
            for i in range(2):
                output.write(
                    json.dumps(failure(f"new{i}", "2026-09-21T16:35:31Z")) + "\n"
                )
            output.flush()
            partial = json.dumps(failure("new2", "2026-09-21T16:35:32Z"))
            output.write(partial[:20])
            output.flush()
            time.sleep(0.05)
            assert incidents == []
            output.write(partial[20:] + "\n")
            output.flush()
        deadline = time.monotonic() + 2
        while not incidents and time.monotonic() < deadline:
            time.sleep(0.01)
        assert incidents[0]["call_ids"] == ["new0", "new1", "new2"]
        assert incidents[0]["line"] == 6
    finally:
        watcher.stop()


def test_resume_reactivates_stored_goal_without_replacing_objective():
    calls = []

    class Client:
        def reserve_goal_operation(self, thread_id):
            calls.append(("reserve", thread_id))
            return SimpleNamespace(
                activate_turn_routing=lambda: calls.append(("route",)),
                wait_for_start=lambda timeout: "next-turn",
            )

        def thread_goal_set(self, thread_id, **kwargs):
            calls.append(("set", thread_id, kwargs))

    state, turn_id = _resume_goal_operation(Client(), "same-thread")
    assert turn_id == "next-turn"
    assert calls[0] == ("reserve", "same-thread")
    assert calls[2][0:2] == ("set", "same-thread")
    assert list(calls[2][2]) == ["status"]
    assert calls[2][2]["status"].value == "active"
