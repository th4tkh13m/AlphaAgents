"""Replacement integrations, lifecycle ownership, and compatibility boundaries."""

import json
import subprocess
import sys
import threading
from dataclasses import asdict
from pathlib import Path

import pytest

from alpha_agents.tree_search.core.mcts.contracts import (
    FixedPUCTConfig,
    Objective,
    fingerprint,
)
from alpha_agents.tree_search.core.mcts.coordinator import FixedPUCTCoordinator
from alpha_agents.tree_search.core.mcts.ports import (
    EvaluationAdapter,
    RoleAdapter,
    RoleSettings,
    StageEvidence,
)


class LocalRequest:
    """A non-dataclass request independent of any model SDK or prompt library."""

    permission_mode = "local"
    collaboration_mode = "default"
    execution_mode = "local"
    timeout_seconds = 7200
    prompt_version = "local-v1"
    library_hash = fingerprint({"integration": "local-increment-v1"})

    def __init__(self, run_id, request_id, role, artifact_id, model, effort, context):
        self.run_id, self.request_id, self.role = run_id, request_id, role
        self.artifact_id, self.model, self.effort = artifact_id, model, effort
        self.context = context

    def to_record(self):
        return {name: getattr(self, name) for name in (
            "run_id", "request_id", "role", "artifact_id", "model", "effort", "context",
            "timeout_seconds", "prompt_version")}

    def render(self):
        return json.dumps(self.to_record(), sort_keys=True)

    @property
    def prompt_hash(self):
        return fingerprint(self.to_record())


class LocalAgent(RoleAdapter):
    prepare_request = staticmethod(LocalRequest)

    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def invoke(self, request, workspace):
        if request.role in {"proposer", "policy"}:
            assert request.context["discount_factor"] == .9
            assert request.context["continuation_policy_id"] == fingerprint(request.context["continuation_policy"])
        if request.role.startswith("judge_"):
            assert "discount_factor" not in request.context
            assert "continuation_policy" not in request.context
        with self.lock:
            self.calls.append(request.role)
        context, role = request.context, request.role
        result = {"artifact_id": request.artifact_id}
        if role == "proposer":
            result["plans"] = [{
                "id": f"increment-{i}", "document": f"Implement bounded increment {i}.",
                "intervention": {"target": "controller", "operation": "increment",
                    "conditions": "verified observation", "parameters": {"amount": i + 1},
                    "scope": ["controller.py"], "mechanism": f"increment {i} at {request.artifact_id}"},
                "novelty_status": "new_action", "novelty": "Distinct bounded state update",
                "diversity_audit": "Compared all update amounts", "closest_plan_ids": [],
                "evidence": ["controller.py"],
            } for i in range(3)]
            catalog = context["plan_archive"]["plan_ids"]
            result["archive_review"] = {"catalog_plan_ids": catalog, "inspected_plan_ids": catalog, "gaps": []}
        elif role == "policy":
            ids = [plan["id"] for plan in context["plans"]]
            result.update(priors={id: 1 for id in ids}, plan_ids=ids,
                evidence_snapshot_id=context["evidence_snapshot_id"], comparisons="Compare state increments",
                uncertainty="Deterministic local evaluator", evidence=["controller.py"])
        elif role == "implementer":
            path = workspace / "controller.py"
            current = int(path.read_text().split("=")[1])
            path.write_text(f"value = {current + 1}\n")
            result.update(status="completed", plan_id=context["plan_id"], attempt_id=context["attempt_id"],
                changes=["Increment state"], blockers=[], evidence=["controller.py"],
                verification=[{"ran": True, "status": "completed", "evidence": ["controller.py"]}])
        elif role.startswith("judge_"):
            result.update(status="completed", phase="code" if role == "judge_code" else "execution",
                rubric_version=context["rubric"], findings=["Inspected state update"],
                coverage={"inspected": ["controller.py"], "unseen": []})
            if role == "judge_execution":
                result.update(canonical_evidence_id=context["canonical_evidence"]["snapshot_id"],
                    dimensions={dimension: {"score": .75, "rationale": "Bounded state update",
                        "confidence": "high", "evidence": ["controller.py"]}
                        for dimension in ("robustness", "decisions", "generalizable")})
        elif role == "value":
            result.update(status="completed", value=0, score_snapshot_id=context["score_snapshot_id"],
                capacity_snapshot_id=context["capacity_snapshot_id"], objective_id=context["objective_id"],
                discount_factor=context["discount_factor"], continuation_policy_id=context["continuation_policy_id"], rationale="No further predicted gain",
                uncertainty="Local fixture", evidence=["controller.py"])
        return result


