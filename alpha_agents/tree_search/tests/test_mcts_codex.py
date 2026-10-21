"""Native role execution contracts; no paid model calls or devices required."""

import json
import sys
from types import SimpleNamespace as NS

import pytest

from alpha_agents.tree_search.core.mcts.responses import validate_response
from alpha_agents.tree_search.infrastructure.process import execute
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.adapter import (
    InvocationDeadline,
    _collect_goal,
    _probe_thread_sandbox,
    goal_objective,
    jsonable,
    plan_payload,
    wait_for_pending_check,
)
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.codex import (
    CodexRuntime,
)
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.prompts import (
    RoleRequest,
)
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.responses import (
    parse_native_plan,
)


@pytest.mark.parametrize("provider", ["openai", "ollama", "lmstudio", "custom"])
@pytest.mark.parametrize("auth_mode", ["codex_login", "environment", "unauthenticated"])
def test_explicit_provider_configuration_uses_owned_namespace(provider, auth_mode):
    runtime = CodexRuntime("/python", "/codex", provider, "https://example.test/v1", auth_mode,
                           "EXAMPLE_API_KEY" if auth_mode == "environment" else None)
    overrides = {key: json.loads(value) for key, value in
                 (override.split("=", 1) for override in runtime.overrides())}
    effective = "alpha_agents_" + provider
    assert overrides["model_provider"] == effective
    prefix = "model_providers." + effective
    assert overrides[prefix + ".base_url"] == runtime.base_url
    assert overrides[prefix + ".requires_openai_auth"] == (auth_mode == "codex_login")
    assert overrides[prefix + ".wire_api"] == "responses"
    assert all(not key.startswith("model_providers." + provider + ".") for key in overrides)
    if auth_mode == "environment":
        assert overrides[prefix + ".env_key"] == "EXAMPLE_API_KEY"
    else:
        assert prefix + ".env_key" not in overrides
    assert runtime.identity


@pytest.mark.parametrize("role", ["policy", "judge_review", "value"])
def test_generic_role_overrides_reach_exec_subcommand_parser(tmp_path, monkeypatch, role):
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles import codex

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = CodexRuntime(sys.executable, sys.executable, "custom", "https://example.test/v1", "unauthenticated")
    gateway = codex.CodexGateway(runtime)
    req = RoleRequest(str(tmp_path), "call-parser", role, "artifact", "gpt-6-luna", "low", {})
    monkeypatch.setattr(gateway, "preflight", lambda workspace: {})
    monkeypatch.setattr(codex, "output_schema", lambda request: {})
    monkeypatch.setattr(codex, "validate_response", lambda result, request, workspace: result)

    def launch(command, cwd, output, timeout, env, stdin_path):
        # The real CLI gives exec a separate parser: root-level -c values
        # never reach it. This invariant protects permissions AND routing.
        assert command[:2] == [runtime.codex_bin, "exec"]
        overrides = {command[i + 1].split("=", 1)[0]: command[i + 1].split("=", 1)[1]
                     for i, value in enumerate(command) if value == "-c"}
        assert json.loads(overrides["approval_policy"]) == "never"
        assert json.loads(overrides["model_provider"]) == runtime.effective_provider
        assert json.loads(overrides["default_permissions"]) == "alpha_agents_role"
        assert json.loads(overrides["model_reasoning_effort"]) == "low"
        assert "--sandbox" not in command
        assert "sandbox_mode" not in overrides
        assert cwd == workspace and 0 < timeout <= 7200 and stdin_path.is_file()
        (output.parent / "final.txt").write_text('{"probe": "completed"}')
        return {"timed_out": False, "returncode": 0}, ""

    monkeypatch.setattr(codex, "execute", launch)
    assert gateway.invoke(req, workspace) == {"probe": "completed"}


def request(tmp_path, role="implementer", context=None):
    return RoleRequest(str(tmp_path), "request-a", role, "artifact-a", "configured-model", "high",
                       context if context is not None else {"plan_id": "plan-a", "attempt_id": "attempt-a"})


def legacy_plan_request(tmp_path, role="proposer", context=None):
    """Historical Plan transport tests are independent of current role defaults."""
    class LegacyPlanRequest(RoleRequest):
        @property
        def collaboration_mode(self):
            return "plan"
        @property
        def execution_mode(self):
            return "plan"
    req = request(tmp_path, role, context)
    return LegacyPlanRequest(**req.to_record())


@pytest.mark.parametrize("terminal", ["completed", "failed"])
def test_pending_diagnostic_receipt_waits_with_original_deadline(tmp_path, terminal):
    root = tmp_path / "check_results"
    root.mkdir()
    req = request(tmp_path, context={"plan_id": "plan-a", "attempt_id": "attempt-a",
                                    "device_check_channel": {"result_directory": str(root)}})
    receipt = {"status": "pending", "check_id": "diagnostic-a", "invocation_id": req.request_id,
               "attempt_id": "attempt-a", "artifact_id": "candidate-a", "sample_kind": "targeted"}
    path = root / "latest.json"
    path.write_text(json.dumps(receipt))
    clock = [0.0]
    deadline = InvocationDeadline(7200, lambda: clock[0])
    def sleep(seconds):
        clock[0] += seconds
        path.write_text(json.dumps({**receipt, "status": terminal}))
    assert wait_for_pending_check(req, tmp_path, deadline, sleep=sleep)
    assert deadline.ends == 7200 and deadline.remaining() == 7199


def test_pending_diagnostic_timeout_cannot_extend_goal_clock(tmp_path):
    root = tmp_path / "check_results"
    root.mkdir()
    req = request(tmp_path, context={"plan_id": "plan-a", "attempt_id": "attempt-a",
                                    "device_check_channel": {"result_directory": str(root)}})
    (root / "latest.json").write_text(json.dumps({"status": "pending", "check_id": "diagnostic-a",
        "invocation_id": req.request_id, "attempt_id": "attempt-a", "artifact_id": "candidate-a",
        "sample_kind": "targeted"}))
    clock = [0.0]
    deadline = InvocationDeadline(2, lambda: clock[0])
    with pytest.raises(TimeoutError, match="Original Codex"):
        wait_for_pending_check(req, tmp_path, deadline, sleep=lambda seconds: clock.__setitem__(0, clock[0]+seconds))
    assert clock[0] == deadline.ends == 2


@pytest.mark.parametrize("field", ["check_id", "invocation_id", "attempt_id", "artifact_id", "sample_kind"])
def test_pending_diagnostic_rejects_changed_receipt_identity(tmp_path, field):
    root = tmp_path / "check_results"
    root.mkdir()
    req = request(tmp_path, context={"plan_id": "plan-a", "attempt_id": "attempt-a",
                                    "device_check_channel": {"result_directory": str(root)}})
    receipt = {"status": "pending", "check_id": "diagnostic-a", "invocation_id": req.request_id,
               "attempt_id": "attempt-a", "artifact_id": "candidate-a", "sample_kind": "targeted"}
    path = root / "latest.json"
    path.write_text(json.dumps(receipt))
    def sleep(_):
        path.write_text(json.dumps({**receipt, "status": "completed", field: "other"}))
    with pytest.raises(ValueError, match="identity"):
        wait_for_pending_check(req, tmp_path, InvocationDeadline(7200), sleep=sleep)


