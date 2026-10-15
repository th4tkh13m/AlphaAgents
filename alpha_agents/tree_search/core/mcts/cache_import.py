"""Explicit preparation-time receipt imports, separate from execution identity."""

from pathlib import Path

from .contracts import canonical_json, fingerprint
from .task_cache import (
    EvidenceCorrupt,
    TaskCache,
    TaskIdentity,
    TaskIncomplete,
    evidence_inventory,
)


def import_completed(cache: TaskCache, identity: TaskIdentity, source_contract: dict, source_result: dict,
                     source_result_hash: str, evidence_root: Path, provenance: dict,
                     *, evaluator_compatibility: dict | None = None):
    """Reuse an explicitly authorized receipt without inventing a new execution.

    The caller copies sealed evidence into this run and supplies the original
    contract/hash. Task identity must match exactly unless an explicit audited
    evaluator-only migration is supplied; imports never consume a
    device lease, become independent samples, or replace an existing outcome.
    """
    expected = identity.contract
    if evaluator_compatibility is not None:
        migration = evaluator_compatibility
        if (not migration.get("reason") or not migration.get("code_audit")
                or migration.get("source_revision") != source_contract.get("evaluator_revision")
                or migration.get("target_revision") != expected["evaluator_revision"]):
            raise ValueError("Evaluator compatibility requires matching revisions and a code audit")
        expected = {**expected, "evaluator_revision": migration["source_revision"]}
    if source_contract != expected:
        raise ValueError("Imported task contract differs from the frozen instance")
    if fingerprint(source_result) != source_result_hash:
        raise EvidenceCorrupt("Imported task result fingerprint is invalid")
    if (not isinstance(source_result.get("success"), bool)
            or source_result.get("task_name") != identity.template
            or source_result.get("seed") != identity.contract["generated_seed"]):
        raise ValueError("Imported outcome identifies a different task instance")
    if not provenance.get("source_run") or not provenance.get("authorization"):
        raise ValueError("Cache import requires source and explicit authorization")
    target = evidence_root.resolve()
    expected = cache.directory.resolve() / "tasks" / identity.key
    if evidence_root.is_symlink() or not target.is_relative_to(expected):
        raise ValueError("Imported evidence must be copied beneath this task in the new run")
    if evidence_inventory(target) != source_result.get("inventory"):
        raise EvidenceCorrupt("Copied evidence differs from the sealed source inventory")
    result = {**source_result, "evidence_root": str(target)}
    result_hash = fingerprint(result)
    record = {**provenance, "source_contract": source_contract,
              "source_result_hash": source_result_hash, "result_hash": result_hash,
              "kind": "cached_observation", "new_execution": False}
    if evaluator_compatibility is not None:
        record["evaluator_compatibility"] = evaluator_compatibility
        record["target_contract"] = identity.contract
    key = "task_cache_import:" + identity.key
    with cache.store() as store, store.transaction():
        row = store.db.execute("SELECT * FROM task_jobs WHERE id=?", (identity.key,)).fetchone()
        prior = store.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is not None:
            if (row["state"] == "completed" and row["contract"] == canonical_json(identity.contract)
                    and row["result_hash"] == result_hash and prior
                    and prior[0] == canonical_json(record)):
                return result
            raise TaskIncomplete("An existing task instance cannot be overwritten by an import")
        store.db.execute("INSERT INTO task_jobs VALUES(?,?,'completed',1,?,?)",
                         (identity.key, canonical_json(identity.contract), canonical_json(result), result_hash))
        store.db.execute("INSERT INTO meta VALUES(?,?)", (key, canonical_json(record)))
    return result
