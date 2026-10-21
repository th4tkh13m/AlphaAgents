"""Explicit production composition for controller.type=fixed_puct."""

import json
import os
from dataclasses import asdict
from pathlib import Path

from alpha_agents.tree_search.bridges.androidworld.prepare_subset import prepare
from alpha_agents.tree_search.bridges.androidworld.runtime_attestation import (
    observe_androidworld,
)
from alpha_agents.tree_search.bridges.androidworld.scheduler import DeviceScheduler
from alpha_agents.tree_search.bridges.androidworld.staged_evaluator import (
    EvaluationRevision,
    StagedArtemisEvaluator,
)
from alpha_agents.tree_search.core.mcts.artifacts import ArtifactStore
from alpha_agents.tree_search.core.mcts.artifacts import manifest as artifact_manifest
from alpha_agents.tree_search.core.mcts.contracts import (
    ARTIFACT_RUBRIC_VERSION,
    FixedPUCTConfig,
    Objective,
    fingerprint,
)
from alpha_agents.tree_search.core.mcts.controller import (
    JUDGE_COVERAGE_POLICY,
)
from alpha_agents.tree_search.core.mcts.coordinator import FixedPUCTCoordinator
from alpha_agents.tree_search.core.mcts.judge_review import SCORING_CONTRACT
from alpha_agents.tree_search.core.mcts.ports import RoleSettings
from alpha_agents.tree_search.core.mcts.task_cache import TaskCache
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.codex import (
    CodexGateway,
    CodexRuntime,
)
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.prompts import (
    RoleRequest,
)


class SearchApplication:
    """Own production resources; the coordinator owns only search state.

    Use as a context manager or call close() after preflight/run/finalize.
    Drain external work before closing the coordinator's state store.
    """

    def __init__(self, coordinator, scheduler, profiler=None):
        self.coordinator = coordinator
        self.scheduler = scheduler
        self.profiler = profiler
        self._closed = False

    @property
    def stop(self):
        return self.coordinator.stop

    def request_stop(self):
        return self.coordinator.request_stop()

    def preflight(self):
        if self.profiler is not None:
            self.profiler.start()
        return self.coordinator.preflight()

    def assess_root(self, **kwargs):
        if self.profiler is not None:
            self.profiler.start()
        return self.coordinator.assess_root(**kwargs)

    def retain_successful_root(self):
        return self.coordinator.retain_successful_root()

    def run(self, **kwargs):
        if self.profiler is not None:
            self.profiler.start()
        return self.coordinator.run(**kwargs)

    def finalize(self):
        if self.profiler is not None:
            self.profiler.start()
        return self.coordinator.finalize()

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.scheduler.close()
        finally:
            try:
                if self.profiler is not None:
                    self.profiler.close()
            finally:
                self.coordinator.close()

    def __enter__(self):
        if self._closed:
            raise RuntimeError("Search application is already closed")
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


def resolve_config(config, base):
    config = json.loads(json.dumps(config))
    def resolve(value, *, interpreter=False):
        path = Path(value).expanduser()
        path = base / path if not path.is_absolute() else path
        # Python discovers pyvenv.cfg from the invoked executable path. Resolving
        # its symlink would silently select the base interpreter's dependencies.
        return os.path.abspath(path) if interpreter else str(path.resolve())
    config["harness"]["source"] = resolve(config["harness"]["source"])
    runtime = config["harness"]["config"]
    for key in ("python", "androidworld_root", "adb_path", "agent_venv"):
        if runtime.get(key):
            runtime[key] = resolve(runtime[key], interpreter=key == "python")
    for key in ("python", "codex_bin"):
        config["codex"]["runtime"][key] = resolve(config["codex"]["runtime"][key], interpreter=key == "python")
    if config["codex"]["runtime"].get("verification_python"):
        config["codex"]["runtime"]["verification_python"] = resolve(config["codex"]["runtime"]["verification_python"], interpreter=True)
    config["evaluation"]["manifest"] = resolve(config["evaluation"]["manifest"])
    if config.get("sequence", {}).get("cached_root_run"):
        config["sequence"]["cached_root_run"] = resolve(config["sequence"]["cached_root_run"])
    return config


def trusted_runner_revision(androidworld_revision, source_attestation=None):
    import hashlib

    from alpha_agents.tree_search.bridges.androidworld import (
        artemis,
        runtime_attestation,
        staged_evaluator,
    )
    from alpha_agents.tree_search.core.mcts import resource_retention, task_cache
    from alpha_agents.tree_search.infrastructure import evidence_sqlite, sqlite_snapshot

    root = Path(artemis.__file__).parent
    adapters = {"evaluator.py": staged_evaluator, "task_cache.py": task_cache, "runtime_attestation.py": runtime_attestation,
                "evidence_sqlite.py": evidence_sqlite, "sqlite_snapshot.py": sqlite_snapshot,
                "resource_retention.py": resource_retention}
    files = ("artemis.py", "artemis_runner.py", "runtime_limits.py", "device_preflight.py", "fixture_preflight.py",
             "task_outcomes.py", "answer_handoff.py", "phase_watch.py", "model_health.py", "memory_compat.py",
             "device_recovery.py", "device_maintenance.py")
    return fingerprint({"androidworld_revision": androidworld_revision,
                        "androidworld_source": source_attestation,
                        "staged_adapter": {name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                                           for name, module in adapters.items()},
                        "trusted_bridge": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}})


