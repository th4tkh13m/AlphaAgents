"""Complete coordinator lifecycle with real artifacts and deterministic ports."""

import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from alpha_agents.tree_search.core.mcts.artifacts import manifest
from alpha_agents.tree_search.core.mcts.contracts import FixedPUCTConfig, Objective
from alpha_agents.tree_search.core.mcts.controller import RoleSettings, StageEvidence
from alpha_agents.tree_search.core.mcts.coordinator import FixedPUCTCoordinator
from alpha_agents.tree_search.core.mcts.finalization import Finalizer, shortlist
from alpha_agents.tree_search.core.mcts.ports import EvaluationAdapter, RoleAdapter
from alpha_agents.tree_search.core.mcts.recovery import reconcile_process
from alpha_agents.tree_search.infrastructure.process import process_birth
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.prompts import (
    RoleRequest,
)
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.telemetry import (
    role_usage,
)
from alpha_agents.tree_search.tests.test_mcts import contract_response, draft


class Gateway(RoleAdapter):
    prepare_request = staticmethod(RoleRequest)
    observed_usage = staticmethod(role_usage)
    def __init__(self):
        self.calls, self.implementations = [], 0
        self.lock = threading.Lock()
    def invoke(self, req, workspace):
        with self.lock:
            self.calls.append(req)
        result = {"artifact_id": req.artifact_id}
        if req.role == "proposer":
            catalog = req.context["plan_archive"]["plan_ids"]
            result.update(plans=[draft(f"candidate-{i}", mechanism=f"recovery mechanism {i} for {req.artifact_id}") for i in range(3)],
                          archive_review={"catalog_plan_ids": catalog, "inspected_plan_ids": catalog, "gaps": []})
        elif req.role == "policy":
            result["priors"] = {plan["id"]: 1 for plan in req.context["plans"]}
        elif req.role == "implementer":
            with self.lock:
                self.implementations += 1
                delta = self.implementations
            file = workspace / "controller.py"
            value = int(file.read_text().split("=")[1])
            file.write_text(f"value = {value + delta}\n")
            assert int(file.read_text().split("=")[1]) == value + delta
            result["status"] = "completed"
        elif req.role == "judge_code":
            result.update(status="completed", phase="code")
        elif req.role == "judge_execution":
            result.update(status="completed", phase="execution",
                          canonical_evidence_id=req.context["canonical_evidence"]["snapshot_id"],
                          dimensions={key: {"score": .75, "rationale": "Inspected bounded behavior",
                          "confidence": "high", "evidence": req.context["canonical_evidence"]["evidence"]}
                          for key in ("robustness", "decisions", "generalizable")})
        elif req.role == "value":
            result.update(value=0, score_snapshot_id=req.context["score_snapshot_id"],
                          capacity_snapshot_id=req.context["capacity_snapshot_id"])
        return contract_response(req, result)


class Evaluator(EvaluationAdapter):
    def __init__(self):
        self.calls, self.fail_artifacts = [], set()
        self.lock = threading.Lock()
    def evaluate(self, artifact, workspace, stage, tasks):
        with self.lock:
            self.calls.append((artifact, stage, tasks))
        if stage == "selection" and artifact in self.fail_artifacts:
            raise RuntimeError("Injected infrastructure outage")
        value = int((workspace / "controller.py").read_text().split("=")[1])
        return StageEvidence(artifact, stage, {task: stage == "screen" or i < value for i, task in enumerate(tasks)},
                             (f"trace:{artifact}",))


@pytest.fixture
def coordinator(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "controller.py").write_text("value = 0\n")
    gateway, evaluator = Gateway(), Evaluator()
    config = FixedPUCTConfig(screen_gate="execution_valid", max_trials=4, max_new_attempts_per_trial=1)
    subset = {"tasks": [{"task": task, "weight": {"display": .5}} for task in ("a", "b")], "coverage": {"test": True}}
    search = FixedPUCTCoordinator(source, tmp_path / "run", config, Objective("subset", "rubric", "judge"),
        gateway, evaluator, RoleSettings("configured-model", "high"), ("screen",), {"a": .5, "b": .5},
        ("a", "b", "c", "d"), subset)
    yield search
    search.close()


