"""Read historical root receipts and import matching single-task observations."""

import json
import shutil
from pathlib import Path

from alpha_agents.tree_search.core.mcts.cache_import import import_completed
from alpha_agents.tree_search.core.mcts.contracts import fingerprint
from alpha_agents.tree_search.infrastructure.sqlite_snapshot import snapshot_connection


def root_receipts(source):
    source = Path(source).resolve()
    with snapshot_connection(source / "tree.sqlite3") as database:
        root = database.execute("SELECT artifact FROM nodes WHERE parent IS NULL AND eligible=1").fetchone()
        if root is None:
            raise ValueError("Cached source has no completed root")
        receipts = []
        for row in database.execute("SELECT contract,result,result_hash FROM task_jobs WHERE state='completed' ORDER BY id"):
            contract = json.loads(row["contract"])
            if contract["artifact_id"] != root["artifact"] or contract.get("sample_kind", "canonical") != "canonical":
                continue
            result = json.loads(row["result"])
            if fingerprint(result) != row["result_hash"] or not isinstance(result.get("success"), bool):
                raise ValueError("Cached root receipt is not a sealed binary outcome")
            receipts.append({"contract": contract, "result": result, "result_hash": row["result_hash"]})
    by_task = {}
    for receipt in receipts:
        task = receipt["contract"]["template"]
        if task in by_task and by_task[task] != receipt:
            raise ValueError("Root has conflicting canonical task receipts")
        by_task[task] = receipt
    return {"source_run": str(source), "artifact_id": root["artifact"], "receipts": by_task,
            "failed_tasks": sorted(task for task, receipt in by_task.items() if not receipt["result"]["success"]),
            "measured_tasks": sorted(by_task)}


def import_root_receipt(application, index, task):
    coordinator = application.coordinator
    artifact = coordinator.artifacts.freeze(coordinator.source)
    receipt = index["receipts"].get(task)
    if not receipt or artifact != receipt["contract"]["artifact_id"]:
        return False  # A carried harness needs its own measurement on this task.
    evaluator = coordinator.evaluator
    identity = evaluator._identity(artifact, task, "s0")
    old = receipt["contract"]
    target = evaluator.cache.directory / "tasks" / identity.key / "imported" / "evidence"
    if not target.exists():
        shutil.copytree(receipt["result"]["evidence_root"], target)
    compatibility = None
    if old["evaluator_revision"] != identity.evaluator_revision:
        compatibility = {"source_revision": old["evaluator_revision"], "target_revision": identity.evaluator_revision,
                         "reason": "User requested cached-root reuse for a new sequential single-task experiment",
                         "code_audit": {"scope": "Adapter orchestration adds an empty Screen partition and one-task selection; grading, task generation and outcome normalization remain unchanged.",
                                        "target_revision": identity.evaluator_revision}}
    import_completed(evaluator.cache, identity, old, receipt["result"], receipt["result_hash"], target,
                     {"source_run": index["source_run"], "authorization": "User requested start from cached root",
                      "source_task": task}, evaluator_compatibility=compatibility)
    return True
