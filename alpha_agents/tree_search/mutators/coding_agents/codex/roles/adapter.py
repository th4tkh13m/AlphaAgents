"""Native Plan/Goal worker, isolated under one outer invocation deadline.

Uses the pinned SDK's raw app-server parameter passthrough for experimental
Plan mode and its logical persisted-goal collection for /goal. No model or
reasoning defaults and no ordinary-turn fallback are provided.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path

from alpha_agents.tree_search.core.mcts.responses import validate_response
from alpha_agents.tree_search.core.shared.storage import write_json
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.codex import (
    CodexRuntime,
)
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.permissions import (
    PROFILE,
    permission_overrides,
    reject_legacy_sandbox_layers,
    role_permissions,
    validate_effective_permissions,
)
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.prompts import (
    GOAL_OBJECTIVE_TEMPLATE,
    PLAN_REPAIR_TEMPLATE,
    RoleRequest,
)
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.responses import (
    parse_json_response,
    parse_native_plan,
)
from alpha_agents.tree_search.mutators.coding_agents.codex.sdk_serialization import (
    to_jsonable as jsonable,
)


class InvocationDeadline:
    def __init__(self, seconds: float, clock=time.monotonic):
        self.clock = clock
        self.started = clock()
        self.ends = self.started + seconds

    def remaining(self) -> float:
        return max(0.0, self.ends - self.clock())

    def require_remaining(self):
        if self.remaining() <= 0:
            raise TimeoutError("Original Codex invocation deadline exhausted")




def goal_objective(request: RoleRequest, context_path: Path) -> str:
    """A developed completion contract; the complete detailed prompt stays intact."""
    if request.execution_mode != "goal":
        raise ValueError("Only the implementation engineer receives a native goal")
    objective = GOAL_OBJECTIVE_TEMPLATE.format(
        plan_id=request.context["plan_id"], attempt_id=request.context["attempt_id"],
        artifact_id=request.artifact_id, context_path=context_path)
    if len(objective) > 4000:
        raise ValueError("Goal identity/path exceeds native objective field limit; full prompt was not truncated")
    return objective


def decline_server_request(method, params):
    if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
        return {"decision": "decline"}
    if method == "item/tool/requestUserInput":
        return {"answers": {}}
    # Unknown interactive server requests must not grant new permissions.
    return {}


def plan_payload(request: RoleRequest, workspace: Path) -> dict:
    if request.execution_mode != "plan":
        raise ValueError("Not a native Plan-mode request")
    return {"cwd": str(workspace), "model": request.model, "effort": request.effort,
            "approvalPolicy": "never", "permissions": PROFILE,
            "collaborationMode": {"mode": "plan", "settings": {
                "model": request.model, "reasoning_effort": request.effort, "developer_instructions": None}}}


def _client(runtime, workspace, profile=None):
    from openai_codex.client import CodexClient, CodexConfig
    return CodexClient(CodexConfig(codex_bin=runtime.codex_bin, cwd=str(workspace),
                                  config_overrides=tuple([*runtime.overrides(), *(permission_overrides(profile) if profile else [])]), experimental_api=True,
                                  client_name="alpha_agents_fixed_puct", client_title="AlphaAgents fixed PUCT"),
                       approval_handler=decline_server_request)


def _assert_plan_preset(client):
    from pydantic import BaseModel, ConfigDict
    class Presets(BaseModel):
        model_config = ConfigDict(extra="allow")
    response = client.request("collaborationMode/list", {}, response_model=Presets).model_dump()
    presets = response.get("data", [])
    if not any(preset.get("mode") == "plan" for preset in presets):
        raise RuntimeError("Native Plan preset unavailable; no ordinary-turn fallback")


def _collect_plan(client, request, workspace, output, deadline=None):
    from openai_codex._run import _collect_turn_result
    from openai_codex.generated.v2_all import TurnCompletedNotification

    deadline = deadline if deadline is not None else InvocationDeadline(request.timeout_seconds)
    _assert_plan_preset(client)
    started = client.thread_start({"cwd": str(workspace), "model": request.model, "permissions": PROFILE,
                                   "approvalPolicy": "never", "ephemeral": False,
                                   "config": {"model_reasoning_effort": request.effort}})
    thread_id = started.thread.id
    write_json(output / "native_session.json", {"thread_id": thread_id, "execution_mode": "plan",
                                               "request_id": request.request_id, "prompt_hash": request.prompt_hash,
                                               "deadline_epoch": time.time() + deadline.remaining()})
    instruction = request.render()
    for attempt in range(3):
        deadline.require_remaining()
        turn = client.turn_start(thread_id, instruction, plan_payload(request, workspace))
        def events():
            with (output / "events.jsonl").open("a") as stream:
                while True:
                    event = client.next_turn_notification(turn.turn.id)
                    stream.write(json.dumps(jsonable(event)) + "\n")
                    stream.flush()
                    yield event
                    if isinstance(event.payload, TurnCompletedNotification) and event.payload.turn.id == turn.turn.id:
                        break
        try:
            result = _collect_turn_result(events(), turn_id=turn.turn.id)
        finally:
            client.unregister_turn_notifications(turn.turn.id)
        record = {"thread_id": thread_id, "turn_id": result.id,
                  "execution_mode": "plan", "status": jsonable(result.status),
                  "items": jsonable(result.items), "usage": jsonable(result.usage)}
        write_json(output / "native_result.json", record)
        write_json(output / f"native_result_attempt_{attempt}.json", record)
        if jsonable(result.status) != "completed":
            raise RuntimeError("Native planning turn did not complete")
        plan_items = [jsonable(item) for item in result.items if jsonable(item).get("type") == "plan"]
        if not plan_items:
            raise ValueError("Completed native Plan item is missing; agent-message/delta text is not authoritative")
        document = plan_items[-1]["text"]
        (output / "native_plan.md").write_text(document)
        (output / f"native_plan_attempt_{attempt}.md").write_text(document)
        deadline.require_remaining()
        try:
            response = parse_native_plan(document, request)
            validate_response(response, request, workspace)
        except ValueError as error:
            feedback = {"validation_error": str(error), "failed_attempt": attempt,
                        "request_id": request.request_id, "artifact_id": request.artifact_id,
                        "remaining_seconds": deadline.remaining()}
            write_json(output / f"plan_validation_failure_{attempt}.json", feedback)
            if attempt == 2:
                raise
            instruction = PLAN_REPAIR_TEMPLATE.replace("[[VALIDATION_ERROR_JSON]]", json.dumps(feedback, indent=2))
            instruction = instruction.replace("[[ORIGINAL_REQUEST]]", request.render())
            (output / f"plan_repair_prompt_{attempt}.md").write_text(instruction)
            continue
        return response
    raise AssertionError("Native plan repair loop failed to settle")


def _collect_goal(client, request, workspace, output, deadline, restart=None):
    from openai_codex.api import Thread

    from alpha_agents.tree_search.mutators.coding_agents.codex.execution import (
        _goal_snapshot,
        _preflight_launcher,
        _require_idle_thread,
        run_watched_goal,
    )
    context_path = output / "goal_context.md"
    context_path.write_text(request.render())
    objective = goal_objective(request, context_path)
    # The supplied full prompt is preserved both in the persisted thread and in
    # the context file, so the goal field limit never shortens its requirements.
    started = client.thread_start({"cwd": str(workspace), "model": request.model,
                                   "permissions": PROFILE, "approvalPolicy": "never", "ephemeral": False,
                                   "developerInstructions": request.render(),
                                   "config": {"model_reasoning_effort": request.effort}})
    thread_id = started.thread.id
    write_json(output / "native_session.json", {"thread_id": thread_id, "execution_mode": "goal",
                                               "request_id": request.request_id, "goal_objective": objective,
                                               "prompt_hash": request.prompt_hash, "goal_context_file": str(context_path),
                                               "deadline_epoch": time.time() + deadline.remaining()})
    result, incident, goal = run_watched_goal(Thread(client, thread_id), objective)
    write_json(output / "goal_state.json", jsonable(goal))
    if incident is not None:
        # Keep the same persisted goal/accounting and original deadline. Reuse
        # the existing watcher helper, not its legacy prompt/default model.
        for recovery in range(2):
            deadline.require_remaining()
            if (jsonable(goal.status) != "paused" or goal.objective != objective
                    or getattr(goal, "thread_id", None) != thread_id):
                raise RuntimeError("Launcher recovery would replace or reset the stored goal")
            _require_idle_thread(client, thread_id, min(10, deadline.remaining()))
            if restart is None:
                raise RuntimeError("Native Goal recovery requires a fresh app-server; no turn fallback")
            client = restart(client)
            deadline.require_remaining()
            resumed = client.thread_resume(thread_id, {
                "cwd": str(workspace), "model": request.model, "permissions": PROFILE,
                "approvalPolicy": "never", "config": {"model_reasoning_effort": request.effort}})
            if resumed.thread.id != thread_id:
                raise RuntimeError("Native Goal recovery reopened a different thread")
            stored = _goal_snapshot(client, thread_id)
            if (stored is None or jsonable(stored.status) != "paused" or stored.objective != objective
                    or getattr(stored, "thread_id", None) != thread_id):
                raise RuntimeError("Native Goal recovery changed its stored objective or lifecycle")
            _preflight_launcher(client)
            result, incident, goal = run_watched_goal(Thread(client, thread_id), None, resume=True)
            write_json(output / f"goal_recovery_{recovery}.json", {"incident": incident, "goal": jsonable(goal),
                                                                 "remaining_seconds": deadline.remaining()})
            if incident is None:
                break
        if incident is not None:
            raise RuntimeError("Codex goal launcher failed after bounded same-thread recovery")
    # A native continuation can mark itself blocked while its already admitted
    # Android diagnostic is still running. Wait for that external state change
    # and resume the same persisted goal, without creating a new objective or
    # resetting the invocation's deadline/accounting.
    recovery = 0
    while (goal is not None and jsonable(goal.status) == "blocked"
           and wait_for_pending_check(request, output, deadline)):
        deadline.require_remaining()
        stored = _goal_snapshot(client, thread_id)
        if (stored is None or jsonable(stored.status) != "blocked"
                or stored.objective != objective
                or getattr(stored, "thread_id", None) != thread_id):
            raise RuntimeError("Diagnostic recovery changed the stored goal identity or lifecycle")
        _require_idle_thread(client, thread_id, min(10, deadline.remaining()))
        result, incident, goal = run_watched_goal(Thread(client, thread_id), None, resume=True)
        write_json(output / f"diagnostic_goal_recovery_{recovery}.json", {
            "incident": incident, "goal": jsonable(goal),
            "remaining_seconds": deadline.remaining(),
        })
        recovery += 1
        if incident is not None:
            raise RuntimeError("Native goal launcher failed during diagnostic recovery")
    # Reading current durable status prevents an intermediate physical turn
    # from being treated as completed engineering.
    goal = _goal_snapshot(client, thread_id)
    write_json(output / "goal_state.json", jsonable(goal))
    physical_turns = client.thread_read(thread_id, include_turns=True).thread.turns
    write_json(output / "native_result.json", {"thread_id": thread_id, "turn_id": result.id,
                                             "execution_mode": "goal", "goal": jsonable(goal),
                                             "physical_turns": jsonable(physical_turns),
                                             "items": jsonable(result.items), "usage": jsonable(result.usage)})
    if (goal is None or getattr(goal, "objective", None) != objective
            or getattr(goal, "thread_id", None) != thread_id):
        raise RuntimeError("Final native Goal no longer matches the invocation's stored objective and thread")
    response = parse_json_response(result.final_response or "")
    if jsonable(goal.status) != "complete" or jsonable(result.status) != "completed":
        if response.get("status") == "completed":
            response["status"] = "partial"
        response.setdefault("blockers", []).append(f"Native goal stopped with status {jsonable(goal.status)}")
    return response


def wait_for_pending_check(request, output, deadline, *, sleep=time.sleep):
    """Await an admitted check's terminal receipt; never treat it as a reward."""
    channel = request.context.get("device_check_channel")
    if not isinstance(channel, dict):
        return False
    root = Path(output) / "check_results"
    if Path(channel["result_directory"]) != root:
        raise ValueError("Diagnostic recovery requires the invocation-owned result channel")
    path = root / "latest.json"
    if not path.is_file() or path.is_symlink():
        return False
    pending = json.loads(path.read_text())
    if pending.get("status") != "pending":
        return False
    expected = {
        "check_id": pending.get("check_id"),
        "invocation_id": request.request_id,
        "attempt_id": request.context["attempt_id"],
        "artifact_id": pending.get("artifact_id"),
        "sample_kind": "targeted",
    }
    if (not expected["check_id"] or not expected["artifact_id"]
            or any(pending.get(key) != value for key, value in expected.items())):
        raise ValueError("Pending diagnostic does not match the native invocation")
    while True:
        deadline.require_remaining()
        if path.is_symlink():
            raise ValueError("Diagnostic receipt cannot be a symbolic link")
        receipt = json.loads(path.read_text())
        if any(receipt.get(key) != value for key, value in expected.items()):
            raise ValueError("Diagnostic receipt changed its invocation or artifact identity")
        if receipt.get("status") in {"completed", "failed"}:
            return True
        if receipt.get("status") != "pending":
            raise ValueError("Admitted diagnostic returned an unknown lifecycle status")
        sleep(min(1, deadline.remaining()))