class LocalEvaluator(EvaluationAdapter):
    def evaluate(self, artifact, workspace, stage, tasks):
        value = int((workspace / "controller.py").read_text().split("=")[1])
        return StageEvidence(artifact, stage, {task: stage == "screen" or value > 0 for task in tasks},
                             ("local-trace",))


def local_search(root, agent=None, evaluator=None):
    source = root / "source"
    source.mkdir(exist_ok=True)
    (source / "controller.py").write_text("value = 0\n")
    return FixedPUCTCoordinator(source, root / "run",
        FixedPUCTConfig(screen_gate="execution_valid", max_trials=2, max_new_attempts_per_trial=1),
        Objective("local-subset", "local-rubric", "local-judge"), agent or LocalAgent(), evaluator or LocalEvaluator(),
        RoleSettings("local-model", "local-effort"), ("screen",), {"a": 1}, ("a", "b"),
        {"tasks": [{"task": "a", "weight": {"display": 1}}], "coverage": {}},
        {"agent": "local-increment-v1", "evaluator": "local-evaluator-v1"})


def test_complete_replacement_search_resume_and_export_without_loading_integrations(tmp_path):
    script = """
import json
import sys
from pathlib import Path
from alpha_agents.tree_search.tests.test_mcts_ports import local_search
root = Path(sys.argv[1])
search = local_search(root)
try:
    first = search.run(stop_after_trials=1, finalize=False)
    assert first['status']['backups'] == 1, first
    assert first['status']['role_invocations'] == first['status']['codex_invocations']
finally:
    search.close()
search = local_search(root)
try:
    result = search.run(resume=True)
    assert result['selection_status'] == 'completed', result
    assert result['status']['backups'] == 2
    assert result['status']['role_invocations']['reserved'] == 0
    assert search.store.score('root').u == 0
    assert (search.directory / 'winner' / 'controller.py').read_text() == 'value = 1\\n'
    assert all(row['resource'] == 'role_execution' for row in result['status']['costs'])
    requests = [json.loads(p.read_text()) for p in (search.directory / 'role_requests').glob('*/request.json')]
    assert all('device_check_channel' not in r['context'] for r in requests)
    assert next(r for r in requests if r['role'] == 'value')['context']['continuation_role_protocol']['implementer_mode'] == 'in_process'
    spent = search.store.budget()['spent']
    search.finalize()
    assert search.store.budget()['spent'] == spent
finally:
    search.close()
assert not any(name.startswith(('openai_codex', 'alpha_agents.tree_search.mutators',
    'alpha_agents.tree_search.bridges')) for name in sys.modules)
"""
    subprocess.run([sys.executable, "-c", script, str(tmp_path)], check=True, timeout=60)


def test_declared_session_preparation_verification_and_recovery_hooks(tmp_path):
    events = []

    class Evaluator(LocalEvaluator):
        def preflight(self, source):
            events.append(("preflight", source))
            return {"ready": True}

        def for_owner(self, owner):
            events.append(("owner", owner))
            return self

        def prepare(self, artifact, stages):
            events.append(("prepare", artifact, stages))

        def verify_stage(self, evidence):
            events.append(("verify", evidence.identity))

        def reconcile_interrupted(self, process_dead):
            events.append(("recover", process_dead(tmp_path / "never-launched.json")))

    search = local_search(tmp_path, evaluator=Evaluator())
    try:
        search.run(stop_after_trials=0, finalize=False)
        search.run(resume=True, stop_after_trials=0, finalize=True)
        search.finalize()
        assert ("recover", True) in events
        assert ("owner", "baseline") in events and ("owner", "final") in events
        assert any(event[0] == "prepare" for event in events)
        assert any(event[0] == "verify" for event in events)
        assert json.loads((search.directory / "preflight.json").read_text())["evaluation"] == {"ready": True}
    finally:
        search.close()


