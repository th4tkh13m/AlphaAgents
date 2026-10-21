"""Scoped implementer requests serviced by the trusted shared device queue."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict
from pathlib import Path

from alpha_agents.tree_search.core.mcts.contracts import canonical_json, fingerprint
from alpha_agents.tree_search.core.mcts.resource_retention import (
    readonly_projection,
    retire_workspace,
)
from alpha_agents.tree_search.core.mcts.storage import TreeStore
from alpha_agents.tree_search.core.mcts.task_cache import evidence_inventory
from alpha_agents.tree_search.core.shared.storage import write_json
from alpha_agents.tree_search.infrastructure.evidence_sqlite import inspection_catalog


class TargetedChecks:
    def __init__(self, store, artifacts, evaluator, workspace, request, allowed_tasks):
        self.directory, self.config, self.objective = store.directory, store.config, store.objective
        self.artifacts, self.evaluator = artifacts, evaluator
        self.workspace, self.request = Path(workspace), request
        self.allowed_tasks = set(allowed_tasks)
        self.root = self.directory / "role_requests" / request.request_id / "check_results"
        self.root.mkdir(parents=True, exist_ok=True)
        self.inbox = self.root.parent / "check_input"
        if self.inbox.is_symlink():
            raise ValueError("Device check inbox cannot use a symbolic link")
        self.inbox.mkdir(exist_ok=True)
        request_file = self.inbox / "check_request.json"
        if request_file.is_symlink():
            raise ValueError("Device check request cannot use a symbolic link")
        if not request_file.exists():
            request_file.write_text("\n")

    @property
    def channel(self):
        return {"request_path": str(self.inbox / "check_request.json"),
                "result_directory": str(self.root), "allowed_tasks": sorted(self.allowed_tasks),
                "protocol": "targeted-check-file-v2", "sample_kind": "targeted"}

    def _store(self):
        return TreeStore(self.directory, self.config, self.objective)

    def validate_handoff(self, result):
        reports = result.get("check_requests")
        if not isinstance(reports, list):
            raise ValueError("Implementer must report its targeted check requests")
        store = self._store()
        try:
            rows = list(store.db.execute("SELECT * FROM targeted_check_requests WHERE invocation=?", (self.request.request_id,)))
            if any(not row["result"] for row in rows):
                raise ValueError("Targeted checks remain incomplete; completed engineering cannot be claimed")
            actual = {row["check_id"]: json.loads(row["result"]) for row in rows}
            if result.get("status") == "completed" and any(item["status"] != "completed" for item in actual.values()):
                raise ValueError("Failed infrastructure checks prevent completed engineering handoff")
            if (any(not isinstance(item, dict) for item in reports)
                    or len({item.get("check_id") for item in reports}) != len(reports)
                    or {item.get("check_id") for item in reports} != set(actual)):
                raise ValueError("Targeted check handoff must report exactly the admitted checks")
            for report in reports:
                trusted = actual[report["check_id"]]
                for field in ("status", "artifact_id", "snapshot_id", "outcomes"):
                    if report.get(field) != trusted.get(field):
                        raise ValueError("Targeted check handoff disagrees with trusted evidence")
        finally:
            store.close()

    def handle(self, payload):
        required = {"check_id", "attempt_id", "tasks", "mechanism", "expected_observation"}
        if (not isinstance(payload, dict) or set(payload) != required
                or not isinstance(payload["check_id"], str) or not payload["check_id"].strip()
                or len(payload["check_id"]) > 200 or payload["attempt_id"] != self.request.context["attempt_id"]):
            raise ValueError("Targeted check requires exact invocation-bound request fields")
        tasks = payload["tasks"]
        if (not isinstance(tasks, list) or not tasks or any(not isinstance(task, str) for task in tasks)
                or len(set(tasks)) != len(tasks) or not set(tasks) <= self.allowed_tasks):
            raise ValueError("Targeted check contains duplicate, unknown, or protected tasks")
        if any(not isinstance(payload[key], str) or not payload[key].strip() for key in ("mechanism", "expected_observation")):
            raise ValueError("Targeted check requires a mechanism and falsifiable expected observation")
        encoded = canonical_json(payload)
        identity = fingerprint({"invocation": self.request.request_id, "check_id": payload["check_id"]})
        store = self._store()
        try:
            with store.transaction():
                existing = store.db.execute("SELECT * FROM targeted_check_requests WHERE id=?", (identity,)).fetchone()
                if existing:
                    if existing["payload"] != encoded:
                        raise ValueError("Targeted check ID cannot be reused with changed inputs")
                    if existing["result"]:
                        return json.loads(existing["result"])
                    raise ValueError("Targeted check is already active or needs explicit recovery")
                store.db.execute("INSERT INTO targeted_check_requests VALUES(?,?,?,?,NULL,'active',NULL,?,NULL)",
                                 (identity, self.request.request_id, payload["check_id"], encoded, time.time()))
            result = {"check_id": payload["check_id"], "attempt_id": payload["attempt_id"],
                      "invocation_id": self.request.request_id, "sample_kind": "targeted"}
            try:
                artifact = self.artifacts.freeze(self.workspace)
                with store.transaction():
                    store.db.execute("UPDATE targeted_check_requests SET artifact=? WHERE id=?", (artifact, identity))
                result["artifact_id"] = artifact
                execution = self.directory / "targeted_workspaces" / identity
                if execution.exists():
                    raise ValueError("Targeted execution workspace already exists")
                self.artifacts.materialize(artifact, execution)
                self.evaluator.prepare(artifact, {"targeted": tuple(tasks)})
                # Make admitted asynchronous work visible before the bounded
                # evaluator blocks. This receipt is not a task result/reward.
                write_json(self.root / "latest.json", {
                    **result, "status": "pending", "snapshot_id": None,
                    "outcomes": None,
                })
                evidence = self.evaluator.evaluate(artifact, execution, "targeted", tuple(tasks))
                evidence.validate_tasks(tasks)
                if evidence.artifact_id != artifact or evidence.stage != "targeted":
                    raise ValueError("Targeted evidence identifies another artifact or stage")
                store.save_evaluation(evidence.identity, artifact, "targeted", asdict(evidence))
                copies, projections, inspections = [], [], []
                for index, reference in enumerate(evidence.evidence):
                    source = Path(reference)
                    if not source.is_absolute():
                        continue  # Symbolic references are retained in the result.
                    # Trust only inventory-verified task evidence belonging to
                    # this run; model-authored paths never enter this channel.
                    resolved = source.resolve()
                    relative = resolved.relative_to(self.directory).parts
                    if (source.is_symlink() or len(relative) < 4 or relative[0] != "tasks"
                            or relative[3] != "evidence" or not resolved.is_dir()):
                        raise ValueError("Targeted result lacks trusted run-local task evidence")
                    inventory = evidence_inventory(source)
                    destination = self.root / identity / f"task-{index}"
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    projections.append(readonly_projection(source, destination))
                    if evidence_inventory(destination) != inventory:
                        raise ValueError("Targeted evidence changed during handoff")
                    inspections.extend(inspection_catalog(destination, inventory))
                    copies.append(str(destination))
                result.update(status="completed", snapshot_id=evidence.identity, outcomes=dict(evidence.outcomes),
                              evidence=list(evidence.evidence), readable_evidence=copies,
                              readable_storage=projections, sqlite_inspection=inspections)
                if self.artifacts.freeze(execution) != artifact:
                    raise ValueError("Targeted stage input changed from its frozen artifact")
            except Exception as failure:
                result.update(status="failed", error=f"{type(failure).__name__}: {failure}")
            with store.transaction():
                store.db.execute("UPDATE targeted_check_requests SET state=?,result=?,finished=? WHERE id=?",
                                 (result["status"], canonical_json(result), time.time(), identity))
            write_json(self.root / (identity + ".json"), result)
            if result["status"] == "completed":
                # Both the authoritative DB result and its handoff receipt are
                # durable before this temporary outer input can be retired.
                retire_workspace(self.directory, execution, self.artifacts, artifact,
                                 self.root / (identity + ".workspace_retention.json"))
            return result
        finally:
            store.close()


class CheckMonitor:
    """Serve requests while the native Goal runs; drain admitted device work."""

    def __init__(self, service):
        self.service = service
        self.stop = threading.Event()
        self.admission = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        seen = None
        while not self.stop.is_set():
            try:
                request_path = Path(self.service.channel["request_path"])
                if request_path.is_symlink() or request_path.parent.is_symlink():
                    if seen != "symlink":
                        write_json(self.service.root / "latest.json", {"status": "rejected", "check_id": None,
                                   "error": "Device check requests cannot use symbolic links"})
                    seen = "symlink"
                    self.stop.wait(.1)
                    continue
                payload = json.loads(request_path.read_text())
                signature = fingerprint(payload)
                if signature != seen:
                    with self.admission:
                        if self.stop.is_set():
                            return
                        seen = signature
                    try:
                        result = self.service.handle(payload)
                    except Exception as failure:
                        result = {"check_id": payload.get("check_id") if isinstance(payload, dict) else None,
                                  "status": "rejected", "error": f"{type(failure).__name__}: {failure}"}
                    write_json(self.service.root / "latest.json", result)
            except (OSError, ValueError):
                pass  # A missing/incomplete request is not an admitted check.
            self.stop.wait(.1)

    def close(self):
        with self.admission:
            self.stop.set()
        self.thread.join()  # Trusted evaluation has per-task retry/cleanup bounds.