def test_blocked_goal_resumes_same_thread_after_admitted_check_finishes(tmp_path, monkeypatch):
    pytest.importorskip("openai_codex")
    from alpha_agents.tree_search.mutators.coding_agents.codex import execution
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles import adapter

    root = tmp_path / "check_results"
    root.mkdir()
    req = request(tmp_path, context={"plan_id": "plan-a", "attempt_id": "attempt-a",
                                    "device_check_channel": {"result_directory": str(root)}})
    receipt = {"status": "pending", "check_id": "diagnostic-a", "invocation_id": req.request_id,
               "attempt_id": "attempt-a", "artifact_id": "candidate-a", "sample_kind": "targeted"}
    (root / "latest.json").write_text(json.dumps(receipt))
    clock = [0.0]
    deadline = InvocationDeadline(7200, lambda: clock[0])
    goal = NS(status="blocked", objective=goal_objective(req, tmp_path / "goal_context.md"),
              thread_id="persisted-thread", tokens_used=123)
    starts, watched = [], []
    class Client:
        def thread_start(self, params):
            starts.append(params)
            return NS(thread=NS(id="persisted-thread"))
        def thread_read(self, thread_id, include_turns=False):
            assert thread_id == goal.thread_id and include_turns
            return NS(thread=NS(turns=[NS(id="original-turn", status="completed"),
                                      NS(id="resumed-turn", status="completed")]))
    def run(thread, instruction, **kwargs):
        assert thread.id == goal.thread_id
        watched.append((instruction, kwargs))
        if len(watched) == 1:
            assert instruction == goal.objective and kwargs == {}
            text = "Device verification is still pending."
        else:
            assert instruction is None and kwargs == {"resume": True}
            assert goal.tokens_used == 123 and deadline.remaining() == 7199
            goal.status, goal.tokens_used = "complete", 200
            text = json.dumps(implementation_response())
        return NS(id="turn", status="completed", items=[], usage={}, final_response=text), None, goal
    def sleep(seconds):
        clock[0] += seconds
        (root / "latest.json").write_text(json.dumps({**receipt, "status": "completed"}))
    monkeypatch.setattr(execution, "run_watched_goal", run)
    monkeypatch.setattr(execution, "_goal_snapshot", lambda client, thread_id: goal)
    monkeypatch.setattr(execution, "_require_idle_thread", lambda client, thread_id, timeout: None)
    original_wait = adapter.wait_for_pending_check
    monkeypatch.setattr(adapter, "wait_for_pending_check", lambda req, output, limit: original_wait(req, output, limit, sleep=sleep))
    response = _collect_goal(Client(), req, tmp_path, tmp_path, deadline)
    assert response["status"] == "completed" and len(starts) == 1 and len(watched) == 2
    assert goal.tokens_used == 200 and deadline.ends == 7200
    record = json.loads((tmp_path / "diagnostic_goal_recovery_0.json").read_text())
    assert record["remaining_seconds"] == 7199


def implementation_response():
    return {"artifact_id": "artifact-a", "plan_id": "plan-a", "attempt_id": "attempt-a",
            "status": "completed", "changes": ["Add verified bounded recovery"], "blockers": [],
            "evidence": ["controller.py"], "verification": [
                {"ran": True, "status": "completed", "evidence": ["controller.py"]}]}


def test_goal_retains_full_prompt_and_explicit_completion_contract(tmp_path):
    req = request(tmp_path, context={"plan_id": "plan-a", "attempt_id": "attempt-a",
                                   "frozen_plan": "Detailed investigation and intervention. " * 1000})
    objective = goal_objective(req, tmp_path / "goal_context.md")
    assert len(objective) <= 4000
    assert len(req.render()) > len(objective)
    assert "goal_context.md" in objective
    assert "attempt-a" in objective and "plan-a" in objective and "artifact-a" in objective
    assert "7,200-second" in objective and "do not reset" in objective
    assert req.execution_mode == "goal"
    assert req.permission_mode == "workspace-write"
    assert "persisted native Codex Goal" in req.render()


def test_plan_payload_preserves_native_default_plan_instructions(tmp_path):
    req = legacy_plan_request(tmp_path, "proposer", {"plan_archive": {"plan_ids": []}})
    payload = plan_payload(req, tmp_path)
    assert payload["collaborationMode"] == {"mode": "plan", "settings": {
        "model": "configured-model", "reasoning_effort": "high", "developer_instructions": None}}
    assert payload["permissions"] == "alpha_agents_role"
    assert "sandboxPolicy" not in payload
    assert "<proposed_plan>" in req.render()
    assert "<proposed_plan>" in req.render() and "single fenced JSON" in req.render()
    with pytest.raises(ValueError):
        plan_payload(request(tmp_path), tmp_path)


@pytest.mark.parametrize("classification", [None, "same_parent_duplicate", "scope_repair_only", "unknown"])
def test_proposal_rejects_missing_or_inadmissible_novelty_classification(tmp_path, classification):
    from alpha_agents.tree_search.core.mcts.planning_scope import (
        novelty_admission_contract,
    )
    from alpha_agents.tree_search.tests.test_mcts import draft

    (tmp_path / "controller.py").write_text("value = 0\n")
    req = request(tmp_path, "proposer", {"plan_archive": {"plan_ids": []},
                                        "novelty_admission_contract": novelty_admission_contract()})
    candidate = {**draft(), "evidence": ["controller.py"]}
    if classification is not None:
        candidate["novelty_status"] = classification
    response = {"artifact_id": req.artifact_id, "plans": [candidate],
                "archive_review": {"catalog_plan_ids": [], "inspected_plan_ids": [], "gaps": []}}
    with pytest.raises(ValueError, match="novelty classification"):
        validate_response(response, req, tmp_path)


@pytest.mark.parametrize("parent", [None, "artifact-a", "different-artifact"])
def test_cross_parent_adaptation_must_identify_a_different_parent(tmp_path, parent):
    from alpha_agents.tree_search.core.mcts.planning_scope import (
        novelty_admission_contract,
    )
    from alpha_agents.tree_search.tests.test_mcts import draft

    (tmp_path / "controller.py").write_text("value = 0\n")
    req = request(tmp_path, "proposer", {"plan_archive": {"plan_ids": ["old"],
        "plans": [{"id": "old", "parent_artifact": parent}]},
        "novelty_admission_contract": novelty_admission_contract()})
    candidate = {**draft(), "evidence": ["controller.py"], "closest_plan_ids": ["old"],
                 "novelty_status": "cross_parent_adaptation"}
    response = {"artifact_id": req.artifact_id, "plans": [candidate],
                "archive_review": {"catalog_plan_ids": ["old"], "inspected_plan_ids": ["old"], "gaps": []}}
    del candidate["intervention"]["scope"]
    if parent == "different-artifact":
        assert validate_response(response, req, tmp_path) is response
    else:
        with pytest.raises(ValueError, match="different parent"):
            validate_response(response, req, tmp_path)


def test_proposal_rejects_new_action_label_that_declares_itself_duplicate(tmp_path):
    from alpha_agents.tree_search.core.mcts.planning_scope import (
        novelty_admission_contract,
    )
    from alpha_agents.tree_search.tests.test_mcts import draft

    (tmp_path / "controller.py").write_text("value = 0\n")
    req = request(tmp_path, "proposer", {"plan_archive": {"plan_ids": ["old"]},
                                        "novelty_admission_contract": novelty_admission_contract()})
    candidate = {**draft(), "evidence": ["controller.py"], "closest_plan_ids": ["old"],
                 "novelty_status": "new_action",
                 "novelty": "Same-parent duplicate of archived old: only the omitted test scope changes."}
    response = {"artifact_id": req.artifact_id, "plans": [candidate],
                "archive_review": {"catalog_plan_ids": ["old"], "inspected_plan_ids": ["old"], "gaps": []}}
    with pytest.raises(ValueError, match="declares a same-parent duplicate"):
        validate_response(response, req, tmp_path)


