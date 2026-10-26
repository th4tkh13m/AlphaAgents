"""Evidence-bound single-task success admission and durable stopping receipt."""

import json
from dataclasses import asdict

from .contracts import canonical_json, fingerprint
from .judge_review import review_score
from .ports import StageEvidence

VERSION = "single-task-repeated-success-strict-root-judge-v2"


def enabled(store):
    return bool(store.config.evaluation_task and store.config.stop_on_task_success)


def qualifying_success(store, node, *, respect_policy=True):
    """Require canonical success and a completed, identity-bound usable review.

    No scalar threshold or engineering narrative substitutes for the persisted
    review. The original task-tree root is the fixed comparison baseline.
    Missing or contradictory records cannot trigger early stopping.
    """
    if not store.config.evaluation_task or (respect_policy and not enabled(store)):
        return None
    root = store.db.execute("SELECT value FROM meta WHERE key='root'").fetchone()
    candidate = store._row("nodes", node)
    if root is None or not candidate["eligible"] or candidate["score"] is None:
        return None
    score, baseline = store.score(node), store.score(root[0])
    is_root = node == root[0]
    if score.u != 1 or (not is_root and score.j <= baseline.j):
        return None
    for current, is_candidate in ((baseline, False), (score, True)):
        record = store.db.execute("SELECT payload FROM judge_snapshots WHERE id=?", (current.j_snapshot_id,)).fetchone()
        if record is None:
            return None
        review = json.loads(record[0])
        if review.get("status") != "completed":
            return None
        try:
            judged, _ = review_score(review)
        except (ValueError, KeyError, TypeError):
            return None
        if (fingerprint(review) != current.j_snapshot_id or judged != current.j
                or review["artifact_id"] != current.artifact_id
                or review["canonical_evidence_id"] != current.u_snapshot_id
                or review["rubric_version"] != store.objective.rubric_version):
            return None
        # The root may itself be compromised. Its low effective J must never
        # allow a manipulated candidate to satisfy the numeric comparison.
        if is_candidate and review.get("integrity", {}).get("controller_flag") == "confirmed_manipulation":
            return None
    record = store.db.execute("SELECT payload FROM evaluation_snapshots WHERE id=?", (score.u_snapshot_id,)).fetchone()
    if record is None:
        return None
    data = json.loads(record[0])
    try:
        evidence = StageEvidence(data["artifact_id"], data["stage"], data["outcomes"], tuple(data["evidence"]), data.get("samples"))
    except (ValueError, KeyError, TypeError):
        return None
    try:
        evidence.validate_repetitions(store.config.evaluation_repetitions)
    except ValueError:
        return None
    if (evidence.identity != score.u_snapshot_id or evidence.artifact_id != score.artifact_id
            or evidence.stage != "s0" or evidence.outcomes != {store.config.evaluation_task: True}):
        return None
    return {"version": VERSION, "task": store.config.evaluation_task, "node": node,
            "artifact_id": score.artifact_id, "score": asdict(score),
            "root_node": root[0], "root_score": asdict(baseline),
            "evaluation_repetitions": store.config.evaluation_repetitions,
            "passes": store.config.evaluation_repetitions,
            "rule": "all canonical repetitions pass; child effective J>original root J; root skip uses its own usable review; no confirmed manipulation"}


def record_success(store, node):
    """Called within the caller's transaction so score and stop commit together."""
    result = qualifying_success(store, node)
    if result is not None:
        store.db.execute("INSERT OR IGNORE INTO meta VALUES('task_success_stop',?)", (canonical_json(result),))
    return result


def stop_receipt(store):
    if not enabled(store):
        return None
    row = store.db.execute("SELECT value FROM meta WHERE key='task_success_stop'").fetchone()
    return json.loads(row[0]) if row else None