def test_two_workers_initialize_real_root_then_drain_exact_stop_count(coordinator):
    result = coordinator.run(stop_after_trials=2)
    assert result["status"]["trials"] == {"completed": 2}
    assert result["status"]["backups"] == 2
    assert result["status"]["codex_invocations"]["reserved"] == 0
    assert coordinator.gateway.implementations == 2
    assert coordinator.store.score("root").u == 0
    assert coordinator.store.db.execute("SELECT count(*) FROM judge_snapshots").fetchone()[0] == 3
    assert coordinator.store.db.execute("SELECT count(*) FROM promotions").fetchone()[0] == 2
    assert result["selection_status"] == "not_started"
    assert not (coordinator.directory / "winner").exists()
    assert (coordinator.directory / "source").exists() is False
    assert (coordinator.source / "controller.py").read_text() == "value = 0\n"
    reviews = result["status"]["reviewer_timings"]["assessments"]
    assert len(reviews) == 3
    assert all(row["state"] == "completed" for row in reviews)
    assert len(result["status"]["runtime_forecasts"]["root_observed_review_timing"]) == 1
    proposals = [request for request in coordinator.gateway.calls if request.role == "proposer"]
    assert proposals
    for request in proposals:
        assert request.context["verification_scope_contract"]["version"] == "test-edit-scope-v1"
        recorded = json.loads((coordinator.directory / "role_requests" / request.request_id / "request.json").read_text())
        assert recorded["context"]["verification_scope_contract"] == request.context["verification_scope_contract"]
        assert recorded["prompt_hash"] == request.prompt_hash
    assert all("verification_scope_contract" not in request.context for request in coordinator.gateway.calls
               if request.role.startswith("judge_"))


def test_resume_preserves_trial_budgets_and_finishes_selection_and_exact_export(coordinator):
    coordinator.run(stop_after_trials=2)
    first = coordinator.store.status()["codex_invocations"]["spent"]
    result = coordinator.run(resume=True)
    assert result["status"]["trials"] == {"completed": 4}
    assert coordinator.store.status()["backups"] == 4
    final = result["final_selection"]
    assert final["selection_status"] == "completed"
    assert final["confirmation_status"] == "not_run" and not final["confirmed"]
    winner = final["winner"]["artifact_id"]
    assert manifest(coordinator.directory / "winner") == coordinator.artifacts.verify(winner)
    assert coordinator.store.status()["codex_invocations"]["spent"] > first
    before = coordinator.store.status()["codex_invocations"]["spent"]
    repeated = coordinator.run(resume=True)
    assert repeated["final_selection"]["winner"]["artifact_id"] == winner
    assert result["status"]["lifecycle"]["finalization"] == "completed"
    assert repeated["status"]["lifecycle"]["finalization"] == "completed"
    assert set(repeated) == set(result)
    assert coordinator.store.status()["codex_invocations"]["spent"] == before
    assert coordinator.store.status()["backups"] == 4
    assert "unconfirmed" in (coordinator.directory / "FINAL_REPORT.md").read_text()
    forecast = json.loads((coordinator.directory / "runtime_forecasts.json").read_text())
    assert forecast == repeated["status"]["runtime_forecasts"]
    assert forecast["device_hour_cap"] is None
    assert "25% margin" in (coordinator.directory / "FINAL_REPORT.md").read_text()
    assert "Observed harness-review timing" in (coordinator.directory / "FINAL_REPORT.md").read_text()
    assert all(stage != "confirmation" for _, stage, _ in coordinator.evaluator.calls)