def test_native_sdk_notifications_preserve_dataclass_and_typed_payload():
    sdk = pytest.importorskip("openai_codex.models")
    event = sdk.Notification("unknown", sdk.UnknownNotification({"nested": {"item": "trace"}}))
    assert jsonable(event) == {"method": "unknown", "payload": {"params": {"nested": {"item": "trace"}}}}


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_unaccepted_plan_retains_native_output_and_usage(tmp_path, monkeypatch, status):
    sdk_run = pytest.importorskip("openai_codex._run")
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles import (
        adapter as codex_worker,
    )
    result = NS(id="turn-a", status=status, items=[{"type": "agentMessage", "text": "Unaccepted output"}],
                usage={"total_tokens": 17})
    monkeypatch.setattr(sdk_run, "_collect_turn_result", lambda events, turn_id: result)
    monkeypatch.setattr(codex_worker, "_assert_plan_preset", lambda client: None)
    unregistered = []
    class Client:
        def thread_start(self, params):
            return NS(thread=NS(id="thread-a"))
        def turn_start(self, thread_id, prompt, params):
            return NS(turn=NS(id="turn-a"))
        def unregister_turn_notifications(self, turn_id):
            unregistered.append(turn_id)
    with pytest.raises((ValueError, RuntimeError)):
        codex_worker._collect_plan(Client(), legacy_plan_request(tmp_path, "proposer"), tmp_path, tmp_path)
    stored = json.loads((tmp_path / "native_result.json").read_text())
    assert stored["status"] == status and stored["usage"] == {"total_tokens": 17}
    assert stored["items"] == result.items
    assert stored["thread_id"] == "thread-a" and stored["turn_id"] == "turn-a"
    assert unregistered == ["turn-a"]
    assert not (tmp_path / "result.json").exists()
    assert not (tmp_path / "native_plan.md").exists()


def test_sandbox_preflight_starts_only_an_ephemeral_restricted_thread(tmp_path):
    calls = []
    class Client:
        def thread_start(self, params):
            calls.append(params)
            return NS(thread=NS(id="ephemeral-probe"))
    _probe_thread_sandbox(Client(), tmp_path)
    assert calls == [{"cwd": str(tmp_path), "permissions": "alpha_agents_role",
                      "approvalPolicy": "never", "ephemeral": True}]


def test_sandbox_preflight_propagates_host_failure(tmp_path):
    class Client:
        def thread_start(self, params):
            raise RuntimeError("bwrap: loopback: Operation not permitted")
    with pytest.raises(RuntimeError, match="bwrap: loopback"):
        _probe_thread_sandbox(Client(), tmp_path)


def test_runtime_permission_grants_exact_resolved_binary_only(tmp_path):
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.permissions import (
        runtime_permissions,
    )
    binary = tmp_path / "release" / "codex"
    binary.parent.mkdir()
    binary.write_text("trusted executable")
    link = tmp_path / "codex-link"
    link.symlink_to(binary)
    original = {"name": "alpha_agents_role", "filesystem": {":minimal": "read"},
                "network_enabled": False}
    profile = runtime_permissions(original, str(link))
    assert profile["filesystem"] == {":minimal": "read", str(binary): "read"}
    assert original["filesystem"] == {":minimal": "read"}
    assert profile["network_enabled"] is False
    with pytest.raises(ValueError, match="exact executable"):
        runtime_permissions(original, str(binary.parent))


