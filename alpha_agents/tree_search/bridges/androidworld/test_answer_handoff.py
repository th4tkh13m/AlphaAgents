"""The bridge submits actual agent output through AndroidWorld's answer action."""

from pathlib import Path
from types import SimpleNamespace
import os
import subprocess
import sys

import pytest

from .answer_handoff import AgentAnswer, submit_answer
from .task_outcomes import MissingAgentAnswerError, bounded_agent_failure


def test_runner_loads_answer_helper_outside_repository(tmp_path):
    runner = Path(__file__).with_name("artemis_runner.py").resolve()
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["DGM_ARTEMIS_WORKTREE"] = str(tmp_path / "candidate")
    env["DGM_ANDROIDWORLD_ROOT"] = str(tmp_path / "benchmark")
    result = subprocess.run(
        [sys.executable, str(runner), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "--tasks" in result.stdout


@pytest.fixture(autouse=True)
def benchmark_imports(monkeypatch):
    root = Path(__file__).resolve().parents[4] / "eval" / "android_world"
    if not (root / "android_world" / "env" / "json_action.py").is_file():
        pytest.skip("AndroidWorld checkout is required for its public action type")
    monkeypatch.syspath_prepend(str(root))


class Environment:
    def __init__(self):
        self.interaction_cache = ""
        self.actions = []

    def execute_action(self, action):
        from android_world.env.json_action import JSONAction

        assert isinstance(action, JSONAction)
        assert action.action_type == "answer"
        self.actions.append(action)
        self.interaction_cache = action.text


@pytest.mark.parametrize("answer", ["0", "7", "7, 9", " 2023-10-15 "])
def test_submits_agent_answer_unchanged(answer):
    env = Environment()
    output = AgentAnswer(answer=answer)
    assert submit_answer(env, output) == answer
    assert env.interaction_cache == answer
    assert len(env.actions) == 1


@pytest.mark.parametrize(
    "output", [None, {}, SimpleNamespace(answer=7), AgentAnswer(answer=""), AgentAnswer(answer="  ")]
)
def test_missing_output_is_scored_agent_failure_before_grading(output):
    env = Environment()
    with pytest.raises(MissingAgentAnswerError, match="non-empty") as raised:
        submit_answer(env, output)
    assert not env.actions
    assert bounded_agent_failure(raised.value, "answer_submission")["failure_kind"] == "missing_answer"


def test_rejected_answer_is_runtime_failure_before_grading():
    env = Environment()
    env.execute_action = lambda action: None
    with pytest.raises(RuntimeError, match="did not retain"):
        submit_answer(env, AgentAnswer(answer="7"))
