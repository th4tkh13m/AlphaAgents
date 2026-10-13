"""Shared Codex SDK goal lifecycle and launcher recovery primitives.

DGM and MCTS supply their own objective, permissions, output contracts, and
outer supervision. This module owns goal collection, durable-goal resume,
rollout launch monitoring, idle checks, and launcher probes.
"""
import time
from pathlib import Path

if __package__:
    from .rollout_watch import LiveRolloutWatcher
else:
    from rollout_watch import LiveRolloutWatcher

def _goal_snapshot(client, thread_id: str):
    from openai_codex.generated.v2_all import ThreadGoalGetResponse

    return client.request(
        "thread/goal/get", {"threadId": thread_id}, response_model=ThreadGoalGetResponse
    ).goal


def _collect_goal(client, state, turn_id: str):
    from openai_codex._goal import _GoalNotificationStream
    from openai_codex._run import _collect_turn_result

    stream = _GoalNotificationStream(
        state=state,
        next_notification=lambda: client.next_goal_notification(state),
        unregister=lambda: client.unregister_goal_operation(state),
        cancel_goal=lambda: client.cancel_goal_operation(state),
    )
    return _collect_turn_result(stream, turn_id=turn_id)


def _resume_goal_operation(client, thread_id: str):
    """Resume the stored goal without clearing its objective or usage."""
    from openai_codex.generated.v2_all import ThreadGoalStatus

    state = client.reserve_goal_operation(thread_id)
    activated = False
    try:
        state.activate_turn_routing()
        client.thread_goal_set(thread_id, status=ThreadGoalStatus.active)
        activated = True
        turn_id = state.wait_for_start(30)
        if turn_id is None:
            raise RuntimeError("stored Codex goal did not start after resume")
        return state, turn_id
    except BaseException:
        if activated:
            client.cancel_goal_operation(state)
        state.finish()
        client.unregister_goal_operation(state)
        raise


def run_watched_goal(thread, instruction: str | None, *, resume: bool = False):
    """Run one goal attempt and request a pause on a persistent launch failure."""
    client = thread._client
    if resume:
        record = client.thread_read(thread.id).thread
        if not record.path:
            raise RuntimeError(
                "persisted Codex thread has no rollout path before resume"
            )
        rollout_path = Path(record.path)
        start_offset = rollout_path.stat().st_size
        state, turn_id = _resume_goal_operation(client, thread.id)
    else:
        state, turn_id = client.start_goal_operation(thread.id, instruction)
        record = client.thread_read(thread.id).thread
        if not record.path:
            client.cancel_goal_operation(state)
            raise RuntimeError(
                "persisted Codex thread has no rollout path after goal start"
            )
        rollout_path = Path(record.path)
        start_offset = 0

    def on_incident(_incident):
        if not state.is_finished():
            client.cancel_goal_operation(state)

    watcher = LiveRolloutWatcher(rollout_path, on_incident, start_offset=start_offset)
    watcher.start()
    try:
        result = _collect_goal(client, state, turn_id)
    finally:
        watcher.stop()
    if watcher.error:
        raise RuntimeError(f"Codex rollout watcher failed: {watcher.error}")
    goal = _goal_snapshot(client, thread.id)
    return result, watcher.incident, goal


def _require_idle_thread(client, thread_id: str, timeout_seconds: float = 10) -> None:
    from openai_codex.generated.v2_all import IdleThreadStatus

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if isinstance(
            client.thread_read(thread_id).thread.status.root, IdleThreadStatus
        ):
            return
        time.sleep(0.2)
    raise RuntimeError("Codex turn remained active after goal pause; restart aborted")


def _preflight_launcher(client) -> None:
    """Check the new app-server's command launcher before reactivating a goal."""
    from openai_codex.generated.v2_all import CommandExecResponse

    result = client.request(
        "command/exec",
        {"command": ["pwd"], "cwd": "/tmp", "timeoutMs": 5000},
        response_model=CommandExecResponse,
    )
    if result.exit_code != 0 or result.stdout.strip() != "/tmp":
        raise RuntimeError(
            f"Codex launcher preflight failed: {result.model_dump(mode='json')}"
        )