def test_verification_environment_grants_only_its_exact_root(tmp_path):
    from pathlib import Path

    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.permissions import (
        runtime_permissions,
    )

    binary = tmp_path / "codex"
    binary.write_text("launcher")
    env = tmp_path / "tools" / "venv"
    python = env / "bin" / "python3.10"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    (env / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
    original = {"name": "alpha_agents_role", "filesystem": {":minimal": "read"}, "network_enabled": False}
    profile = runtime_permissions(original, str(binary), str(python))
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles import verification
    assert profile["filesystem"] == {":minimal": "read", str(binary): "read", str(env): "read",
                                      str(Path(verification.__file__).resolve()): "read"}
    assert str(tmp_path) not in profile["filesystem"]
    assert str(env.parent) not in profile["filesystem"]
    assert original["filesystem"] == {":minimal": "read"}
    assert profile["network_enabled"] is False
    plain_python = tmp_path / "plain" / "bin" / "python"
    plain_python.parent.mkdir(parents=True)
    plain_python.symlink_to(sys.executable)
    with pytest.raises(ValueError, match="virtual-environment"):
        runtime_permissions(original, str(binary), str(plain_python))


def test_optional_verification_tools_preserve_existing_runtime_records():
    runtime = CodexRuntime("/python", "/codex", "openai", "https://example.test/v1", "codex_login")
    assert "verification_python" not in runtime.record
    assert runtime.implementation_tools_context() == {}
    tools = CodexRuntime(**runtime.record, verification_python="/prepared/venv/bin/python")
    assert tools.identity != runtime.identity
    assert tools.record["verification_python"] == "/prepared/venv/bin/python"
    context = tools.implementation_tools_context()["trusted_verification_tools"]
    import shlex
    from pathlib import Path

    from alpha_agents.tree_search.mutators.coding_agents.codex.roles import verification
    assert shlex.split(context["command"]) == ["/prepared/venv/bin/python", str(Path(verification.__file__).resolve())]
    assert "protected benchmarks, evaluators, or parent evidence" in context["purpose"]


@pytest.mark.parametrize("native_status", ["paused", "blocked", "budget_limited", "usage_limited"])
def test_noncomplete_goal_never_becomes_completed_engineering(tmp_path, monkeypatch, native_status):
    pytest.importorskip("openai_codex")
    from alpha_agents.tree_search.mutators.coding_agents.codex import (
        execution as worker,
    )
    req = request(tmp_path)
    (tmp_path / "controller.py").write_text("retries = 2\n")
    calls = []
    class Client:
        def thread_start(self, params):
            calls.append(params)
            return NS(thread=NS(id="persisted-thread"))
        def thread_read(self, thread_id, include_turns=False):
            assert thread_id == "persisted-thread" and include_turns
            return NS(thread=NS(turns=[NS(id="physical-turn-a", status="completed")]))
    goal = NS(status=native_status, objective=goal_objective(req, tmp_path / "goal_context.md"),
              thread_id="persisted-thread", tokens_used=123)
    result = NS(id="turn-a", status="completed", items=[], usage={"total": 123},
                final_response=json.dumps(implementation_response()))
    def watched(thread, instruction, **kwargs):
        assert instruction == goal_objective(req, tmp_path / "goal_context.md")
        assert thread.id == "persisted-thread" and kwargs == {}
        return result, None, goal
    monkeypatch.setattr(worker, "run_watched_goal", watched)
    monkeypatch.setattr(worker, "_goal_snapshot", lambda client, thread: goal)
    response = _collect_goal(Client(), req, tmp_path, tmp_path, InvocationDeadline(7200))
    assert response["status"] == "partial"
    assert native_status in response["blockers"][0]
    assert calls[0]["developerInstructions"] == req.render()
    assert calls[0]["permissions"] == "alpha_agents_role"
    assert "sandbox" not in calls[0]
    assert (tmp_path / "goal_context.md").read_text() == req.render()
    telemetry = json.loads((tmp_path / "native_result.json").read_text())
    assert telemetry["physical_turns"][0]["id"] == "physical-turn-a"
    validate_response(response, req, tmp_path)


def test_goal_recovery_reopens_same_goal_without_resetting_deadline(tmp_path, monkeypatch):
    pytest.importorskip("openai_codex")
    from alpha_agents.tree_search.mutators.coding_agents.codex import (
        execution as worker,
    )
    req = request(tmp_path)
    clock = [10.0]
    deadline = InvocationDeadline(7200, lambda: clock[0])
    objective = goal_objective(req, tmp_path / "goal_context.md")
    paused = NS(status="paused", objective=objective, thread_id="same-thread", tokens_used=450)
    complete = NS(status="complete", objective=objective, thread_id="same-thread", tokens_used=800)
    calls = []
    class Client:
        def __init__(self, name):
            self.name = name
        def thread_start(self, params):
            assert self.name == "first"
            calls.append("start")
            return NS(thread=NS(id="same-thread"))
        def thread_resume(self, thread, params):
            assert self.name == "replacement" and thread == "same-thread"
            calls.append("resume")
            return NS(thread=NS(id=thread))
        def thread_read(self, thread, include_turns=False):
            return NS(thread=NS(turns=[NS(id="turn-1"), NS(id="turn-2")]))
    first, replacement = Client("first"), Client("replacement")
    result = NS(id="turn-2", status="completed", items=[], usage={}, final_response=json.dumps(implementation_response()))
    def watched(thread, instruction, *, resume=False):
        calls.append((thread._client.name, instruction, resume))
        clock[0] += 300
        if not resume:
            return result, {"reason": "launcher failure"}, paused
        assert thread._client is replacement and instruction is None
        return result, None, complete
    snapshots = iter([paused, complete])
    monkeypatch.setattr(worker, "run_watched_goal", watched)
    monkeypatch.setattr(worker, "_goal_snapshot", lambda *args: next(snapshots))
    monkeypatch.setattr(worker, "_require_idle_thread", lambda *args: calls.append("idle"))
    monkeypatch.setattr(worker, "_preflight_launcher", lambda client: calls.append("preflight"))
    def restart(client):
        assert client is first
        calls.append("restart")
        clock[0] += 30
        return replacement
    response = _collect_goal(first, req, tmp_path, tmp_path, deadline, restart)
    assert response["status"] == "completed"
    assert calls.count("start") == 1 and calls.count("resume") == 1
    assert calls.index("idle") < calls.index("restart") < calls.index("resume") < calls.index("preflight")
    assert deadline.started == 10 and deadline.ends == 7210
    assert deadline.remaining() == 6570
    assert json.loads((tmp_path / "goal_recovery_0.json").read_text())["remaining_seconds"] == 6570


@pytest.mark.parametrize("changed", ["objective", "thread", "missing"])
def test_goal_completion_rejects_changed_durable_identity_and_preserves_evidence(tmp_path, monkeypatch, changed):
    pytest.importorskip("openai_codex")
    from alpha_agents.tree_search.mutators.coding_agents.codex import (
        execution as worker,
    )

    req = request(tmp_path)
    objective = goal_objective(req, tmp_path / "goal_context.md")
    original = NS(status="complete", objective=objective, thread_id="owned-thread", tokens_used=123)
    final = (None if changed == "missing" else NS(
        status="complete", objective="Different engineering target" if changed == "objective" else objective,
        thread_id="foreign-thread" if changed == "thread" else "owned-thread", tokens_used=123))
    class Client:
        def thread_start(self, params):
            return NS(thread=NS(id="owned-thread"))
        def thread_read(self, thread_id, include_turns=False):
            assert thread_id == "owned-thread"
            return NS(thread=NS(turns=[NS(id="physical-turn", status="completed")]))
    result = NS(id="physical-turn", status="completed", items=[], usage={"total": 123},
                final_response=json.dumps(implementation_response()))
    monkeypatch.setattr(worker, "run_watched_goal", lambda *args: (result, None, original))
    monkeypatch.setattr(worker, "_goal_snapshot", lambda *args: final)
    with pytest.raises(RuntimeError, match="stored objective and thread"):
        _collect_goal(Client(), req, tmp_path, tmp_path, InvocationDeadline(7200))
    telemetry = json.loads((tmp_path / "native_result.json").read_text())
    assert telemetry["thread_id"] == "owned-thread"
    assert telemetry["physical_turns"][0]["id"] == "physical-turn"
    assert telemetry["goal"] == jsonable(final)
    assert not (tmp_path / "result.json").exists()


def test_completed_engineering_rejects_unrun_checks_or_wrong_attempt(tmp_path):
    (tmp_path / "controller.py").write_text("retries = 2\n")
    response = implementation_response()
    response["verification"][0]["ran"] = False
    with pytest.raises(ValueError, match="unrun check"):
        validate_response(response, request(tmp_path), tmp_path)
    response = implementation_response()
    response["attempt_id"] = "wrong-attempt"
    with pytest.raises(ValueError, match="identity mismatch"):
        validate_response(response, request(tmp_path), tmp_path)


def test_full_prompt_transport_uses_file_input_beyond_argument_size(tmp_path):
    prompt = "Detailed evidence and autonomous implementation instructions.\n" * 10000
    source = tmp_path / "prompt.md"
    source.write_text(prompt)
    command = [sys.executable, "-c", "import sys; print(len(sys.stdin.buffer.read()))"]
    report, stdout = execute(command, tmp_path, tmp_path / "process", timeout=5, stdin_path=source)
    assert report["returncode"] == 0
    assert int(stdout.strip()) == len(prompt.encode())
    assert prompt not in json.dumps(report)


@pytest.mark.parametrize("heading_id", ["candidate-a", "`candidate-a`", "``candidate-a``"])
def test_native_plan_retains_developed_sections_and_ignores_fenced_headings(tmp_path, heading_id):
    req = request(tmp_path, "proposer", {"plan_archive": {"plan_ids": []}})
    metadata = {"id": "candidate-a", "intervention": {"target": "controller", "operation": "add recovery",
        "conditions": "verified failure",
        "mechanism": "bounded verified retry"}, "novelty": "Distinct recovery mechanism",
        "diversity_audit": "Compared archive mechanisms", "closest_plan_ids": [], "evidence": ["controller.py"]}
    document = ('## Archive review\n```json\n{"catalog_plan_ids": [], "inspected_plan_ids": [], "gaps": []}\n```\n'
                '## Candidate candidate-a\n```json\n' + json.dumps(metadata) + '\n```\n'
                '### Diagnosis\nInspect actual control flow and failed trajectories.\n'
                '```python\n## This is code, not a candidate heading\n```\n'
                '### Implementation steps\nIn controller.py, add a bounded recovery branch with at most two retries.\n'
                '### Verification\nRun meaningful behavioral checks.\n'
                '## Diversity audit\nCompare conditions, mechanisms, parameters and affected behavior.\n')
    document = document.replace("## Candidate candidate-a", f"## Candidate {heading_id}")
    (tmp_path / "controller.py").write_text("retries = 0\n")
    result = parse_native_plan(document, req)
    assert result["native_document"] == document
    assert "### Verification" in result["plans"][0]["document"]
    assert "add a bounded recovery branch with at most two retries" in result["plans"][0]["document"]
    validate_response(result, req, tmp_path)
    for invalid_id in ("other", "`other`", "`candidate-a` extra", "`candidate-a", "`candidate-a``"):
        with pytest.raises(ValueError, match="IDs do not agree"):
            parse_native_plan(document.replace(f"## Candidate {heading_id}", f"## Candidate {invalid_id}"), req)


@pytest.mark.parametrize("role", ["proposer", "policy", "implementer", "value"])
def test_reasoning_roles_receive_external_sealed_sqlite_read_guidance(tmp_path, role):
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.prompts import (
        CONTEXT_GUIDANCE,
    )

    req = request(tmp_path, role)
    sqlite_guidance = req.context["trajectory_interpretation_contract"]["sqlite"]
    assert sqlite_guidance == CONTEXT_GUIDANCE["judge_reference_contract"]["sqlite"]
    assert "mode=ro&immutable=1" in sqlite_guidance
    assert "nonempty WAL" in sqlite_guidance
    assert sqlite_guidance in req.render()


def test_role_profiles_grant_only_workspace_and_explicit_canonical_evidence(tmp_path):
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.permissions import (
        permission_overrides,
        role_permissions,
    )

    workspace = tmp_path / "workspaces" / "judge"
    workspace.mkdir(parents=True)
    evidence = tmp_path / "tasks" / "task-id" / "execution-id" / "evidence"
    evidence.mkdir(parents=True)
    hidden_plan = tmp_path / "plan_archive" / "plan-id" / "PLAN.md"
    hidden_plan.parent.mkdir(parents=True)
    hidden_plan.write_text("incoming plan")
    req = request(tmp_path, "judge_review", {"artifact": "artifact-a", "rubric": "rubric",
        "canonical_evidence": {"evidence": [str(evidence)]}})
    profile = role_permissions(req, workspace)
    assert profile["filesystem"][str(workspace)] == "read"
    assert profile["filesystem"][str(evidence)] == "read"
    assert str(hidden_plan) not in profile["filesystem"]
    assert str(tmp_path) not in profile["filesystem"]
    assert ":root" not in profile["filesystem"]
    assert not profile["network_enabled"]
    overrides = permission_overrides(profile)
    assert 'default_permissions="alpha_agents_role"' in overrides
    assert not any("sandbox_mode" in override for override in overrides)
    req = request(tmp_path, "implementer", {"plan_id": "plan-a", "attempt_id": "attempt-a"})
    assert role_permissions(req, workspace)["filesystem"][str(workspace)] == "write"


def test_large_evidence_manifest_compacts_only_already_granted_read_roots(tmp_path):
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.permissions import (
        permission_overrides,
        role_permissions,
    )

    root = tmp_path / "tasks" / "task" / "execution" / "evidence"
    root.mkdir(parents=True)
    files = []
    for index in range(700):
        path = root / (f"frame-{index}-" + "x" * 150 + ".json")
        path.write_text("{}")
        files.append(str(path))
    other = tmp_path / "tasks" / "other" / "execution" / "evidence" / "result.json"
    other.parent.mkdir(parents=True)
    other.write_text("{}")
    context = {"canonical_evidence": {"files": files, "root": str(root), "other": str(other)}}
    profile = role_permissions(request(tmp_path, "judge_review", context), tmp_path / "workspace")
    assert profile["filesystem"][str(root)] == "read"
    assert all(path not in profile["filesystem"] for path in files)
    assert profile["filesystem"][str(other)] == "read"
    assert str(other.parent) not in profile["filesystem"]
    assert str(root.parent) not in profile["filesystem"]
    assert max(len(value.encode()) for value in permission_overrides(profile)) < 10000


@pytest.mark.parametrize("forbidden", ["tree.sqlite3", "role_requests/other/prompt.md", "plan_archive/id/PLAN.md", "plan_archive/id/PLAN.json", "evidence_archive/id/ATTEMPT.json", "evidence_archive/id/CONTINUATION.json"])
def test_judge_canonical_context_cannot_grant_forbidden_run_reads(tmp_path, forbidden):
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.permissions import (
        role_permissions,
    )

    path = tmp_path / forbidden
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("excluded evidence")
    req = request(tmp_path, "judge_review", {"canonical_evidence": {"evidence": [str(path)]}})
    with pytest.raises(ValueError, match="forbidden"):
        role_permissions(req, tmp_path / "workspace")


def test_proposer_can_read_archived_plan_files_without_reading_other_requests(tmp_path):
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.permissions import (
        role_permissions,
    )

    path = tmp_path / "plan_archive" / "hash" / "PLAN.md"
    path.parent.mkdir(parents=True)
    path.write_text("complete plan")
    req = request(tmp_path, "proposer", {"plan_archive": {"plans": [{"document_path": str(path)}]}})
    profile = role_permissions(req, tmp_path / "workspace")
    assert profile["filesystem"][str(path)] == "read"
    assert str(tmp_path / "plan_archive") not in profile["filesystem"]


def test_legacy_sandbox_settings_fail_permission_preflight():
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.permissions import (
        reject_legacy_sandbox_layers,
    )

    with pytest.raises(RuntimeError, match="legacy sandbox_mode"):
        reject_legacy_sandbox_layers([{"config": {"sandbox_mode": "read-only"}}])
    reject_legacy_sandbox_layers([{"config": {"default_permissions": "alpha_agents_role"}}])
    reject_legacy_sandbox_layers([{"disabledReason": "not loaded", "config": {"sandbox_mode": "read-only"}}])


def test_effective_permission_validation_rejects_inherited_broad_reads():
    import copy

    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.permissions import (
        validate_effective_permissions,
    )

    profile = {"name": "role", "filesystem": {":minimal": "read", "/assigned/workspace": "read"}}
    config = {"default_permissions": "role", "permissions": {"role": {
        "filesystem": {**profile["filesystem"], "glob_scan_max_depth": None},
        "network": {"enabled": False}, "extends": None, "workspace_roots": None}}}
    validate_effective_permissions(config, profile)
    for field, value in (("extends", ":read-only"), ("workspace_roots", {"/other": True})):
        changed = copy.deepcopy(config)
        changed["permissions"]["role"][field] = value
        with pytest.raises(RuntimeError, match="differs"):
            validate_effective_permissions(changed, profile)
    changed = copy.deepcopy(config)
    changed["permissions"]["role"]["filesystem"][":root"] = "read"
    with pytest.raises(RuntimeError, match="differs"):
        validate_effective_permissions(changed, profile)


def test_canonical_code_evidence_retains_locations_and_external_task_references(tmp_path):
    from alpha_agents.tree_search.core.mcts.responses import canonical_code_evidence

    workspace = tmp_path / "original-workspace"
    workspace.mkdir()
    code = workspace / "controller.py"
    code.write_text("retries = 2\n")
    task = tmp_path / "canonical-task" / "trace.json"
    task.parent.mkdir()
    task.write_text("ordered trajectory")
    original = {"dimensions": {"robustness": {"evidence": [str(code) + ":7:3", str(task), "snapshot-id"]}},
                "explanation": str(code)}
    normalized, provenance = canonical_code_evidence(original, workspace)
    assert normalized["dimensions"]["robustness"]["evidence"] == ["code:controller.py:7:3", str(task), "snapshot-id"]
    assert provenance == [{"original": str(code) + ":7:3", "canonical": "code:controller.py:7:3"}]
    assert original["dimensions"]["robustness"]["evidence"][0] == str(code) + ":7:3"
    assert normalized["explanation"] == str(code)


@pytest.mark.parametrize("kind", ["database", "image", "sibling", "symlink", "narrative", "model_claim"])
def test_final_judge_citations_use_only_assigned_code_and_canonical_task_files(tmp_path, kind):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    evidence = tmp_path / "tasks" / "task" / "execution" / "evidence"
    (evidence / "runtime" / "images").mkdir(parents=True)
    database = evidence / "runtime" / "data_engine.db"
    database.write_bytes(b"fixture")
    image = evidence / "runtime" / "images" / "screen.jpg"
    image.write_bytes(b"fixture")
    sibling = tmp_path / "unsupplied.db"
    sibling.write_bytes(b"not assigned")
    alias = evidence / "escape.db"
    alias.symlink_to(sibling)
    references = {"database": str(database), "image": str(image), "sibling": str(sibling),
                  "symlink": str(alias), "narrative": f"Source: {database} — inspected trajectory",
                  "model_claim": "unverified-model-authored-reference"}
    from alpha_agents.tree_search.tests.judge_fixtures import review_payload

    req = request(tmp_path, "judge_review", {"rubric": "rubric", "canonical_evidence": {
        "snapshot_id": "canonical", "evidence": [str(evidence)]}})
    response = review_payload(artifact=req.artifact_id, rubric="rubric", evidence=references[kind])
    if kind in {"database", "image"}:
        assert validate_response(response, req, workspace) == response
    else:
        with pytest.raises(ValueError, match="Unknown or out-of-scope"):
            validate_response(response, req, workspace)


def test_role_usage_preserves_reported_scopes_and_marks_missing_usage_unknown(tmp_path):
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.telemetry import (
        role_usage,
    )

    assert role_usage(tmp_path)["usage"] is None
    assert role_usage(tmp_path)["scope"] == "unavailable"
    process = tmp_path / "process"
    process.mkdir()
    (process / "stdout.txt").write_text('not JSON\n' + json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 3}}) + '\n')
    reported = role_usage(tmp_path)
    assert reported["usage"] == [{"input_tokens": 10, "output_tokens": 3}]
    assert reported["scope"] == "reported_turn_completions"
    (tmp_path / "native_result.json").write_text(json.dumps({"execution_mode": "goal", "usage": {"total_tokens": 7}}))
    reported = role_usage(tmp_path)
    assert reported["usage"] == {"total_tokens": 7}
    assert reported["scope"] == "adapter_reported_usage"


