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

    def __init__(self, coordinator, scheduler):
        self.coordinator = coordinator
        self.scheduler = scheduler
        self._closed = False

    @property
    def stop(self):
        return self.coordinator.stop

    def request_stop(self):
        return self.coordinator.request_stop()

    def preflight(self):
        return self.coordinator.preflight()

    def run(self, **kwargs):
        return self.coordinator.run(**kwargs)

    def finalize(self):
        return self.coordinator.finalize()

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.scheduler.close()
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
    return config


def trusted_runner_revision(androidworld_revision, source_attestation=None):
    import hashlib

    from alpha_agents.tree_search.bridges.androidworld import (
        artemis,
        runtime_attestation,
        staged_evaluator,
    )
    from alpha_agents.tree_search.core.mcts import task_cache

    root = Path(artemis.__file__).parent
    adapters = {"evaluator.py": staged_evaluator, "task_cache.py": task_cache, "runtime_attestation.py": runtime_attestation}
    files = ("artemis.py", "artemis_runner.py", "runtime_limits.py", "device_preflight.py", "fixture_preflight.py",
             "task_outcomes.py", "answer_handoff.py", "phase_watch.py", "model_health.py", "memory_compat.py")
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
    attestation = observe_androidworld(Path(harness["androidworld_root"]), evaluation["androidworld_revision"])
    harness["androidworld_source_attestation"] = attestation
    revision = EvaluationRevision(evaluation_model_identity(evaluation, harness, output), trusted_runner_revision(evaluation["androidworld_revision"], attestation),
                                  evaluation["fixture_identity"])
    library = RoleRequest("identity", "identity", "proposer", "identity", roles.model, roles.effort, {}).library_hash
    objective = Objective(subset["manifest_id"], ARTIFACT_RUBRIC_VERSION, fingerprint({"roles": asdict(roles),
        "prompt_library": library, "coverage": JUDGE_COVERAGE_POLICY, "unknown_dimension": .5}),
        evaluation.get("judge_weight", .1))
    source, output = Path(config["harness"]["source"]), Path(output).resolve()
    if output.is_relative_to(source.resolve()):
        raise ValueError("Run output must be outside the immutable harness source")
    devices = tuple(harness.pop("androidworld_devices"))
    if len(devices) > search.device_limit:
        raise ValueError("Configured device pool exceeds the frozen controller limit")
    scheduler = DeviceScheduler(devices, autostart=False)
    coordinator = None
    try:
        cache = TaskCache(output, search, objective)
        artifacts = ArtifactStore(output / "artifacts")
        evaluator = StagedArtemisEvaluator(cache, artifacts, scheduler, harness, revision,
            {"screen": tuple(subset["screen"]), "selection": tuple(subset["selection"]),
             "confirmation": tuple(subset["protected_confirmation"])}, "baseline")
        coordinator = FixedPUCTCoordinator(source, output, search, objective, CodexGateway(runtime), evaluator, roles,
            subset["screen"], {item["task"]: item["weight"]["numerator"] / item["weight"]["denominator"] for item in subset["tasks"]},
            subset["selection"], subset, {"codex": runtime.record, "android": harness, "revision": asdict(revision),
                                         "devices": devices, "androidworld_revision": evaluation["androidworld_revision"]})
        scheduler.start()
        return SearchApplication(coordinator, scheduler)
    except BaseException:
        try:
            scheduler.close()
        finally:
            if coordinator is not None:
                coordinator.close()
        raise