def test_complete_finalization_uses_android_utility_even_when_combined_score_disagrees(coordinator):
    coordinator.run(stop_after_trials=2)
    candidates = shortlist(coordinator.store)
    # Explicitly provide trusted full-Selection results that reverse S0 order.
    for i, candidate in enumerate(candidates):
        successes = 0 if i == 0 else 1 if i == 1 else 4
        evidence = StageEvidence(candidate["artifact_id"], "selection",
            {task: index < successes for index, task in enumerate(coordinator.selection_tasks)}, (f"final:{i}",))
        # Promotions already completed a full snapshot; this fixture replaces
        # only its setup evidence before testing winner selection.
        with coordinator.store.transaction():
            coordinator.store.db.execute("DELETE FROM evaluation_snapshots WHERE artifact=? AND stage='selection'", (candidate["artifact_id"],))
        coordinator.store.save_evaluation(evidence.identity, candidate["artifact_id"], "selection",
            {"artifact_id": evidence.artifact_id, "stage": evidence.stage, "outcomes": evidence.outcomes, "evidence": evidence.evidence})
    final = coordinator.finalize()
    assert final["winner"]["artifact_id"] == candidates[-1]["artifact_id"]
    assert final["winner"]["utility"] == 1
    assert final["winner"]["u"] < candidates[1]["u"]


def test_final_selection_tie_prefers_baseline_and_deduplicates_artifacts(coordinator):
    coordinator.run(stop_after_trials=0)
    baseline = coordinator.store.score("root")
    # A duplicate artifact from another lineage must not enlarge the shortlist.
    with coordinator.store.transaction():
        coordinator.store.db.execute("INSERT INTO nodes(id,artifact,parent,depth,score,eligible) VALUES('duplicate',?,'root',1,?,1)",
                                     (baseline.artifact_id, json.dumps({**baseline.__dict__, "objective": baseline.objective.__dict__})))
    assert len(shortlist(coordinator.store)) == 1
    final = coordinator.finalize()
    assert final["winner"]["node"] == "root"


def test_incomplete_final_selection_preserves_shortlist_and_completed_evidence_for_resume(coordinator):
    coordinator.run(stop_after_trials=2)
    baseline = coordinator.store.score("root").artifact_id
    coordinator.evaluator.fail_artifacts.add(baseline)
    first = coordinator.finalize()
    assert first["selection_status"] == "final_evaluation_incomplete" and first["winner"] is None
    assert not (coordinator.directory / "winner").exists()
    calls = len(coordinator.evaluator.calls)
    coordinator.evaluator.fail_artifacts.clear()
    final = coordinator.finalize()
    assert final["shortlist"] == first["shortlist"]
    assert final["selection_status"] == "completed"
    assert len(coordinator.evaluator.calls) == calls + 1


def test_root_assessment_crash_before_node_commit_reuses_completed_roles(coordinator, monkeypatch):
    add = coordinator.store.add_root
    def crash(*args):
        raise RuntimeError("Injected root commit crash")
    monkeypatch.setattr(coordinator.store, "add_root", crash)
    with pytest.raises(RuntimeError, match="root commit crash"):
        coordinator.run(stop_after_trials=0)
    calls = len(coordinator.gateway.calls)
    evaluations = len(coordinator.evaluator.calls)
    monkeypatch.setattr(coordinator.store, "add_root", add)
    coordinator.run(resume=True, stop_after_trials=0)
    assert len(coordinator.gateway.calls) == calls == 2
    assert len(coordinator.evaluator.calls) == evaluations == 2
    assert coordinator.store.status()["codex_invocations"]["reserved"] == 0


def test_final_export_crash_after_selection_commit_is_resumable(coordinator, monkeypatch):
    coordinator.run(stop_after_trials=0)
    original = Finalizer.export
    def crash(*args, **kwargs):
        raise RuntimeError("Injected export crash")
    monkeypatch.setattr(Finalizer, "export", crash)
    with pytest.raises(RuntimeError, match="export crash"):
        coordinator.finalize()
    assert coordinator.store.db.execute("SELECT state FROM finalizations").fetchone()[0] == "completed"
    before = len(coordinator.evaluator.calls)
    monkeypatch.setattr(Finalizer, "export", original)
    final = coordinator.finalize()
    assert len(coordinator.evaluator.calls) == before
    assert manifest(coordinator.directory / "winner") == coordinator.artifacts.verify(final["winner"]["artifact_id"])