@pytest.mark.parametrize("native_result", [False, True])
@pytest.mark.parametrize("camel_case", [False, True])
def test_goal_usage_keeps_durable_counter_separate_from_last_turn(tmp_path, native_result, camel_case):
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.telemetry import (
        role_usage,
    )

    (tmp_path / "native_session.json").write_text(json.dumps({
        "thread_id": "owned-thread", "execution_mode": "goal", "goal_objective": "Implement and verify"}))
    fields = ({"threadId": "owned-thread", "tokensUsed": 800, "timeUsedSeconds": 600, "updatedAt": 1234}
              if camel_case else
              {"thread_id": "owned-thread", "tokens_used": 800, "time_used_seconds": 600, "updated_at": 1234})
    (tmp_path / "goal_state.json").write_text(json.dumps({
        **fields, "objective": "Implement and verify", "status": "complete" if native_result else "paused"}))
    if native_result:
        (tmp_path / "native_result.json").write_text(json.dumps({
            "execution_mode": "goal", "usage": {"total_tokens": 7}}))
    observed = role_usage(tmp_path)
    assert observed["usage"] == ({"total_tokens": 7} if native_result else None)
    goal = observed["goal_usage"]
    assert goal["scope"] == "persisted_goal_reported_counter"
    assert goal["tokens_used"] == 800
    assert goal["time_used_seconds"] == 600
    assert goal["updated_at"] == 1234
    assert goal["status"] == ("complete" if native_result else "paused")
    assert goal["thread_id"] == "owned-thread"
    assert len(goal["objective_hash"]) == 64
    assert goal["billing_verified"] is False