@pytest.mark.parametrize("agent_checks,evaluator_checks,expected", [(False, True, False), (True, False, False), (True, True, True)])
def test_diagnostics_require_both_declared_capabilities(tmp_path, agent_checks, evaluator_checks, expected):
    class Agent(LocalAgent):
        supports_checks = agent_checks

        def invoke(self, request, workspace):
            if request.role == "implementer":
                assert ("device_check_channel" in request.context) is expected
            return super().invoke(request, workspace)

        def invoke_with_checks(self, request, workspace, service):
            assert expected
            result = self.invoke(request, workspace)
            service.validate_handoff(result)
            return result

    class Evaluator(LocalEvaluator):
        supports_checks = evaluator_checks

    search = local_search(tmp_path, agent=Agent(), evaluator=Evaluator())
    try:
        result = search.run(stop_after_trials=1, finalize=False)
        assert result["status"]["backups"] == 1, result
    finally:
        search.close()


def test_unsupported_checks_fail_explicitly():
    with pytest.raises(NotImplementedError, match="does not support"):
        LocalAgent().invoke_with_checks(None, Path("."), None)


@pytest.mark.parametrize("generic,historical", [("role_invocation_cap", "codex_invocation_cap"),
                                               ("role_timeout_seconds", "codex_run_timeout_seconds")])
def test_generic_config_names_preserve_frozen_schema(generic, historical):
    value = 2000 if generic == "role_invocation_cap" else 7200
    old = FixedPUCTConfig(**{"screen_gate": "execution_valid", historical: value})
    new = FixedPUCTConfig.from_mapping({"screen_gate": "execution_valid", generic: value})
    assert getattr(new, generic) == value
    assert asdict(new) == asdict(old) and new.identity == old.identity
    with pytest.raises(ValueError, match="Conflicting"):
        FixedPUCTConfig.from_mapping({"screen_gate": "execution_valid", generic: value, historical: value + 1})


@pytest.mark.parametrize("prepared", [False, True])
def test_codex_context_and_request_binding_match_previous_protocol(tmp_path, prepared):
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.codex import (
        CodexGateway,
        CodexRuntime,
    )
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.prompts import (
        RoleRequest,
    )

    runtime = CodexRuntime("/sdk/python", "/codex", "openai", "https://example.test/v1", "codex_login",
                           verification_python=str(tmp_path / "venv/bin/python") if prepared else None)
    gateway = CodexGateway(runtime)
    tools = runtime.implementation_tools_context()
    common = {"version": "continuation-role-protocol-v1", "model": "model", "effort": "low",
              "invocation_deadline_seconds": 7200, "verification_scope_contract": {}, "novelty_admission_contract": {}}
    before = {"continuation_role_protocol": {**common, "proposer_mode": "native_plan", "implementer_mode": "native_goal",
        "implementation_verification": {"prepared_environment_configured": bool(tools), **tools}}}
    after = gateway.enrich_context("value", {"continuation_role_protocol": common})
    assert after == before
    args = (str(tmp_path), "call-fixed", "value", "artifact", "model", "low")
    old, new = RoleRequest(*args, before), gateway.prepare_request(*args, after)
    assert new.to_record() == asdict(old)
    assert new.render() == old.render() and new.prompt_hash == old.prompt_hash and new.library_hash == old.library_hash
    assert gateway.enrich_context("implementer", {"plan": "frozen"}) == {"plan": "frozen", **tools}
    assert gateway.enrich_context("judge_code", {"artifact": "exact"}) == {"artifact": "exact"}


@pytest.mark.parametrize("failure", [None, "runtime", "scheduler-close"])
def test_application_closes_external_resources_before_state_once(failure):
    from alpha_agents.tree_search.composition import SearchApplication

    events = []

    class Scheduler:
        def close(self):
            events.append("scheduler-close")
            if failure == "scheduler-close":
                raise RuntimeError(failure)

    class Coordinator:
        def run(self):
            events.append("run")
            if failure == "runtime":
                raise RuntimeError(failure)
            return {"completed": True}

        def close(self):
            events.append("state-close")

    app = SearchApplication(Coordinator(), Scheduler())
    if failure:
        with pytest.raises(RuntimeError, match=failure):
            with app:
                app.run()
    else:
        with app:
            assert app.run() == {"completed": True}
    app.close()
    assert events == ["run", "scheduler-close", "state-close"]


