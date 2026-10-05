"""Identify agent failures without treating evaluator failures as scores."""


class MissingAgentAnswerError(ValueError):
    """The agent supplied no usable answer for an information-retrieval task."""


def bounded_agent_failure(exc, phase):
    """Score known agent failures while rejecting unknown integration errors."""
    if phase == "answer_submission" and isinstance(exc, MissingAgentAnswerError):
        kind = "missing_answer"
    elif phase != "agent_execution":
        return {}
    elif isinstance(exc, TimeoutError):
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
        "missing_answer": "MissingAgentAnswerError:",
    }.get(kind)
    return bool(
        expected
        and error.startswith(expected)
        and episode.get("outcome_status") == "agent_failed"
        and episode.get("failure_phase") == (
            "answer_submission" if kind == "missing_answer" else "agent_execution"
        )
        and episode.get("artemis_status") == "failed"
        and episode.get("artemis_error") == error
        and episode.get("androidworld_reward") is None
        and type(episode.get("success")) in (int, float)
        and episode["success"] == 0
    )