@pytest.mark.parametrize("change", [
    {"threadId": "other-thread"}, {"objective": "Different objective"},
    {"tokensUsed": True}, {"tokensUsed": -1}, {"tokensUsed": 1.5}, {"tokensUsed": None},
])
def test_goal_usage_rejects_foreign_identity_or_invalid_counter(tmp_path, change):
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.telemetry import (
        role_usage,
    )

    (tmp_path / "native_session.json").write_text(json.dumps({
        "thread_id": "owned-thread", "execution_mode": "goal", "goal_objective": "Implement"}))
    (tmp_path / "goal_state.json").write_text(json.dumps({
        "threadId": "owned-thread", "objective": "Implement", "tokensUsed": 8, **change}))
    observed = role_usage(tmp_path)
    assert observed["goal_usage"] is None
    assert observed["usage"] is None


def test_goal_usage_requires_session_and_retains_unknown_optional_counters(tmp_path):
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.telemetry import (
        role_usage,
    )

    (tmp_path / "goal_state.json").write_text(json.dumps({
        "threadId": "owned-thread", "objective": "Implement", "tokensUsed": 0,
        "timeUsedSeconds": True, "updatedAt": -1}))
    assert role_usage(tmp_path)["goal_usage"] is None
    (tmp_path / "native_session.json").write_text(json.dumps({
        "thread_id": "owned-thread", "execution_mode": "goal", "goal_objective": "Implement"}))
    observed = role_usage(tmp_path)["goal_usage"]
    assert observed["tokens_used"] == 0
    assert observed["time_used_seconds"] is None
    assert observed["updated_at"] is None


def test_policy_can_cite_declared_archive_files_with_line_locations(tmp_path):
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.permissions import (
        role_permissions,
    )

    archive = tmp_path / "plan_archive" / "plan-hash"
    archive.mkdir(parents=True)
    document, metadata = archive / "PLAN.md", archive / "PLAN.json"
    document.write_text("Full immutable intervention\n")
    metadata.write_text('{"novelty": "different executable mechanism"}')
    context = {"plans": [{"id": "plan-a", "document_path": str(document), "metadata_path": str(metadata)}],
               "evidence_snapshot_id": "snapshot-a"}
    req = request(tmp_path, "policy", context)
    response = {"artifact_id": req.artifact_id, "status": "completed", "plan_ids": ["plan-a"],
                "evidence_snapshot_id": "snapshot-a", "priors": {"plan-a": 1},
                "comparisons": ["Compare the concrete intervention"], "uncertainty": "Limited observed outcomes",
                "evidence": [str(document) + ":1", str(metadata)]}
    assert validate_response(response, req, tmp_path / "workspace") == response
    profile = role_permissions(req, tmp_path / "workspace")
    assert profile["filesystem"][str(document)] == "read"
    assert profile["filesystem"][str(metadata)] == "read"
    assert str(archive) not in profile["filesystem"]


def test_value_can_cite_exact_reachable_code_without_reading_sibling_views(tmp_path):
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.permissions import (
        role_permissions,
    )

    view = tmp_path / "value_code_views" / ("a" * 64)
    sibling = tmp_path / "value_code_views" / ("b" * 64)
    view.mkdir(parents=True)
    sibling.mkdir()
    code, unrelated = view / "controller.py", sibling / "controller.py"
    code.write_text("Exact reachable artifact\n")
    unrelated.write_text("Another artifact\n")
    context = {"score_snapshot_id": "score", "capacity_snapshot_id": "capacity", "objective_id": "objective", "discount_factor": .9, "continuation_policy_id": "policy",
               "reachable_code_views": [{"node_id": "child", "artifact_id": "a" * 64, "code_path": str(view)}]}
    req = request(tmp_path, "value", context)
    from alpha_agents.tree_search.tests.value_fixtures import forecast_payload
    context["score"] = {"u": .5, "j": .5, "objective": {"judge_weight": .1}}
    req = request(tmp_path, "value", context)
    response = forecast_payload(context, req.artifact_id, .1, str(code) + ":1:1")
    assert validate_response(response, req, tmp_path / "workspace") == response
    profile = role_permissions(req, tmp_path / "workspace")
    assert profile["filesystem"][str(view)] == "read"
    assert str(sibling) not in profile["filesystem"]
    with pytest.raises(ValueError, match="out-of-scope"):
        validate_response({**response, "evidence": [str(unrelated)]}, req, tmp_path / "workspace")


@pytest.mark.parametrize("changed", [None, {"status": "pending"}, {"sample_kind": "canonical"},
                                    {"invocation_id": "other"}, {"attempt_id": "other"},
                                    {"snapshot_id": "invented"}, {"artifact_id": "invented"},
                                    {"check_id": ""}])