def test_cli_status_and_export_do_not_launch_roles_or_change_statistics(coordinator, tmp_path):
    coordinator.run(stop_after_trials=0)
    prior = coordinator.store.status()
    calls = len(coordinator.gateway.calls)
    root = Path(__file__).resolve().parents[3]
    status = subprocess.run([sys.executable, "-m", "alpha_agents.tree_search", "--output", str(coordinator.directory),
                             "--command", "status"], cwd=root, capture_output=True, text=True, timeout=10)
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout) == prior
    artifact = coordinator.store.score("root").artifact_id
    destination = tmp_path / "exported-root"
    exported = subprocess.run([sys.executable, "-m", "alpha_agents.tree_search", "--output", str(coordinator.directory),
        "--command", "export", "--artifact", artifact, "--destination", str(destination)],
        cwd=root, capture_output=True, text=True, timeout=10)
    assert exported.returncode == 0, exported.stderr
    assert manifest(destination) == coordinator.artifacts.verify(artifact)
    assert coordinator.store.status() == prior and len(coordinator.gateway.calls) == calls


def test_cli_invocation_grant_is_audited_and_preserves_frozen_identity(coordinator):
    coordinator.run(stop_after_trials=0)
    root = Path(__file__).resolve().parents[3]
    grant = subprocess.run([sys.executable, "-m", "alpha_agents.tree_search", "--output", str(coordinator.directory),
        "--command", "grant-invocations", "--invocation-cap", "2100", "--recovery-reason", "Operator-approved recovery capacity"],
        cwd=root, capture_output=True, text=True, timeout=10)
    assert grant.returncode == 0, grant.stderr
    assert coordinator.store.budget()["cap"] == 2100
    assert coordinator.store.config.codex_invocation_cap == 2000
    assert coordinator.store.db.execute("SELECT count(*) FROM invocation_grants").fetchone()[0] == 1


def test_process_recovery_terminates_only_the_tagged_owned_group(tmp_path):
    token = "fixed-puct-test-owned-process"
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True,
                               env={**os.environ, "ALPHA_PROCESS_LAUNCH_ID": token})
    path = tmp_path / "process.json"
    try:
        path.write_text(json.dumps({"status": "running", "pid": process.pid,
                                    "process_birth": process_birth(process.pid), "launch_id": token}))
        assert not reconcile_process(path, terminate=False)
        assert reconcile_process(path)
        assert process.wait(timeout=5) != 0
        assert (tmp_path / "recovery.json").exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def test_wrong_birth_time_does_not_terminate_an_unrelated_reused_pid(tmp_path):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    path = tmp_path / "process.json"
    try:
        path.write_text(json.dumps({"status": "running", "pid": process.pid,
                                    "process_birth": "wrong-birth", "launch_id": "unrelated-token"}))
        assert reconcile_process(path)
        assert process.poll() is None
    finally:
        process.kill()
        process.wait()


def test_operator_stop_drains_admitted_attempts_without_new_promotions_or_finalization(coordinator):
    invoke = coordinator.gateway.invoke
    def stopped(req, workspace):
        result = invoke(req, workspace)
        if req.role == "implementer":
            coordinator.request_stop()
        return result
    coordinator.gateway.invoke = stopped
    result = coordinator.run()
    assert coordinator.gateway.implementations == 1
    assert result["selection_status"] == "not_started"
    assert coordinator.store.budget()["reserved"] == 0
    assert coordinator.store.db.execute("SELECT count(*) FROM promotions").fetchone()[0] == 0
    assert coordinator.store.db.execute("SELECT count(*) FROM trials WHERE state!='completed'").fetchone()[0] == 0