def evaluation_model_identity(evaluation, harness, output):
    """Allow unknown weights without sharing cached outcomes across runs."""
    revision = evaluation.get("model_revision")
    if revision:
        return revision
    model = harness.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("An evaluation model name is required")
    run_identity = fingerprint({"output": str(Path(output).resolve()),
                                "endpoint": harness.get("base_url"), "model": model})
    return f"unverified:{model}:run:{run_identity}"


def build(config, output):
    if config["controller"]["type"] == "task_sequence":
        return build_sequence(config, output)
    if config["harness"]["type"] != "androidworld":
        raise ValueError("Fixed PUCT v1 uses the trusted staged AndroidWorld/Artemis adapter")
    settings = config["controller"]
    search = FixedPUCTConfig.from_mapping({key: value for key, value in settings.items() if key != "type"})
    roles = RoleSettings(config["codex"]["model"], config["codex"]["effort"])
    runtime = CodexRuntime(**config["codex"]["runtime"])
    evaluation = config["evaluation"]
    harness = dict(config["harness"]["config"])
    if "device_hour_cap" in settings or "device_hour_cap" in harness:
        raise ValueError("V1 does not configure a device-hour cap")
    subset = prepare(json.loads(Path(evaluation["manifest"]).read_text()), harness["seed"])
    if search.evaluation_task:
        task = search.evaluation_task
        if task not in subset["screen"] + subset["selection"]:
            raise ValueError("Single-task objective must be a development task in the manifest")
        subset = {"algorithm": "single-development-task-v1", "seed": harness["seed"],
                  "source_manifest_hash": subset["source_manifest_hash"],
                  "tasks": [{"task": task, "weight": {"numerator": 1, "denominator": 1, "display": 1.0}}],
                  "screen": [], "selection": [task], "protected_confirmation": subset["protected_confirmation"],
                  "complement": [], "coverage": {"selected_tasks": 1, "generalization": "unmeasured"}}
        subset["manifest_id"] = fingerprint(subset)
    attestation = observe_androidworld(Path(harness["androidworld_root"]), evaluation["androidworld_revision"])
    harness["androidworld_source_attestation"] = attestation
    revision = EvaluationRevision(evaluation_model_identity(evaluation, harness, output), trusted_runner_revision(evaluation["androidworld_revision"], attestation),
                                  evaluation["fixture_identity"])
    library = RoleRequest("identity", "identity", "proposer", "identity", roles.model, roles.effort, {}).library_hash
    objective = Objective(subset["manifest_id"], ARTIFACT_RUBRIC_VERSION, fingerprint({"roles": asdict(roles),
        "prompt_library": library, "coverage": JUDGE_COVERAGE_POLICY, "scoring": SCORING_CONTRACT}),
        evaluation.get("judge_weight", .1))
    source, output = Path(config["harness"]["source"]), Path(output).resolve()
    if output.is_relative_to(source.resolve()):
        raise ValueError("Run output must be outside the immutable harness source")
    devices = tuple(harness.pop("androidworld_devices"))
    if len(devices) > search.device_limit:
        raise ValueError("Configured device pool exceeds the frozen controller limit")
    from alpha_agents.tree_search.bridges.androidworld.device_maintenance import (
        DeviceMaintenance,
    )
    maintenance = DeviceMaintenance(output, harness, devices)
    scheduler = DeviceScheduler(devices, autostart=False, implementor_limit=search.implementor_limit,
                                recover_device=maintenance)
    coordinator = None
    try:
        cache = TaskCache(output, search, objective)
        maintenance.cache = cache
        artifacts = ArtifactStore(output / "artifacts")
        evaluator = StagedArtemisEvaluator(cache, artifacts, scheduler, harness, revision,
            {"screen": tuple(subset["screen"]), "selection": tuple(subset["selection"]),
             "confirmation": tuple(subset["protected_confirmation"])}, "baseline")
        coordinator = FixedPUCTCoordinator(source, output, search, objective, CodexGateway(runtime), evaluator, roles,
            subset["screen"], {item["task"]: item["weight"]["numerator"] / item["weight"]["denominator"] for item in subset["tasks"]},
            subset["selection"], subset, {"codex": runtime.record, "android": harness, "revision": asdict(revision),
                                         "devices": devices, "androidworld_revision": evaluation["androidworld_revision"]})
        scheduler.start()
        from alpha_agents.tree_search.infrastructure.perfetto import TraceWatcher

        return SearchApplication(coordinator, scheduler, TraceWatcher(output, devices=devices))
    except BaseException:
        try:
            scheduler.close()
        finally:
            if coordinator is not None:
                coordinator.close()
        raise