def test_implementation_snapshot_citation_requires_own_completed_diagnostic_receipt(tmp_path, changed):
    from alpha_agents.tree_search.core.mcts.responses import _evidence

    results = tmp_path / "role_requests" / "request-a" / "check_results"
    results.mkdir(parents=True)
    req = request(tmp_path, "implementer", {"attempt_id": "attempt-a",
                  "device_check_channel": {"result_directory": str(results)}})
    receipt = {"status": "completed", "sample_kind": "targeted", "invocation_id": req.request_id,
               "attempt_id": "attempt-a", "check_id": "check-a", "snapshot_id": "a" * 64,
               "artifact_id": "b" * 64}
    (results / ("c" * 64 + ".json")).write_text(json.dumps({**receipt, **(changed or {})}))
    # A later pending receipt must not hide an earlier completed diagnostic.
    (results / "latest.json").write_text(json.dumps({**receipt, "status": "pending"}))
    if changed:
        with pytest.raises(ValueError, match="out-of-scope"):
            _evidence(["a" * 64], req, tmp_path / "workspace")
    else:
        _evidence(["a" * 64], req, tmp_path / "workspace")
        with pytest.raises(ValueError, match="out-of-scope"):
            _evidence(["d" * 64], req, tmp_path / "workspace")
        other = request(tmp_path, "policy", req.context)
        with pytest.raises(ValueError, match="out-of-scope"):
            _evidence(["a" * 64], other, tmp_path / "workspace")


def test_diagnostic_snapshot_citation_rejects_receipt_symlink(tmp_path):
    from alpha_agents.tree_search.core.mcts.responses import _evidence

    results = tmp_path / "role_requests" / "request-a" / "check_results"
    results.mkdir(parents=True)
    req = request(tmp_path, "implementer", {"attempt_id": "attempt-a",
                  "device_check_channel": {"result_directory": str(results)}})
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"status": "completed", "sample_kind": "targeted",
        "invocation_id": req.request_id, "attempt_id": "attempt-a", "check_id": "check-a",
        "snapshot_id": "a" * 64, "artifact_id": "b" * 64}))
    (results / ("c" * 64 + ".json")).symlink_to(outside)
    with pytest.raises(ValueError, match="out-of-scope"):
        _evidence(["a" * 64], req, tmp_path / "workspace")