@pytest.mark.parametrize("failure", [None, "evaluator", "coordinator", "start"])
def test_production_build_owns_resources_and_cleans_partial_construction(tmp_path, monkeypatch, failure):
    from alpha_agents.tree_search import composition

    config = json.loads((Path(__file__).parents[1] / "examples/fixed_puct.json").read_text())
    config["harness"]["source"] = str(tmp_path / "source")
    config["evaluation"]["manifest"] = str(tmp_path / "manifest.json")
    (tmp_path / "manifest.json").write_text("{}")
    subset = {"manifest_id": "subset", "screen": ["screen"], "selection": ["a"],
              "protected_confirmation": ["held-out"], "tasks": [{"task": "a", "weight": {"numerator": 1, "denominator": 1}}]}
    monkeypatch.setattr(composition, "prepare", lambda *args: subset)
    monkeypatch.setattr(composition, "observe_androidworld", lambda *args: {})
    monkeypatch.setattr(composition, "trusted_runner_revision", lambda *args: "trusted")
    monkeypatch.setattr(composition, "TaskCache", lambda *args: object())
    events = []

    class Scheduler:
        def __init__(self, devices, autostart, implementor_limit, recover_device):
            assert autostart is False
            assert implementor_limit == 9
            assert callable(recover_device)

        def start(self):
            events.append("start")
            if failure == "start":
                raise RuntimeError(failure)

        def close(self):
            events.append("scheduler-close")

    def evaluator(*args):
        if failure == "evaluator":
            raise RuntimeError(failure)
        return object()

    class Coordinator:
        def __init__(self, *args):
            if failure == "coordinator":
                raise RuntimeError(failure)

        def close(self):
            events.append("state-close")

    monkeypatch.setattr(composition, "DeviceScheduler", Scheduler)
    monkeypatch.setattr(composition, "StagedArtemisEvaluator", evaluator)
    monkeypatch.setattr(composition, "FixedPUCTCoordinator", Coordinator)
    if failure:
        with pytest.raises(RuntimeError, match=failure):
            composition.build(config, tmp_path / "run")
    else:
        app = composition.build(config, tmp_path / "run")
        assert not hasattr(app.coordinator, "scheduler")
        app.close()
        app.close()
    assert events.count("scheduler-close") == 1
    assert events.count("state-close") == (1 if failure in {None, "start"} else 0)


def test_coordinator_closes_state_when_artifact_initialization_fails(tmp_path, monkeypatch):
    import sqlite3

    from alpha_agents.tree_search.core.mcts import coordinator
    from alpha_agents.tree_search.core.mcts.storage import TreeStore

    opened = []

    def store(*args):
        result = TreeStore(*args)
        opened.append(result)
        return result

    def fail_artifacts(*args):
        raise OSError("Artifact directory unavailable")

    monkeypatch.setattr(coordinator, "TreeStore", store)
    monkeypatch.setattr(coordinator, "ArtifactStore", fail_artifacts)
    with pytest.raises(OSError, match="Artifact directory"):
        local_search(tmp_path)
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].db.execute("SELECT 1")


def test_rejected_frozen_identity_closes_its_new_connection(tmp_path, monkeypatch):
    import sqlite3

    from alpha_agents.tree_search.core.mcts import storage

    config = FixedPUCTConfig(screen_gate="execution_valid")
    objective = Objective("subset", "rubric", "judge")
    original = storage.TreeStore(tmp_path, config, objective)
    original.close()
    opened = []
    connect = sqlite3.connect

    def capture(*args, **kwargs):
        connection = connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(storage.sqlite3, "connect", capture)
    with pytest.raises(ValueError, match="identical frozen"):
        storage.TreeStore(tmp_path, config, Objective("different-subset", "rubric", "judge"))
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
