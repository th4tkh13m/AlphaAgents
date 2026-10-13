"""Observed role telemetry without treating missing usage as zero tokens."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def goal_usage(directory: Path) -> dict | None:
    """Preserve the durable Goal counter separately from adapter turn usage.

    This is an SDK-reported counter, not a billing estimate or a sum of turns.
    A checkpoint may precede interruption; its observation time remains explicit.
    Require the recorded invocation identity rather than accepting an unrelated
    Goal file left in the directory.
    """
    source = directory / "goal_state.json"
    try:
        goal = json.loads(source.read_text())
        session = json.loads((directory / "native_session.json").read_text())
        if not isinstance(goal, dict) or not isinstance(session, dict):
            return None
        thread_id = goal.get("threadId", goal.get("thread_id"))
        objective = goal.get("objective")
        if (session.get("execution_mode") != "goal"
                or not isinstance(thread_id, str) or not thread_id
                or thread_id != session.get("thread_id")
                or not isinstance(objective, str) or not objective
                or objective != session.get("goal_objective")):
            return None
        tokens = goal.get("tokensUsed", goal.get("tokens_used"))
        if type(tokens) is not int or tokens < 0:
            return None
        elapsed = goal.get("timeUsedSeconds", goal.get("time_used_seconds"))
        updated = goal.get("updatedAt", goal.get("updated_at"))
        return {"source": str(source), "scope": "persisted_goal_reported_counter",
                "thread_id": thread_id,
                "objective_hash": hashlib.sha256(objective.encode()).hexdigest(),
                "status": goal.get("status"), "tokens_used": tokens,
                "time_used_seconds": elapsed if type(elapsed) is int and elapsed >= 0 else None,
                "updated_at": updated if type(updated) is int and updated >= 0 else None,
                "billing_verified": False}
    except (OSError, ValueError, TypeError):
        return None


def role_usage(directory: Path) -> dict:
    durable_goal = goal_usage(directory)
    native = directory / "native_result.json"
    if native.exists():
        try:
            payload = json.loads(native.read_text())
            return {"source": str(native), "scope": "adapter_reported_usage",
                    "execution_mode": payload.get("execution_mode"), "usage": payload.get("usage"),
                    "goal_state_source": str(directory / "goal_state.json") if (directory / "goal_state.json").exists() else None,
                    "goal_usage": durable_goal}
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    events = directory / "process" / "stdout.txt"
    usages = []
    try:
        with events.open() as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
                    usages.append(event["usage"])
    except OSError:
        pass
    return {"source": str(events) if usages else None,
            "scope": "reported_turn_completions" if usages else "unavailable", "usage": usages or None,
            "goal_usage": durable_goal}
