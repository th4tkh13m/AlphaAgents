"""Persistent per-instance evidence in the controller's authoritative SQLite DB."""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from alpha_agents.tree_search.core.mcts.contracts import (
    canonical_json,
    fingerprint,
    finite,
)
from alpha_agents.tree_search.core.mcts.storage import TreeStore
from alpha_agents.tree_search.infrastructure.evidence_sqlite import (
    inspection_catalog,
    prepare_inspection_databases,
)


class TaskIncomplete(RuntimeError):
    pass


class EvidenceCorrupt(TaskIncomplete):
    pass


def instance_seed(seed: int, task: str, index=0):
    return int(hashlib.sha256(f"{seed}_{task}_{index}".encode()).hexdigest(), 16) % (2**32)


@dataclass(frozen=True)
class TaskIdentity:
    artifact_id: str
    template: str
    seed: int
    model_revision: str
    evaluator_revision: str
    fixture_identity: str
    runner_configuration: dict
    runtime_limits: dict
    instance_index: int = 0
    sample_kind: str = "canonical"
    repetition: int = 0

    def __post_init__(self):
        if not all((self.artifact_id, self.template, self.model_revision, self.evaluator_revision, self.fixture_identity)):
            raise ValueError("Task cache requires explicit artifact, model, evaluator and fixture identities")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or isinstance(self.instance_index, bool) or self.instance_index != 0:
            raise ValueError("V1 freezes one index-zero instance at an integer seed")
        if not self.runner_configuration or not self.runtime_limits:
            raise ValueError("Runner configuration and runtime limits must be part of cache identity")
        if self.sample_kind not in {"canonical", "targeted"}:
            raise ValueError("Separate canonical benchmark samples from targeted diagnostics")
        if isinstance(self.repetition, bool) or not isinstance(self.repetition, int) or self.repetition < 0:
            raise ValueError("Repetition must be a nonnegative integer")
        canonical_json(asdict(self))

    @property
    def contract(self):
        return {**asdict(self), "generated_seed": instance_seed(self.seed, self.template, self.instance_index)}

    @property
    def key(self):
        return fingerprint(self.contract)


def evidence_inventory(root: Path):
    root = root.resolve()
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise EvidenceCorrupt("Trusted task evidence must not contain mutable symlink targets")
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    digest.update(block)
            files.append({"path": str(path.relative_to(root)), "size": path.stat().st_size,
                          "sha256": digest.hexdigest()})
    if not files:
        raise EvidenceCorrupt("Completed task has no retained evidence")
    return files


