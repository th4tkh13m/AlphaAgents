"""Persisted proposer Goals, document handoff, and bounded same-goal repair."""
import json
import sys
from types import ModuleType
from types import SimpleNamespace as NS

import pytest

from alpha_agents.tree_search.mutators.coding_agents.codex.roles import adapter, prompts
from alpha_agents.tree_search.tests.test_design_handoff import (
    document,
    request,
    summary,
)


def goal_result(text, turn="turn-1"):
    return NS(id=turn, status="completed", final_response=text, items=[], usage={"total_tokens": 100})


def test_proposer_uses_readonly_goal_with_role_specific_objective(tmp_path):
    req = request(tmp_path)
    assert req.execution_mode == "goal"
    assert req.collaboration_mode == "default"
    assert req.permission_mode == "read-only"
    objective = adapter.goal_objective(req, tmp_path / "goal_context.md")
    assert len(objective) <= 4000
    assert objective == objective.strip()
    assert req.artifact_id in objective and req.request_id in objective
    assert "on every continuation or resumption" in objective
    assert "seven-section" in objective
    assert "read-only" in objective
    assert "plan_id" not in req.context and "attempt_id" not in req.context
    assert "final Markdown response" in req.render()
    with pytest.raises(ValueError, match="Plan-mode"):
        adapter.plan_payload(req, tmp_path)


def test_proposer_goal_objective_contributes_to_prompt_identity(tmp_path, monkeypatch):
    req = request(tmp_path)
    before = req.library_hash
    monkeypatch.setattr(prompts, "PROPOSER_GOAL_OBJECTIVE_TEMPLATE",
                        prompts.PROPOSER_GOAL_OBJECTIVE_TEMPLATE + " Updated requirement.")
    assert req.library_hash != before


@pytest.mark.parametrize("status", ["blocked", "paused", "budget_limited", "usage_limited"])
def test_incomplete_proposer_goal_cannot_admit_a_document(tmp_path, status):
    req = request(tmp_path)
    goal = NS(status=status, objective="objective", thread_id="same-thread")
    with pytest.raises(RuntimeError, match="must complete"):
        adapter._collect_proposal_goal_handoff(req, tmp_path, tmp_path, adapter.InvocationDeadline(7200),
            goal_result(document(summary())), goal, "objective", "same-thread", lambda _: pytest.fail("No repair"))


def test_proposer_repair_is_bounded_and_retains_rejected_documents(tmp_path):
    req = request(tmp_path)
    goal = NS(status="complete", objective="objective", thread_id="same-thread")
    repairs = []
    def resume(instruction):
        repairs.append(instruction)
        return goal_result("Still only an outline", f"turn-{len(repairs)+1}"), goal
    with pytest.raises(ValueError, match="seven required sections"):
        adapter._collect_proposal_goal_handoff(req, tmp_path, tmp_path, adapter.InvocationDeadline(7200),
            goal_result("Only an outline"), goal, "objective", "same-thread", resume)
    assert len(repairs) == 2
    assert all((tmp_path / f"proposed_design_attempt_{i}.md").is_file() for i in range(3))
    assert all((tmp_path / f"plan_validation_failure_{i}.json").is_file() for i in range(3))
    assert not (tmp_path / "result.json").exists()


def test_proposer_repair_preserves_original_deadline(tmp_path):
    clock = [0.0]
    deadline = adapter.InvocationDeadline(10, lambda: clock[0])
    goal = NS(status="complete", objective="objective", thread_id="same-thread")
    def resume(instruction):
        clock[0] = 11
        return goal_result(document(summary())), goal
    with pytest.raises(TimeoutError, match="Original Codex"):
        adapter._collect_proposal_goal_handoff(request(tmp_path), tmp_path, tmp_path, deadline,
            goal_result("Only an outline"), goal, "objective", "same-thread", resume)
    assert deadline.ends == 10


def test_full_goal_collection_repairs_in_same_thread_without_starting_new_objective(tmp_path, monkeypatch):
    from alpha_agents.tree_search.mutators.coding_agents.codex import execution

    # Exercise the whole adapter without a model call or installed SDK. Only the
    # Thread wrapper is substituted; the actual role collector and validator run.
    api = ModuleType("openai_codex.api")
    api.Thread = lambda client, thread_id: NS(_client=client, id=thread_id)
    monkeypatch.setitem(sys.modules, "openai_codex.api", api)
    req = request(tmp_path, existing_plans=[summary(1)])
    (tmp_path / "controller.py").write_text("value = 0\n")
    context_path = tmp_path / "goal_context.md"
    objective = adapter.goal_objective(req, context_path)
    goal = NS(status="complete", objective=objective, thread_id="same-thread", tokens_used=100)
    clock = [0.0]
    deadline = adapter.InvocationDeadline(7200, lambda: clock[0])
    calls, starts = [], []

    class Client:
        def thread_start(self, payload):
            starts.append(payload)
            return NS(thread=NS(id="same-thread"))
        def thread_read(self, thread_id, include_turns=False):
            assert thread_id == "same-thread" and include_turns
            return NS(thread=NS(turns=[NS(id=f"turn-{i+1}", status="completed") for i in range(len(calls))]))

    def watched(thread, instruction, **kwargs):
        assert thread.id == "same-thread"
        calls.append((instruction, kwargs))
        clock[0] += 1
        if len(calls) == 1:
            assert instruction == objective and not kwargs
            return goal_result("Only an outline"), None, goal
        assert instruction is None and kwargs == {"resume": True}
        feedback = context_path.read_text()
        assert "validation_error" in feedback
        assert "seven required sections" in feedback
        assert req.render() in feedback
        assert goal.objective == objective and goal.tokens_used == 100
        goal.tokens_used = 200
        return goal_result(document(summary(2)), "turn-2"), None, goal

    monkeypatch.setattr(execution, "run_watched_goal", watched)
    monkeypatch.setattr(execution, "_goal_snapshot", lambda client, thread_id: goal)
    monkeypatch.setattr(execution, "_require_idle_thread", lambda *args: None)
    result = adapter._collect_goal(Client(), req, tmp_path, tmp_path, deadline)
    assert len(starts) == 1 and len(calls) == 2
    assert starts[0]["developerInstructions"] == req.render()
    assert "collaborationMode" not in starts[0]
    assert result["plans"][0]["document"] == document(summary(2))
    assert result["status"] == "completed"
    assert deadline.ends == 7200 and deadline.remaining() == 7198
    assert goal.tokens_used == 200
    native = json.loads((tmp_path / "native_result.json").read_text())
    assert native["execution_mode"] == "goal" and native["turn_id"] == "turn-2"
    assert len(native["physical_turns"]) == 2


def test_goal_objective_canonicalizes_server_trimmed_whitespace(tmp_path, monkeypatch):
    req = request(tmp_path)
    template = adapter.PROPOSER_GOAL_OBJECTIVE_TEMPLATE
    monkeypatch.setattr(adapter, "PROPOSER_GOAL_OBJECTIVE_TEMPLATE", "\n  " + template + "  \n")
    objective = adapter.goal_objective(req, tmp_path / "goal_context.md")
    expected = template.format(artifact_id=req.artifact_id, request_id=req.request_id,
                               context_path=tmp_path / "goal_context.md").strip()
    assert objective == expected