def run_native(request: RoleRequest, runtime: CodexRuntime, workspace: Path, output: Path):
    deadline = InvocationDeadline(request.timeout_seconds)
    output.mkdir(parents=True, exist_ok=True)
    if request.role not in {"proposer", "implementer"}:
        raise ValueError("Native worker is reserved for Plan/Goal roles")
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.permissions import (
        runtime_permissions,
    )
    profile = runtime_permissions(role_permissions(request, workspace), runtime.codex_bin,
                                  runtime.verification_python if request.role == "implementer" else None)
    write_json(output / "permissions.json", profile)
    client = _client(runtime, workspace, profile)
    current_client = [client]
    ended = threading.Event()
    expired = threading.Event()
    def watchdog():
        if not ended.wait(deadline.remaining()):
            expired.set()
            # Do not let a failed cancellation RPC extend the original deadline.
            # The outer supervisor also kills/joins the entire worker group.
            def interrupt():
                try:
                    session_path = output / "native_session.json"
                    if session_path.exists():
                        thread_id = json.loads(session_path.read_text())["thread_id"]
                        if request.execution_mode == "goal":
                            current_client[0].pause_goal(thread_id)
                        thread = current_client[0].thread_read(thread_id, include_turns=True).thread
                        active = next((turn for turn in thread.turns if jsonable(turn.status) == "inProgress"), None)
                        if active:
                            current_client[0].turn_interrupt(thread_id, active.id)
                except Exception:
                    pass
            cancel = threading.Thread(target=interrupt, daemon=True)
            cancel.start()
            cancel.join(timeout=1)
            current_client[0].close()
    timer = threading.Thread(target=watchdog, daemon=True)
    timer.start()
    try:
        client.start()
        initialize = client.initialize()
        write_json(output / "runtime_metadata.json", jsonable(initialize))
        deadline.require_remaining()
        def restart(previous):
            deadline.require_remaining()
            previous.close()
            replacement = _client(runtime, workspace, profile)
            current_client[0] = replacement
            replacement.start()
            initialized = replacement.initialize()
            with (output / "transport_recovery.jsonl").open("a") as stream:
                stream.write(json.dumps({"runtime": jsonable(initialized),
                                         "remaining_seconds": deadline.remaining()}) + "\n")
            deadline.require_remaining()
            return replacement
        result = (_collect_plan(client, request, workspace, output, deadline) if request.execution_mode == "plan"
                  else _collect_goal(client, request, workspace, output, deadline, restart))
        if expired.is_set():
            raise TimeoutError("Original two-hour native invocation deadline reached")
        validate_response(result, request, workspace)
        write_json(output / "result.json", result)
    finally:
        ended.set()
        timer.join(timeout=2)
        current_client[0].close()


