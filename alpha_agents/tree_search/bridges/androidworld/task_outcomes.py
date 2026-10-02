"""Identify bounded agent failures without treating evaluator failures as scores."""


def bounded_agent_failure(exc, phase):
    """Only an agent execution timeout or graph limit is a scored failure."""
    if phase != "agent_execution":
        return {}
    if isinstance(exc, TimeoutError):
        kind = "llm_timeout" if "LLM call timed out" in str(exc) else "task_timeout"
    elif any(
        cls.__name__ == "GraphRecursionError" and cls.__module__ == "langgraph.errors"
        for cls in type(exc).__mro__
    ):
        kind = "execution_limit"
    else:
        return {}
    return {
        "outcome_status": "agent_failed",
        "failure_phase": phase,
        "failure_kind": kind,
        "artemis_status": "failed",
        "artemis_error": f"{type(exc).__name__}: {exc}",
        # The grader did not run. Do not invent an AndroidWorld reward.
        "androidworld_reward": None,
    }


def is_bounded_agent_failure(episode):
    """Accept only the explicit trusted-runner failure record with score zero."""
    kind = episode.get("failure_kind")
    error = episode.get("exception")
    if not isinstance(error, str):
        return False
    expected = {
        "llm_timeout": "TimeoutError: LLM call timed out",
        "task_timeout": "TimeoutError:",
        "execution_limit": "GraphRecursionError:",
    }.get(kind)
    return bool(
        expected
        and error.startswith(expected)
        and episode.get("outcome_status") == "agent_failed"
        and episode.get("failure_phase") == "agent_execution"
        and episode.get("artemis_status") == "failed"
        and episode.get("artemis_error") == error
        and episode.get("androidworld_reward") is None
        and type(episode.get("success")) in (int, float)
        and episode["success"] == 0
    )
