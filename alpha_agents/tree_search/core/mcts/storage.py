"""SQLite authority, atomic admission, fenced ownership, and idempotent backup.

One coordinator holds the run lock; each worker opens its own connection.
No transaction is held while a model, evaluator, or filesystem operation runs.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator

from alpha_agents.tree_search.core.mcts.contracts import (
    BACKUP_VERSION,
    PREDICTION_VERSION,
    CombinedScore,
    FixedPUCTConfig,
    Objective,
    Reward,
    canonical_json,
    fingerprint,
    finite,
    judge_aggregate,
    reverse_returns,
    validate_value,
)
from alpha_agents.tree_search.core.mcts.plan_archive import (
    document_hash,
    freeze_document,
)
from alpha_agents.tree_search.core.mcts.planning_scope import (
    declares_same_parent_duplicate,
)
from alpha_agents.tree_search.core.mcts.policy import Edge, prepare_priors, select_edge
from alpha_agents.tree_search.core.shared.storage import run_lock


class CapacityError(RuntimeError):
    """No unreserved admission capacity; protected completion remains intact."""


class OwnershipError(RuntimeError):
    """An expired or competing operation cannot publish."""


class CatalogChanged(OwnershipError):
    """Publication requires another diversity review of newly registered plans."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS nodes(
 id TEXT PRIMARY KEY, artifact TEXT NOT NULL, parent TEXT REFERENCES nodes(id),
 plan TEXT, depth INTEGER NOT NULL CHECK(depth>=0),
 score TEXT, eligible INTEGER NOT NULL DEFAULT 0, closed INTEGER NOT NULL DEFAULT 0,
 n INTEGER NOT NULL DEFAULT 0, prior_version TEXT
);
CREATE TABLE IF NOT EXISTS plans(
 id TEXT PRIMARY KEY, node TEXT NOT NULL REFERENCES nodes(id),
 slot INTEGER NOT NULL, document TEXT NOT NULL, action_key TEXT NOT NULL,
 metadata TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0, w REAL NOT NULL DEFAULT 0,
 UNIQUE(node, slot), UNIQUE(node, action_key)
);
CREATE TABLE IF NOT EXISTS priors(
 id TEXT PRIMARY KEY, node TEXT NOT NULL REFERENCES nodes(id),
 evidence TEXT NOT NULL, assessment TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operations(
 node TEXT PRIMARY KEY REFERENCES nodes(id), kind TEXT NOT NULL,
 owner TEXT NOT NULL, token INTEGER NOT NULL, heartbeat REAL NOT NULL,
 request TEXT NOT NULL, evidence TEXT NOT NULL, active INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS trials(
 id TEXT PRIMARY KEY, state TEXT NOT NULL, launched INTEGER NOT NULL UNIQUE,
 frontier TEXT NOT NULL REFERENCES nodes(id), stop TEXT
);
CREATE TABLE IF NOT EXISTS attempts(
 id TEXT PRIMARY KEY, trial TEXT NOT NULL REFERENCES trials(id),
 plan TEXT NOT NULL REFERENCES plans(id), slot INTEGER NOT NULL,
 state TEXT NOT NULL, child TEXT REFERENCES nodes(id), error TEXT,
 n INTEGER NOT NULL DEFAULT 0, w REAL NOT NULL DEFAULT 0,
 UNIQUE(plan, slot)
);
CREATE TABLE IF NOT EXISTS selections(
 trial TEXT NOT NULL REFERENCES trials(id), position INTEGER NOT NULL,
 parent TEXT NOT NULL REFERENCES nodes(id), plan TEXT NOT NULL REFERENCES plans(id),
 attempt TEXT NOT NULL REFERENCES attempts(id), prior_version TEXT NOT NULL REFERENCES priors(id),
 implementation_priors TEXT NOT NULL, released INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(trial,position)
);
CREATE TABLE IF NOT EXISTS path(
 trial TEXT NOT NULL REFERENCES trials(id), position INTEGER NOT NULL,
 parent TEXT NOT NULL REFERENCES nodes(id), child TEXT NOT NULL REFERENCES nodes(id),
 plan TEXT NOT NULL REFERENCES plans(id), attempt TEXT NOT NULL REFERENCES attempts(id),
 reward TEXT NOT NULL, PRIMARY KEY(trial,position)
);
CREATE TABLE IF NOT EXISTS reservations(
 owner TEXT NOT NULL, purpose TEXT NOT NULL, remaining INTEGER NOT NULL CHECK(remaining>=0),
 PRIMARY KEY(owner,purpose)
);
CREATE TABLE IF NOT EXISTS invocations(
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, purpose TEXT NOT NULL, started REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS role_reuses(
 id TEXT PRIMARY KEY, source TEXT NOT NULL REFERENCES invocations(id),
 owner TEXT NOT NULL, purpose TEXT NOT NULL, recorded REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS evaluation_snapshots(
 id TEXT PRIMARY KEY, artifact TEXT NOT NULL, stage TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS judge_snapshots(
 id TEXT PRIMARY KEY, artifact TEXT NOT NULL, canonical_id TEXT NOT NULL,
 rubric TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(artifact,canonical_id,rubric)
);
CREATE TABLE IF NOT EXISTS judge_narrative_reviews(
 id TEXT PRIMARY KEY, judge_id TEXT NOT NULL UNIQUE REFERENCES judge_snapshots(id),
 invocation TEXT NOT NULL REFERENCES invocations(id), payload TEXT NOT NULL,
 reason TEXT NOT NULL, provenance TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS archive_evidence_snapshots(
 id TEXT PRIMARY KEY, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assessment_timings(
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, artifact TEXT NOT NULL,
 state TEXT NOT NULL, started REAL NOT NULL, finished REAL, events TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS targeted_check_requests(
 id TEXT PRIMARY KEY, invocation TEXT NOT NULL REFERENCES invocations(id),
 check_id TEXT NOT NULL, payload TEXT NOT NULL, artifact TEXT,
 state TEXT NOT NULL, result TEXT, started REAL NOT NULL, finished REAL,
 UNIQUE(invocation,check_id)
);
CREATE TABLE IF NOT EXISTS recovery_funding(
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, requirements TEXT NOT NULL,
 reason TEXT NOT NULL, recorded REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS frontier_snapshots(
 trial TEXT NOT NULL REFERENCES trials(id), id TEXT NOT NULL, snapshot TEXT NOT NULL,
 PRIMARY KEY(trial,id)
);
CREATE TABLE IF NOT EXISTS values_assessed(
 id TEXT PRIMARY KEY, trial TEXT NOT NULL REFERENCES trials(id),
 frontier TEXT NOT NULL, score_id TEXT NOT NULL, capacity TEXT NOT NULL,
 value REAL NOT NULL, provenance TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backups(
 trial TEXT PRIMARY KEY REFERENCES trials(id), payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invocation_grants(
 id TEXT PRIMARY KEY, previous_cap INTEGER NOT NULL, new_cap INTEGER NOT NULL,
 reason TEXT NOT NULL, recorded REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS costs(
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, stage TEXT NOT NULL,
 resource TEXT NOT NULL, seconds REAL NOT NULL CHECK(seconds>=0),
 classification TEXT NOT NULL, metadata TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_jobs(
 id TEXT PRIMARY KEY, contract TEXT NOT NULL, state TEXT NOT NULL,
 retry_limit INTEGER NOT NULL, result TEXT, result_hash TEXT
);
CREATE TABLE IF NOT EXISTS task_batches(
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, artifact TEXT NOT NULL,
 stage TEXT NOT NULL, tasks TEXT NOT NULL, jobs TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_runs(
 id TEXT PRIMARY KEY, job TEXT NOT NULL REFERENCES task_jobs(id),
 ordinal INTEGER NOT NULL, device TEXT NOT NULL, state TEXT NOT NULL,
 output TEXT NOT NULL, started REAL NOT NULL, finished REAL, payload TEXT,
 UNIQUE(job,ordinal)
);
CREATE TABLE IF NOT EXISTS task_dispatches(
 id TEXT PRIMARY KEY, job TEXT NOT NULL REFERENCES task_jobs(id), session TEXT NOT NULL,
 pool_size INTEGER NOT NULL, owner TEXT NOT NULL, stage TEXT NOT NULL, state TEXT NOT NULL,
 queued REAL NOT NULL, started REAL, finished REAL, device TEXT,
 queue_delay REAL, callback_seconds REAL
);
CREATE TABLE IF NOT EXISTS task_recovery_rounds(
 id TEXT PRIMARY KEY, job TEXT NOT NULL REFERENCES task_jobs(id),
 round INTEGER NOT NULL, first_ordinal INTEGER NOT NULL,
 attempts INTEGER NOT NULL CHECK(attempts=2), reason TEXT NOT NULL, recorded REAL NOT NULL,
 UNIQUE(job,round)
);
CREATE TABLE IF NOT EXISTS task_reuses(
 id TEXT PRIMARY KEY, batch TEXT NOT NULL REFERENCES task_batches(id),
 job TEXT NOT NULL REFERENCES task_jobs(id), result_hash TEXT NOT NULL, recorded REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS device_faults(
 id TEXT PRIMARY KEY, device TEXT NOT NULL, job TEXT NOT NULL,
 reason TEXT NOT NULL, recorded REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS promotions(
 artifact TEXT PRIMARY KEY, node TEXT NOT NULL REFERENCES nodes(id),
 utility REAL NOT NULL, state TEXT NOT NULL, snapshot TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS finalizations(
 id TEXT PRIMARY KEY, state TEXT NOT NULL, shortlist TEXT NOT NULL,
 payload TEXT, created REAL NOT NULL
);
"""


def decode_score(value: str) -> CombinedScore:
    data = json.loads(value)
    data["objective"] = Objective(**data["objective"])
    return CombinedScore(**data)


class TreeStore:
    @classmethod
    def readonly(cls, directory: Path):
        """Status/export inspection performs no schema, metadata, or WAL writes."""
        instance = cls.__new__(cls)
        instance.directory = directory.resolve()
        instance.db = sqlite3.connect((instance.directory / "tree.sqlite3").as_uri() + "?mode=ro", uri=True,
                                      timeout=30, isolation_level=None)
        instance.db.row_factory = sqlite3.Row
        frozen = json.loads(instance.db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()[0])
        # Historical configs remain inspectable; execution still compares the
        # exact frozen identity below and cannot reuse their undiscounted stats.
        settings = {key: value for key, value in frozen["config"].items() if key != "value_horizon"}
        instance.config, instance.objective = FixedPUCTConfig(**settings), Objective(**frozen["objective"])
        return instance

    def __init__(self, directory: Path, config: FixedPUCTConfig, objective: Objective):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.config, self.objective = config, objective
        self.db = sqlite3.connect(self.directory / "tree.sqlite3", timeout=30, isolation_level=None)
        try:
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=30000")
            self.db.executescript(SCHEMA)
            with self.transaction():
                identity = canonical_json({
                    "controller_type": "fixed_puct", "schema_version": 1,
                    "config": asdict(config), "objective": asdict(objective),
                    "prediction_version": PREDICTION_VERSION, "backup_version": BACKUP_VERSION,
                })
                row = self.db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()
                if row and row[0] != identity:
                    raise ValueError("Resume requires identical frozen configuration and objective")
                self.db.execute("INSERT OR IGNORE INTO meta VALUES('identity',?)", (identity,))
                self.db.execute("INSERT OR IGNORE INTO meta VALUES('invocation_cap',?)", (str(config.role_invocation_cap),))
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def lock(self):
        return run_lock(self.directory)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex}"

    def _row(self, table: str, id: str):
        # Table identifiers are internal constants, never external input.
        if table not in {"nodes", "plans", "trials", "attempts", "priors", "values_assessed", "judge_snapshots", "invocations"}:
            raise ValueError("Unknown record type")
        row = self.db.execute(f"SELECT * FROM {table} WHERE id=?", (id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown {table} record {id}")
        return row

    def budget(self) -> dict[str, int]:
        spent = self.db.execute("SELECT count(*) FROM invocations").fetchone()[0]
        reserved = self.db.execute("SELECT coalesce(sum(remaining),0) FROM reservations").fetchone()[0]
        cap = int(self.db.execute("SELECT value FROM meta WHERE key='invocation_cap'").fetchone()[0])
        return {"cap": cap, "spent": spent, "reserved": reserved, "available": cap - spent - reserved}

    def grant_task_recovery(self, job: str, reason: str, recovery_id: str) -> dict:
        """An explicit operator round permits two more infrastructure attempts.

        Ordinals, task contracts, completed outcomes and original retry limits
        stay unchanged. A stable request ID makes CLI retries idempotent.
        """
        if not all(isinstance(value, str) and value.strip() for value in (job, reason, recovery_id)):
            raise ValueError("Task recovery requires a job, reason, and stable recovery ID")
        with self.transaction():
            previous = self.db.execute("SELECT * FROM task_recovery_rounds WHERE id=?", (recovery_id,)).fetchone()
            if previous:
                if previous["job"] != job or previous["reason"] != reason:
                    raise ValueError("Recovery ID already identifies a different grant")
                return dict(previous)
            task = self.db.execute("SELECT * FROM task_jobs WHERE id=?", (job,)).fetchone()
            if task is None:
                raise ValueError("Unknown task job")
            if task["state"] not in {"infra_failed", "interrupted"}:
                raise ValueError("Only exhausted, inactive infrastructure failures can receive recovery")
            consumed = self.db.execute("SELECT count(*) FROM task_runs WHERE job=?", (job,)).fetchone()[0]
            granted = self.db.execute("SELECT coalesce(sum(attempts),0) FROM task_recovery_rounds WHERE job=?", (job,)).fetchone()[0]
            allowance = 1 + task["retry_limit"] + granted
            if consumed != allowance or self.db.execute(
                    "SELECT 1 FROM task_runs WHERE job=? AND state='running'", (job,)).fetchone():
                raise ValueError("Existing task attempt allowance must be exhausted before a new recovery round")
            number = self.db.execute("SELECT count(*) FROM task_recovery_rounds WHERE job=?", (job,)).fetchone()[0] + 1
            self.db.execute("INSERT INTO task_recovery_rounds VALUES(?,?,?,?,2,?,?)",
                            (recovery_id, job, number, consumed + 1, reason, time.time()))
            return dict(self.db.execute("SELECT * FROM task_recovery_rounds WHERE id=?", (recovery_id,)).fetchone())

    def grant_invocations(self, new_cap: int, reason: str):
        """Apply an explicit operator grant without changing frozen search inputs."""
        if isinstance(new_cap, bool) or not isinstance(new_cap, int) or not reason.strip():
            raise ValueError("An invocation-cap grant requires an integer cap and audit reason")
        with self.transaction():
            old = self.budget()["cap"]
            if new_cap <= old:
                raise ValueError("An invocation-cap grant must increase the cap")
            self.db.execute("INSERT INTO invocation_grants VALUES(?,?,?,?,?)",
                            (self._id("grant"), old, new_cap, reason, time.time()))
            self.db.execute("UPDATE meta SET value=? WHERE key='invocation_cap'", (str(new_cap),))

    def reserve_value_recovery(self, trial: str):
        """Meter a fresh initial/retry pair; failure rolls back the old reserve."""
        with self.transaction():
            t = self._row("trials", trial)
            if t["state"] != "awaiting_value":
                raise ValueError("Trial is not awaiting value recovery")
            self._release(trial)
            self._reserve(trial, {"value": 2})

    def record_cost(self, id: str, owner: str, stage: str, resource: str, seconds: float,
                    classification: str = "observed", metadata: dict | None = None):
        seconds = finite(seconds, "cost duration")
        if seconds < 0 or classification not in {"observed", "upper_bound", "estimate"}:
            raise ValueError("Costs require nonnegative duration and explicit accounting class")
        if (not isinstance(resource, str) or not resource or not resource.isascii()
                or not resource.replace("_", "").replace("-", "").isalnum()):
            raise ValueError("Cost resource must be a nonempty integration identifier")
        value = (id, owner, stage, resource, seconds, classification, canonical_json(metadata or {}))
        with self.transaction():
            old = self.db.execute("SELECT * FROM costs WHERE id=?", (id,)).fetchone()
            if old:
                if tuple(old) != value:
                    raise ValueError("Cost record identity cannot be overwritten")
                return
            self.db.execute("INSERT INTO costs VALUES(?,?,?,?,?,?,?)", value)

    def save_archive_evidence(self, payload: dict) -> str:
        serialized = canonical_json(payload)
        identity = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        with self.transaction():
            old = self.db.execute("SELECT payload FROM archive_evidence_snapshots WHERE id=?", (identity,)).fetchone()
            if old and old[0] != serialized:
                raise ValueError("Archive evidence snapshot cannot be overwritten")
            self.db.execute("INSERT OR IGNORE INTO archive_evidence_snapshots VALUES(?,?)", (identity, serialized))
        return identity

    def save_evaluation(self, id: str, artifact: str, stage: str, payload: dict):
        if stage not in {"screen", "s0", "selection", "targeted"}:
            raise ValueError("Confirmation is excluded from the v1 workflow")
        serialized = canonical_json(payload)
        if fingerprint(payload) != id or payload.get("artifact_id") != artifact or payload.get("stage") != stage:
            raise ValueError("Evaluation snapshot identity mismatch")
        with self.transaction():
            old = self.db.execute("SELECT payload FROM evaluation_snapshots WHERE id=?", (id,)).fetchone()
            if old and old[0] != serialized:
                raise ValueError("Evaluation snapshot cannot be overwritten")
            self.db.execute("INSERT OR IGNORE INTO evaluation_snapshots VALUES(?,?,?,?)", (id, artifact, stage, serialized))

    def status(self) -> dict:
        from alpha_agents.tree_search.core.mcts.reporting import (
            dispatch_metrics,
            retained_performance,
            reviewer_timings,
            runtime_forecasts,
            value_calibration,
        )

        """JSON-serializable read-only status; accounting never gates devices."""
        def counts(table, column):
            return {row[0]: row[1] for row in self.db.execute(
                f"SELECT {column},count(*) FROM {table} GROUP BY {column}")}
        costs = [dict(row) for row in self.db.execute(
            "SELECT stage,resource,classification,sum(seconds) AS seconds FROM costs GROUP BY stage,resource,classification ORDER BY stage,resource,classification")]
        for row in costs:
            row["hours"] = row["seconds"] / 3600
        plans = [dict(row) for row in self.db.execute(
            "SELECT id,node,n,w,CASE WHEN n=0 THEN 0 ELSE w/n END AS q FROM plans ORDER BY id")]
        lifecycle = {row[0]: json.loads(row[1]) for row in self.db.execute(
            "SELECT key,value FROM meta WHERE key IN ('root_state','search_state')")}
        final = self.db.execute("SELECT state FROM finalizations WHERE id='final'").fetchone()
        lifecycle["finalization"] = final[0] if final else "not_started"
        has_task_recovery = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_recovery_rounds'").fetchone()
        recovery_rounds = [dict(row) for row in self.db.execute(
            "SELECT * FROM task_recovery_rounds ORDER BY recorded,id")] if has_task_recovery else []
        granted_attempts = {}
        for recovery in recovery_rounds:
            granted_attempts[recovery["job"]] = granted_attempts.get(recovery["job"], 0) + recovery["attempts"]
        incomplete_tasks = []
        for row in self.db.execute("SELECT j.*, (SELECT count(*) FROM task_runs r WHERE r.job=j.id) AS consumed "
                                   "FROM task_jobs j WHERE j.state!='completed' ORDER BY j.id"):
            contract = json.loads(row["contract"])
            incomplete_tasks.append({"job_id": row["id"], "state": row["state"],
                "artifact_id": contract["artifact_id"], "task": contract["template"],
                "consumed_attempts": row["consumed"],
                "remaining_attempts": max(0, 1 + row["retry_limit"] + granted_attempts.get(row["id"], 0) - row["consumed"])})
        frozen = json.loads(self.db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()[0])
        return {"controller_type": "fixed_puct", "schema_version": 1,
                "return_contract": {"prediction_version": frozen.get("prediction_version"),
                                    "backup_version": frozen.get("backup_version"),
                                    "discount_factor": frozen["config"].get("discount_factor")},
                "retained_performance": retained_performance(self),
                "value_calibration": value_calibration(self),
                "lifecycle": lifecycle,
                "trials": counts("trials", "state"), "attempts": counts("attempts", "state"),
                "nodes": self.db.execute("SELECT count(*) FROM nodes").fetchone()[0],
                "backups": self.db.execute("SELECT count(*) FROM backups").fetchone()[0],
                # Retain the historical alias for old status consumers.
                "role_invocations": self.budget(), "codex_invocations": self.budget(), "plans": plans, "costs": costs,
                "task_jobs": counts("task_jobs", "state"), "task_attempts": counts("task_runs", "state"),
                "task_recovery_rounds": recovery_rounds, "incomplete_tasks": incomplete_tasks,
                "role_response_reuses": self.db.execute("SELECT count(*) FROM role_reuses").fetchone()[0]
                    if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='role_reuses'").fetchone() else 0,
                "cache_reuses": self.db.execute("SELECT count(*) FROM task_reuses").fetchone()[0],
                "device_faults": [dict(row) for row in self.db.execute("SELECT * FROM device_faults ORDER BY recorded,id")],
                "scheduler_metrics": dispatch_metrics(self),
                "runtime_forecasts": runtime_forecasts(self),
                "reviewer_timings": reviewer_timings(self),
                "targeted_checks": [{**dict(row), "result": json.loads(row["result"]) if row["result"] else None}
                    for row in self.db.execute("SELECT id,invocation,check_id,artifact,state,started,finished,result FROM targeted_check_requests ORDER BY started,id")]
                    if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='targeted_check_requests'").fetchone() else [],
                "confirmation_status": "not_run", "confirmed": False}

    def _reserve(self, owner: str, requirements: dict[str, int]):
        if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in requirements.values()):
            raise ValueError("Invocation reservations must be positive integers")
        if self.budget()["available"] < sum(requirements.values()):
            raise CapacityError("Insufficient unreserved role invocations")
        for purpose, amount in requirements.items():
            self.db.execute("INSERT INTO reservations VALUES(?,?,?)", (owner, purpose, amount))

    def reserve_calls(self, owner: str, requirements: dict[str, int]):
        with self.transaction():
            self._reserve(owner, requirements)

    def reserve_recovery(self, owner: str, requirements: dict[str, int], reason: str):
        """Fund an explicitly requested bounded recovery within the original cap."""
        if not reason or any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in requirements.values()):
            raise ValueError("Recovery funding requires positive requirements and an audit reason")
        with self.transaction():
            existing = {r[0]: r[1] for r in self.db.execute("SELECT purpose,remaining FROM reservations WHERE owner=?", (owner,))}
            deficits = {p: n - existing.get(p, 0) for p, n in requirements.items() if n > existing.get(p, 0)}
            if sum(deficits.values()) > self.budget()["available"]:
                raise CapacityError("Recovery needs additional unreserved role capacity or an audited cap grant")
            for purpose, amount in deficits.items():
                self.db.execute("INSERT INTO reservations VALUES(?,?,?) ON CONFLICT(owner,purpose) DO UPDATE SET remaining=remaining+excluded.remaining",
                                (owner, purpose, amount))
            if deficits:
                self.db.execute("INSERT INTO recovery_funding VALUES(?,?,?,?,?)",
                                (self._id("recovery"), owner, canonical_json(deficits), reason, time.time()))
            return deficits

    def save_judge(self, artifact: str, canonical_id: str, payload: dict):
        if (payload.get("artifact_id") != artifact or payload.get("canonical_evidence_id") != canonical_id or
            payload.get("phase") != "execution" or payload.get("rubric_version") != self.objective.rubric_version):
            raise ValueError("Judge snapshot must identify its exact canonical artifact evidence and rubric")
        id = fingerprint(payload)
        with self.transaction():
            existing = self.db.execute("SELECT id FROM judge_snapshots WHERE artifact=? AND canonical_id=? AND rubric=?",
                                       (artifact, canonical_id, self.objective.rubric_version)).fetchone()
            if existing:
                return existing[0]
            self.db.execute("INSERT OR IGNORE INTO judge_snapshots VALUES(?,?,?,?,?)",
                            (id, artifact, canonical_id, self.objective.rubric_version, canonical_json(payload)))
        return id

    def review_judge_narrative(self, judge_id: str, invocation: str, reason: str, provenance: dict) -> str:
        """Adopt reviewed prose from a metered response without changing rewards.

        The original snapshot remains immutable. A different score or unknown
        convention requires a new experiment, rather than a narrative repair.
        This method is an explicit operator review action, never model publication.
        """
        if not reason.strip() or not provenance:
            raise ValueError("Narrative review requires an explanation and manual provenance")
        original = self._row("judge_snapshots", judge_id)
        call = self._row("invocations", invocation)
        if call["purpose"] != "judge_execution":
            raise ValueError("Narrative review requires a metered execution judge")
        directory = self.directory / "role_requests" / invocation
        request = json.loads((directory / "request.json").read_text())
        payload = json.loads((directory / "response.json").read_text())
        before = json.loads(original["payload"])
        if (request.get("role") != "judge_execution" or request.get("artifact_id") != original["artifact"] or
                payload.get("status") != "completed" or payload.get("phase") != "execution" or
                payload.get("artifact_id") != original["artifact"] or
                payload.get("canonical_evidence_id") != original["canonical_id"] or
                payload.get("rubric_version") != original["rubric"]):
            raise ValueError("Reviewed response must identify the same completed canonical assessment")
        expected = {"robustness", "decisions", "generalizable"}
        if set(payload.get("dimensions", {})) != expected or set(before.get("dimensions", {})) != expected:
            raise ValueError("Narrative review requires all original dimensions")
        judge_aggregate(payload["dimensions"])
        for name in expected:
            if payload["dimensions"][name].get("score") != before["dimensions"][name].get("score"):
                raise ValueError("Narrative review cannot change dimension scores or unknown status")
        record = {"judge_id": judge_id, "invocation": invocation, "payload": payload,
                  "reason": reason, "provenance": provenance}
        id = fingerprint(record)
        with self.transaction():
            existing = self.db.execute("SELECT id FROM judge_narrative_reviews WHERE judge_id=?", (judge_id,)).fetchone()
            if existing and existing[0] != id:
                raise ValueError("An accepted narrative review is immutable")
            self.db.execute("INSERT OR IGNORE INTO judge_narrative_reviews VALUES(?,?,?,?,?,?)",
                            (id, judge_id, invocation, canonical_json(payload), reason, canonical_json(provenance)))
        return id

    def stop_trial(self, trial: str, reason: str):
        if not reason:
            raise ValueError("Trial cutoff requires an explicit reason")
        with self.transaction():
            row = self._row("trials", trial)
            if row["state"] == "completed":
                raise ValueError("Cannot change a committed trial")
            if row["stop"] is None:
                self.db.execute("UPDATE trials SET stop=? WHERE id=?", (reason, trial))

    def consume_call(self, owner: str, purpose: str) -> str:
        with self.transaction():
            updated = self.db.execute(
                "UPDATE reservations SET remaining=remaining-1 WHERE owner=? AND purpose=? AND remaining>0",
                (owner, purpose),
            ).rowcount
            if not updated:
                raise CapacityError("No invocation reserved for this owner and purpose")
            id = self._id("call")
            self.db.execute("INSERT INTO invocations VALUES(?,?,?,?)", (id, owner, purpose, time.time()))
            return id

    def _release(self, owner: str):
        self.db.execute("DELETE FROM reservations WHERE owner=?", (owner,))

    def release_calls(self, owner: str):
        with self.transaction():
            self._release(owner)

    def add_root(self, id: str, score: CombinedScore):
        if score.objective != self.objective:
            raise ValueError("Root objective mismatch")
        with self.transaction():
            if self.db.execute("SELECT 1 FROM nodes").fetchone():
                raise ValueError("A fixed run has exactly one root")
            self.db.execute(
                "INSERT INTO nodes(id,artifact,depth,score,eligible) VALUES(?,?,0,?,1)",
                (id, score.artifact_id, canonical_json(asdict(score))),
            )
            self.db.execute("INSERT INTO meta VALUES('root',?)", (id,))

    def score(self, node: str) -> CombinedScore:
        row = self._row("nodes", node)
        if row["score"] is None:
            raise ValueError("Node has no usable frozen combined score")
        return decode_score(row["score"])

    def reserve_trial(self) -> str:
        with self.transaction():
            count = self.db.execute("SELECT count(*) FROM trials").fetchone()[0]
            if count >= self.config.max_trials:
                raise CapacityError("Trial limit reached")
            active = self.db.execute("SELECT count(*) FROM trials WHERE state!='completed'").fetchone()[0]
            if active >= self.config.trial_workers:
                raise CapacityError("Trial workers occupied or recovery pending")
            if self.db.execute("SELECT 1 FROM trials WHERE state IN ('awaiting_value','awaiting_judge','awaiting_evaluation')").fetchone():
                raise CapacityError("Assessment recovery pending; new admission stopped")
            root = self.db.execute("SELECT value FROM meta WHERE key='root'").fetchone()
            if root is None:
                raise ValueError("A scored root is required before trial admission")
            id = self._id("trial")
            self._reserve(id, {"value": 2})
            self.db.execute("INSERT INTO trials VALUES(?,'active',?,?,NULL)", (id, count + 1, root[0]))
            return id

    def claim_operation(self, node: str, kind: str, owner: str, evidence: str, now: float | None = None):
        if kind not in {"expansion", "refresh"}:
            raise ValueError("Unknown node operation")
        with self.transaction():
            n = self._row("nodes", node)
            if not n["eligible"] or n["depth"] >= self.config.max_depth:
                raise ValueError("Ineligible expansion/refresh node")
            if kind == "expansion" and n["closed"]:
                return None
            # Proposal diversity depends on the complete shared plan archive.
            # Hold its publication reservation through proposal and policy work;
            # another node may still implement, evaluate, or refresh priors.
            # The existing heartbeat/fencing/reclaim protocol also owns this
            # reservation, so a crash cannot leave an untracked mutex behind.
            if kind == "expansion" and self.db.execute(
                "SELECT 1 FROM operations WHERE kind='expansion' AND active=1"
            ).fetchone():
                return None
            if kind == "refresh":
                if not n["closed"] or not n["prior_version"]:
                    raise ValueError("Prior refresh requires registered frozen plans")
                prior = self._row("priors", n["prior_version"])
                if prior["evidence"] == evidence:
                    return None
            old = self.db.execute("SELECT * FROM operations WHERE node=?", (node,)).fetchone()
            if old and old["active"]:
                return None
            token = old["token"] + 1 if old else 1
            request = self._id("operation")
            self._reserve(request, {"proposal": 2, "policy": 2} if kind == "expansion" else {"policy": 2})
            self.db.execute("INSERT OR REPLACE INTO operations VALUES(?,?,?,?,?,?,?,1)",
                            (node, kind, owner, token, time.time() if now is None else now, request, evidence))
            return dict(self.db.execute("SELECT * FROM operations WHERE node=?", (node,)).fetchone())

    def _owned(self, node: str, owner: str, token: int):
        op = self.db.execute("SELECT * FROM operations WHERE node=?", (node,)).fetchone()
        if op is None or not op["active"] or op["owner"] != owner or op["token"] != token:
            raise OwnershipError("Node operation no longer owned by this fencing token")
        return op

    def fail_operation(self, node: str, owner: str, token: int, reason: str):
        """Caller has joined its operation; keep expansion open, release unused calls."""
        with self.transaction():
            op = self._owned(node, owner, token)
            self._release(op["request"])
            self.db.execute("UPDATE operations SET active=0 WHERE node=?", (node,))
            self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"operation_error:{node}", reason))

    def heartbeat(self, node: str, owner: str, token: int, now: float | None = None):
        with self.transaction():
            self._owned(node, owner, token)
            self.db.execute("UPDATE operations SET heartbeat=? WHERE node=?",
                            (time.time() if now is None else now, node))

    def reclaim_operation(self, node: str, token: int, verify_process_dead, now: float | None = None):
        # Verification/cancellation must finish outside a transaction. A heartbeat
        # racing with verification prevents reclaim, even with the same token.
        op = self.db.execute("SELECT * FROM operations WHERE node=?", (node,)).fetchone()
        clock = time.time() if now is None else now
        if not op or not op["active"] or op["token"] != token or clock - op["heartbeat"] < 120:
            raise OwnershipError("Operation is not stale")
        if not verify_process_dead(dict(op)):
            raise OwnershipError("Recorded operation is still live")
        with self.transaction():
            current = self._owned(node, op["owner"], token)
            if current["heartbeat"] != op["heartbeat"]:
                raise OwnershipError("Operation heartbeat changed during process verification")
            self._release(current["request"])
            self.db.execute("UPDATE operations SET active=0, token=token+1 WHERE node=?", (node,))

    def publish_plans(self, node: str, owner: str, token: int, drafts: list[dict], raw: dict[str, float],
                      archive_review: dict, zero_reason: str | None = None,
                      policy_response: dict | None = None,
                      required_plan_count: int | None = None) -> list[str]:
        if required_plan_count is not None and (required_plan_count != 3 or len(drafts) != 3):
            raise ValueError("Expansion requires exactly three distinct plans")
        if not isinstance(archive_review, dict) or archive_review.get("gaps") or "inspected_plan_ids" not in archive_review:
            raise ValueError("Complete archive review with explicit inspected IDs is required")
        if not drafts and not zero_reason:
            raise ValueError("Closing an empty expansion requires a justified reason")
        if len(drafts) > self.config.max_plans:
            raise ValueError("Plan cap exceeded")
        if set(raw) != {draft["id"] for draft in drafts} or len(raw) != len(drafts):
            raise ValueError("Exactly one prior for every distinct proposed plan is required")
        assessment = prepare_priors(raw, self.config.prior_floor) if drafts else None
        with self.transaction():
            op = self._owned(node, owner, token)
            if op["kind"] != "expansion" or self._row("nodes", node)["closed"]:
                raise OwnershipError("Expansion already closed")
            archive = {row[0] for row in self.db.execute("SELECT id FROM plans")}
            if not set(archive_review["inspected_plan_ids"]) <= archive:
                raise ValueError("Archive review references unknown plans")
            # The complete catalog ID set is rechecked at publication. Concurrent
            # new plans require a diversity recheck, not a silent stale publish.
            if set(archive_review.get("catalog_plan_ids", [])) != archive:
                raise CatalogChanged("Plan catalog changed or was incomplete; diversity recheck required")
            for slot, draft in enumerate(drafts, 1):
                if "novelty_status" in draft and draft["novelty_status"] not in {"new_action", "cross_parent_adaptation"}:
                    raise ValueError("Cannot publish a candidate classified as duplicate or inadmissible")
                if "novelty_status" in draft and declares_same_parent_duplicate(draft):
                    raise ValueError("Cannot publish a declared same-parent duplicate")
                required = ("document", "intervention", "novelty", "diversity_audit", "evidence")
                if not all(draft.get(key) for key in required):
                    raise ValueError("Plan requires full document, intervention, novelty, diversity, and evidence")
                if not set(draft.get("closest_plan_ids", [])) <= archive:
                    raise ValueError("Novelty audit references unknown existing plans")
                action_key = fingerprint(normalize_intervention(draft["intervention"]))
                metadata = {**draft, "archive_review": archive_review,
                            "document_hash": document_hash(draft["document"])}
                self.db.execute("INSERT INTO plans(id,node,slot,document,action_key,metadata) VALUES(?,?,?,?,?,?)",
                                (draft["id"], node, slot, draft["document"], action_key, canonical_json(metadata)))
            version = self._id("prior") if assessment else None
            if assessment:
                payload = self._prior_assessment(node, op["evidence"], assessment, policy_response)
                self.db.execute("INSERT INTO priors VALUES(?,?,?,?)",
                                (version, node, op["evidence"], canonical_json(payload)))
            self.db.execute("UPDATE nodes SET closed=1,prior_version=? WHERE id=?", (version, node))
            if zero_reason:
                self.db.execute("INSERT INTO meta VALUES(?,?)", (f"closed_reason:{node}", zero_reason))
            self._release(op["request"])
            self.db.execute("UPDATE operations SET active=0 WHERE node=?", (node,))
        # SQLite commits first; exports can be recovered without another proposal.
        # Never hold the publication transaction during filesystem writes.
        for draft in drafts:
            freeze_document(self.directory, draft["id"], draft["document"],
                            document_hash(draft["document"]))
        return [draft["id"] for draft in drafts]

    def _prior_assessment(self, node, evidence, assessment, response):
        """Bind full policy reasoning to the numeric version in its transaction."""
        payload = asdict(assessment)
        if response is not None:
            if (not isinstance(response, dict) or response.get("priors") != payload["raw"]
                    or set(response.get("plan_ids", [])) != set(payload["raw"])
                    or response.get("evidence_snapshot_id") != evidence
                    or response.get("artifact_id") != self._row("nodes", node)["artifact"]
                    or not response.get("comparisons") or not response.get("uncertainty")
                    or not response.get("evidence")):
                raise ValueError("Policy reasoning must match this artifact, evidence, and raw priors")
            provenance = response.get("controller_provenance")
            if provenance is not None:
                if not isinstance(provenance, dict) or not self.db.execute(
                        "SELECT 1 FROM invocations WHERE id=? AND purpose='policy'",
                        (provenance.get("request_id"),)).fetchone():
                    raise ValueError("Policy provenance references an unknown policy invocation")
        # Older/direct numeric-only publications remain explicitly unknown.
        payload["policy_response"] = response
        payload["policy_response_hash"] = fingerprint(response) if response is not None else None
        return payload

    def refresh_priors(self, node: str, owner: str, token: int, raw: dict[str, float],
                       policy_response: dict | None = None) -> str:
        assessment = prepare_priors(raw, self.config.prior_floor)
        with self.transaction():
            op = self._owned(node, owner, token)
            if op["kind"] != "refresh":
                raise OwnershipError("Not a refresh operation")
            ids = {r[0] for r in self.db.execute("SELECT id FROM plans WHERE node=?", (node,))}
            if set(raw) != ids:
                raise ValueError("Prior refresh must cover the frozen plan set exactly")
            old = self._row("nodes", node)["prior_version"]
            if old and self._row("priors", old)["evidence"] == op["evidence"]:
                raise ValueError("Prior refresh requires newer evidence")
            id = self._id("prior")
            payload = self._prior_assessment(node, op["evidence"], assessment, policy_response)
            self.db.execute("INSERT INTO priors VALUES(?,?,?,?)", (id, node, op["evidence"], canonical_json(payload)))
            self.db.execute("UPDATE nodes SET prior_version=? WHERE id=?", (id, node))
            self._release(op["request"])
            self.db.execute("UPDATE operations SET active=0 WHERE node=?", (node,))
            return id

    def capacity(self, node: str) -> dict[str, Any]:
        n = dict(self._row("nodes", node))
        plans = []
        for p in self.db.execute("SELECT * FROM plans WHERE node=? ORDER BY slot", (node,)):
            attempts = [dict(a) for a in self.db.execute("SELECT * FROM attempts WHERE plan=? ORDER BY slot", (p["id"],))]
            plans.append({"id": p["id"], "remaining": self.config.max_implementations - len(attempts),
                          "attempts": attempts, "n": p["n"], "w": p["w"]})
        pending = any(a["state"] not in {"eligible", "invalid"} for p in plans for a in p["attempts"])
        eligible = any(a["state"] == "eligible" for p in plans for a in p["attempts"])
        op = self.db.execute("SELECT * FROM operations WHERE node=? AND active=1", (node,)).fetchone()
        exhausted = bool(n["closed"] and not op and not pending and not eligible and all(p["remaining"] == 0 for p in plans))
        return {"node": node, "artifact_id": n["artifact"],
                "score": json.loads(n["score"]) if n["score"] else None,
                "depth": n["depth"], "closed": bool(n["closed"]),
                "remaining_plan_slots": 0 if n["closed"] else self.config.max_plans - len(plans),
                "plans": plans, "prior_version": n["prior_version"], "visits": n["n"],
                "pending": pending or bool(op), "exhausted": exhausted,
                "terminal": n["depth"] == self.config.max_depth or exhausted}

    def development_evidence(self, node: str) -> dict:
        """Material observations only: visits/reservations do not trigger calls."""
        parent = self._row("nodes", node)
        attempts = []
        for row in self.db.execute(
                "SELECT a.id,a.plan,a.slot,a.state,a.child,a.error,n.artifact,n.score "
                "FROM attempts a JOIN plans p ON p.id=a.plan LEFT JOIN nodes n ON n.id=a.child "
                "WHERE p.node=? AND a.state IN ('eligible','invalid') ORDER BY a.id", (node,)):
            entry = dict(row)
            entry["score"] = json.loads(entry["score"]) if entry["score"] else None
            entry["evaluations"] = [dict(result) for result in self.db.execute(
                "SELECT id,stage,payload FROM evaluation_snapshots WHERE artifact=? ORDER BY id", (entry["artifact"],))]
            for result in entry["evaluations"]:
                result["payload"] = json.loads(result["payload"])
            attempts.append(entry)
        result = {"node": node, "artifact_id": parent["artifact"],
                  "parent_score": json.loads(parent["score"]), "observed_attempts": attempts}
        review = self._judge_narrative_review(self.score(node).j_snapshot_id)
        if review:
            result["judge_narrative_review_id"] = review["id"]
        return result

    def _judge_narrative_review(self, judge_id: str):
        # Read-only status/export must also support databases predating this
        # additive table without performing a schema migration.
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='judge_narrative_reviews'").fetchone():
            return None
        return self.db.execute("SELECT * FROM judge_narrative_reviews WHERE judge_id=?", (judge_id,)).fetchone()

    def judge_evidence(self, judge_id: str) -> dict:
        original = self._row("judge_snapshots", judge_id)
        result = {"snapshot_id": judge_id, "canonical_id": original["canonical_id"],
                  "rubric": original["rubric"], "payload": json.loads(original["payload"])}
        review = self._judge_narrative_review(judge_id)
        if review:
            result["payload"] = json.loads(review["payload"])
            result["narrative_review"] = {
                "review_id": review["id"], "invocation_id": review["invocation"],
                "original_score_snapshot_id": judge_id,
                "reviewed_response_id": fingerprint(result["payload"]),
                "reason": review["reason"], "provenance_id": fingerprint(json.loads(review["provenance"])),
                "semantics": "Judge prose comes from this explicit manual review. All dimension scores and the original scalar snapshot remain unchanged; original prose is retained in SQLite for audit.",
            }
        return result

    def select_and_reserve(self, trial: str, require_current_priors: bool = False) -> dict[str, Any]:
        import json

        with self.transaction():
            t = self._row("trials", trial)
            if t["state"] != "active" or t["stop"]:
                raise ValueError("Only continuing active trials can select")
            if self.db.execute("SELECT 1 FROM selections WHERE trial=? AND released=0 AND position NOT IN (SELECT position FROM path WHERE trial=?)", (trial, trial)).fetchone():
                raise ValueError("An unfinished selection is already reserved")
            node = self._row("nodes", t["frontier"])
            cap = self.capacity(node["id"])
            if cap["terminal"]:
                return {"kind": "terminal", "capacity": cap}
            used = self.db.execute("SELECT count(*) FROM attempts WHERE trial=?", (trial,)).fetchone()[0]
            if used >= self.config.max_new_attempts_per_trial:
                return {"kind": "cutoff", "capacity": cap, "reason": "trial_attempt_limit"}
            if not node["closed"]:
                archive_owned = self.db.execute(
                    "SELECT 1 FROM operations WHERE kind='expansion' AND active=1"
                ).fetchone()
                return {"kind": "wait" if cap["pending"] or archive_owned else "expand", "node": node["id"]}
            prior = self._row("priors", node["prior_version"])
            if require_current_priors:
                operation = self.db.execute("SELECT 1 FROM operations WHERE node=? AND active=1", (node["id"],)).fetchone()
                if operation:
                    return {"kind": "wait", "node": node["id"]}
                evidence = self.development_evidence(node["id"])
                if prior["evidence"] != fingerprint(evidence):
                    return {"kind": "refresh", "node": node["id"], "evidence_snapshot_id": fingerprint(evidence)}
            priors = json.loads(prior["assessment"])["effective"]
            used = self.db.execute("SELECT count(*) FROM attempts WHERE trial=?", (trial,)).fetchone()[0]
            total = self.db.execute("SELECT count(*) FROM attempts").fetchone()[0]
            if used >= self.config.max_new_attempts_per_trial:
                return {"kind": "cutoff", "capacity": cap, "reason": "trial_attempt_limit"}
            can_generate = (used < self.config.max_new_attempts_per_trial and total < self.config.max_attempts
                            and self.budget()["available"] >= 3)
            choices = []
            for p in self.db.execute("SELECT * FROM plans WHERE node=? ORDER BY id", (node["id"],)):
                attempts = [dict(a) for a in self.db.execute("SELECT * FROM attempts WHERE plan=? ORDER BY slot", (p["id"],))]
                children = [a for a in attempts if a["state"] == "eligible"]
                new = can_generate and len(attempts) < self.config.max_implementations
                reserved = self.db.execute("SELECT 1 FROM selections WHERE plan=? AND released=0", (p["id"],)).fetchone()
                if (new or children) and not reserved:
                    choices.append((p, attempts, children, new))
            if not choices:
                reserved = self.db.execute("SELECT 1 FROM selections WHERE parent=? AND released=0 AND trial!=?", (node["id"], trial)).fetchone()
                return {"kind": "wait" if cap["pending"] or reserved else "cutoff", "capacity": cap}
            selected = select_edge([Edge(p["id"], p["n"], p["w"], priors[p["id"]]) for p, _, _, _ in choices], node["n"], self.config.c_puct)
            p, attempts, children, new = next(item for item in choices if item[0]["id"] == selected.id)
            implementation_priors = {a["id"]: 1 / len(children) for a in children}
            if new:
                aid = self._id("attempt")
                self._reserve(aid, {"implementation": 1, "judge_code": 1, "judge_execution": 1})
                self.db.execute("INSERT INTO attempts(id,trial,plan,slot,state) VALUES(?,?,?,?,'reserved')",
                                (aid, trial, p["id"], len(attempts) + 1))
                child = None
            else:
                edge = select_edge([Edge(str(a["slot"]), a["n"], a["w"], implementation_priors[a["id"]]) for a in children], p["n"], self.config.c_impl)
                a = next(a for a in children if str(a["slot"]) == edge.id)
                aid, child = a["id"], a["child"]
            pos = self.db.execute("SELECT count(*) FROM selections WHERE trial=?", (trial,)).fetchone()[0]
            self.db.execute("INSERT INTO selections VALUES(?,?,?,?,?,?,?,0)",
                            (trial, pos, node["id"], p["id"], aid, node["prior_version"], canonical_json(implementation_priors)))
            return {"kind": "implement" if new else "stored", "attempt": aid, "child": child,
                    "parent": node["id"], "plan": p["id"], "position": pos, "prior_version": node["prior_version"]}

    def register_child(self, attempt: str, child: str, artifact: str):
        with self.transaction():
            a = self._row("attempts", attempt)
            if a["state"] != "reserved":
                raise ValueError("Attempt is not awaiting artifact freeze")
            parent = self._row("nodes", self._row("plans", a["plan"])["node"])
            self.db.execute("INSERT INTO nodes(id,artifact,parent,plan,depth) VALUES(?,?,?,?,?)",
                            (child, artifact, parent["id"], a["plan"], parent["depth"] + 1))
            self.db.execute("UPDATE attempts SET child=?,state='evaluating' WHERE id=?", (child, attempt))

    def publish_score(self, attempt: str, score: CombinedScore):
        with self.transaction():
            a = self._row("attempts", attempt)
            if a["state"] not in {"evaluating", "awaiting_judge"} or not a["child"]:
                raise ValueError("Attempt is not awaiting evaluation/review")
            child = self._row("nodes", a["child"])
            if child["score"] is not None or child["artifact"] != score.artifact_id or score.objective != self.objective:
                raise ValueError("Score must match exact artifact and frozen objective, and cannot be overwritten")
            self.db.execute("UPDATE nodes SET score=?,eligible=1 WHERE id=?", (canonical_json(asdict(score)), child["id"]))
            self.db.execute("UPDATE attempts SET state='eligible' WHERE id=?", (attempt,))
            self._release(attempt)

    def fail_attempt(self, attempt: str, reason: str):
        with self.transaction():
            a = self._row("attempts", attempt)
            if a["state"] in {"eligible", "invalid"}:
                raise ValueError("Completed attempt cannot become invalid")
            self.db.execute("UPDATE attempts SET state='invalid',error=? WHERE id=?", (reason, attempt))
            self.db.execute("UPDATE selections SET released=1 WHERE attempt=?", (attempt,))
            self._release(attempt)
            self.db.execute("UPDATE trials SET stop='invalid_attempt' WHERE id=?", (a["trial"],))

    def append_transition(self, trial: str, position: int):
        with self.transaction():
            t = self._row("trials", trial)
            s = self.db.execute("SELECT * FROM selections WHERE trial=? AND position=?", (trial, position)).fetchone()
            if t["state"] != "active" or s is None or s["released"] or s["parent"] != t["frontier"]:
                raise ValueError("Transition must follow the active reserved path")
            a = self._row("attempts", s["attempt"])
            if a["state"] != "eligible":
                raise ValueError("Only a scored eligible child can extend the path")
            reward = self.score(s["parent"]).reward_to(self.score(a["child"]))
            self.db.execute("INSERT INTO path VALUES(?,?,?,?,?,?,?)",
                            (trial, position, s["parent"], a["child"], s["plan"], a["id"], canonical_json(asdict(reward))))
            self.db.execute("UPDATE trials SET frontier=? WHERE id=?", (a["child"], trial))

    def awaiting(self, trial: str, kind: str):
        if kind not in {"value", "judge", "evaluation"}:
            raise ValueError("Unknown pending assessment")
        with self.transaction():
            t = self._row("trials", trial)
            if t["state"] == "completed":
                raise ValueError("Cannot reopen a completed trial")
            self.db.execute("UPDATE trials SET state=? WHERE id=?", (f"awaiting_{kind}", trial))
            if kind == "judge":
                self.db.execute("UPDATE attempts SET state='awaiting_judge' WHERE trial=? AND state='evaluating'", (trial,))

    def continuation_policy(self) -> dict:
        """Frozen hypothetical procedure; run limits do not truncate its target."""
        identity = self.db.execute("SELECT value FROM meta WHERE key='run_identity'").fetchone()
        return {
            "version": PREDICTION_VERSION, "discount_factor": self.config.discount_factor,
            "objective_id": self.objective.identity,
            "execution_identity": fingerprint(json.loads(identity[0])) if identity else None,
            "selection": {"algorithm": "two-level-puct", "c_puct": self.config.c_puct,
                          "c_impl": self.config.c_impl, "prior_floor": self.config.prior_floor},
            "max_plans": self.config.max_plans, "max_implementations": self.config.max_implementations,
            "expansion": "Propose three admissible frozen plans at an open node; preserve used local capacity.",
            "implementation": "Fill unused slots incrementally first; once full, select exact eligible stored children with implementation PUCT.",
            "assessment": "Apply the same canonical S0 utility and frozen judge rubric/weight to every child.",
            "updates": "Add completed evidence, refresh policy priors when material evidence changes, and update statistics with discounted returns.",
            "computation": "Continue beyond actual depth, trial, attempt and invocation limits; these are execution cutoffs only.",
            "termination": "Absorb only at closed nodes with no unused local slots, eligible stored children or pending work; future rewards are zero.",
            "uncertainty": "Pending work and assessment outages are not termination; later results are unobserved at this snapshot.",
            "discount_unit": "One traversed harness transition, including stored traversal; plan bookkeeping, failed attempts and elapsed time add no discount steps.",
        }

    def snapshot_frontier(self, trial: str | None, *, node: str | None = None) -> dict:
        """Freeze continuation capacity; trial-free snapshots support role review."""
        if (trial is None) == (node is None):
            raise ValueError("Specify exactly one trial or preparation node")
        with self.transaction():
            frontier = self._row("trials", trial)["frontier"] if trial is not None else node
            reachable = []
            pending = [frontier]
            while pending:
                node = pending.pop(0)
                reachable.append(self.capacity(node))
                pending.extend(row[0] for row in self.db.execute(
                    "SELECT id FROM nodes WHERE parent=? AND eligible=1 ORDER BY id", (node,)))
            evidence = {}
            for capacity in reachable:
                node = capacity["node"]
                plans = [dict(row) for row in self.db.execute("SELECT * FROM plans WHERE node=? ORDER BY slot", (node,))]
                for plan in plans:
                    plan["metadata"] = json.loads(plan["metadata"])
                prior = self._row("priors", capacity["prior_version"]) if capacity["prior_version"] else None
                evidence[node] = {"canonical_evidence": self.node_evidence(node), "plans": plans,
                    "development_evidence": self.development_evidence(node),
                    "evaluations": [{"snapshot_id": row["id"], "stage": row["stage"], "payload": json.loads(row["payload"])}
                        for row in self.db.execute("SELECT id,stage,payload FROM evaluation_snapshots WHERE artifact=? "
                            "AND stage IN ('screen','s0','selection','targeted') ORDER BY stage,id", (capacity["artifact_id"],))],
                    "prior": {**dict(prior), "assessment": json.loads(prior["assessment"])} if prior else None,
                    "previous_values": [dict(row) for row in self.db.execute(
                        "SELECT id,score_id,capacity,value,provenance FROM values_assessed WHERE frontier=? ORDER BY rowid", (node,))]}
                for value in evidence[node]["previous_values"]:
                    original_capacity = json.loads(value.pop("capacity"))
                    value["capacity_snapshot_id"] = original_capacity["snapshot_id"]
                    value["capacity_summary"] = {key: field for key, field in original_capacity.items() if key != "reachable_evidence"}
                    value["provenance"] = json.loads(value["provenance"])
            policy = self.continuation_policy()
            snapshot = {**reachable[0], "reachable_subtree": reachable,
                        "reachable_evidence": evidence,
                        "score_id": self.score(frontier).identity,
                        "objective_id": self.objective.identity,
                        "observed_path_rowid": self.db.execute("SELECT coalesce(max(rowid),0) FROM path").fetchone()[0],
                        "discount_factor": self.config.discount_factor,
                        "continuation_policy": policy, "continuation_policy_id": fingerprint(policy),
                        "continuation_version": PREDICTION_VERSION}
            snapshot["snapshot_id"] = fingerprint(snapshot)
            if trial is not None:
                self.db.execute("INSERT OR IGNORE INTO frontier_snapshots VALUES(?,?,?)",
                                (trial, snapshot["snapshot_id"], canonical_json(snapshot)))
            return snapshot

    def node_evidence(self, node: str) -> dict:
        score = self.score(node)
        observed = self.db.execute("SELECT payload FROM evaluation_snapshots WHERE id=?", (score.u_snapshot_id,)).fetchone()
        judge = self.db.execute("SELECT payload FROM judge_snapshots WHERE id=?", (score.j_snapshot_id,)).fetchone()
        screen = self.db.execute("SELECT id,payload FROM evaluation_snapshots WHERE artifact=? AND stage='screen' ORDER BY rowid LIMIT 1", (score.artifact_id,)).fetchone()
        result = {"artifact_id": score.artifact_id, "s0_snapshot_id": score.u_snapshot_id,
                "s0": json.loads(observed[0]) if observed else {"status": "unavailable"},
                "judge_snapshot_id": score.j_snapshot_id,
                "judge": json.loads(judge[0]) if judge else {"status": "unavailable"},
                "screen": {"snapshot_id": screen[0], **json.loads(screen[1])} if screen else {"status": "unavailable"}}
        review = self._judge_narrative_review(score.j_snapshot_id)
        if review:
            judged = self.judge_evidence(score.j_snapshot_id)
            result["judge"] = judged["payload"]
            result["judge_narrative_review"] = judged["narrative_review"]
        references = []
        for reference in result["s0"].get("evidence", []):
            if not isinstance(reference, str):
                continue
            path = Path(reference)
            if not path.is_absolute() or not path.is_relative_to(self.directory):
                continue
            relative = path.relative_to(self.directory).parts
            if (len(relative) == 4 and relative[0] == "tasks" and relative[3] == "evidence"
                    and len(relative[1]) == 64 and all(c in "0123456789abcdef" for c in relative[1])
                    and path.is_dir() and path.resolve() == path):
                references.append({"task_id": relative[1], "evidence_path": reference})
        if references:
            result["canonical_task_references"] = {"version": "canonical-task-reference-index-v1",
                                                    "tasks": references}
        return result

    def persist_value(self, trial: str, value: float, capacity: dict, provenance: dict) -> str:
        with self.transaction():
            t = self._row("trials", trial)
            if t["state"] not in {"active", "awaiting_value"}:
                raise ValueError("Trial is not awaiting frontier assessment")
            frozen = self.db.execute("SELECT snapshot FROM frontier_snapshots WHERE trial=? AND id=?", (trial, capacity.get("snapshot_id"))).fetchone()
            if not frozen or frozen[0] != canonical_json(capacity):
                raise ValueError("Value requires the unchanged persisted frontier capacity snapshot")
            if capacity.get("node") != t["frontier"]:
                raise ValueError("Value capacity snapshot identifies a different frontier")
            score = self.score(t["frontier"])
            value = validate_value(score, value)
            if capacity["exhausted"] and value != 0:
                raise ValueError("Structurally exhausted continuation has zero net value")
            if not provenance:
                raise ValueError("Value requires provenance")
            for key in ("discount_factor", "continuation_policy_id"):
                if key in provenance and provenance[key] != capacity[key]:
                    raise ValueError(f"Value {key} mismatch")
            provenance = {**provenance, "discount_factor": capacity["discount_factor"],
                          "continuation_policy_id": capacity["continuation_policy_id"]}
            id = self._id("value")
            self.db.execute("INSERT INTO values_assessed VALUES(?,?,?,?,?,?,?)",
                            (id, trial, t["frontier"], score.identity, canonical_json(capacity), value, canonical_json(provenance)))
            return id

    def backup(self, trial: str, value_id: str | None = None) -> dict:
        import json

        with self.transaction():
            old = self.db.execute("SELECT payload FROM backups WHERE trial=?", (trial,)).fetchone()
            if old:
                return json.loads(old[0])
            t = self._row("trials", trial)
            if t["state"] == "awaiting_judge":
                raise ValueError("Combined reward cannot be backed up without final judge")
            unfinished = self.db.execute("SELECT 1 FROM selections s WHERE s.trial=? AND s.released=0 AND NOT EXISTS (SELECT 1 FROM path p WHERE p.trial=s.trial AND p.position=s.position)", (trial,)).fetchone()
            if unfinished:
                raise ValueError("Admitted work has not settled")
            path = list(self.db.execute("SELECT * FROM path WHERE trial=? ORDER BY position", (trial,)))
            cap = self.capacity(t["frontier"])
            capacity_at_backup = cap
            if value_id is not None:
                value = self._row("values_assessed", value_id)
                if value["trial"] != trial or value["frontier"] != t["frontier"] or value["score_id"] != self.score(t["frontier"]).identity:
                    raise ValueError("Value assessment does not identify this frozen frontier")
                v = value["value"]
                cap = json.loads(value["capacity"])
            elif not path or cap["exhausted"]:
                v = 0.0
            else:
                raise ValueError("Computational cutoff requires a persisted value, never a zero substitute")
            rewards = [Reward(**json.loads(row["reward"])) for row in path]
            returns = reverse_returns(rewards, v, self.config.discount_factor)
            for row, result in zip(path, returns):
                self.db.execute("UPDATE plans SET n=n+1,w=w+? WHERE id=?", (result["total"], row["plan"]))
                self.db.execute("UPDATE attempts SET n=n+1,w=w+? WHERE id=?", (result["total"], row["attempt"]))
                self.db.execute("UPDATE nodes SET n=n+1 WHERE id=?", (row["parent"],))
            payload = {"trial": trial, "value_id": value_id, "returns": returns, "path": [dict(r) for r in path],
                       "backup_version": BACKUP_VERSION, "discount_factor": self.config.discount_factor,
                       "continuation_policy_id": cap.get("continuation_policy_id", fingerprint(self.continuation_policy())),
                       "empty_failed": not bool(path), "capacity": cap,
                       "capacity_at_backup": capacity_at_backup, "stop_reason": t["stop"]}
            self.db.execute("INSERT INTO backups VALUES(?,?)", (trial, canonical_json(payload)))
            self.db.execute("UPDATE trials SET state='completed' WHERE id=?", (trial,))
            self.db.execute("UPDATE selections SET released=1 WHERE trial=?", (trial,))
            self._release(trial)
            return payload


def normalize_intervention(intervention: dict) -> dict:
    required = {"target", "operation", "conditions", "parameters", "scope", "mechanism"}
    if set(intervention) != required:
        raise ValueError("Intervention requires exactly target, operation, conditions, parameters, scope, and mechanism")
    def normalize(value):
        if isinstance(value, str):
            return " ".join(value.split())
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in sorted(value.items())}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value
    # Preserve parameter values (e.g. case-sensitive file names); semantic
    # equivalence remains a documented proposer judgment, not this exact key.
    result = {key: normalize(value) for key, value in intervention.items()}
    result["parameters"] = intervention["parameters"]
    if not all(result[key] for key in ("target", "operation", "scope", "mechanism")):
        raise ValueError("Intervention mechanism and edit scope cannot be empty")
    return result
