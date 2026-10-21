"""Readable frozen continuation evidence and exact readonly code views."""

from __future__ import annotations

import json

from alpha_agents.tree_search.core.mcts.artifacts import manifest
from alpha_agents.tree_search.core.mcts.plan_archive import freeze_continuation_evidence


def verify_code_views(store, artifacts, views):
    root = store.directory / "value_code_views"
    if root.is_symlink():
        raise ValueError("Value code views cannot use symbolic roots")
    for view in views:
        destination = root / view["artifact_id"]
        if str(destination.resolve()) != view["code_path"] or manifest(destination) != artifacts.verify(view["artifact_id"]):
            raise ValueError("Value code view differs from the frozen artifact")


def restore_continuation(store, context):
    declared = context.get("continuation_evidence")
    if not declared:
        return
    row = store.db.execute("SELECT payload FROM archive_evidence_snapshots WHERE id=?", (declared["evidence_bundle_hash"],)).fetchone()
    if row is None:
        raise ValueError("Continuation evidence has no authoritative snapshot")
    exported = freeze_continuation_evidence(store.directory, json.loads(row[0]))
    if any(declared.get(key) != value for key, value in exported.items()):
        raise ValueError("Continuation evidence identity mismatch")


def continuation_context(store, artifacts, capacity):
    if "reachable_evidence" not in capacity:
        raise ValueError("Value capacity lacks frozen reachable evidence")
    store.save_archive_evidence(capacity)
    frozen = freeze_continuation_evidence(store.directory, capacity)
    root = store.directory / "value_code_views"
    if root.is_symlink():
        raise ValueError("Value code views cannot use symbolic roots")
    root.mkdir(parents=True, exist_ok=True)
    views = []
    for node in capacity["reachable_subtree"]:
        artifact = node["artifact_id"]
        records = artifacts.verify(artifact)
        destination = root / artifact
        artifacts.readonly_view(artifact, root)
        if manifest(destination) != records:
            raise ValueError("Value code view differs from the frozen artifact")
        views.append({"node_id": node["node"], "artifact_id": artifact, "code_path": str(destination.resolve())})
    references = sorted({reference for evidence in capacity["reachable_evidence"].values()
                         for evaluation in evidence["evaluations"]
                         for reference in evaluation["payload"].get("evidence", [])})
    return {"prior_implementations": capacity["prior_implementations"],
            "value_report_contract": capacity["value_report_contract"],
            "capacity": {key: value for key, value in capacity.items() if key != "reachable_evidence"},
            "reachable_attempt_ids": sorted({attempt["id"] for node in capacity["reachable_subtree"]
                                              for plan in node["plans"] for attempt in plan["attempts"]}),
            "continuation_evidence": {**frozen, "capacity_snapshot_id": capacity["snapshot_id"],
                                      "canonical_evidence": references},
            "reachable_code_views": views,
            "canonical_evidence": {key: value for key, value in capacity["reachable_evidence"][capacity["node"]]["canonical_evidence"].items()
                                   if key != "judge"}}
