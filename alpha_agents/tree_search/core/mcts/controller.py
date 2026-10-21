"""Trial orchestration over persistent selection and independently supplied ports.

The trial engine is executable with deterministic ports and real artifact
workspaces. Production Codex and staged Artemis adapters implement these same
boundaries; the DGM composition path is untouched.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from typing import Mapping

from alpha_agents.tree_search.core.mcts.artifacts import ArtifactStore
from alpha_agents.tree_search.core.mcts.assessment_timing import AssessmentTiming
from alpha_agents.tree_search.core.mcts.contracts import (
    CombinedScore,
    fingerprint,
    unit,
)
from alpha_agents.tree_search.core.mcts.judge_review import review_score
from alpha_agents.tree_search.core.mcts.plan_archive import (
    freeze_attempt_evidence,
    freeze_document,
    freeze_metadata,
)
from alpha_agents.tree_search.core.mcts.ports import (
    RoleGateway,
    RoleSettings,
    StagedEvaluator,
    StageEvidence,
)
from alpha_agents.tree_search.core.mcts.resource_retention import (
    cleanup_sqlite_scratch,
    retire_workspace,
    verification_files,
)
from alpha_agents.tree_search.core.mcts.responses import (
    canonical_code_evidence,
    canonical_file_evidence,
    validate_response,
)
from alpha_agents.tree_search.core.mcts.storage import (
    CapacityError,
    OwnershipError,
    TreeStore,
)
from alpha_agents.tree_search.core.mcts.targeted_checks import TargetedChecks
from alpha_agents.tree_search.core.mcts.task_cache import evidence_inventory
from alpha_agents.tree_search.core.mcts.value_context import (
    continuation_context,
    restore_continuation,
    verify_code_views,
)
from alpha_agents.tree_search.core.shared.storage import write_json
from alpha_agents.tree_search.infrastructure.evidence_sqlite import inspection_catalog

JUDGE_COVERAGE_POLICY = "all-s0-outcomes-failed-then-success-id-v2"


def canonical_judge_context(s0: StageEvidence, tasks: tuple[str, ...]) -> dict:
    """Order review inputs without changing the sealed stage or its identity."""
    s0.validate_tasks(tasks)
    ordered = sorted(tasks, key=lambda task: (s0.outcomes[task], task))
    # Use the frozen requested order, not JSON mapping iteration: persistence
    # sorts mapping keys while preserving the evidence list's original order.
    if len(s0.evidence) == len(tasks):
        roots = dict(zip(tasks, s0.evidence, strict=True))
        evidence = [roots[task] for task in ordered]
        review = [{"task": task, "success": s0.outcomes[task], "evidence": [roots[task]]}
                  for task in ordered]
    else:
        evidence = list(s0.evidence)
        review = [{"task": task, "success": s0.outcomes[task], "evidence": evidence}
                  for task in ordered]
    # This catalog is invocation metadata, not a changed stage snapshot. Exact
    # file paths let consumers enumerate databases, ledgers and images without
    # reconstructing opaque execution IDs from the directory names.
    catalog, inspections = [], []
    file_number = 0
    for reference in dict.fromkeys(evidence):
        root = Path(reference)
        if not root.is_absolute() or not root.is_dir():
            continue  # Symbolic/shared evidence ports need not expose files.
        files = evidence_inventory(root)
        inspections.extend(inspection_catalog(root, files))
        records = []
        for item in files:
            file_number += 1
            records.append({**item, "absolute_file": str(root / item["path"]),
                            "reference_id": f"s0-file-{file_number:04d}"})
        catalog.append({"evidence_root_path": reference, "files": records})
    return {"snapshot_id": s0.identity, "outcomes": dict(s0.outcomes),
            "evidence": evidence, "task_review_order": review,
            "file_catalog": catalog, "sqlite_inspection": inspections}


class AssessmentPending(RuntimeError):
    pass


class EvaluationPending(AssessmentPending):
    """An evaluator outage with the frozen attempt retained for recovery."""


class ScreenRejected(ValueError):
    pass


class TrialExecutor:
    """A worker owns its store connection; model calls never hold its transaction."""

    def __init__(self, store: TreeStore, artifacts: ArtifactStore, gateway: RoleGateway,
                 evaluator: StagedEvaluator, settings: RoleSettings,
                 screen_tasks: tuple[str, ...], s0_weights: Mapping[str, float],
                 stop_admission: threading.Event | None = None, on_score=None, selection_tasks=None):
        self.store, self.artifacts = store, artifacts
        self.gateway, self.evaluator, self.settings = gateway, evaluator, settings
        self.screen_tasks = screen_tasks
        self.s0_weights = dict(s0_weights)
        self.selection_tasks = tuple(selection_tasks) if selection_tasks is not None else tuple(s0_weights)
        single = store.config.evaluation_task
        if single and (screen_tasks or self.s0_weights != {single: 1.0} or self.selection_tasks != (single,)):
            raise ValueError("Single-task search evaluates exactly its one task without Screen")
        if (not screen_tasks and not single) or len(set(screen_tasks)) != len(screen_tasks) or not s0_weights:
            raise ValueError("Frozen Screen and S0 task manifests are required")
        if any(unit(w, "task weight") == 0 for w in s0_weights.values()) or abs(sum(s0_weights.values()) - 1) > 1e-12:
            raise ValueError("Frozen S0 task weights must be positive and sum to one")
        if set(screen_tasks) & set(s0_weights):
            raise ValueError("Screen and S0 partitions must be disjoint")
        self.stop_admission = stop_admission if stop_admission is not None else threading.Event()
        self.on_score = on_score

    def _role(self, owner: str, purpose: str, role: str, artifact: str, context: dict, workspace: Path) -> dict:
        if not role.startswith("judge_"):
            context = {**context, "utility_context": {
                "version": "weighted-canonical-combined-v2", "s0_task_weights": self.s0_weights,
                "judge_weight": self.store.objective.judge_weight,
                "evaluation_task": self.store.config.evaluation_task}}
        if role == "policy":
            policy = self.store.continuation_policy()
            context = {**context, "discount_factor": policy["discount_factor"],
                       "continuation_policy_id": fingerprint(policy), "continuation_policy": policy}
        if role == "proposer":
            from alpha_agents.tree_search.core.mcts.planning_scope import (
                verification_scope_contract,
            )
            context = {**context, "verification_scope_contract": verification_scope_contract()}
        if role == "value":
            from alpha_agents.tree_search.core.mcts.planning_scope import (
                verification_scope_contract,
            )
            from alpha_agents.tree_search.core.mcts.value_forecast import CONTRACT
            context = {**context, "value_report_contract": dict(CONTRACT), "continuation_role_protocol": {
                "version": "continuation-role-protocol-v1",
                "model": self.settings.model, "effort": self.settings.effort,
                "invocation_deadline_seconds": self.store.config.role_timeout_seconds,
                "verification_scope_contract": verification_scope_contract(),
                "plan_format": "technical-design-v1",
                "proposal_calls_per_expansion": self.store.config.max_plans,
            }}
        context = self.gateway.enrich_context(role, context)
        # Isolate role checkpoint/reuse writes from the worker's state connection.
        store = TreeStore(self.store.directory, self.store.config, self.store.objective)
        resources = ExitStack()
        completed_request_dir = None
        try:
            if not role.startswith("judge_"):
                self._restore_archive_evidence(context, store)
            if role == "value":
                restore_continuation(store, context)
            proposed = self.gateway.prepare_request(str(self.store.directory), "validate-before-launch", role, artifact,
                                   self.settings.model, self.settings.effort, context)
            proposed.render()
            # Exhaust the cursor before publishing reuse records. A concurrent
            # worker may commit while a cached response is inspected; a live WAL
            # read snapshot cannot then be upgraded to a write transaction.
            invocations = list(store.db.execute("SELECT id FROM invocations WHERE purpose=? ORDER BY started,id", (purpose,)))
            for invocation in invocations:
                prior = self.store.directory / "role_requests" / invocation[0]
                try:
                    record = json.loads((prior / "request.json").read_text())
                    fields = ("role", "artifact_id", "model", "effort", "context", "prompt_version", "library_hash")
                    if any(record.get(key) != getattr(proposed, key) for key in fields):
                        continue
                    if role != "implementer" and record.get("workspace"):
                        from alpha_agents.tree_search.core.mcts.artifacts import (
                            manifest,
                        )
                        if manifest(Path(record["workspace"])) != self.artifacts.verify(artifact):
                            continue
                    response = json.loads((prior / "response.json").read_text())
                    if role != "implementer" and record.get("workspace"):
                        response, _ = canonical_code_evidence(response, Path(record["workspace"]))
                    validate_response(response, proposed, workspace)
                    with store.transaction():
                        store.db.execute("INSERT INTO role_reuses VALUES(?,?,?,?,?)", (
                            store._id("role-reuse"), invocation[0], owner, purpose, time.time()))
                    if role == "policy":
                        response = {**response, "controller_provenance": {
                            "request_id": invocation[0], "model": record["model"], "effort": record["effort"],
                            "prompt_hash": record.get("prompt_hash"), "library_hash": record.get("library_hash")}}
                    return response
                except (OSError, ValueError, KeyError, TypeError):
                    continue
            request_id = store.consume_call(owner, purpose)
            checks = None
            if role == "implementer" and self.gateway.supports_checks and self.evaluator.supports_checks:
                queued_at = time.time()
                admission_started = time.monotonic()
                testing_evaluator = resources.enter_context(self.evaluator.implementation_session(request_id))
                write_json(self.store.directory / "role_requests" / request_id / "device_admission.json", {
                    "queued_at": queued_at, "admitted_at": time.time(),
                    "queue_seconds": time.monotonic() - admission_started,
                    "reservation_owner": request_id, "release": "after-handoff-and-check-drain"})
                binding = self.gateway.prepare_request(str(self.store.directory), request_id, role, artifact,
                                      self.settings.model, self.settings.effort, context)
                checks = TargetedChecks(store, self.artifacts, testing_evaluator, workspace, binding,
                    self.screen_tasks + self.selection_tasks)
                context = {**context, "device_check_channel": checks.channel}
            request = self.gateway.prepare_request(str(self.store.directory), request_id, role, artifact,
                                  self.settings.model, self.settings.effort, context)
            request_dir = self.store.directory / "role_requests" / request_id
            write_json(request_dir / "request.json", {**request.to_record(), "prompt_hash": request.prompt_hash,
                       "library_hash": request.library_hash, "permission_mode": request.permission_mode,
                       "workspace": str(workspace),
                       "collaboration_mode": request.collaboration_mode, "execution_mode": request.execution_mode})
            (request_dir / "prompt.md").write_text(request.render())
            started = time.monotonic()
            completion = "interrupted"
            try:
                result = (self.gateway.invoke_with_checks(request, workspace, checks) if checks
                          else self.gateway.invoke(request, workspace))
                if not isinstance(result, dict) or result.get("artifact_id") != artifact:
                    raise ValueError("Role response must identify its exact input artifact")
                validate_response(result, request, workspace)
                write_json(request_dir / "raw_response.json", result)
                if role != "implementer":
                    result, file_mappings = canonical_file_evidence(result, request)
                    if file_mappings:
                        write_json(request_dir / "file_evidence_provenance.json", {
                            "canonical_evidence_id": context["canonical_evidence"]["snapshot_id"],
                            "references": file_mappings})
                    result, mappings = canonical_code_evidence(result, workspace)
                    validate_response(result, request, workspace)
                    if mappings:
                        write_json(request_dir / "code_evidence_provenance.json", {
                            "artifact_id": artifact, "workspace": str(workspace), "references": mappings})
                    from alpha_agents.tree_search.core.mcts.artifacts import manifest
                    if manifest(workspace) != self.artifacts.verify(artifact):
                        raise ValueError("Read-only invocation changed its frozen code view")
                write_json(request_dir / "response.json", result)
                completed_request_dir = request_dir
                completion = "response_validated"
                if role == "policy":
                    result = {**result, "controller_provenance": {
                        "request_id": request_id, "model": request.model, "effort": request.effort,
                        "prompt_hash": request.prompt_hash, "library_hash": request.library_hash}}
                return result
            except Exception as error:
                completion = "failed"
                write_json(request_dir / "failure.json", {"type": type(error).__name__, "error": str(error)})
                raise
            finally:
                elapsed = time.monotonic() - started
                metadata = {"request_id": request_id, "role": role, "purpose": purpose,
                            "artifact_id": artifact, "model": request.model, "effort": request.effort,
                            "status": completion, "usage": self.gateway.observed_usage(request_dir)}
                write_json(request_dir / "observed_metrics.json", {"elapsed_seconds": elapsed, **metadata})
                store.record_cost("role:" + request_id, owner, role, self.gateway.telemetry_name, elapsed, "observed", metadata)
        finally:
            try:
                resources.close()
                if completed_request_dir is not None and role != "implementer":
                    cleanup_sqlite_scratch(completed_request_dir)
            finally:
                store.close()

    def _workspace(self, artifact: str, purpose: str) -> Path:
        destination = self.store.directory / "workspaces" / f"{purpose}-{uuid.uuid4().hex}"
        self.artifacts.materialize(artifact, destination)
        return destination

    def _readonly_role(self, owner, purpose, role, artifact, context):
        workspace = self.artifacts.readonly_view(artifact, self.store.directory / "value_code_views")
        if role == "value":
            verify_code_views(self.store, self.artifacts, context.get("reachable_code_views", []))
        try:
            result = self._role(owner, purpose, role, artifact, context, workspace)
        finally:
            from alpha_agents.tree_search.core.mcts.artifacts import manifest
            if manifest(workspace) != self.artifacts.verify(artifact):
                raise ValueError(f"Read-only role {role} modified the harness")
            if role == "value":
                verify_code_views(self.store, self.artifacts, context.get("reachable_code_views", []))
        return result

    def _catalog(self) -> dict:
        plans = []
        # Finish the read cursor before storing derived snapshots. A live WAL
        # read snapshot cannot be upgraded after a peer commits new state.
        for p in list(self.store.db.execute("SELECT * FROM plans ORDER BY id")):
            parent = self.store._row("nodes", p["node"])
            attempts = [dict(r) for r in self.store.db.execute("SELECT * FROM attempts WHERE plan=? ORDER BY slot", (p["id"],))]
            metadata = json.loads(p["metadata"])
            frozen = freeze_document(self.store.directory, p["id"], p["document"],
                                     metadata.get("document_hash"))
            summaries = []
            for attempt in attempts:
                child = self.store._row("nodes", attempt["child"]) if attempt["child"] else None
                score = self.store.score(attempt["child"]) if child and child["score"] else None
                summary = {"id": attempt["id"], "slot": attempt["slot"], "state": attempt["state"],
                    "error": attempt["error"], "child": attempt["child"], "artifact_id": child["artifact"] if child else None,
                    "u": score.u if score else None, "j": score.j if score else None, "f": score.f if score else None,
                    "u_snapshot_id": score.u_snapshot_id if score else None,
                    "j_snapshot_id": score.j_snapshot_id if score else None}
                evaluations = [{"snapshot_id": row["id"], "stage": row["stage"],
                                "payload": json.loads(row["payload"])} for row in self.store.db.execute(
                    "SELECT id,stage,payload FROM evaluation_snapshots WHERE artifact=? "
                    "AND stage IN ('screen','s0','selection','targeted') ORDER BY stage,id",
                    (child["artifact"] if child else None,))]
                judges = [self.store.judge_evidence(row["id"])
                          for row in self.store.db.execute(
                    "SELECT * FROM judge_snapshots WHERE artifact=? ORDER BY id", (child["artifact"] if child else None,))]
                bundle = {"version": "attempt-evidence-v1", "plan_id": p["id"],
                          "parent_artifact_id": parent["artifact"], "attempt": summary,
                          "score": asdict(score) if score else None,
                          "evaluations": evaluations, "judges": judges,
                          "availability": {"score": "available" if score else "unavailable",
                                           "evaluations": "available" if evaluations else "unavailable",
                                           "judges": "available" if judges else "unavailable",
                                           "canonical_s0": "available" if score and any(
                                               row["snapshot_id"] == score.u_snapshot_id for row in evaluations) else "unavailable",
                                           "canonical_judge": "available" if score and any(
                                               row["snapshot_id"] == score.j_snapshot_id for row in judges) else "unavailable"}}
                self.store.save_archive_evidence(bundle)
                summaries.append({**summary, **freeze_attempt_evidence(self.store.directory, bundle),
                    # Only trusted evaluation references grant trajectory reads.
                    # Model-authored judge findings stay inside the readable file.
                    "canonical_evidence": sorted({reference for evaluation in evaluations
                        for reference in evaluation["payload"].get("evidence", [])})})
            plans.append({"id": p["id"], "parent_artifact": parent["artifact"], **frozen,
                          **freeze_metadata(self.store.directory, p["id"], metadata),
                          "intervention": metadata["intervention"], "attempts": summaries,
                          "status": "registered" if not attempts else "attempted"})
        return {"snapshot_id": fingerprint(plans), "plans": plans, "plan_ids": [p["id"] for p in plans],
                "attempt_ids": sorted(attempt["id"] for plan in plans for attempt in plan["attempts"])}

    def _restore_archive_evidence(self, context: dict, store: TreeStore):
        # Rebuild the exact older snapshot supplied to a recovered request, even
        # when the attempt has since acquired new outcomes or changed state.
        for plan in context.get("plan_archive", {}).get("plans", []):
            for attempt in plan.get("attempts", []):
                digest = attempt.get("evidence_bundle_hash")
                if digest is None:
                    continue
                row = store.db.execute("SELECT payload FROM archive_evidence_snapshots WHERE id=?", (digest,)).fetchone()
                if row is None:
                    raise ValueError("Archive evidence has no authoritative snapshot")
                exported = freeze_attempt_evidence(store.directory, json.loads(row[0]))
                if any(attempt.get(key) != value for key, value in exported.items()):
                    raise ValueError("Archive evidence file identity mismatch")

    def _node_evidence(self, node):
        return self.store.node_evidence(node)

    def _stage(self, artifact, stage, tasks):
        row = self.store.db.execute("SELECT payload FROM evaluation_snapshots WHERE artifact=? AND stage=? ORDER BY rowid LIMIT 1", (artifact, stage)).fetchone()
        if row:
            payload = json.loads(row[0])
            evidence = StageEvidence(payload["artifact_id"], payload["stage"], payload["outcomes"], tuple(payload["evidence"]))
            evidence.validate_tasks(tasks)
            try:
                self.evaluator.verify_stage(evidence)
            except OwnershipError:
                raise
            except (RuntimeError, OSError) as error:
                raise EvaluationPending(f"Cached evaluation verification unavailable: {error}") from error
            return evidence
        workspace = self._workspace(artifact, stage)
        try:
            evidence = self.evaluator.evaluate(artifact, workspace, stage, tasks)
        except OwnershipError:
            raise
        except (RuntimeError, OSError) as error:
            raise EvaluationPending(f"{stage} evaluation unavailable: {error}") from error
        evidence.validate_tasks(tasks)
        if evidence.artifact_id != artifact or evidence.stage != stage:
            raise ValueError("Stage outcome identity mismatch")
        if self.artifacts.freeze(workspace) != artifact:
            raise ValueError("Evaluated stage input changed from its frozen artifact")
        self.store.save_evaluation(evidence.identity, artifact, stage, asdict(evidence))
        retire_workspace(self.store.directory, workspace, self.artifacts, artifact,
                         self.store.directory / "workspace_retention" / (workspace.name + ".json"))
        return evidence

    def _expand(self, node: str, worker: str):
        artifact = self.store._row("nodes", node)["artifact"]
        score = self.store.score(node)
        development = self.store.development_evidence(node)
        evidence = fingerprint(development)
        operation = self.store.claim_operation(node, "expansion", worker, evidence,
                                               proposal_calls=2 * self.store.config.max_plans)
        if operation is None:
            return
        finished = threading.Event()
        def heartbeat():
            s = TreeStore(self.store.directory, self.store.config, self.store.objective)
            try:
                while not finished.wait(30):
                    s.heartbeat(node, worker, operation["token"])
            finally:
                s.close()
        monitor = threading.Thread(target=heartbeat, daemon=True)
        monitor.start()
        success = False
        failure_reason = "Expansion failed; preserved role-request evidence"
        try:
            from alpha_agents.tree_search.core.mcts.design_documents import (
                FORMAT,
                SUMMARY_FIELDS,
            )
            from alpha_agents.tree_search.core.mcts.implementation_history import (
                implementation_history,
            )
            context = {"artifact": artifact, "score": asdict(score),
                       "plan_format": FORMAT, "required_plan_count": 1,
                       "node_depth": self.store._row("nodes", node)["depth"],
                       "prior_implementations": implementation_history(self.store, node),
                       "evidence_snapshot_id": evidence,
                       "canonical_evidence": self._node_evidence(node), "development_evidence": development}
            plans = []
            for slot in range(1, self.store.config.max_plans + 1):
                existing = [{key: plan[key] for key in SUMMARY_FIELDS} for plan in plans]
                slot_context = {**context, "proposal_slot": slot,
                                "existing_plans": existing}
                # Each logical slot gets a separate native Goal invocation. Its
                # context is stable across recovery, enabling metered reuse.
                for retry in range(2):
                    try:
                        proposal = self._readonly_role(operation["request"], "proposal", "proposer", artifact, slot_context)
                        if len(proposal["plans"]) != 1:
                            raise ValueError("Each proposer invocation must return exactly one plan")
                        plan = proposal["plans"][0]
                        from alpha_agents.tree_search.core.mcts.storage import (
                            normalize_intervention,
                        )
                        action = normalize_intervention(plan["intervention"])
                        if any(action == normalize_intervention(prior["intervention"]) for prior in plans):
                            raise ValueError("Proposed intervention duplicates an existing plan at this node")
                        plans.append({**plan, "candidate_id": plan["id"],
                                      "id": "plan-" + fingerprint({"node": node, "slot": slot})[:24],
                                      "proposal_slot": slot,
                                      "existing_plan_ids": [prior["id"] for prior in plans]})
                        break
                    except (sqlite3.Error, OwnershipError):
                        raise
                    except Exception:
                        if retry == 1:
                            raise AssessmentPending(f"Proposal slot {slot} failed twice; expansion remains open for recovery")
            policy = None
            for retry in range(2):
                try:
                    policy = self._readonly_role(operation["request"], "policy", "policy", artifact,
                                                 {**context, "capacity": self.store.capacity(node), "plans": plans})
                    raw = policy["priors"]
                    from alpha_agents.tree_search.core.mcts.policy import prepare_priors
                    prepare_priors(raw, self.store.config.prior_floor)
                    if set(raw) != {plan["id"] for plan in plans}:
                        raise ValueError("Policy must cover exactly the proposed plans")
                    break
                except (sqlite3.Error, OwnershipError):
                    raise
                except Exception:
                    if retry == 1:
                        raise AssessmentPending("Policy retry allowance exhausted; expansion remains open for recovery")
            self.store.publish_plans(node, worker, operation["token"], plans, raw,
                                     policy_response=policy,
                                     required_plan_count=self.store.config.max_plans,
                                     plan_format=FORMAT)
            success = True
            return

        except Exception as failure:
            failure_reason = f"Expansion failed: {type(failure).__name__}: {failure}"
            raise
        finally:
            finished.set()
            monitor.join()
            if not success:
                self.store.fail_operation(node, worker, operation["token"], failure_reason)

    def _refresh(self, node: str, worker: str):
        development = self.store.development_evidence(node)
        evidence = fingerprint(development)
        operation = self.store.claim_operation(node, "refresh", worker, evidence)
        if operation is None:
            return
        finished = threading.Event()
        def heartbeat():
            store = TreeStore(self.store.directory, self.store.config, self.store.objective)
            try:
                while not finished.wait(30):
                    store.heartbeat(node, worker, operation["token"])
            finally:
                store.close()
        monitor = threading.Thread(target=heartbeat, daemon=True)
        monitor.start()
        success = False
        try:
            artifact = self.store._row("nodes", node)["artifact"]
            plans = []
            for row in self.store.db.execute("SELECT * FROM plans WHERE node=? ORDER BY slot", (node,)):
                metadata = json.loads(row["metadata"])
                plans.append({**metadata, "id": row["id"], "document": row["document"],
                    **freeze_document(self.store.directory, row["id"], row["document"], metadata.get("document_hash"))})
            context = {"artifact": artifact, "score": asdict(self.store.score(node)),
                       "plans": plans, "capacity": self.store.capacity(node),
                       "evidence_snapshot_id": evidence, "development_evidence": development,
                       "canonical_evidence": self._node_evidence(node)}
            for _ in range(2):
                try:
                    response = self._readonly_role(operation["request"], "policy", "policy", artifact, context)
                    self.store.refresh_priors(node, worker, operation["token"], response["priors"],
                                              policy_response=response)
                    success = True
                    return
                except (sqlite3.Error, OwnershipError):
                    raise
                except Exception:
                    pass
            raise AssessmentPending("Policy refresh failed twice; previous valid priors retained")
        finally:
            finished.set()
            monitor.join()
            if not success:
                self.store.fail_operation(node, worker, operation["token"], "Policy refresh failed; previous priors retained")

    def assess(self, owner: str, artifact: str) -> CombinedScore:
        """Use identical artifact-only review and canonical evaluation for root/child."""
        try:
            self.evaluator.prepare(artifact, {**({"screen": self.screen_tasks} if self.screen_tasks else {}),
                                              "s0": tuple(self.s0_weights)})
        except OwnershipError:
            raise
        except (RuntimeError, OSError) as error:
            raise EvaluationPending(f"Evaluation preparation unavailable: {error}") from error
        existing = self.store.db.execute("SELECT * FROM judge_snapshots WHERE artifact=? ORDER BY rowid LIMIT 1", (artifact,)).fetchone()
        if existing:
            if self.screen_tasks:
                screen = self._stage(artifact, "screen", self.screen_tasks)
                if self.store.config.screen_gate == "strict_all_success" and not all(screen.outcomes.values()):
                    raise ScreenRejected("Strict Screen gate rejected a valid task failure")
            s0 = self._stage(artifact, "s0", tuple(self.s0_weights))
            if existing["canonical_id"] != s0.identity:
                raise ValueError("Canonical judge identifies another frozen evaluation")
            j, _ = review_score(json.loads(existing["payload"]))
            u = sum(self.s0_weights[t] * outcome for t, outcome in s0.outcomes.items())
            return CombinedScore(artifact, s0.identity, existing["id"], self.store.objective, u, j)
        timing = AssessmentTiming(self.store, owner, artifact)
        state = "failed"
        try:
            score = self._assess_review(owner, artifact, timing)
            state = "completed"
            return score
        except ScreenRejected:
            state = "screen_rejected"
            raise
        except EvaluationPending:
            state = "evaluation_unavailable"
            raise
        except AssessmentPending:
            state = "judge_unavailable"
            raise
        finally:
            timing.finish(state)

    def _assess_review(self, owner: str, artifact: str, timing: AssessmentTiming) -> CombinedScore:
        judge_context = {"artifact": artifact, "rubric": self.store.objective.rubric_version,
                         "coverage_policy": JUDGE_COVERAGE_POLICY}
        timing.mark("android_start")
        try:
            if self.screen_tasks:
                screen = self._stage(artifact, "screen", self.screen_tasks)
                if self.store.config.screen_gate == "strict_all_success" and not all(screen.outcomes.values()):
                    raise ScreenRejected("Strict Screen gate rejected a valid task failure")
            s0 = self._stage(artifact, "s0", tuple(self.s0_weights))
            timing.mark("s0_ready")
        finally:
            timing.mark("android_finish")
        canonical = canonical_judge_context(s0, tuple(self.s0_weights))
        timing.mark("review_start")
        try:
            final = self._readonly_role(owner, "judge_review", "judge_review", artifact,
                                       {**judge_context, "canonical_evidence": canonical})
            if final.get("status") != "completed":
                raise ValueError("Final judge assessment is unavailable")
            if final.get("canonical_evidence_id") != s0.identity or final.get("rubric_version") != self.store.objective.rubric_version:
                raise ValueError("Final judge evidence/rubric identity mismatch")
            j, _ = review_score(final)
        except (sqlite3.Error, OwnershipError):
            raise
        except Exception as error:
            raise AssessmentPending("Final judge unavailable; preserve evaluation evidence") from error
        finally:
            timing.mark("review_finish")
        u = sum(self.s0_weights[task] * outcome for task, outcome in s0.outcomes.items())
        judge_id = self.store.save_judge(artifact, s0.identity, final)
        canonical_judge = json.loads(self.store.db.execute("SELECT payload FROM judge_snapshots WHERE id=?", (judge_id,)).fetchone()[0])
        j, _ = review_score(canonical_judge)
        timing.mark("score_ready")
        return CombinedScore(artifact, s0.identity, judge_id, self.store.objective, u, j)

    def _implement(self, trial: str, selection: dict):
        attempt = selection["attempt"]
        parent = self.store._row("nodes", selection["parent"])["artifact"]
        plan = self.store._row("plans", selection["plan"])
        workspace = self._workspace(parent, "implementation")
        attempt_dir = self.store.directory / "attempts" / attempt
        write_json(attempt_dir / "execution.json", {"workspace": str(workspace), "parent_artifact_id": parent,
                                                    "plan_id": plan["id"], "attempt_id": attempt})
        implementation_status = "failed"
        result = None
        try:
            result = self._role(attempt, "implementation", "implementer", parent,
                                {"frozen_plan": plan["document"], "plan_id": plan["id"], "attempt_id": attempt,
                                 "parent_score": asdict(self.store.score(selection["parent"])),
                                 "parent_evidence": self._node_evidence(selection["parent"])}, workspace)
            self.store.save_implementation_report(attempt, result)
            implementation_status = result.get("status", "failed")
        except (sqlite3.Error, OwnershipError):
            raise
        except Exception:
            # A bounded failed engineering invocation may still leave a permitted
            # useful patch; its engineering status is not silently relabeled.
            pass
        artifact = self.artifacts.freeze(workspace)
        if artifact == parent:
            self.store.fail_attempt(attempt, "Empty patch")
            return
        child = "node-" + uuid.uuid4().hex
        attempt_dir.mkdir(parents=True, exist_ok=True)
        patch = self.artifacts.binary_patch(parent, artifact)
        (attempt_dir / "model_patch.diff").write_bytes(patch)
        write_json(attempt_dir / "implementation.json", {"status": implementation_status, "artifact_id": artifact,
                                                       "parent_artifact_id": parent, "child_id": child,
                                                       "patch_sha256": hashlib.sha256(patch).hexdigest()})
        self.store.register_child(attempt, child, artifact)
        owned_calls = [self.store.directory / "role_requests" / row[0] for row in self.store.db.execute(
            "SELECT id FROM invocations WHERE owner=? AND purpose='implementation'", (attempt,))]
        from alpha_agents.tree_search.core.mcts.recovery import reconcile_process
        if (implementation_status == "completed" and result is not None
                and all(reconcile_process(path / "process/process.json", terminate=False) for path in owned_calls)):
            retire_workspace(self.store.directory, workspace, self.artifacts, artifact,
                             attempt_dir / "workspace_retention.json", verification_files(workspace, result))
            for path in owned_calls:
                cleanup_sqlite_scratch(path)
        try:
            score = self.assess(attempt, artifact)
        except ScreenRejected as error:
            self.store.fail_attempt(attempt, str(error))
            return
        except EvaluationPending:
            self.store.awaiting(trial, "evaluation")
            raise
        except AssessmentPending:
            self.store.awaiting(trial, "judge")
            raise
        except Exception:
            self.store.awaiting(trial, "evaluation")
            raise
        self.store.publish_score(attempt, score)
        if self.on_score:
            self.on_score(child)
        self.store.append_transition(trial, selection["position"])

    def run(self, trial: str) -> dict:
        # A persisted valid prediction survives a crash before backup. Resume
        # that transaction without another model invocation or new snapshot.
        backed = self.store.db.execute("SELECT 1 FROM backups WHERE trial=?", (trial,)).fetchone()
        if backed:
            return self.store.backup(trial)
        current = self.store._row("trials", trial)
        persisted = self.store.db.execute(
            "SELECT id FROM values_assessed WHERE trial=? AND frontier=? AND score_id=? ORDER BY rowid LIMIT 1",
            (trial, current["frontier"], self.store.score(current["frontier"]).identity),
        ).fetchone()
        if persisted and current["state"] in {"active", "awaiting_value"}:
            return self.store.backup(trial, persisted[0])
        if current["state"] == "awaiting_value":
            original = self.store.db.execute("SELECT snapshot FROM frontier_snapshots WHERE trial=? ORDER BY rowid DESC LIMIT 1", (trial,)).fetchone()
            if original is None:
                raise ValueError("Pending value has no original capacity snapshot")
            return self._finish(trial, json.loads(original[0]))
        if current["stop"]:
            return self._finish(trial)
        worker = "worker-" + uuid.uuid4().hex
        while True:
            if self.stop_admission.is_set():
                self.store.stop_trial(trial, "operator_or_global_stop")
                break
            selection = self.store.select_and_reserve(trial, require_current_priors=True)
            kind = selection["kind"]
            if kind in {"expand", "refresh"}:
                try:
                    if kind == "expand":
                        self._expand(selection["node"], worker)
                    else:
                        self._refresh(selection["node"], worker)
                except CapacityError:
                    # Retain the persisted stop-reason spelling for old run readers.
                    self.store.stop_trial(trial, "codex_admission_capacity")
                    break
                except AssessmentPending:
                    self.store.stop_trial(trial, "proposal_or_policy_failure")
                    break
            elif kind == "wait":
                if self.stop_admission.wait(.1):
                    self.store.stop_trial(trial, "operator_or_global_stop")
                    break
            elif kind == "implement":
                self._implement(trial, selection)
                if self.store._row("trials", trial)["stop"] == "invalid_attempt":
                    break
            elif kind == "stored":
                self.store.append_transition(trial, selection["position"])
            else:
                self.store.stop_trial(trial, "structural_terminal" if kind == "terminal" else selection.get("reason", "computational_cutoff"))
                break
        return self._finish(trial)

    def _finish(self, trial, cap=None):
        cap = cap if cap is not None else self.store.snapshot_frontier(trial)
        # The tree depth limit stops generation, not the continuing value target.
        if cap["exhausted"] or not self.store.db.execute("SELECT 1 FROM path WHERE trial=?", (trial,)).fetchone():
            return self.store.backup(trial)
        score = self.store.score(cap["node"])
        for _ in range(2):
            try:
                response = self._readonly_role(trial, "value", "value", score.artifact_id,
                                              {"score": asdict(score), "score_snapshot_id": score.identity,
                                               **continuation_context(self.store, self.artifacts, cap),
                                               "capacity_snapshot_id": cap["snapshot_id"],
                                               "objective_id": self.store.objective.identity,
                                               "discount_factor": cap["discount_factor"],
                                               "continuation_policy_id": cap["continuation_policy_id"]})
                if response.get("capacity_snapshot_id") != cap["snapshot_id"] or response.get("score_snapshot_id") != score.identity:
                    raise ValueError("Value response frontier identity mismatch")
                vid = self.store.persist_value(trial, response["value"], cap, response)
            except (sqlite3.Error, OwnershipError):
                raise
            except Exception:
                continue
            return self.store.backup(trial, vid)
        self.store.awaiting(trial, "value")
        raise AssessmentPending("Value failed twice; pending frontier retained without synthetic backup")

    def recover(self, trial):
        """Called only after the coordinator has reconciled owned processes."""
        current = self.store._row("trials", trial)
        if current["state"] == "completed":
            return self.store.backup(trial)
        persisted = self.store.db.execute("SELECT 1 FROM values_assessed WHERE trial=? AND frontier=?", (trial, current["frontier"])).fetchone()
        if current["state"] == "awaiting_value" and not persisted:
            self.store.reserve_recovery(trial, {"value": 2}, "Operator resume: original pending frontier assessment")
        unfinished = self.store.db.execute("SELECT * FROM selections s WHERE trial=? AND released=0 AND NOT EXISTS (SELECT 1 FROM path p WHERE p.trial=s.trial AND p.position=s.position)", (trial,)).fetchone()
        if unfinished:
            selection = dict(unfinished)
            attempt = selection["attempt"]
            record = self.store._row("attempts", attempt)
            with self.store.transaction():
                self.store.db.execute("UPDATE trials SET state='active' WHERE id=?", (trial,))
            directory = self.store.directory / "attempts" / attempt
            if record["state"] == "reserved":
                frozen = directory / "implementation.json"
                if frozen.exists():
                    frozen = json.loads(frozen.read_text())
                    parent = self.store._row("nodes", selection["parent"])["artifact"]
                    if frozen["parent_artifact_id"] != parent:
                        raise ValueError("Frozen attempt checkpoint identifies another parent artifact")
                    self.artifacts.verify(frozen["artifact_id"])
                    patch = self.artifacts.binary_patch(parent, frozen["artifact_id"])
                    if hashlib.sha256(patch).hexdigest() != frozen["patch_sha256"]:
                        raise ValueError("Frozen attempt patch identity changed")
                    (directory / "model_patch.diff").write_bytes(patch)
                    self.store.register_child(attempt, frozen["child_id"], frozen["artifact_id"])
                elif self.store.db.execute("SELECT 1 FROM invocations WHERE owner=? AND purpose='implementation'", (attempt,)).fetchone():
                    execution = directory / "execution.json"
                    if execution.exists():
                        workspace = Path(json.loads(execution.read_text())["workspace"])
                        if workspace.is_dir():
                            partial = self.artifacts.freeze(workspace)
                            write_json(directory / "interrupted_partial.json", {"artifact_id": partial, "status": "interrupted_unfrozen"})
                    self.store.fail_attempt(attempt, "Interrupted implementation before its frozen-attempt checkpoint")
                else:
                    self._implement(trial, selection)
            record = self.store._row("attempts", attempt)
            if record["state"] in {"evaluating", "awaiting_judge"}:
                artifact = self.store._row("nodes", record["child"])["artifact"]
                completed_judge = self.store.db.execute("SELECT 1 FROM judge_snapshots WHERE artifact=?", (artifact,)).fetchone()
                if not completed_judge:
                    requirements = {"judge_review": 1}
                    self.store.reserve_recovery(attempt, requirements, "Operator resume: frozen artifact assessment")
                try:
                    score = self.assess(attempt, artifact)
                except ScreenRejected as error:
                    self.store.fail_attempt(attempt, str(error))
                except EvaluationPending:
                    self.store.awaiting(trial, "evaluation")
                    raise
                except AssessmentPending:
                    self.store.awaiting(trial, "judge")
                    raise
                except Exception:
                    self.store.awaiting(trial, "evaluation")
                    raise
                else:
                    self.store.publish_score(attempt, score)
                    if self.on_score:
                        self.on_score(record["child"])
            record = self.store._row("attempts", attempt)
            if record["state"] == "eligible" and not self.store.db.execute("SELECT 1 FROM path WHERE trial=? AND position=?", (trial, selection["position"])).fetchone():
                self.store.append_transition(trial, selection["position"])
        return self.run(trial)
