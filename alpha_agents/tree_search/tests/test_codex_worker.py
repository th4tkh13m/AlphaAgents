import json
import sys
import types

import pytest
from alpha_agents.tree_search.mutators.coding_agents.codex.worker import (
    GOAL_CONTEXT_FILENAME,
    build_goal_objective,
    build_instruction,
    run_codex,
    write_goal_context,
)


def test_instruction_requires_evidence_grounded_mobile_edits():
    instruction = build_instruction(
        "Improve retries.", "Run pytest.", '{"selected_task": "Clock"}'
    )

    assert "Improve retries." in instruction
    assert "Run pytest." in instruction
    assert "# Role and objective" in instruction
    assert "assigned evaluation environment" in instruction
    assert "The benchmark definition is immutable" in instruction
    assert "autonomous reliability engineer" in instruction
    assert "# How to work" in instruction
    assert "runtime resource" in instruction
    assert "emulator" not in instruction
    assert '"selected_task": "Clock"' in instruction
    assert "do not merely describe a proposed solution" in instruction
    assert "Do not edit benchmark task definitions" in instruction
    assert (
        "An evaluator error, timeout, crash, or transport failure is investigation evidence"
        in instruction
    )
    assert "agent roles, prompts, tools, and evaluator plumbing" in instruction
    assert "# Permitted modifications" in instruction
    assert "Agent logic and prompts" in instruction


def test_goal_context_keeps_full_evidence_out_of_the_persisted_goal(tmp_path):
    git_dir = tmp_path / "gitdir"
    (git_dir / "info").mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n")

    context = write_goal_context(str(worktree), "x" * 10_000)
    objective = build_goal_objective(context)

    assert len(objective) < 4_000
    assert context.name == GOAL_CONTEXT_FILENAME
    assert context.read_text().endswith("x" * 10_000)
    assert GOAL_CONTEXT_FILENAME in (git_dir / "info" / "exclude").read_text()


def test_run_codex_uses_authorized_full_access_and_records_response(monkeypatch, tmp_path):
    calls = {}

    class FakeResult:
        final_response = "Implemented the retry policy."

    class FakeThread:
        id = "thr_test"

        def run(self, instruction, **kwargs):
            calls["run"] = (instruction, kwargs)
            return FakeResult()

    class FakeCodex:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def thread_start(self, **kwargs):
            calls["thread_start"] = kwargs
            return FakeThread()

    fake_sdk = types.SimpleNamespace(
        Codex=FakeCodex,
        Sandbox=types.SimpleNamespace(full_access="full_access"),
        ApprovalMode=types.SimpleNamespace(deny_all="deny_all"),
    )
    monkeypatch.setitem(sys.modules, "openai_codex", fake_sdk)
    monkeypatch.setenv("DGM_CODEX_EFFORT", "high")
    history = tmp_path / "history.md"
    telemetry = tmp_path / "codex_execution.json"

    response = run_codex(
        problem_statement="Improve retries.",
        git_dir=str(tmp_path),
        chat_history_file=str(history),
        test_description="Run pytest.",
        evidence_bundle=None,
        model="test-codex",
        telemetry_file=str(telemetry),
    )

    assert response == "Implemented the retry policy."
    assert calls["thread_start"] == {
        "cwd": str(tmp_path),
        "model": "test-codex",
        "config": {"model_reasoning_effort": "high"},
        "sandbox": "full_access",
        "approval_mode": "deny_all",
        "ephemeral": False,
    }
    assert calls["run"][1] == {"cwd": str(tmp_path), "effort": "high"}
    assert "thr_test" in history.read_text()
    assert "Implemented the retry policy." in history.read_text()
    assert json.loads(telemetry.read_text())["thread_id"] == "thr_test"
    assert json.loads(telemetry.read_text())["effort"] == "high"
    assert json.loads(telemetry.read_text())["approval_policy"] == "never"
    assert json.loads(telemetry.read_text())["sandbox"] == "full_access"
    assert json.loads(telemetry.read_text())["execution_mode"] == "turn_compatibility"
    assert (tmp_path / GOAL_CONTEXT_FILENAME).exists()


def test_run_codex_explains_how_to_install_the_cli_authenticated_sdk(
    monkeypatch, tmp_path
):
    # ``delitem`` merely causes Python to import the real installed SDK again.
    # A ``None`` sentinel faithfully simulates an unavailable optional module.
    monkeypatch.setitem(sys.modules, "openai_codex", None)

    with pytest.raises(RuntimeError, match="codex login"):
        run_codex(
            problem_statement="x",
            git_dir="/dgm",
            chat_history_file=str(tmp_path / "history.md"),
            test_description=None,
            evidence_bundle=None,
            model="test-codex",
        )
