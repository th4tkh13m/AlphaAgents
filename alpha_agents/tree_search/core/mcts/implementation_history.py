"""Curate lineage context from authoritative implementation JSON.

The current artifact's incoming implementation is included for proposal and value
forecasting. The root has no incoming implementation and is always excluded.
"""
from __future__ import annotations

import json

from alpha_agents.tree_search.core.mcts.contracts import fingerprint

REPORT_FIELDS = ("status", "changes", "verification", "check_requests", "blockers", "limitations")


def implementation_history(store, node: str) -> dict:
    lineage = []
    seen = set()
    while node is not None:
        if node in seen:
            raise ValueError("Cycle in implementation ancestry")
        seen.add(node)
        row = store._row("nodes", node)
        if row["parent"] is None:
            break
        attempt = store.db.execute("SELECT id FROM attempts WHERE child=?", (node,)).fetchall()
        if len(attempt) != 1:
            raise ValueError("Lineage node must have exactly one implementation attempt")
        attempt_id = attempt[0][0]
        report = store.db.execute(
            "SELECT payload FROM implementation_reports WHERE attempt=?", (attempt_id,)).fetchone()
        entry = {"node_id": node, "parent_node_id": row["parent"],
                 "artifact_id": row["artifact"], "plan_id": row["plan"],
                 "attempt_id": attempt_id}
        if report is None:
            # Older runs may predate persisted reports. Do not invent history or
            # read model-authored paths from another invocation to fill the gap.
            entry.update({"availability": "unavailable", "reason": "No persisted implementation JSON"})
        else:
            payload = json.loads(report[0])
            entry.update({"availability": "available", "report_id": fingerprint(payload),
                          "implementation": {key: payload[key] for key in REPORT_FIELDS if key in payload}})
        score = store.score(node) if row["score"] else None
        if score is not None:
            entry["canonical_outcome"] = {"u": score.u, "j": score.j, "f": score.f,
                                           "u_snapshot_id": score.u_snapshot_id,
                                           "j_snapshot_id": score.j_snapshot_id}
        lineage.append(entry)
        node = row["parent"]
    lineage.reverse()
    result = {"version": "implementation-lineage-v1", "includes_current_node": True,
              "excludes_root": True, "implementations": lineage}
    return {**result, "snapshot_id": fingerprint(result)}