def test_legacy_process_recovery_waits_for_verified_owned_process_death(tmp_path):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    path = tmp_path / "process.json"
    try:
        path.write_text(json.dumps({"status": "running", "pid": process.pid,
                                    "process_birth": process_birth(process.pid)}))
        assert not reconcile_process(path, terminate=False)
        assert reconcile_process(path)
        assert process.wait(timeout=5) != 0
        assert (tmp_path / "recovery.json").exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def test_legacy_recovery_refuses_to_kill_a_group_from_a_nonleader_pid(tmp_path):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    path = tmp_path / "process.json"
    try:
        assert os.getpgid(process.pid) != process.pid
        path.write_text(json.dumps({"status": "running", "pid": process.pid,
                                    "process_birth": process_birth(process.pid)}))
        assert not reconcile_process(path)
        assert process.poll() is None
    finally:
        process.kill()
        process.wait()


def test_legacy_recovery_does_not_confirm_death_until_birth_verified_process_exits(tmp_path, monkeypatch):
    from alpha_agents.tree_search.core.mcts import recovery

    path = tmp_path / "process.json"
    path.write_text(json.dumps({"status": "running", "pid": 987654, "process_birth": "owned-birth"}))
    observations = []
    states = iter([True, True, False])
    def alive(pid, birth):
        observations.append((pid, birth))
        return next(states)
    monkeypatch.setattr(recovery, "process_birth", lambda pid: "owned-birth")
    monkeypatch.setattr(recovery, "_legacy_alive", alive)
    monkeypatch.setattr(recovery.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(recovery.os, "getpgrp", lambda: 123456)
    signals = []
    monkeypatch.setattr(recovery.os, "killpg", lambda group, sig: signals.append((group, sig)))
    monkeypatch.setattr(recovery.time, "sleep", lambda seconds: None)
    assert recovery.reconcile_process(path)
    assert len(observations) == 3
    assert signals == [(987654, signal.SIGKILL)]


def test_controller_refreshes_priors_after_observations_without_resetting_statistics(coordinator):
    coordinator.run(stop_after_trials=1)
    store = coordinator.store
    executor = coordinator._executor(store, "refresh-test")
    original = store._row("nodes", "root")["prior_version"]
    statistics = [tuple(row) for row in store.db.execute("SELECT id,n,w FROM plans ORDER BY id")]
    selected_versions = [row[0] for row in store.db.execute("SELECT prior_version FROM selections ORDER BY trial,position")]
    before = len(coordinator.gateway.calls)
    executor._refresh("root", "refresh-worker")
    assert len(coordinator.gateway.calls) == before + 1
    response_request = coordinator.gateway.calls[-1]
    assert response_request.role == "policy"
    assert response_request.context["development_evidence"]["observed_attempts"]
    assert response_request.context["plans"][0]["document_path"].endswith("PLAN.md")
    assert store._row("nodes", "root")["prior_version"] != original
    assert [tuple(row) for row in store.db.execute("SELECT id,n,w FROM plans ORDER BY id")] == statistics
    assert [row[0] for row in store.db.execute("SELECT prior_version FROM selections ORDER BY trial,position")] == selected_versions
    assert selected_versions == [original]
    executor._refresh("root", "another-worker")
    assert len(coordinator.gateway.calls) == before + 1
    evidence_before_visits = store.development_evidence("root")
    store.db.execute("UPDATE nodes SET n=n+1 WHERE id='root'")
    assert store.development_evidence("root") == evidence_before_visits
    executor._refresh("root", "another-worker")
    assert len(coordinator.gateway.calls) == before + 1
    assert store.budget()["reserved"] == 0
    trial = store.reserve_trial()
    selected = store.select_and_reserve(trial, require_current_priors=True)
    assert selected["kind"] == "implement"
    assert selected["prior_version"] == store._row("nodes", "root")["prior_version"]
    assert selected["prior_version"] != original
    store.fail_attempt(selected["attempt"], "Test cleanup after version verification")
    store.backup(trial)


def test_failed_policy_refresh_retains_last_valid_priors_and_releases_reserve(coordinator):
    from alpha_agents.tree_search.core.mcts.controller import AssessmentPending

    coordinator.run(stop_after_trials=1)
    store = coordinator.store
    original = store._row("nodes", "root")["prior_version"]
    calls = []
    def unavailable(request, workspace):
        assert request.role == "policy"
        calls.append(request)
        raise RuntimeError("Policy endpoint unavailable")
    coordinator.gateway.invoke = unavailable
    with pytest.raises(AssessmentPending, match="failed twice"):
        coordinator._executor(store, "refresh-test")._refresh("root", "refresh-worker")
    assert len(calls) == 2
    assert store._row("nodes", "root")["prior_version"] == original
    assert store.budget()["reserved"] == 0
    assert not store.db.execute("SELECT active FROM operations WHERE node='root'").fetchone()[0]


def test_pending_refresh_blocks_peer_selection_and_duplicate_policy_owner(coordinator):
    from alpha_agents.tree_search.core.mcts.contracts import fingerprint

    coordinator.run(stop_after_trials=1)
    store = coordinator.store
    evidence = fingerprint(store.development_evidence("root"))
    owner = store.claim_operation("root", "refresh", "worker-one", evidence)
    assert owner is not None
    assert store.claim_operation("root", "refresh", "worker-two", evidence) is None
    trial = store.reserve_trial()
    assert store.select_and_reserve(trial, require_current_priors=True)["kind"] == "wait"
    assert not store.db.execute("SELECT 1 FROM selections WHERE trial=?", (trial,)).fetchone()
    store.fail_operation("root", "worker-one", owner["token"], "Test interruption")
    assert store.select_and_reserve(trial, require_current_priors=True)["kind"] == "refresh"
    store.stop_trial(trial, "operator_stop")
    store.backup(trial)


def test_final_report_exposes_per_node_task_weights_and_judge_components(coordinator):
    coordinator.run(stop_after_trials=2)
    coordinator.finalize()
    report = json.loads((coordinator.directory / "score_breakdowns.json").read_text())
    assert len(report["nodes"]) == coordinator.store.status()["nodes"]
    for node in report["nodes"]:
        if not node["eligible"]:
            continue
        assert sum(task["contribution"] for task in node["weighted_tasks"]) == pytest.approx(node["u"])
        assert sum(d["aggregate_contribution"] for d in node["judge_dimensions"].values()) == pytest.approx(node["j"])
        assert node["f"] == pytest.approx((1 - node["lambda"]) * node["u"] + node["lambda"] * node["j"])
        assert node["missing_sources"] == []
        if node["parent"]:
            assert node["parent_reward"]["total"] == pytest.approx(
                node["parent_reward"]["weighted_android"] + node["parent_reward"]["weighted_judge"])
    markdown = (coordinator.directory / "FINAL_REPORT.md").read_text()
    assert "Weighted contribution" in markdown and "Effective rating" in markdown
    assert "Predicted continuation contributions remain separate" in markdown


def test_full_30_trial_software_protocol_with_three_concurrent_plan_branches(tmp_path):
    class DiverseGateway(Gateway):
        def invoke(self, request, workspace):
            result = super().invoke(request, workspace)
            if request.role == "proposer":
                result["plans"] = [draft("candidate-" + name, mechanism=mechanism) for name, mechanism in (
                    ("observe", "verify observations before acting"),
                    ("recover", "bounded recovery after failed verification"),
                    ("memory", "invalidate stale context after state changes"))]
                result = contract_response(request, result)
            return result
    source = tmp_path / "source"
    source.mkdir()
    (source / "controller.py").write_text("value = 0\n")
    gateway, evaluator = DiverseGateway(), Evaluator()
    tasks = tuple(f"Task{i:02}" for i in range(20))
    s0 = tasks[:10]
    subset = {"tasks": [{"task": task, "weight": {"display": .1}} for task in s0], "coverage": {"fixture": "software-only"}}
    search = FixedPUCTCoordinator(source, tmp_path / "run", FixedPUCTConfig(screen_gate="execution_valid"),
        Objective("software-subset", "rubric", "judge"), gateway, evaluator, RoleSettings("configured-model", "high"),
        tuple(f"Screen{i}" for i in range(5)), {task: .1 for task in s0}, tasks, subset)
    try:
        result = search.run()
        assert result["errors"] == []
        assert result["status"]["trials"] == {"completed": 30}
        assert result["status"]["backups"] == 30
        assert result["selection_status"] == "completed"
        assert not result["confirmed"]
        assert search.store.db.execute("SELECT max(depth) FROM nodes").fetchone()[0] <= 8
        assert search.store.db.execute("SELECT count(*) FROM attempts").fetchone()[0] <= 90
        assert search.store.db.execute("SELECT max(count) FROM (SELECT count(*) AS count FROM plans GROUP BY node)").fetchone()[0] == 3
        assert search.store.db.execute("SELECT max(count) FROM (SELECT count(*) AS count FROM attempts GROUP BY plan)").fetchone()[0] <= 3
        assert search.store.budget()["reserved"] == 0
        assert all(request.execution_mode == "goal" for request in gateway.calls if request.role == "implementer")
        assert all(request.execution_mode == "plan" for request in gateway.calls if request.role == "proposer")
        winner = result["final_selection"]["winner"]["artifact_id"]
        assert manifest(search.directory / "winner") == search.artifacts.verify(winner)
        assert all(stage != "confirmation" for _, stage, _ in evaluator.calls)
        calls = len(gateway.calls)
        resumed = search.run(resume=True)
        assert resumed["final_selection"]["winner"]["artifact_id"] == winner
        assert resumed["status"]["lifecycle"]["finalization"] == "completed"
        assert result["status"]["lifecycle"]["finalization"] == "completed"
        assert len(gateway.calls) == calls
        assert search.store.status()["backups"] == 30
    finally:
        search.close()


def test_reconcile_preserves_partial_reviewer_events_without_inventing_finish(coordinator):
    from alpha_agents.tree_search.core.mcts.assessment_timing import AssessmentTiming

    artifact = coordinator._freeze_run()
    timing = AssessmentTiming(coordinator.store, "interrupted-baseline", artifact)
    timing.mark("code_start")
    timing.mark("android_start")
    timing.mark("android_finish")
    before = coordinator.store.status()["reviewer_timings"]["assessments"][0]
    coordinator._reconcile()
    after = coordinator.store.status()["reviewer_timings"]["assessments"][0]
    assert after["state"] == "interrupted"
    assert after["events"] == before["events"]
    assert after["android_phase_seconds"] == before["android_phase_seconds"]
    assert after["code_phase_seconds"] is None
    assert after["execution_phase_seconds"] is None
    assert after["added_continuation_seconds"] is None
    with pytest.raises(ValueError, match="no longer active"):
        timing.mark("code_finish")


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="Requires hard process termination")
@pytest.mark.parametrize("boundary", ["before_backup", "after_backup"])
def test_coordinator_sigkill_at_backup_boundary_resumes_exactly_once(tmp_path, boundary):
    """A killed coordinator must recover durable value/accounting without replay."""
    script = r'''
import os
import signal
import sys
from pathlib import Path
from alpha_agents.tree_search.core.mcts.contracts import FixedPUCTConfig, Objective
from alpha_agents.tree_search.core.mcts.controller import RoleSettings
from alpha_agents.tree_search.core.mcts.coordinator import FixedPUCTCoordinator
from alpha_agents.tree_search.core.mcts.storage import TreeStore
from alpha_agents.tree_search.tests.test_mcts_coordinator import Gateway, Evaluator
root = Path(sys.argv[1])
source = root / "source"
source.mkdir()
(source / "controller.py").write_text("value = 0\n")
config = FixedPUCTConfig(screen_gate="execution_valid", max_trials=2,
                        trial_workers=1, max_new_attempts_per_trial=1)
subset = {"tasks": [{"task": task, "weight": {"display": .5}}
                     for task in ("a", "b")], "coverage": {"test": True}}
search = FixedPUCTCoordinator(source, root / "run", config,
    Objective("subset", "rubric", "judge"), Gateway(), Evaluator(),
    RoleSettings("configured-model", "high"), ("screen",), {"a": .5, "b": .5},
    ("a", "b", "c", "d"), subset)
original = TreeStore.backup
boundary = sys.argv[2]
def crash(self, *args, **kwargs):
    if boundary == "before_backup":
        os.kill(os.getpid(), signal.SIGKILL)
    original(self, *args, **kwargs)
    os.kill(os.getpid(), signal.SIGKILL)
TreeStore.backup = crash
search.run(stop_after_trials=1, finalize=False)
raise AssertionError("Crash boundary was not reached")
'''
    killed = subprocess.run([sys.executable, "-c", script, str(tmp_path), boundary],
                            capture_output=True, text=True, timeout=30)
    assert killed.returncode == -signal.SIGKILL, killed.stderr
    source = tmp_path / "source"
    config = FixedPUCTConfig(screen_gate="execution_valid", max_trials=2,
                            trial_workers=1, max_new_attempts_per_trial=1)
    subset = {"tasks": [{"task": task, "weight": {"display": .5}}
                         for task in ("a", "b")], "coverage": {"test": True}}
    gateway = Gateway()
    search = FixedPUCTCoordinator(source, tmp_path / "run", config,
        Objective("subset", "rubric", "judge"), gateway, Evaluator(),
        RoleSettings("configured-model", "high"), ("screen",), {"a": .5, "b": .5},
        ("a", "b", "c", "d"), subset)
    try:
        store = search.store
        expected_backups = int(boundary == "after_backup")
        assert store.status()["backups"] == expected_backups
        values = [tuple(row) for row in store.db.execute("SELECT * FROM values_assessed")]
        assert len(values) == 1
        invocations = store.budget()["spent"]
        result = search.run(resume=True, stop_after_trials=0, finalize=False)
        assert result["status"]["trials"] == {"completed": 1}
        assert result["status"]["backups"] == 1
        assert result["status"]["codex_invocations"]["spent"] == invocations
        assert result["status"]["codex_invocations"]["reserved"] == 0
        assert [tuple(row) for row in store.db.execute("SELECT * FROM values_assessed")] == values
        assert gateway.calls == []
        statistics = [tuple(row) for row in store.db.execute("SELECT id,n FROM nodes ORDER BY id")]
        assert dict(statistics)["root"] == 1
        assert sum(visits for node, visits in statistics if node != "root") == 0
        plan_statistics = [tuple(row) for row in store.db.execute("SELECT id,n,w FROM plans ORDER BY id")]
        attempt_statistics = [tuple(row) for row in store.db.execute("SELECT id,n,w FROM attempts ORDER BY id")]
        assert len(plan_statistics) == 3 and len(attempt_statistics) == 1
        visited_plans = [row for row in plan_statistics if row[1] == 1]
        assert len(visited_plans) == 1
        assert visited_plans[0][1:] == pytest.approx((1, .45))
        assert all(row[1:] == (0, 0.0) for row in plan_statistics if row[1] == 0)
        assert attempt_statistics[0][1:] == pytest.approx((1, .45))
        assert (source / "controller.py").read_text() == "value = 0\n"
        again = search.run(resume=True, stop_after_trials=0, finalize=False)
        assert again["status"]["backups"] == 1
        assert [tuple(row) for row in store.db.execute("SELECT id,n FROM nodes ORDER BY id")] == statistics
        assert store.budget()["spent"] == invocations and gateway.calls == []
        assert [tuple(row) for row in store.db.execute("SELECT id,n,w FROM plans ORDER BY id")] == plan_statistics
        assert [tuple(row) for row in store.db.execute("SELECT id,n,w FROM attempts ORDER BY id")] == attempt_statistics
    finally:
        search.close()
