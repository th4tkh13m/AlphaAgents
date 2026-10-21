"""One owned coordinator, asynchronous trial workers, and resumable finalization."""

from __future__ import annotations

import json
import queue
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict
from pathlib import Path

from alpha_agents.tree_search.core.mcts.artifacts import ArtifactStore
from alpha_agents.tree_search.core.mcts.contracts import canonical_json
from alpha_agents.tree_search.core.mcts.controller import (
    AssessmentPending,
    ScreenRejected,
    TrialExecutor,
)
from alpha_agents.tree_search.core.mcts.finalization import Finalizer
from alpha_agents.tree_search.core.mcts.ports import (
    RoleGateway,
    StagedEvaluator,
    StageEvidence,
)
from alpha_agents.tree_search.core.mcts.recovery import reconcile_process
from alpha_agents.tree_search.core.mcts.storage import CapacityError, TreeStore
from alpha_agents.tree_search.core.mcts.success_stop import (
    qualifying_success,
    record_success,
    stop_receipt,
)
from alpha_agents.tree_search.core.shared.storage import write_json


class FixedPUCTCoordinator:
    def __init__(self, source: Path, directory: Path, config, objective, gateway: RoleGateway, evaluator: StagedEvaluator, settings,
                 screen_tasks, s0_weights, selection_tasks, subset, runtime_identity=None):
        self.source, self.directory = source.resolve(), directory.resolve()
        self.config, self.objective, self.gateway, self.evaluator, self.settings = config, objective, gateway, evaluator, settings
        self.screen_tasks, self.s0_weights = tuple(screen_tasks), dict(s0_weights)
        self.selection_tasks, self.subset = tuple(selection_tasks), subset
        self.runtime_identity = runtime_identity or {}
        self.store = TreeStore(directory, config, objective)
        try:
            self.artifacts = ArtifactStore(directory / "artifacts")
        except BaseException:
            self.store.close()
            raise
        self.stop = threading.Event()
        self.scored = queue.Queue()

    def close(self):
        self.store.close()

    def _status(self):
        return {**self.store.status(), "device_pool": self.evaluator.scheduling_status()}

    def request_stop(self):
        """Stop new admission; already admitted bounded work still drains."""
        self.stop.set()

    def _meta(self, key, value):
        with self.store.transaction():
            self.store.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, canonical_json(value)))

    def _evaluator(self, owner):
        return self.evaluator.for_owner(owner)

    def _executor(self, store, owner):
        return TrialExecutor(store, self.artifacts, self.gateway, self._evaluator(owner), self.settings,
                             self.screen_tasks, self.s0_weights, self.stop, on_score=self.scored.put,
                             selection_tasks=self.selection_tasks)

    def _freeze_run(self):
        root = self.artifacts.freeze(self.source)
        prompt = self.gateway.prepare_request(str(self.directory), "identity", "proposer", root,
                             self.settings.model, self.settings.effort, {})
        record = {"root_artifact_id": root, "settings": asdict(self.settings), "prompt_library": prompt.library_hash,
                  "screen_tasks": self.screen_tasks, "s0_weights": self.s0_weights,
                  "selection_tasks": self.selection_tasks, "subset": self.subset,
                  "runtime_identity": self.runtime_identity}
        encoded = canonical_json(record)
        with self.store.transaction():
            old = self.store.db.execute("SELECT value FROM meta WHERE key='run_identity'").fetchone()
            if old and old[0] != encoded:
                raise ValueError("Resume requires identical root, prompts, role settings, tasks and runtime identity")
            self.store.db.execute("INSERT OR IGNORE INTO meta VALUES('run_identity',?)", (encoded,))
        write_json(self.directory / "run_configuration.json", {**record, "controller": asdict(self.config),
                                                               "objective": asdict(self.objective)})
        write_json(self.directory / "subset.json", self.subset)
        write_json(self.directory / "runtime_forecasts.json", self.store.status()["runtime_forecasts"])
        return root

    def preflight(self):
        with self.store.lock():
            root = self._freeze_run()
            return self._preflight(root)

    def assess_root(self, *, resume=False):
        """Measure only the root, without admitting or recovering search trials."""
        with self.store.lock():
            existed = self.store.db.execute("SELECT 1 FROM meta WHERE key IN ('root_pending','root')").fetchone()
            if existed and not resume:
                raise ValueError("Root already initialized; use resume")
            artifact = self._freeze_run()
            if resume:
                self._reconcile()
            self._preflight(artifact)
            root = self._initialize_root(artifact, resume)
            return {**asdict(self.store.score(root)), "success_stop_qualified": bool(
                qualifying_success(self.store, root, respect_policy=False))}

    def retain_successful_root(self):
        """Verify and export a passing single-task root; never finalize MCTS."""
        with self.store.lock():
            task = self.config.evaluation_task
            if not task:
                raise ValueError("Root-success handoff requires a single-task objective")
            root = self.store.db.execute("SELECT value FROM meta WHERE key='root'").fetchone()
            if root is None:
                raise ValueError("Root-success handoff requires a scored root")
            score = self.store.score(root[0])
            if score.u != 1:
                raise ValueError("Only a passing root can bypass search")
            if not qualifying_success(self.store, root[0], respect_policy=False):
                raise ValueError("Passing root requires a usable completed judge without confirmed manipulation")
            if self.store.db.execute("SELECT 1 FROM trials WHERE state!='completed'").fetchone() or self.store.budget()["reserved"]:
                raise ValueError("Root handoff requires admitted work to drain")
            row = self.store.db.execute("SELECT payload FROM evaluation_snapshots WHERE id=?", (score.u_snapshot_id,)).fetchone()
            if row is None:
                raise ValueError("Passing root requires its canonical observation")
            payload = json.loads(row[0])
            evidence = StageEvidence(payload["artifact_id"], payload["stage"], payload["outcomes"], tuple(payload["evidence"]))
            evidence.validate_tasks((task,))
            if evidence.artifact_id != score.artifact_id or evidence.identity != score.u_snapshot_id or evidence.outcomes != {task: True}:
                raise ValueError("Passing root must match its canonical observation")
            self.evaluator.verify_stage(evidence)
            finalizer = Finalizer(self.store, self.artifacts, self._evaluator("root-handoff"),
                                  self.selection_tasks, self.subset, self.runtime_identity)
            finalizer.export(score.artifact_id, self.directory / "winner", provisional=False)
            result = {"selection_status": "completed", "selection_mode": "passing-root-no-search",
                      "search_skipped": True, "task": task, "winner": {"artifact_id": score.artifact_id, "score": asdict(score)},
                      "selected_actions": [], "completed_rollouts": self.store.db.execute("SELECT count(*) FROM backups").fetchone()[0],
                      "configured_rollouts": self.config.max_trials, "confirmed": False,
                      "reason": "Fresh canonical root already succeeds on this task"}
            write_json(self.directory / "root_handoff.json", result)
            return result

    def _preflight(self, root):
        checks = {"root_artifact_id": root, "controller_type": "fixed_puct"}
        checks[self.gateway.telemetry_name] = self.gateway.preflight()
        checks[self.evaluator.telemetry_name] = self.evaluator.preflight(source=self.source)
        write_json(self.directory / "preflight.json", checks)
        return checks

    def _reconcile(self):
        # The run lock proves that a previous coordinator has exited. Process
        # tags/birth times separately prove its surviving subprocess ownership.
        for call in self.store.db.execute("SELECT id FROM invocations ORDER BY started,id"):
            path = self.directory / "role_requests" / call[0] / "process" / "process.json"
            if not reconcile_process(path):
                raise AssessmentPending("Cannot prove or reclaim a recorded role process")
        self.evaluator.reconcile_interrupted(reconcile_process)
        operations = list(self.store.db.execute("SELECT * FROM operations WHERE active=1"))
        for operation in operations:
            # Preserve the explicit 120-second stale-owner rule even when all
            # old processes have already been proved dead.
            while time.time() - operation["heartbeat"] < 120:
                time.sleep(min(.2, max(.001, 120 - (time.time() - operation["heartbeat"]))))
            self.store.reclaim_operation(operation["node"], operation["token"], lambda _: True)
        # All previous-owner processes have been reconciled. Preserve recorded
        # intervals without inventing a monotonic endpoint after a restart.
        with self.store.transaction():
            self.store.db.execute("UPDATE assessment_timings SET state='interrupted',finished=? WHERE state='running'",
                                  (time.time(),))
            self.store.db.execute("UPDATE targeted_check_requests SET state='interrupted',finished=? WHERE state='active'",
                                  (time.time(),))

    def _initialize_root(self, artifact, resume):
        root = self.store.db.execute("SELECT value FROM meta WHERE key='root'").fetchone()
        if root:
            if self.store.score(root[0]).artifact_id != artifact:
                raise ValueError("Fixed root artifact changed")
            self.store.release_calls("baseline")
            return root[0]
        owner = "baseline"
        pending = self.store.db.execute("SELECT value FROM meta WHERE key='root_pending'").fetchone()
        if pending:
            if not resume:
                raise ValueError("Root initialization exists; use resume")
            if not self.store.db.execute("SELECT 1 FROM judge_snapshots WHERE artifact=?", (artifact,)).fetchone():
                requirements = {"judge_review": 1}
                self.store.reserve_recovery(owner, requirements, "Operator resume: root assessment")
        else:
            with self.store.transaction():
                self.store._reserve(owner, {"judge_review": 1})
                self.store.db.execute("INSERT INTO meta VALUES('root_pending',?)", (canonical_json({"artifact_id": artifact}),))
        executor = self._executor(self.store, owner)
        try:
            score = executor.assess(owner, artifact)
            write_json(self.directory / "baseline_score.json", asdict(score))
            self.store.add_root("root", score)
            self.store.release_calls(owner)
            self._meta("root_state", "completed")
            self._meta("incumbent_s0", score.u)
            write_json(self.directory / "runtime_forecasts.json", self.store.status()["runtime_forecasts"])
        except ScreenRejected as failure:
            self.store.release_calls(owner)
            self._meta("root_state", {"state": "screen_rejected", "error": str(failure)})
            raise
        except Exception as failure:
            self._meta("root_state", {"state": "awaiting_assessment", "error": str(failure)})
            raise
        return "root"

    def _trial(self, trial, recovering):
        store = TreeStore(self.directory, self.config, self.objective)
        try:
            executor = self._executor(store, trial)
            return executor.recover(trial) if recovering else executor.run(trial)
        finally:
            store.close()

    def _offer_promotion(self, node):
        if self.config.evaluation_task:
            return None  # The canonical task is already the complete objective.
        score = self.store.score(node)
        with self.store.transaction():
            row = self.store.db.execute("SELECT value FROM meta WHERE key='incumbent_s0'").fetchone()
            incumbent = float(json.loads(row[0])) if row else self.store.score("root").u
            if score.u <= incumbent:
                return None
            self.store.db.execute("INSERT OR REPLACE INTO meta VALUES('incumbent_s0',?)", (canonical_json(score.u),))
            if self.store.db.execute("SELECT count(*) FROM promotions").fetchone()[0] >= 3:
                return None
            if self.store.db.execute("SELECT 1 FROM promotions WHERE artifact=?", (score.artifact_id,)).fetchone():
                return None
            self.store.db.execute("INSERT INTO promotions VALUES(?,?,?,'pending',NULL,NULL)", (score.artifact_id, node, score.u))
            return score.artifact_id

    def _promotion(self, artifact):
        store = TreeStore(self.directory, self.config, self.objective)
        try:
            row = store.db.execute("SELECT * FROM promotions WHERE artifact=?", (artifact,)).fetchone()
            candidate = {"artifact_id": artifact, "node": row["node"], "u": row["utility"]}
            finalizer = Finalizer(store, self.artifacts, self._evaluator("promotion:" + artifact),
                                  self.selection_tasks, self.subset, self.runtime_identity)
            try:
                result = finalizer._stage(candidate)
            except Exception as failure:
                with store.transaction():
                    store.db.execute("UPDATE promotions SET state='incomplete',error=? WHERE artifact=?", (str(failure), artifact))
                return {"artifact_id": artifact, "status": "incomplete", "error": str(failure)}
            with store.transaction():
                store.db.execute("UPDATE promotions SET state='completed',snapshot=?,error=NULL WHERE artifact=?", (result["snapshot_id"], artifact))
            return result
        finally:
            store.close()

    def run(self, *, resume=False, stop_after_trials=None, finalize=None):
        if stop_after_trials is not None and (isinstance(stop_after_trials, bool) or not isinstance(stop_after_trials, int) or stop_after_trials < 0):
            raise ValueError("Stop-after-trials must be a nonnegative additional-trial count")
        if finalize is None:
            finalize = stop_after_trials is None
        with self.store.lock():
            existed = self.store.db.execute("SELECT 1 FROM meta WHERE key IN ('root_pending','root')").fetchone()
            if existed and not resume:
                raise ValueError("Run already initialized; use resume")
            if self.store.db.execute("SELECT 1 FROM finalizations WHERE id='final'").fetchone():
                if not resume:
                    raise ValueError("Finalization already froze this run")
                self._freeze_run()
                self._reconcile()
                final_selection = self.finalize(locked=True)
                result = {"status": self.store.status(), "errors": [], "promotions": [],
                          "final_selection": final_selection,
                          "selection_status": final_selection["selection_status"],
                          "confirmed": False, "confirmation_status": "not_run"}
                write_json(self.directory / "status.json", result["status"])
                write_json(self.directory / "run_result.json", result)
                return result
            artifact = self._freeze_run()
            if resume:
                self._reconcile()
            self._preflight(artifact)
            self.stop.clear()
            self._initialize_root(artifact, resume)
            # Rediscover committed success before admitting work, including a
            # passing root and recovery after score publication but before export.
            with self.store.transaction():
                for row in self.store.db.execute("SELECT id FROM nodes WHERE eligible=1 ORDER BY depth,id"):
                    if record_success(self.store, row[0]):
                        break
            start_count = self.store.db.execute("SELECT count(*) FROM trials").fetchone()[0]
            pending = [row[0] for row in self.store.db.execute("SELECT id FROM trials WHERE state!='completed' ORDER BY launched")]
            if pending and not resume:
                raise ValueError("Pending trials require resume")
            errors, promotion_results = [], []
            self._meta("search_state", "running")
            with ThreadPoolExecutor(max_workers=self.config.trial_workers) as trials, ThreadPoolExecutor(max_workers=1) as promotions:
                active, promoting = {}, {}
                for row in self.store.db.execute("SELECT artifact FROM promotions WHERE state!='completed'"):
                    promoting[promotions.submit(self._promotion, row[0])] = row[0]
                for row in self.store.db.execute("SELECT id FROM nodes WHERE eligible=1 AND parent IS NOT NULL ORDER BY rowid"):
                    promoted = self._offer_promotion(row[0])
                    if promoted:
                        promoting[promotions.submit(self._promotion, promoted)] = promoted
                while True:
                    while len(active) < self.config.trial_workers and pending and not self.stop.is_set():
                        trial = pending.pop(0)
                        active[trials.submit(self._trial, trial, True)] = trial
                    while (len(active) < self.config.trial_workers and not pending and not self.stop.is_set()
                           and not stop_receipt(self.store)):
                        launched = self.store.db.execute("SELECT count(*) FROM trials").fetchone()[0]
                        if stop_after_trials is not None and launched - start_count >= stop_after_trials:
                            break
                        try:
                            trial = self.store.reserve_trial()
                        except CapacityError:
                            break
                        active[trials.submit(self._trial, trial, False)] = trial
                    while not self.scored.empty():
                        node = self.scored.get_nowait()
                        if self.stop.is_set():
                            continue
                        promoted = self._offer_promotion(node)
                        if promoted:
                            promoting[promotions.submit(self._promotion, promoted)] = promoted
                    if not active:
                        break
                    done, _ = wait(active, timeout=.1, return_when=FIRST_COMPLETED)
                    for future in done:
                        trial = active.pop(future)
                        try:
                            future.result()
                        except AssessmentPending as failure:
                            # Durable trial-local recovery is not a reason to
                            # truncate peers or close admission for replacements.
                            errors.append({"trial": trial, "error": str(failure), "scope": "trial"})
                        except Exception as failure:
                            errors.append({"trial": trial, "error": str(failure), "scope": "global"})
                            self.stop.set()
                    write_json(self.directory / "status.json", self._status())
                # A score can arrive between the previous queue drain and a
                # trial's final completion; do not lose its promotion eligibility.
                while not self.scored.empty():
                    node = self.scored.get_nowait()
                    if self.stop.is_set():
                        continue
                    promoted = self._offer_promotion(node)
                    if promoted:
                        promoting[promotions.submit(self._promotion, promoted)] = promoted
                for future in promoting:
                    promotion_results.append(future.result())
            pending_count = self.store.db.execute("SELECT count(*) FROM trials WHERE state!='completed'").fetchone()[0]
            self._meta("search_state", "recovery_pending" if pending_count else
                       "qualified_task_success" if stop_receipt(self.store) else "drained")
            result = {"status": self.store.status(), "errors": errors, "promotions": promotion_results,
                      "selection_status": "not_started", "confirmed": False, "confirmation_status": "not_run"}
            if finalize and not pending_count and not self.stop.is_set():
                result["final_selection"] = self.finalize(locked=True)
                result["selection_status"] = result["final_selection"]["selection_status"]
            result["status"] = self._status()
            write_json(self.directory / "status.json", result["status"])
            write_json(self.directory / "run_result.json", result)
            return result

    def finalize(self, *, locked=False):
        if not locked:
            with self.store.lock():
                self._freeze_run()
                self._reconcile()
                return self.finalize(locked=True)
        finalizer = Finalizer(self.store, self.artifacts, self._evaluator("final"), self.selection_tasks,
                              self.subset, self.runtime_identity)
        return finalizer.run()
