"""Transfer Artemis answer output to AndroidWorld's public action interface."""

from pydantic import BaseModel, Field

if __package__:
    from .task_outcomes import MissingAgentAnswerError
else:
    from task_outcomes import MissingAgentAnswerError


class AgentAnswer(BaseModel):
    answer: str = Field(
        description=(
            "The answer requested by the task, using the task's requested format. "
            "Return only the answer, without explanation or Markdown. "
            "Separate multiple requested values with commas."
        )
    )


def submit_answer(env, output) -> str:
    """Submit the agent's output unchanged; never infer or repair its answer."""
    from android_world.env import json_action

    answer = getattr(output, "answer", None)
    if not isinstance(answer, str) or not answer.strip():
        raise MissingAgentAnswerError(
            "Artemis did not return a non-empty information-retrieval answer"
        )
    env.execute_action(
        json_action.JSONAction(action_type=json_action.ANSWER, text=answer)
    )
    if env.interaction_cache != answer:
        raise RuntimeError("AndroidWorld did not retain the submitted agent answer")
    return answer