def test_device_check_permissions_belong_only_to_current_engineering_invocation(tmp_path):
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.permissions import (
        role_permissions,
    )

    owned = tmp_path / "role_requests" / "request-a"
    results = owned / "check_results"
    results.mkdir(parents=True)
    inbox = owned / "check_input"
    inbox.mkdir()
    channel = {"request_path": str(inbox / "check_request.json"), "result_directory": str(results)}
    context = {"plan_id": "plan-a", "attempt_id": "attempt-a", "device_check_channel": channel}
    req = request(tmp_path, "implementer", context)
    profile = role_permissions(req, tmp_path / "workspace")
    assert profile["filesystem"][str(inbox)] == "write"
    assert channel["request_path"] not in profile["filesystem"]
    assert profile["filesystem"][channel["result_directory"]] == "read"
    assert str(owned) not in profile["filesystem"]
    evidence = results / "latest.json"
    evidence.write_text('{"outcomes": {"task": false}}')
    response = implementation_response()
    response["verification"].append({"ran": True, "status": "completed", "evidence": [str(evidence)]})
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace" / "controller.py").write_text("permitted harness")
    assert validate_response(response, req, tmp_path / "workspace") == response
    with pytest.raises(ValueError, match="this implementer"):
        role_permissions(request(tmp_path, "policy", context), tmp_path / "workspace")
    with pytest.raises(ValueError, match="this implementer"):
        role_permissions(request(tmp_path, "implementer", {**context, "device_check_channel": {
            **channel, "result_directory": str(tmp_path / "role_requests" / "other")}}), tmp_path / "workspace")
    with pytest.raises(ValueError, match="this implementer"):
        role_permissions(request(tmp_path, "implementer", {**context, "device_check_channel": {
            **channel, "request_path": str(owned / "check_request.json")}}), tmp_path / "workspace")
    inbox.rmdir()
    inbox.symlink_to(results, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic links"):
        role_permissions(req, tmp_path / "workspace")


def test_goal_termination_stops_check_admission_and_drains_active_work(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.codex import (
        CodexGateway,
    )

    started, release, completed = threading.Event(), threading.Event(), threading.Event()
    class Service:
        root = tmp_path / "results"
        channel = {"request_path": str(tmp_path / "request.json")}
        def handle(self, payload):
            started.set()
            assert release.wait(5)
            completed.set()
            return {"check_id": payload["check_id"], "status": "completed"}
    service = Service()
    service.root.mkdir()
    class GoalGateway(CodexGateway):
        def invoke(self, req, workspace):
            assert req.execution_mode == "goal" and req.timeout_seconds == 7200
            (tmp_path / "request.json").write_text('{"check_id": "active"}')
            assert started.wait(5)
            raise TimeoutError("Native Goal reached its original deadline")
    gateway = GoalGateway(None)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(gateway.invoke_with_checks, request(tmp_path), tmp_path / "workspace", service)
        try:
            assert started.wait(5)
            assert not future.done()
        finally:
            release.set()
        with pytest.raises(TimeoutError, match="original deadline"):
            future.result(timeout=5)
    assert completed.is_set()


@pytest.mark.parametrize("status,value", [(None, 0), ("partial", 0), ("partial", None), ("failed", None)])
def test_unavailable_value_assessment_cannot_publish_zero_forecast(tmp_path, status, value):
    context = {"score_snapshot_id": "score", "capacity_snapshot_id": "capacity",
               "objective_id": "objective", "discount_factor": .9, "continuation_policy_id": "policy"}
    req = request(tmp_path, "value", context)
    from alpha_agents.tree_search.tests.value_fixtures import forecast_payload
    context["score"] = {"u": .5, "j": .5, "objective": {"judge_weight": .1}}
    req = request(tmp_path, "value", context)
    response = forecast_payload(context, req.artifact_id, evidence="context:score_snapshot_id")
    response.update(value=value)
    if status is None:
        response.pop("status")
    else:
        response["status"] = status
    with pytest.raises(ValueError):
        validate_response(response, req, tmp_path)
    completed = forecast_payload(context, req.artifact_id, evidence="context:score_snapshot_id")
    assert validate_response(completed, req, tmp_path)["value"] == 0


def test_assessment_schema_freezes_identity_and_snapshot_aliases_are_exact(tmp_path):
    from alpha_agents.tree_search.core.mcts.responses import _evidence
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.responses import (
        output_schema,
    )

    context = {'score_snapshot_id': 'score-hash', 'capacity_snapshot_id': 'capacity-hash',
               'objective_id': 'objective-hash', 'discount_factor': .9, 'continuation_policy_id': 'policy-hash',
               'nested': {'other_id': 'nested-hash'}}
    req = request(tmp_path, 'value', context)
    envelope = output_schema(req)
    schema = envelope['properties']
    assert 'status' in envelope['required']
    assert schema['status']['enum'] == ['completed', 'partial', 'failed']
    assert schema['value']['type'] == ['number', 'null']
    for key in ('score_snapshot_id', 'capacity_snapshot_id', 'objective_id', 'continuation_policy_id'):
        assert schema[key]['enum'] == [context[key]]
        _evidence([f'context:{key}'], req, tmp_path)
    assert schema['discount_factor']['enum'] == [.9]
    assert 'remaining_depth' not in schema
    for invalid in ('context:missing_id', 'context:discount_factor', 'context:nested.other_id',
                    'context:other_id', 'context:capacity_snapshot_id explanation'):
        with pytest.raises(ValueError, match='out-of-scope'):
            _evidence([invalid], req, tmp_path)
    policy = request(tmp_path, 'policy', {'plans': [{'id': 'plan-a'}], 'evidence_snapshot_id': 'policy-hash'})
    assert output_schema(policy)['properties']['evidence_snapshot_id']['enum'] == ['policy-hash']
    _evidence(['context:evidence_snapshot_id'], policy, tmp_path)


def test_native_worker_rejects_changed_goal_template_before_model_launch(tmp_path, monkeypatch):
    from dataclasses import asdict

    from alpha_agents.tree_search.mutators.coding_agents.codex.roles import prompts
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.adapter import (
        load_native_request,
    )

    request = prompts.RoleRequest("run", "request", "implementer", "artifact", "model", "low",
                                  {"plan_id": "plan", "attempt_id": "attempt"})
    path = tmp_path / "native_request.json"
    path.write_text(json.dumps({**asdict(request), "_prompt_binding": {
        "version": "native-prompt-binding-v1", "library_hash": request.library_hash,
        "prompt_hash": request.prompt_hash}}))
    assert load_native_request(path).library_hash == request.library_hash
    monkeypatch.setattr(prompts, "IMPLEMENTER_GOAL_OBJECTIVE_TEMPLATE", prompts.IMPLEMENTER_GOAL_OBJECTIVE_TEMPLATE + "changed")
    with pytest.raises(ValueError, match="prompt assets differ"):
        load_native_request(path)


def test_native_worker_requires_binding_and_rejects_changed_guidance(tmp_path, monkeypatch):
    from dataclasses import asdict

    from alpha_agents.tree_search.mutators.coding_agents.codex.roles import prompts
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.adapter import (
        load_native_request,
    )

    request = prompts.RoleRequest("run", "request", "proposer", "artifact", "model", "low", {})
    path = tmp_path / "native_request.json"
    path.write_text(json.dumps(asdict(request)))
    with pytest.raises(ValueError, match="exact parent prompt binding"):
        load_native_request(path)
    path.write_text(json.dumps({**asdict(request), "_prompt_binding": {
        "version": "native-prompt-binding-v1", "library_hash": request.library_hash,
        "prompt_hash": request.prompt_hash}}))
    changed = json.loads(json.dumps(prompts.CONTEXT_GUIDANCE))
    changed["utility_context"]["definition"] += "changed"
    monkeypatch.setattr(prompts, "CONTEXT_GUIDANCE", changed)
    with pytest.raises(ValueError, match="prompt assets differ"):
        load_native_request(path)


def _repair_plan_document(failure=None):
    from alpha_agents.tree_search.tests.test_mcts import draft
    document = '## Archive review\n```json\n{"catalog_plan_ids": [], "inspected_plan_ids": [], "gaps": []}\n```\n'
    for suffix in ('a', 'b', 'c'):
        metadata = draft('candidate-'+suffix, 'bounded mechanism '+suffix)
        metadata.pop('document')
        metadata['evidence'] = ['controller.py' if failure != 'path' else 'missing/controller.py']
        if failure == 'metadata':
            metadata.pop('diversity_audit')
        document += '## Candidate candidate-'+suffix+'\n```json\n'+json.dumps(metadata)+'\n```\n### Verification\nInspect and test the bounded mechanism.\n'
    return document+'## Diversity audit\nCompare the three distinct mechanisms and their conditions.\n'


def _plan_repair_client(monkeypatch, documents, clock=None):
    sdk_run = pytest.importorskip('openai_codex._run')
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles import adapter
    monkeypatch.setattr(adapter, '_assert_plan_preset', lambda client: None)
    turns, released = [], []
    def collect(events, turn_id):
        index = int(turn_id.split('-')[-1])
        if clock is not None:
            clock[0] += 11
        return NS(id=turn_id, status='completed', items=[{'type':'plan','text':documents[index]}], usage={'total_tokens':17})
    monkeypatch.setattr(sdk_run, '_collect_turn_result', collect)
    class Client:
        def thread_start(self, params):
            return NS(thread=NS(id='same-planning-thread'))
        def turn_start(self, thread_id, prompt, params):
            turns.append((thread_id,prompt,params))
            return NS(turn=NS(id='turn-'+str(len(turns)-1)))
        def unregister_turn_notifications(self, turn_id):
            released.append(turn_id)
    return Client(), turns, released


@pytest.mark.parametrize('failure', ['metadata','path'])
def test_native_plan_repairs_actual_validation_errors_in_same_thread_and_mode(tmp_path, monkeypatch, failure):
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles import adapter
    (tmp_path/'controller.py').write_text('value = 1\n')
    bad, good = _repair_plan_document(failure), _repair_plan_document()
    client, turns, released = _plan_repair_client(monkeypatch, [bad,good])
    req = legacy_plan_request(tmp_path,'proposer',{'plan_archive':{'plan_ids':[]},'required_plan_count':3})
    deadline = InvocationDeadline(7200)
    end = deadline.ends
    result = adapter._collect_plan(client,req,tmp_path,tmp_path,deadline)
    assert len(result['plans']) == 3 and result['native_document'] == good
    assert len(turns) == 2 and released == ['turn-0','turn-1']
    assert all(t[0]=='same-planning-thread' and t[2]['collaborationMode']['mode']=='plan' for t in turns)
    assert turns[0][2] == turns[1][2] and req.render() in turns[1][1]
    assert 'validation_error' in turns[1][1] and 'same persisted planning thread' in ' '.join(turns[1][1].split())
    assert deadline.ends == end
    assert (tmp_path/'native_plan_attempt_0.md').read_text() == bad
    assert (tmp_path/'native_plan_attempt_1.md').read_text() == good
    assert json.loads((tmp_path/'native_result.json').read_text())['turn_id'] == 'turn-1'


def test_native_plan_repair_remains_bounded_and_preserves_rejected_drafts(tmp_path,monkeypatch):
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles import adapter
    (tmp_path/'controller.py').write_text('value = 1\n')
    bad = _repair_plan_document('metadata')
    client,turns,released = _plan_repair_client(monkeypatch,[bad]*3)
    req=legacy_plan_request(tmp_path,'proposer',{'plan_archive':{'plan_ids':[]},'required_plan_count':3})
    with pytest.raises(ValueError,match='Incomplete detailed candidate plan'):
        adapter._collect_plan(client,req,tmp_path,tmp_path,InvocationDeadline(7200))
    assert len(turns)==3 and len(released)==3
    assert all((tmp_path/f'plan_validation_failure_{i}.json').is_file() for i in range(3))
    assert not (tmp_path/'result.json').exists()


def test_native_plan_expired_original_deadline_prevents_repair_turn(tmp_path,monkeypatch):
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles import adapter
    (tmp_path/'controller.py').write_text('value = 1\n')
    clock=[0.0]
    client,turns,_ = _plan_repair_client(monkeypatch,[_repair_plan_document('metadata')],clock)
    req=legacy_plan_request(tmp_path,'proposer',{'plan_archive':{'plan_ids':[]},'required_plan_count':3})
    deadline=InvocationDeadline(10,lambda:clock[0])
    with pytest.raises(TimeoutError,match='Original Codex'):
        adapter._collect_plan(client,req,tmp_path,tmp_path,deadline)
    assert len(turns)==1 and deadline.ends==10


def test_native_plan_repair_instructions_contribute_to_prompt_identity(tmp_path,monkeypatch):
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles import prompts
    req=request(tmp_path,'proposer',{'plan_archive':{'plan_ids':[]}})
    original=req.library_hash
    monkeypatch.setattr(prompts,'PLAN_REPAIR_TEMPLATE',prompts.PLAN_REPAIR_TEMPLATE+' changed')
    assert req.library_hash != original