def build_sequence(config, output):
    from alpha_agents.tree_search.bridges.androidworld.sequence_cache import (
        import_root_receipt,
        root_receipts,
    )
    from alpha_agents.tree_search.core.mcts.task_sequence import TaskSequence
    from alpha_agents.tree_search.core.shared.storage import write_json

    sequence = config["sequence"]
    scope = sequence.get("task_scope", "cached_failures")
    if scope not in {"cached_failures", "all_development"}:
        raise ValueError("task_scope must be cached_failures or all_development")
    reuse_results = sequence.get("reuse_cached_root_results", scope == "cached_failures")
    if not isinstance(reuse_results, bool):
        raise ValueError("reuse_cached_root_results must be a boolean")
    skip_passing = config["sequence"].get("skip_successful_roots", False)
    if not isinstance(skip_passing, bool):
        raise ValueError("skip_successful_roots must be a boolean")
    manifest = json.loads(Path(config["evaluation"]["manifest"]).read_text())
    rows = [row for row in manifest["rows"] if row["stage"].lower() in {"screen", "selection"}]
    development = {row["task"] for row in rows}
    index = None
    if scope == "all_development":
        if reuse_results:
            raise ValueError("all_development requires fresh root evaluations, not cached outcome reuse")
        if sequence.get("order", "difficulty_then_human_steps") != "difficulty_then_human_steps":
            raise ValueError("all_development requires difficulty_then_human_steps ordering")
        levels = {"easy": 0, "medium": 1, "hard": 2}
        if len(rows) != len(development) or not rows:
            raise ValueError("Development tasks must be nonempty and unique")
        for row in rows:
            if (row.get("difficulty") not in levels or type(row.get("optimal_human_steps")) is not int
                    or row["optimal_human_steps"] <= 0):
                raise ValueError("Difficulty ordering requires known difficulty and positive optimal_human_steps")
        rows.sort(key=lambda row: (levels[row["difficulty"]], row["optimal_human_steps"], row["task"]))
        tasks = [row["task"] for row in rows]
        expected = sequence.get("expected_task_count", len(tasks))
        if type(expected) is not int or expected != len(tasks):
            raise ValueError("Development task count differs from expected_task_count")
    else:
        index = root_receipts(sequence["cached_root_run"])
        tasks = index["failed_tasks"]
    if not tasks:
        raise ValueError("Sequence requires at least one task")
    if not set(tasks) <= development:
        raise ValueError("Cached failures contain an unknown or protected task")
    if len(config["harness"]["config"]["androidworld_devices"]) != 4:
        raise ValueError("The sequential experiment requires exactly four emulators")
    source = Path(config["harness"]["source"])
    if index and fingerprint(artifact_manifest(source)) != index["artifact_id"]:
        raise ValueError("Sequence source must be the cached root harness")
    output = Path(output)
    if index:
        index_path = output / "cached_root_index.json"
        if index_path.exists():
            if json.loads(index_path.read_text()) != index:
                raise ValueError("Cached-root receipt index changed; use a new sequence directory")
        else:
            write_json(index_path, index)
    if scope == "all_development":
        order = {"scope": scope, "order": "difficulty_then_human_steps",
                 "tasks": [{key: row[key] for key in ("task", "stage", "difficulty", "optimal_human_steps")} for row in rows]}
        order_path = output / "task_order.json"
        if order_path.exists() and json.loads(order_path.read_text()) != order:
            raise ValueError("Frozen development task order changed; use a new sequence directory")
        write_json(order_path, order)

    # Honor the experiment's limits; do not silently restore the old 3x3/depth-8
    # defaults inside every task factory.
    controller = dict(config["controller"])
    controller.update(type="fixed_puct", device_limit=4)
    for key, value in {"max_depth": 8, "max_trials": 30, "max_plans": 3, "max_implementations": 3}.items():
        controller.setdefault(key, value)
    controller.setdefault("max_new_attempts_per_trial", controller["max_depth"])
    controller.setdefault("max_attempts", controller["max_trials"] * controller["max_new_attempts_per_trial"])
    # Validate before constructing any device scheduler or role runtime.
    FixedPUCTConfig.from_mapping({**{key: value for key, value in controller.items() if key != "type"},
                                  "evaluation_task": tasks[0]})

    def application(task, position, harness_source, directory):
        settings = json.loads(json.dumps(config))
        settings.pop("sequence")
        settings["controller"] = {**controller, "evaluation_task": task}
        settings["harness"]["source"] = str(harness_source)
        app = build(settings, directory)
        try:
            if reuse_results:
                import_root_receipt(app, index, task)
            write_json(directory / "task_sequence_context.json", {"task": task, "position": position,
                       "source_harness": str(harness_source), "task_scope": scope,
                       "task_order": str(output / "task_order.json") if scope == "all_development" else None,
                       "cached_root_index": str(output / "cached_root_index.json") if index else None,
                       "reuse_cached_root_results": reuse_results})
            return app
        except BaseException:
            app.close()
            raise

    from alpha_agents.tree_search.infrastructure.sequence_monitor import SequenceMonitor
    return TaskSequence(output, source, tasks, config, application, monitor=SequenceMonitor(output),
                        skip_successful_roots=skip_passing)
