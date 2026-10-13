"""Shared-queue singleton Artemis adapter; Confirmation cannot be admitted."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path

from alpha_agents.tree_search.bridges.androidworld import artemis
from alpha_agents.tree_search.bridges.androidworld.devices import local_device_ports
from alpha_agents.tree_search.bridges.androidworld.runtime import (
    evaluation_lock,
    expose_agent_venv,
)
from alpha_agents.tree_search.bridges.androidworld.scheduler import (
    DeviceScheduler,
    TaskJob,
    UnhealthyDevice,
)
from alpha_agents.tree_search.core.mcts.artifacts import ArtifactStore, manifest
from alpha_agents.tree_search.core.mcts.contracts import (
    canonical_json,
    fingerprint,
    finite,
)
from alpha_agents.tree_search.core.mcts.ports import EvaluationAdapter, StageEvidence
from alpha_agents.tree_search.core.mcts.task_cache import (
    TaskCache,
    TaskIdentity,
    TaskIncomplete,
)
from alpha_agents.tree_search.core.shared.storage import write_json


@dataclass(frozen=True)
class EvaluationRevision:
    model: str
    evaluator: str
    fixture: str

    def __post_init__(self):
        if not all((self.model, self.evaluator, self.fixture)):
            raise ValueError("Model cache identity, trusted evaluator revision and fixture identity are required")


class SharedEvaluationState:
    def __init__(self):
        self.lock = threading.Lock()
        self.inflight: dict[str, Future] = {}
        self.session_id = uuid.uuid4().hex


class StagedArtemisEvaluator(EvaluationAdapter):
    telemetry_name = "android"
    supports_checks = True
    def __init__(self, cache: TaskCache, artifacts: ArtifactStore, scheduler: DeviceScheduler,
                 config: dict, revision: EvaluationRevision, partitions: dict[str, tuple[str, ...]], owner: str,
                 *, runner=artemis.run_stage, health_check=None, shared=None):
        self.cache, self.artifacts, self.scheduler = cache, artifacts, scheduler
        self.config, self.revision, self.partitions, self.owner = dict(config), revision, partitions, owner
        self.runner, self.health_check = runner, health_check or self._health
        self.shared = shared or SharedEvaluationState()
        if set(partitions) != {"screen", "selection", "confirmation"}:
            raise ValueError("Freeze Screen, Selection, and protected Confirmation task membership")
        flattened = [task for tasks in partitions.values() for task in tasks]
        if len(flattened) != len(set(flattened)) or any(not tasks for tasks in partitions.values()):
            raise ValueError("Frozen task partitions must be nonempty, unique, and disjoint")
        required = {"python", "androidworld_root", "adb_path", "model", "base_url", "seed"}
        if not required <= set(config) or any(not config[key] for key in required - {"seed"}):
            raise ValueError("Explicit trusted Android runtime configuration is required")
        if isinstance(config["seed"], bool) or not isinstance(config["seed"], int):
            raise ValueError("Freeze an integer evaluation seed")
        if config.get("task_timeout", 1800) != 1800 or config.get("stage_timeout", 2100) != 2100:
            raise ValueError("V1 singleton task envelopes are 120 + 1800 + 180 seconds")
        self.config.update(task_timeout=1800, stage_timeout=2100, phase_supervision=True)
        self.config.setdefault("llm_timeout", 300)
        self.ports = local_device_ports(list(scheduler.devices), self.config)
        self.cache.directory.mkdir(parents=True, exist_ok=True)
        execution = self._identity("identity", "identity").contract
        for key in ("artifact_id", "template", "generated_seed"):
            execution.pop(key)
        frozen = canonical_json({"execution": execution, "partitions": partitions})
        with self.cache.store() as store, store.transaction():
            existing = store.db.execute("SELECT value FROM meta WHERE key='evaluation_identity'").fetchone()
            if existing and existing[0] != frozen:
                raise ValueError("Resume requires the same frozen evaluation protocol and task partitions")
            store.db.execute("INSERT OR IGNORE INTO meta VALUES('evaluation_identity',?)", (frozen,))

    def for_owner(self, owner):
        return StagedArtemisEvaluator(self.cache, self.artifacts, self.scheduler, self.config, self.revision,
                                     self.partitions, owner, runner=self.runner, health_check=self.health_check,
                                     shared=self.shared)

    def reconcile_interrupted(self, process_dead):
        self.cache.reconcile_interrupted(
            lambda row: process_dead(Path(row["output"]) / "runner" / "process.json"),
            self.recover_completed,
        )

    def preflight(self, source=None):
        self._verify_runtime_source()
        for key in ("python", "adb_path"):
            path = Path(self.config[key])
            if not path.is_file() or not os.access(path, os.X_OK):
                raise ValueError(f"Configured {key} is missing or not executable")
        if not Path(self.config["androidworld_root"]).is_dir():
            raise ValueError("Configured AndroidWorld source root is missing")
        env = os.environ.copy()
        roots = [self.config["androidworld_root"]]
        if source:
            roots.append(str(source))
        env["PYTHONPATH"] = os.pathsep.join(roots + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        dependencies = subprocess.run([self.config["python"], "-c",
            "import sys, json; import artemis, android_world, adbutils, grpc; "
            "print(json.dumps({'python': sys.version, 'artemis': artemis.__file__, 'android_world': android_world.__file__}))"],
            capture_output=True, text=True, timeout=30, env=env)
        if dependencies.returncode:
            raise RuntimeError("Android runtime dependency preflight failed: " + dependencies.stderr[-2000:])
        from alpha_agents.tree_search.bridges.androidworld.runtime_attestation import (
            verify_imports,
        )
        dependency_record = json.loads(dependencies.stdout)
        verify_imports(Path(self.config["androidworld_root"]), Path(source) if source else None, dependency_record)
        from urllib.request import urlopen
        with urlopen(self.config["base_url"].rstrip("/") + "/models", timeout=10) as response:
            catalog = json.load(response)
        if self.config["model"] not in {entry["id"] for entry in catalog.get("data", [])}:
            raise RuntimeError("Configured GUI model is not advertised by its endpoint")
        devices = []
        for device in self.scheduler.devices:
            with evaluation_lock(self.cache.directory, device, blocking=False):
                state = subprocess.run([self.config["adb_path"], "-s", device, "get-state"],
                                       capture_output=True, text=True, timeout=10)
                if state.returncode or state.stdout.strip() != "device":
                    raise RuntimeError(f"Configured emulator {device} is not ready")
                devices.append({"serial": device, **self.ports[device], "ready": True})
        return {"dependencies": dependencies.stdout.strip(), "model_catalog_id": fingerprint(catalog),
                "devices": devices, "revision": self.revision.__dict__,
                "runtime_source_attestation": self.config.get("androidworld_source_attestation"),
                "fixture_preflight": "performed per selected task before scoring"}

    def _verify_runtime_source(self):
        expected = self.config.get("androidworld_source_attestation")
        if expected is not None:
            from alpha_agents.tree_search.bridges.androidworld.runtime_attestation import (
                verify_androidworld,
            )
            return verify_androidworld(Path(self.config["androidworld_root"]), expected)
        return None

    def _identity(self, artifact, task, stage=None):
        # Scheduling/stage/output/device coordinates do not change the paired
        # instance. Every behavior-affecting runner option remains in identity.
        excluded = {"androidworld_device_ports", "androidworld_assigned_device", "console_port", "grpc_port", "task_file",
                    "runtime_forecasts", "agent_venv"}
        runner = {key: value for key, value in self.config.items() if key not in excluded}
        return TaskIdentity(artifact, task, self.config["seed"], self.revision.model,
                            self.revision.evaluator, self.revision.fixture, runner,
                            {"startup": 120, "task": 1800, "cleanup": 180, "outer": 2100,
                             "llm": self.config["llm_timeout"]},
                            sample_kind="targeted" if stage == "targeted" else "canonical")

    def _check_tasks(self, stage, tasks):
        if stage not in {"screen", "s0", "selection", "targeted"}:
            raise ValueError("Confirmation is excluded from V1 evaluation")
        allowed = set(self.partitions["screen"] if stage == "screen" else
                      self.partitions["selection"] if stage in {"s0", "selection"} else
                      self.partitions["screen"] + self.partitions["selection"])
        if not tasks or len(tasks) != len(set(tasks)) or not set(tasks) <= allowed:
            raise ValueError("Task stage contains duplicate, unknown, or protected instances")

    def prepare(self, artifact, stages):
        """Register mandatory jobs and retry policy before code review/evaluation."""
        self.artifacts.verify(artifact)
        self._verify_runtime_source()
        batches = {}
        for stage, tasks in stages.items():
            self._check_tasks(stage, tasks)
            identities = tuple(self._identity(artifact, task, stage) for task in tasks)
            batch = self.cache.register(self.owner, artifact, stage, identities)
            batches[stage] = batch
            for identity in identities:
                if self.cache.cached(identity) is None:
                    with self.cache.store() as store, store.transaction():
                        if store.db.execute("SELECT 1 FROM costs WHERE id=?", (f"upper:{identity.key}",)).fetchone() is None:
                            estimate = finite(self.config.get("runtime_forecasts", {}).get(identity.template, 1800), "runtime forecast")
                            if estimate < 0:
                                raise ValueError("Runtime forecasts cannot be negative")
                            store.db.execute("INSERT INTO costs VALUES(?,?,?,?,?,'upper_bound',?)",
                                (f"upper:{identity.key}", identity.key, stage, "device", 4200,
                                 canonical_json({"initial_plus_one_infrastructure_retry": True})))
                            store.db.execute("INSERT INTO costs VALUES(?,?,?,?,?,'estimate',?)",
                                (f"estimate:{identity.key}", identity.key, stage, "device", estimate,
                                 canonical_json({"forecast_only": True})))
        return batches

    def _health(self, device, timeout):
        if timeout <= 0:
            return False
        started = time.monotonic()
        try:
            state = subprocess.run([self.config["adb_path"], "-s", device, "get-state"],
                                   capture_output=True, text=True, timeout=min(10, timeout))
            if state.returncode or state.stdout.strip() != "device":
                return False
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                return False
            home = subprocess.run([self.config["adb_path"], "-s", device, "shell", "input", "keyevent", "3"],
                                  capture_output=True, text=True, timeout=min(10, remaining))
            return home.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _attempt(self, identity, stage, device):
        source_before = self._verify_runtime_source()
        directory = self.cache.directory / "tasks" / identity.key / uuid.uuid4().hex
        output, workspace = directory / "evidence", directory / "workspace"
        self.artifacts.materialize(identity.artifact_id, workspace)
        if self.config.get("agent_venv"):
            expose_agent_venv(workspace, self.config["agent_venv"])
        output.mkdir(parents=True)
        run, summary, error, unhealthy = None, None, None, False
        started = time.monotonic()
        # Fail boundedly if another controller already owns this emulator.
        with evaluation_lock(self.cache.directory, device, blocking=False):
            started = time.monotonic()
            run = self.cache.begin(identity, device, output)
            try:
                config = {**self.config, **self.ports[device], "androidworld_assigned_device": device,
                          "device_lease_started_monotonic": started}
                summary = self.runner(workspace, output, stage, config, self.cache.directory,
                                      tasks=[identity.template], already_locked=True)
                if manifest(workspace) != self.artifacts.verify(identity.artifact_id):
                    raise TaskIncomplete("Evaluated harness changed from its frozen artifact")
                if summary.get("status") != "completed" or len(summary.get("task_results", [])) != 1:
                    raise TaskIncomplete("Singleton evaluation has no complete trusted outcome")
                episode = summary["task_results"][0]
                if episode.get("task_name") != identity.template or episode.get("index") != 0 or episode.get("seed") != identity.contract["generated_seed"]:
                    raise TaskIncomplete("Singleton runner returned a mismatched task instance")
                if not isinstance(episode.get("success"), bool):
                    raise TaskIncomplete("Singleton runner did not normalize binary success")
                source_after = self._verify_runtime_source()
                if source_before is not None:
                    write_json(output / "runtime_source_attestation.json", {"before": source_before, "after": source_after})
            except Exception as failure:
                error = f"{type(failure).__name__}: {failure}"
            finally:
                # Confirm runner cleanup before admitting another job on this
                # device. A kill or unconfirmed teardown quarantines it.
                metadata = (summary or {}).get("manifest", {})
                remaining = 2100 - (time.monotonic() - started)
                phase_path = output / "phases.json"
                try:
                    phases = json.loads(phase_path.read_text())["events"]
                    cleanup = next(p["entered"] for p in phases if p["phase"] == "cleanup")
                    remaining = min(remaining, cleanup + 180 - time.monotonic())
                    finished = phases[-1]["phase"] == "finished"
                except (OSError, ValueError, KeyError, StopIteration, TypeError):
                    finished = False
                try:
                    unhealthy = not (finished and metadata.get("cleanup_status") == "completed" and
                                     self.health_check(device, max(0, remaining)))
                except Exception as health_error:
                    unhealthy = True
                    error = (error + "; " if error else "") + f"Health check failed: {health_error}"
                write_json(output / "device_cleanup.json", {"device": device, "healthy": not unhealthy,
                                                            "lease_elapsed_seconds": time.monotonic() - started,
                                                            "remaining_cleanup_seconds": max(0, remaining)})
                if unhealthy:
                    error = (error + "; " if error else "") + "Bounded device cleanup could not be confirmed"
        elapsed = time.monotonic() - started
        outcome = None if error else {"task_name": identity.template, "success": episode["success"],
                                      "seed": episode["seed"], "raw_reward": episode.get("androidworld_reward"),
                                      "outcome_status": episode.get("outcome_status"), "episode": episode,
                                      "evidence_root": str(output.resolve())}
        try:
            self.cache.finish(run, outcome, error, elapsed, stage, unhealthy=unhealthy)
        except Exception as publication_error:
            # Evidence validation failures retain an infrastructure attempt;
            # they cannot strand a running cache owner or become a reward.
            if outcome is None:
                raise
            error = f"Trusted evidence publication failed: {publication_error}"
            self.cache.finish(run, None, error, elapsed, stage, unhealthy=unhealthy)
        if unhealthy:
            raise UnhealthyDevice(error)
        if error:
            raise TaskIncomplete(error)
        return self.cache.cached(identity)

    def recover_completed(self, row):
        """Recover a completed trusted task interrupted before its DB commit.

        The coordinator must first prove process death and hold the run lock.
        Unknown full lease duration is reported as a timeout bound, never a new sample.
        """
        output = Path(row["output"])
        expected_source = self.config.get("androidworld_source_attestation")
        if expected_source is not None:
            try:
                source_record = json.loads((output / "runtime_source_attestation.json").read_text())
            except (OSError, ValueError):
                return None
            if source_record != {"before": expected_source, "after": expected_source}:
                return None
        try:
            process = json.loads((output / "runner" / "process.json").read_text())
            request = json.loads((output / "evaluation_request.json").read_text())
            cleanup = json.loads((output / "device_cleanup.json").read_text())
            phases = json.loads((output / "phases.json").read_text())["events"]
            metadata = json.loads((output / "manifest.json").read_text())
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if not isinstance(phases, list) or any(not isinstance(event, dict) for event in phases):
            return None
        if (process.get("status") != "finished" or process.get("returncode") != 0 or process.get("timed_out") or
            cleanup.get("healthy") is not True or cleanup.get("device") != row["device"] or
            [event.get("phase") for event in phases] != ["startup", "task", "cleanup", "finished"]):
            return None
        with self.cache.store() as store:
            contract = json.loads(store.db.execute("SELECT contract FROM task_jobs WHERE id=?", (row["job"],)).fetchone()[0])
        stage, tasks = request.get("stage"), request.get("tasks")
        if tasks != [contract["template"]]:
            return None
        self._check_tasks(stage, tasks)
        identity = self._identity(contract["artifact_id"], contract["template"], stage)
        if identity.key != row["job"]:
            return None
        configured = request.get("config", {})
        expected = {**self.config, **self.ports[row["device"]], "androidworld_assigned_device": row["device"]}
        for key, value in expected.items():
            if key != "runtime_forecasts" and configured.get(key) != value:
                return None
        if manifest(output.parent / "workspace") != self.artifacts.verify(identity.artifact_id):
            raise TaskIncomplete("Interrupted task's execution workspace changed from its artifact")
        summary = artemis.normalize_manifest(metadata, tasks, expected, process)
        if summary["status"] != "completed":
            return None
        episode = summary["task_results"][0]
        outcome = {"task_name": identity.template, "success": episode["success"], "seed": episode["seed"],
                   "raw_reward": episode.get("androidworld_reward"), "outcome_status": episode.get("outcome_status"),
                   "episode": episode, "evidence_root": str(output.resolve())}
        return outcome, cleanup["lease_elapsed_seconds"], stage

    def _submit(self, identity, stage):
        with self.shared.lock:
            # A competing stage may have completed after this caller's first
            # cache check but before it acquired the shared admission lock.
            cached = self.cache.cached(identity)
            if cached is not None:
                ready = Future()
                ready.set_result(cached)
                return ready
            existing = self.shared.inflight.get(identity.key)
            if existing is not None:
                if existing.done() and (existing.cancelled() or existing.exception() is not None):
                    self.shared.inflight.pop(identity.key)
                else:
                    self.scheduler.promote_dependency(existing.job_id, self.owner, stage)
                    return existing
            job_id = uuid.uuid4().hex
            queued = time.monotonic()
            self.cache.register_dispatch(job_id, identity.key, self.shared.session_id,
                                         len(self.scheduler.devices), self.owner, stage)
            def execute_dispatch(device):
                started = time.monotonic()
                self.cache.start_dispatch(job_id, device, started - queued)
                state = "failed"
                try:
                    result = self._attempt(identity, stage, device)
                    state = "completed"
                    return result
                finally:
                    self.cache.finish_dispatch(job_id, state, time.monotonic() - started)
            try:
                future = self.scheduler.submit(TaskJob(job_id, self.owner, stage,
                    self.config.get("runtime_forecasts", {}).get(identity.template, 1800), execute_dispatch))
            except Exception:
                self.cache.finish_unstarted_dispatch(job_id)
                raise
            future.job_id = job_id
            self.shared.inflight[identity.key] = future
            # Do not install a callback under this non-reentrant lock: an
            # already completed future invokes it immediately.
        def release(_):
            self.cache.finish_unstarted_dispatch(job_id)
            with self.shared.lock:
                if self.shared.inflight.get(identity.key) is future:
                    self.shared.inflight.pop(identity.key)
        future.add_done_callback(release)
        return future

    def evaluate(self, artifact, workspace, stage, tasks):
        self._check_tasks(stage, tasks)
        if manifest(workspace) != self.artifacts.verify(artifact):
            raise TaskIncomplete("Stage input differs from immutable harness artifact")
        self.prepare(artifact, {stage: tasks})
        identities = [self._identity(artifact, task, stage) for task in tasks]
        batch = self.cache.register(self.owner, artifact, stage, tuple(identities))
        results, futures, errors = {}, {}, []
        for identity in identities:
            cached = self.cache.cached(identity, batch)
            if cached is not None:
                results[identity.template] = cached
            else:
                try:
                    futures[identity.template] = self._submit(identity, stage)
                except Exception as failure:
                    errors.append(str(failure))
        for identity in identities:
            if identity.template in results:
                continue
            if identity.template not in futures:
                continue
            future = futures[identity.template]
            for retry in range(2):
                try:
                    results[identity.template] = future.result()
                    break
                except Exception as failure:
                    if retry:
                        errors.append(str(failure))
                    else:
                        try:
                            future = self._submit(identity, stage)
                        except Exception as admission_error:
                            errors.append(str(admission_error))
                            break
        # Drain every admitted task before reporting an incomplete stage.
        if errors:
            raise TaskIncomplete("Stage remains incomplete: " + "; ".join(errors))
        return StageEvidence(artifact, stage, {task: results[task]["success"] for task in tasks},
                             tuple(results[task]["evidence_root"] for task in tasks))

    def verify_stage(self, evidence):
        self._verify_runtime_source()
        self._check_tasks(evidence.stage, tuple(evidence.outcomes))
        roots = []
        for task, success in evidence.outcomes.items():
            result = self.cache.cached(self._identity(evidence.artifact_id, task, evidence.stage))
            if result is None or result["success"] != success:
                raise TaskIncomplete("Persisted stage no longer matches its canonical task cache")
            roots.append(result["evidence_root"])
        if set(roots) != set(evidence.evidence):
            raise TaskIncomplete("Persisted stage evidence references changed")