def _probe_thread_sandbox(client, workspace):
    """Initialize the actual filesystem sandbox without a model turn or goal."""
    started = client.thread_start({"cwd": str(workspace), "permissions": PROFILE,
                                   "approvalPolicy": "never", "ephemeral": True})
    if not started.thread.id:
        raise RuntimeError("Sandbox preflight did not create an ephemeral thread")


def load_native_request(path: Path) -> RoleRequest:
    payload = json.loads(path.read_text())
    binding = payload.pop("_prompt_binding", None)
    if not isinstance(binding, dict) or binding.get("version") != "native-prompt-binding-v1":
        raise ValueError("Native request requires an exact parent prompt binding")
    request = RoleRequest(**payload)
    if (binding.get("library_hash") != request.library_hash
            or binding.get("prompt_hash") != request.prompt_hash):
        raise ValueError("Worker prompt assets differ from the admitted invocation")
    return request


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    runtime = CodexRuntime(**json.loads(args.runtime.read_text()))
    if args.preflight:
        profile = {"name": PROFILE, "filesystem": {":minimal": "read", str(args.workspace.resolve()): "read"},
                   "network_enabled": False}
        from alpha_agents.tree_search.mutators.coding_agents.codex.roles.permissions import (
            runtime_permissions,
        )
        profile = runtime_permissions(profile, runtime.codex_bin, runtime.verification_python)
        client = _client(runtime, args.workspace.resolve(), profile)
        try:
            client.start()
            initialize = client.initialize()
            _assert_plan_preset(client)
            from openai_codex.generated.v2_all import ConfigReadResponse
            configuration = jsonable(client.request("config/read", {
                "includeLayers": True, "cwd": str(args.workspace.resolve())}, response_model=ConfigReadResponse))
            if configuration.get("layers") is None:
                raise RuntimeError("Cannot verify permission configuration provenance")
            reject_legacy_sandbox_layers(configuration["layers"])
            validate_effective_permissions(configuration.get("config", {}), profile)
            if not any(layer.get("config", {}).get("permissions", {}).get(PROFILE)
                       for layer in configuration["layers"] if isinstance(layer.get("config"), dict)):
                raise RuntimeError("Runtime did not load the configured role permission profile")
            if not all(hasattr(client, method) for method in (
                "start_goal_operation", "reserve_goal_operation", "thread_goal_set", "cancel_goal_operation")):
                raise RuntimeError("SDK lacks native persisted Goal lifecycle support")
            _probe_thread_sandbox(client, args.workspace.resolve())
            write_json(args.output / "preflight.json", {
                "runtime": jsonable(initialize), "native_plan_preset": True, "native_goal_sdk": True, "legacy_sandbox_layers": False, "role_permission_profile_loaded": True,
                "ephemeral_thread_sandbox_initialized": True, "model_turns_started": 0})
        finally:
            client.close()
        return
    if args.request is None:
        parser.error("--request is required unless --preflight is selected")
    request = load_native_request(args.request)
    run_native(request, runtime, args.workspace.resolve(), args.output.resolve())
