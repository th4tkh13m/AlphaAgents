"""Identify agent failures without treating evaluator failures as scores."""

from pathlib import Path


def candidate_exception_origin(exc, agent_root):
    """Attribute an exception to a candidate raise site, not its caller frames.

    Transport and OS failures remain infrastructure errors even when raised
    through candidate code. The trusted runner supplies the immutable root.
    """
    if agent_root is None:
        return None
    current, seen = exc, set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        module = type(current).__module__
        if isinstance(current, OSError) or module.split('.')[0] in {
            'httpx', 'httpcore', 'aiohttp', 'grpc', 'requests', 'openai',
        }:
            return None
        current = current.__cause__ or current.__context__
    trace = exc.__traceback__
    if trace is None:
        return None
    while trace.tb_next is not None:
        trace = trace.tb_next
    source = Path(trace.tb_frame.f_code.co_filename).resolve()
    root = (Path(agent_root) / 'artemis').resolve()
    if not source.is_relative_to(root):
        return None
    return {"source": source.relative_to(root).as_posix(),
            "exception_type": type(exc).__name__}


class MissingAgentAnswerError(ValueError):
    """The agent supplied no usable answer for an information-retrieval task."""


def bounded_agent_failure(exc, phase, *, agent_root=None):
    """Classify bounded failures and candidate-local raise sites; preserve outages."""
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
        origin = candidate_exception_origin(exc, agent_root)
        if origin is None:
            return {}
        kind = "candidate_exception"
    return {
        "outcome_status": "agent_failed",
        "failure_phase": phase,
        "failure_kind": kind,
        "artemis_status": "failed",
        "artemis_error": f"{type(exc).__name__}: {exc}",
        # The grader did not run. Do not invent an AndroidWorld reward.
        "androidworld_reward": None,
        **({"failure_origin": origin} if kind == "candidate_exception" else {}),
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
    if kind == "candidate_exception":
        origin = episode.get("failure_origin", {})
        if not isinstance(origin, dict):
            return False
        source, name = origin.get("source"), origin.get("exception_type")
        if (not isinstance(source, str) or not source or Path(source).is_absolute()
                or '..' in Path(source).parts or not isinstance(name, str)
                or not name.isidentifier()):
            return False
        expected = name + ":"
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