class TaskCache:
    """Open a short-lived connection per operation, never across device work."""

    def __init__(self, directory: Path, config, objective):
        self.directory, self.config, self.objective = directory, config, objective

    @contextmanager
    def store(self):
        store = TreeStore(self.directory, self.config, self.objective)
        try:
            yield store
        finally:
            store.close()

    def register(self, owner: str, artifact: str, stage: str, identities: tuple[TaskIdentity, ...]):
        if stage not in {"screen", "s0", "targeted", "selection"}:
            raise ValueError("Confirmation is excluded from V1 task admission")
        if not owner or not identities or len({(i.template, i.repetition) for i in identities}) != len(identities):
            raise ValueError("Register a nonempty batch of unique task/repetition identities")
        if any(i.artifact_id != artifact for i in identities):
            raise ValueError("Task batch artifact mismatch")
        tasks, jobs = [i.template for i in identities], [i.key for i in identities]
        batch = fingerprint({"owner": owner, "artifact": artifact, "stage": stage, "tasks": tasks, "jobs": jobs})
        with self.store() as store, store.transaction():
            for identity in identities:
                encoded = canonical_json(identity.contract)
                prior = store.db.execute("SELECT contract FROM task_jobs WHERE id=?", (identity.key,)).fetchone()
                if prior and prior[0] != encoded:
                    raise ValueError("Task contract fingerprint collision")
                store.db.execute("INSERT OR IGNORE INTO task_jobs VALUES(?,?, 'registered',1,NULL,NULL)", (identity.key, encoded))
            store.db.execute("INSERT OR IGNORE INTO task_batches VALUES(?,?,?,?,?,?)",
                             (batch, owner, artifact, stage, canonical_json(tasks), canonical_json(jobs)))
        return batch

    def register_dispatch(self, dispatch: str, job: str, session: str, pool_size: int, owner: str, stage: str):
        if isinstance(pool_size, bool) or not 1 <= pool_size <= 9 or stage not in {"screen", "s0", "targeted", "selection"}:
            raise ValueError("Dispatch requires a permitted stage and configured device pool")
        with self.store() as store, store.transaction():
            store.db.execute("INSERT INTO task_dispatches VALUES(?,?,?,?,?,?,'queued',?,NULL,NULL,NULL,NULL,NULL)",
                             (dispatch, job, session, pool_size, owner, stage, time.time()))

    def start_dispatch(self, dispatch: str, device: str, queue_delay: float):
        if finite(queue_delay, "queue delay") < 0:
            raise ValueError("Queue delay cannot be negative")
        with self.store() as store, store.transaction():
            updated = store.db.execute("UPDATE task_dispatches SET state='running',started=?,device=?,queue_delay=? "
                "WHERE id=? AND state='queued'", (time.time(), device, finite(queue_delay, "queue delay"), dispatch))
            if updated.rowcount != 1:
                raise ValueError("Only a queued dispatch can begin")

    def finish_dispatch(self, dispatch: str, state: str, elapsed: float):
        if state not in {"completed", "failed"} or finite(elapsed, "callback duration") < 0:
            raise ValueError("Dispatch requires observed nonnegative duration and terminal state")
        with self.store() as store, store.transaction():
            updated = store.db.execute("UPDATE task_dispatches SET state=?,finished=?,callback_seconds=? "
                "WHERE id=? AND state='running'", (state, time.time(), elapsed, dispatch))
            if updated.rowcount != 1:
                raise ValueError("Only a running dispatch can finish")

    def finish_unstarted_dispatch(self, dispatch: str):
        with self.store() as store, store.transaction():
            store.db.execute("UPDATE task_dispatches SET state='not_started',finished=? WHERE id=? AND state='queued'",
                             (time.time(), dispatch))

    def cached(self, identity: TaskIdentity, batch: str | None = None):
        with self.store() as store:
            row = store.db.execute("SELECT * FROM task_jobs WHERE id=?", (identity.key,)).fetchone()
            if row is None or row["state"] != "completed":
                return None
            payload = json.loads(row["result"])
            if fingerprint(payload) != row["result_hash"]:
                raise EvidenceCorrupt("Cached task result fingerprint changed")
            root = Path(payload["evidence_root"])
            if not root.is_dir() or evidence_inventory(root) != payload["inventory"]:
                raise EvidenceCorrupt("Cached task evidence is missing or changed; first outcome cannot be replaced")
            if batch:
                with store.transaction():
                    store.db.execute("INSERT INTO task_reuses VALUES(?,?,?,?,?)",
                                     (store._id("reuse"), batch, identity.key, row["result_hash"], time.time()))
            return payload

    def begin(self, identity: TaskIdentity, device: str, output: Path):
        with self.store() as store, store.transaction():
            row = store.db.execute("SELECT * FROM task_jobs WHERE id=?", (identity.key,)).fetchone()
            if row is None:
                raise ValueError("Mandatory task must be registered before device admission")
            if row["state"] == "completed":
                raise TaskIncomplete("Completed first outcome is immutable; use its cache")
            if row["state"] == "running":
                raise TaskIncomplete("Another execution owns this task instance")
            ordinal = store.db.execute("SELECT count(*) FROM task_runs WHERE job=?", (identity.key,)).fetchone()[0] + 1
            additional = store.db.execute(
                "SELECT coalesce(sum(attempts),0) FROM task_recovery_rounds WHERE job=?", (identity.key,)).fetchone()[0]
            if ordinal > 1 + row["retry_limit"] + additional:
                raise TaskIncomplete("Infrastructure retry allowance exhausted; explicit recovery required")
            run = store._id("task-run")
            store.db.execute("INSERT INTO task_runs VALUES(?,?,?,?, 'running',?,?,NULL,NULL)",
                             (run, identity.key, ordinal, device, str(output.resolve()), time.time()))
            store.db.execute("UPDATE task_jobs SET state='running' WHERE id=?", (identity.key,))
            return run

    def finish(self, run: str, outcome: dict | None, error: str | None, elapsed: float, stage: str,
               unhealthy=False, cost_classification="observed"):
        if finite(elapsed, "observed task lease") < 0:
            raise ValueError("Task lease duration cannot be negative")
        if cost_classification not in {"observed", "upper_bound"}:
            raise ValueError("Interrupted lease bounds must remain distinct from observed durations")
        if (outcome is None) == (error is None):
            raise ValueError("Exactly one trusted outcome or infrastructure error is required")
        if outcome is not None:
            if not isinstance(outcome.get("success"), bool) or not outcome.get("task_name"):
                raise ValueError("Completed outcome needs a normalized boolean and exact template")
            root = Path(outcome["evidence_root"])
            prepare_inspection_databases(root)
            inventory = evidence_inventory(root)
            inspection_catalog(root, inventory)
            outcome = {**outcome, "inventory": inventory}
        with self.store() as store, store.transaction():
            row = store.db.execute("SELECT * FROM task_runs WHERE id=?", (run,)).fetchone()
            if row is None or row["state"] != "running":
                raise ValueError("Only the active task attempt may publish")
            contract = json.loads(store.db.execute("SELECT contract FROM task_jobs WHERE id=?", (row["job"],)).fetchone()[0])
            if outcome is not None and (outcome["task_name"] != contract["template"] or outcome["seed"] != contract["generated_seed"]):
                raise ValueError("Trusted task result identifies a different instance")
            state = "completed" if outcome is not None else "infra_failed"
            encoded = canonical_json(outcome if outcome is not None else {"error": error, "unhealthy": unhealthy})
            store.db.execute("UPDATE task_runs SET state=?,finished=?,payload=? WHERE id=?", (state, time.time(), encoded, run))
            store.db.execute("UPDATE task_jobs SET state=?,result=?,result_hash=? WHERE id=?",
                             (state, encoded if outcome is not None else None, fingerprint(outcome) if outcome is not None else None, row["job"]))
            # One observed lease includes reset, runtime, failure and cleanup.
            store.db.execute("INSERT INTO costs VALUES(?,?,?,?,?,?,?)",
                             (f"lease:{run}", row["job"], stage, "device", elapsed, cost_classification,
                              canonical_json({"device": row["device"], "ordinal": row["ordinal"], "state": state,
                                              "recovered_from_checkpoint": cost_classification == "upper_bound"})))
            if unhealthy:
                store.db.execute("INSERT INTO device_faults VALUES(?,?,?,?,?)",
                                 (store._id("device-fault"), row["device"], row["job"], error, time.time()))

    def reconcile_interrupted(self, process_dead, recover_completed=None):
        """Coordinator only, under its run lock and after owned process cleanup."""
        with self.store() as store:
            rows = list(store.db.execute("SELECT * FROM task_runs WHERE state='running'"))
            reconciled = []
            for row in rows:
                if not process_dead(dict(row)):
                    raise TaskIncomplete("Cannot reclaim task ownership while its process may still be live")
                if recover_completed is not None:
                    recovered = recover_completed(dict(row))
                    if recovered is not None:
                        outcome, elapsed, stage = recovered
                        self.finish(row["id"], outcome, None, 2100, stage, cost_classification="upper_bound")
                        reconciled.append(row["id"])
                        continue
                with store.transaction():
                    store.db.execute("UPDATE task_runs SET state='interrupted',finished=?,payload=? WHERE id=?",
                                     (time.time(), canonical_json({"reason": "confirmed process death"}), row["id"]))
                    store.db.execute("UPDATE task_jobs SET state='interrupted' WHERE id=?", (row["job"],))
                    store.db.execute("INSERT OR IGNORE INTO costs VALUES(?,?,?,?,?,'upper_bound',?)",
                                     (f"interrupted:{row['id']}", row["job"], "interrupted", "device",
                                      2100, canonical_json({"reason": "actual lease duration unavailable; declared task envelope bound"})))
                reconciled.append(row["id"])
            with store.transaction():
                store.db.execute("UPDATE task_dispatches SET state='interrupted',finished=? WHERE state='running'",
                                 (time.time(),))
                store.db.execute("UPDATE task_dispatches SET state='not_started',finished=? WHERE state='queued'",
                                 (time.time(),))
            return reconciled
