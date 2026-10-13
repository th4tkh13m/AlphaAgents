"""Task fairness, device failure, and durable compatible evidence acceptance."""

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from alpha_agents.tree_search.bridges.androidworld.scheduler import (
    DeviceScheduler,
    DevicesUnavailable,
    TaskJob,
    UnhealthyDevice,
)
from alpha_agents.tree_search.core.mcts.contracts import FixedPUCTConfig, Objective
from alpha_agents.tree_search.core.mcts.task_cache import (
    EvidenceCorrupt,
    TaskCache,
    TaskIdentity,
    TaskIncomplete,
    instance_seed,
)
from alpha_agents.tree_search.infrastructure.process import execute


def job(id, batch, duration=1, stage="s0", run=lambda device: device):
    return TaskJob(id, batch, stage, duration, run)


def test_queue_round_robin_longest_first_and_critical_before_promotions():
    scheduler = DeviceScheduler(("d0",), autostart=False)
    futures = [scheduler.submit(job("promotion", "promotion", 100, "selection")),
               scheduler.submit(job("a-short", "a", 1)), scheduler.submit(job("a-long", "a", 10)),
               scheduler.submit(job("b-short", "b", 2)), scheduler.submit(job("b-long", "b", 20))]
    scheduler.start()
    scheduler.close()
    assert all(f.done() for f in futures)
    assert [h["job"] for h in scheduler.history if h["event"] == "start"] == [
        "a-long", "b-long", "a-short", "b-short", "promotion"]


def test_two_queued_trials_each_get_three_devices_then_release_every_lease():
    scheduler = DeviceScheduler(tuple(f"d{i}" for i in range(6)), autostart=False)
    all_started = threading.Barrier(7)
    finish = threading.Event()
    def run(device):
        all_started.wait(timeout=5)
        assert finish.wait(5)
        return device
    futures = [scheduler.submit(job(f"{batch}-{i}", batch, run=run)) for batch in ("a", "b") for i in range(3)]
    scheduler.start()
    all_started.wait(timeout=5)
    assert scheduler.status()["running"] == {"a": 3, "b": 3}
    finish.set()
    scheduler.close()
    assert len({f.result() for f in futures}) == 6
    assert sum(h["event"] == "release" for h in scheduler.history) == 6


def test_single_trial_borrows_all_six_and_closed_queue_drains_admitted_work():
    scheduler = DeviceScheduler(tuple(f"d{i}" for i in range(6)), autostart=False)
    started = threading.Barrier(7)
    finish = threading.Event()
    def run(device):
        started.wait(timeout=5)
        assert finish.wait(5)
        return device
    futures = [scheduler.submit(job(str(i), "a", run=run)) for i in range(6)]
    scheduler.start()
    started.wait(timeout=5)
    scheduler.close(wait=False)
    with pytest.raises(RuntimeError, match="closed"):
        scheduler.submit(job("late", "a"))
    finish.set()
    scheduler.close()
    assert all(f.result() for f in futures)


def test_cleanup_failure_quarantines_device_and_fails_queued_work_without_hanging():
    scheduler = DeviceScheduler(("d0",), autostart=False)
    def unhealthy(device):
        raise UnhealthyDevice("cleanup could not restore health")
    failed = scheduler.submit(job("bad", "a", 10, run=unhealthy))
    queued = scheduler.submit(job("queued", "a", 1))
    scheduler.start()
    scheduler.close()
    with pytest.raises(UnhealthyDevice):
        failed.result()
    with pytest.raises(DevicesUnavailable):
        queued.result()
    assert scheduler.status()["quarantined"] == {"d0": "cleanup could not restore health"}
    assert scheduler.status()["running"]["a"] == 0


@pytest.fixture
def cache(tmp_path):
    return TaskCache(tmp_path / "run", FixedPUCTConfig(screen_gate="execution_valid"),
                     Objective("s0", "rubric", "judge"))


def identity(task="TaskA"):
    return TaskIdentity("artifact-a", task, 42, "model-revision-a", "evaluator-revision-a", "fixture-a",
                        {"model": "model-a", "base_url": "http://model/v1", "memory_compatibility": True},
                        {"startup": 120, "task": 1800, "cleanup": 180, "llm": 300})


def complete(cache, tmp_path, ident, *, success=False):
    output = tmp_path / "evidence"
    output.mkdir()
    (output / "manifest.json").write_text(json.dumps({"success": success}))
    (output / "trace.txt").write_text("Retained ordered trajectory evidence")
    run = cache.begin(ident, "d0", output)
    cache.finish(run, {"task_name": ident.template, "success": success,
                       "seed": instance_seed(ident.seed, ident.template), "evidence_root": str(output),
                       "raw_reward": float(success)}, None, 12.5, "s0")
    return run


def test_first_valid_failure_is_cached_across_s0_and_full_selection(cache, tmp_path):
    ident = identity()
    cache.register("trial-a", ident.artifact_id, "s0", (ident,))
    complete(cache, tmp_path, ident, success=False)
    selection = cache.register("final", ident.artifact_id, "selection", (ident,))
    assert cache.cached(ident, selection)["success"] is False
    with pytest.raises(TaskIncomplete, match="immutable"):
        cache.begin(ident, "d1", tmp_path / "retry")
    with cache.store() as store:
        assert store.db.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 1
        assert store.db.execute("SELECT count(*) FROM task_reuses").fetchone()[0] == 1
        assert store.status()["costs"][0]["seconds"] == 12.5
    reopened = TaskCache(cache.directory, cache.config, cache.objective)
    assert reopened.cached(ident)["success"] is False


@pytest.mark.parametrize("changes", [
    {"artifact_id": "artifact-b"}, {"seed": 43}, {"model_revision": "model-revision-b"},
    {"evaluator_revision": "evaluator-revision-b"}, {"fixture_identity": "fixture-b"},
    {"runner_configuration": {"model": "other"}}, {"runtime_limits": {"task": 900}}])
def test_compatibility_changes_never_reuse_prior_outcome(cache, tmp_path, changes):
    original = identity()
    cache.register("a", original.artifact_id, "s0", (original,))
    complete(cache, tmp_path, original)
    changed = replace(original, **changes)
    assert changed.key != original.key
    assert cache.cached(changed) is None


def test_missing_or_corrupt_evidence_cannot_replace_first_outcome(cache, tmp_path):
    ident = identity()
    cache.register("a", ident.artifact_id, "s0", (ident,))
    complete(cache, tmp_path, ident)
    (tmp_path / "evidence" / "trace.txt").write_text("changed")
    with pytest.raises(EvidenceCorrupt, match="cannot be replaced"):
        cache.cached(ident)


def test_infrastructure_failure_gets_one_retry_and_records_both_leases(cache, tmp_path):
    ident = identity()
    cache.register("a", ident.artifact_id, "screen", (ident,))
    for i in range(2):
        run = cache.begin(ident, "d0", tmp_path / f"attempt-{i}")
        cache.finish(run, None, "fixture unavailable", 5, "screen")
    with pytest.raises(TaskIncomplete, match="retry allowance exhausted"):
        cache.begin(ident, "d0", tmp_path / "third")
    with cache.store() as store:
        assert store.db.execute("SELECT sum(seconds) FROM costs").fetchone()[0] == 10
        assert store.db.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 2


def test_interruption_requires_verified_process_death_and_consumes_existing_attempt(cache, tmp_path):
    ident = identity()
    cache.register("a", ident.artifact_id, "s0", (ident,))
    run = cache.begin(ident, "d0", tmp_path / "interrupted")
    with pytest.raises(TaskIncomplete, match="may still be live"):
        cache.reconcile_interrupted(lambda row: False)
    assert cache.reconcile_interrupted(lambda row: row["id"] == run) == [run]
    retry = cache.begin(ident, "d0", tmp_path / "retry")
    cache.finish(retry, None, "failed again", 3, "s0")
    with pytest.raises(TaskIncomplete, match="exhausted"):
        cache.begin(ident, "d0", tmp_path / "third")


def test_task_registration_rejects_confirmation_and_unregistered_execution(cache, tmp_path):
    ident = identity()
    with pytest.raises(ValueError, match="Confirmation"):
        cache.register("final", ident.artifact_id, "confirmation", (ident,))
    with pytest.raises(ValueError, match="registered"):
        cache.begin(ident, "d0", tmp_path / "unregistered")


def test_wrong_seed_cannot_publish_a_different_instance(cache, tmp_path):
    ident = identity()
    cache.register("a", ident.artifact_id, "s0", (ident,))
    output = tmp_path / "evidence"
    output.mkdir()
    (output / "manifest.json").write_text("{}")
    run = cache.begin(ident, "d0", output)
    with pytest.raises(ValueError, match="different instance"):
        cache.finish(run, {"task_name": ident.template, "success": True, "seed": -1,
                           "evidence_root": str(output)}, None, 5, "s0")


@pytest.mark.parametrize("hang_phase", ["startup", "task", "cleanup"])
def test_phase_watchdog_kills_and_joins_the_correct_hung_phase(tmp_path, hang_phase):
    script = tmp_path / "runner.py"
    script.write_text('''import json, time, sys
from pathlib import Path
record = Path(sys.argv[1]); events = []
for phase in ["startup", "task", "cleanup", "finished"]:
    events.append({"phase": phase, "entered": time.monotonic()})
    temporary = record.with_suffix(".tmp")
    temporary.write_text(json.dumps({"events": events})); temporary.replace(record)
    if phase == sys.argv[2]:
        time.sleep(30)
''')
    report, _ = execute([sys.executable, str(script), str(tmp_path / "phase.json"), hang_phase],
                         tmp_path, tmp_path / "process", timeout=3, phase_path=tmp_path / "phase.json",
                         phase_limits={"startup": .5, "task": .5, "cleanup": .5})
    assert report["timed_out"]
    assert report["timeout_phase"] == hang_phase
    assert report["returncode"] != 0
    with pytest.raises(ProcessLookupError):
        os.kill(report["pid"], 0)


def test_phase_transition_does_not_erase_time_already_spent(tmp_path):
    script = tmp_path / "runner.py"
    script.write_text('''import json, time, sys
from pathlib import Path
started = time.monotonic()
time.sleep(.3)
Path(sys.argv[1]).write_text(json.dumps({"events": [
    {"phase": "startup", "entered": started},
    {"phase": "task", "entered": time.monotonic()}]}))
time.sleep(30)
''')
    report, _ = execute([sys.executable, str(script), str(tmp_path / "phase.json")], tmp_path,
                         tmp_path / "process", timeout=3, phase_path=tmp_path / "phase.json",
                         phase_limits={"startup": .1, "task": 2, "cleanup": 2})
    assert report["timed_out"] and report["timeout_phase"] == "startup"


@pytest.fixture
def staged_adapter(cache, tmp_path, request):
    from alpha_agents.tree_search.bridges.androidworld.phase_watch import enter_phase
    from alpha_agents.tree_search.bridges.androidworld.staged_evaluator import (
        EvaluationRevision,
        StagedArtemisEvaluator,
    )
    from alpha_agents.tree_search.core.mcts.artifacts import ArtifactStore
    source = tmp_path / "source"
    source.mkdir()
    (source / "controller.py").write_text("retries = 2\n")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    artifact = artifacts.freeze(source)
    workspace = tmp_path / "input"
    artifacts.materialize(artifact, workspace)
    scheduler = DeviceScheduler(("emulator-5554", "emulator-5556"))
    calls = []
    failures = {}
    def runner(workspace, output, stage, config, lock_root, *, tasks, already_locked):
        assert len(tasks) == 1 and already_locked
        task = tasks[0]
        calls.append((task, stage, config["androidworld_assigned_device"]))
        for phase in ("startup", "task", "cleanup", "finished"):
            enter_phase(output / "phases.json", phase)
        (output / "trajectory.txt").write_text("Actual fixture trajectory")
        invalid = failures.get(task, 0) > 0
        failures[task] = max(0, failures.get(task, 0) - 1)
        summary = {"status": "invalid_runtime" if invalid else "completed",
                   "manifest": {"cleanup_status": "completed"},
                   "task_results": [{"task_name": task, "index": 0, "seed": instance_seed(config["seed"], task),
                                     "success": task != "TaskA", "androidworld_reward": float(task != "TaskA"),
                                     "outcome_status": "completed"}]}
        metadata = {"status": "completed", "model": config["model"], "model_base_url": config["base_url"],
                    "seed": config["seed"], "tasks": tasks, "combinations": 1, "cleanup_status": "completed",
                    "episodes": [{"template": task, "index": 0, "seed": instance_seed(config["seed"], task),
                                  "success": float(task != "TaskA"), "androidworld_reward": float(task != "TaskA"),
                                  "artemis_status": "completed", "exception": None, "seconds": .1}]}
        summary["manifest"] = metadata
        (output / "manifest.json").write_text(json.dumps(metadata))
        (output / "evaluation_request.json").write_text(json.dumps({"stage": stage, "tasks": tasks,
                                                                    "config": config, "worktree": str(workspace)}))
        (output / "runner").mkdir()
        (output / "runner" / "process.json").write_text(json.dumps({"status": "finished", "returncode": 0,
                                                                    "timed_out": False, "elapsed_seconds": .1}))
        (output / "summary.json").write_text(json.dumps(summary))
        return summary
    config = {"python": sys.executable, "androidworld_root": "/trusted/androidworld", "adb_path": "/trusted/adb",
              "model": "model-a", "base_url": "http://model/v1", "seed": 42,
              "androidworld_device_ports": {"emulator-5554": {"console_port": 5554, "grpc_port": 8554},
                                             "emulator-5556": {"console_port": 5556, "grpc_port": 8556}}}
    if getattr(request, "param", None) == "attested":
        from alpha_agents.tree_search.bridges.androidworld.runtime_attestation import (
            observe_androidworld,
        )

        runtime_root = tmp_path / "androidworld"
        package = runtime_root / "android_world"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("# Runtime fixture\n")
        (package / "controller.py").write_text("value = 0\n")
        subprocess.run(["git", "init", "-q", str(runtime_root)], check=True)
        subprocess.run(["git", "-C", str(runtime_root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(runtime_root), "-c", "user.name=Runtime test", "-c", "user.email=runtime@example.invalid",
                        "commit", "-qm", "runtime fixture"], check=True)
        revision = subprocess.run(["git", "-C", str(runtime_root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        config.update(androidworld_root=str(runtime_root), androidworld_source_attestation=observe_androidworld(runtime_root, revision))
    adapter = StagedArtemisEvaluator(cache, artifacts, scheduler, config,
        EvaluationRevision("model-revision-a", "evaluator-revision-a", "fixture-a"),
        {"screen": ("ScreenA",), "selection": ("TaskA", "TaskB", "TaskC"), "confirmation": ("HeldoutA",)},
        "trial-a", runner=runner, health_check=lambda device, seconds: seconds > 0)
    yield adapter, artifact, workspace, calls, failures
    scheduler.close()


def test_staged_adapter_pairs_s0_and_selection_and_only_runs_missing_complement(staged_adapter):
    adapter, artifact, workspace, calls, failures = staged_adapter
    adapter.prepare(artifact, {"screen": ("ScreenA",), "s0": ("TaskA", "TaskB")})
    with adapter.cache.store() as store:
        assert store.db.execute("SELECT count(*) FROM task_jobs").fetchone()[0] == 3
        assert store.db.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 0
    s0 = adapter.evaluate(artifact, workspace, "s0", ("TaskA", "TaskB"))
    assert s0.outcomes == {"TaskA": False, "TaskB": True}
    full = adapter.for_owner("final").evaluate(artifact, workspace, "selection", ("TaskA", "TaskB", "TaskC"))
    assert full.outcomes == {"TaskA": False, "TaskB": True, "TaskC": True}
    assert sorted(task for task, _, _ in calls) == ["TaskA", "TaskB", "TaskC"]
    assert s0.evidence == full.evidence[:2]
    with adapter.cache.store() as store:
        assert store.db.execute("SELECT count(*) FROM task_reuses").fetchone()[0] == 2
        assert store.db.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 3
        assert store.db.execute("SELECT count(*) FROM costs WHERE classification='observed'").fetchone()[0] == 3
        dispatches = list(store.db.execute("SELECT * FROM task_dispatches"))
        assert len(dispatches) == 3  # Compatible cache hits did not enter the queue.
        assert all(row["state"] == "completed" and row["queue_delay"] >= 0 and row["callback_seconds"] >= 0 for row in dispatches)
        assert len({row["session"] for row in dispatches}) == 1
        metrics = store.status()["scheduler_metrics"]
        assert metrics["queue_delay_samples"] == 3
        assert 0 <= metrics["sessions"][0]["assigned_slot_utilization"] <= 1
        from alpha_agents.tree_search.core.mcts.storage import TreeStore
        reopened = TreeStore.readonly(adapter.cache.directory)
        try:
            assert reopened.status()["scheduler_metrics"] == metrics
        finally:
            reopened.close()


def test_staged_adapter_retries_infrastructure_once_with_fresh_workspace(staged_adapter):
    adapter, artifact, workspace, calls, failures = staged_adapter
    failures["TaskA"] = 1
    evidence = adapter.evaluate(artifact, workspace, "s0", ("TaskA",))
    assert evidence.outcomes == {"TaskA": False}
    assert len(calls) == 2
    with adapter.cache.store() as store:
        assert [row[0] for row in store.db.execute("SELECT state FROM task_runs ORDER BY ordinal")] == ["infra_failed", "completed"]
        outputs = [row[0] for row in store.db.execute("SELECT output FROM task_runs")]
        assert len(set(outputs)) == 2


def test_staged_adapter_repeated_infrastructure_failure_drains_all_admitted_tasks(staged_adapter):
    adapter, artifact, workspace, calls, failures = staged_adapter
    failures["TaskA"] = 10
    with pytest.raises(TaskIncomplete, match="remains incomplete"):
        adapter.evaluate(artifact, workspace, "s0", ("TaskA", "TaskB", "TaskC"))
    assert sum(task == "TaskA" for task, _, _ in calls) == 2
    assert sorted(task for task, _, _ in calls if task != "TaskA") == ["TaskB", "TaskC"]
    assert sum(adapter.scheduler.status()["running"].values()) == 0


def test_staged_adapter_rejects_heldout_tasks_before_any_runner_call(staged_adapter):
    adapter, artifact, workspace, calls, failures = staged_adapter
    with pytest.raises(ValueError, match="Confirmation"):
        adapter.evaluate(artifact, workspace, "confirmation", ("HeldoutA",))
    with pytest.raises(ValueError, match="protected"):
        adapter.evaluate(artifact, workspace, "s0", ("HeldoutA",))
    assert calls == []


def test_staged_adapter_health_failure_is_persisted_and_quarantines_devices(staged_adapter):
    adapter, artifact, workspace, calls, failures = staged_adapter
    adapter.health_check = lambda device, seconds: False
    with pytest.raises(TaskIncomplete, match="remains incomplete"):
        adapter.evaluate(artifact, workspace, "s0", ("TaskA",))
    assert len(adapter.scheduler.status()["quarantined"]) == 2
    with adapter.cache.store() as store:
        assert store.db.execute("SELECT count(*) FROM device_faults").fetchone()[0] == 2
        assert store.db.execute("SELECT count(*) FROM task_jobs WHERE state='running'").fetchone()[0] == 0


def test_targeted_diagnostics_never_replace_canonical_scoring_samples(staged_adapter):
    adapter, artifact, workspace, calls, failures = staged_adapter
    diagnostics = adapter.evaluate(artifact, workspace, "targeted", ("TaskA",))
    canonical = adapter.evaluate(artifact, workspace, "s0", ("TaskA",))
    assert diagnostics.evidence != canonical.evidence
    assert [stage for _, stage, _ in calls] == ["targeted", "s0"]
    with adapter.cache.store() as store:
        assert store.db.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 2


def test_admitted_check_publishes_pending_receipt_before_device_result(staged_adapter):
    from alpha_agents.tree_search.core.mcts.targeted_checks import TargetedChecks
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.prompts import (
        RoleRequest,
    )

    adapter, artifact, workspace, _, _ = staged_adapter
    entered, release = threading.Event(), threading.Event()
    evaluate = adapter.evaluate
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return evaluate(*args, **kwargs)
    adapter.evaluate = delayed
    with adapter.cache.store() as store:
        with store.transaction():
            store._reserve("engineering", {"implementation": 1})
        invocation = store.consume_call("engineering", "implementation")
        req = RoleRequest(str(store.directory), invocation, "implementer", artifact,
                          "configured-model", "high", {"attempt_id": "engineering", "plan_id": "plan"})
        checks = TargetedChecks(store, adapter.artifacts, adapter, workspace, req,
                               ("TaskA",), lambda parent, child: parent == child)
        payload = {"check_id": "slow-device-check", "attempt_id": "engineering", "tasks": ["TaskA"],
                   "mechanism": "bounded recovery", "expected_observation": "Observed task result"}
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(checks.handle, payload)
            try:
                assert entered.wait(5) and not future.done()
                pending = json.loads((checks.root / "latest.json").read_text())
                assert pending["status"] == "pending" and pending["snapshot_id"] is None and pending["outcomes"] is None
                assert pending["check_id"] == payload["check_id"] and pending["invocation_id"] == req.request_id
                assert pending["artifact_id"] == artifact and pending["sample_kind"] == "targeted"
                assert store.db.execute("SELECT count(*) FROM evaluation_snapshots WHERE stage='targeted'").fetchone()[0] == 0
            finally:
                release.set()
            result = future.result(timeout=5)
        assert result["status"] == "completed" and result["outcomes"] == {"TaskA": False}


def test_implementer_targeted_channel_uses_shared_cache_queue_and_actual_handoff(staged_adapter):
    from alpha_agents.tree_search.core.mcts.targeted_checks import TargetedChecks
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.prompts import (
        RoleRequest,
    )

    adapter, artifact, workspace, calls, _ = staged_adapter
    with adapter.cache.store() as store:
        with store.transaction():
            store._reserve("engineering", {"implementation": 1})
        invocation = store.consume_call("engineering", "implementation")
        request = RoleRequest(str(store.directory), invocation, "implementer", artifact, "configured-model", "high",
                              {"attempt_id": "engineering", "plan_id": "frozen-plan"})
        checks = TargetedChecks(store, adapter.artifacts, adapter, workspace, request,
                               ("ScreenA", "TaskA", "TaskB", "TaskC"), lambda parent, child: parent == child)
        payload = {"check_id": "recovery", "attempt_id": "engineering", "tasks": ["TaskA"],
                   "mechanism": "bounded recovery", "expected_observation": "Retry once after failed verification"}
        first = checks.handle(payload)
        assert first["status"] == "completed" and first["outcomes"] == {"TaskA": False}
        assert first["sample_kind"] == "targeted" and first["artifact_id"] == artifact
        assert first["readable_evidence"] and all(Path(path).is_dir() for path in first["readable_evidence"])
        assert checks.handle(payload) == first
        second = checks.handle({**payload, "check_id": "recovery-replay"})
        assert second["snapshot_id"] == first["snapshot_id"]
        assert len(calls) == 1
        with pytest.raises(ValueError, match="changed inputs"):
            checks.handle({**payload, "tasks": ["TaskB"]})
        with pytest.raises(ValueError, match="protected"):
            checks.handle({**payload, "check_id": "protected", "tasks": ["HeldoutA"]})
        reports = [{key: result.get(key) for key in ("check_id", "status", "artifact_id", "snapshot_id", "outcomes")}
                   for result in (first, second)]
        checks.validate_handoff({"check_requests": reports})
        with pytest.raises(ValueError, match="trusted evidence"):
            checks.validate_handoff({"check_requests": [{**reports[0], "outcomes": {"TaskA": True}}, reports[1]]})
        canonical = adapter.evaluate(artifact, workspace, "s0", ("TaskA",))
        assert canonical.identity != first["snapshot_id"]
        assert [stage for _, stage, _ in calls] == ["targeted", "s0"]
        assert store.db.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 2
        assert store.db.execute("SELECT count(*) FROM costs WHERE resource='device' AND classification='observed'").fetchone()[0] == 2
        assert store.budget()["spent"] == 1


@pytest.mark.parametrize("staged_adapter", ["attested"], indirect=True)
def test_runtime_drift_rejects_cached_and_new_work_before_device_execution(staged_adapter):
    adapter, artifact, workspace, calls, _ = staged_adapter
    canonical = adapter.evaluate(artifact, workspace, "s0", ("TaskA",))
    assert len(calls) == 1
    runtime = Path(adapter.config["androidworld_root"]) / "android_world" / "controller.py"
    runtime.write_text("value = 1\n")
    with pytest.raises(ValueError, match="changed"):
        adapter.verify_stage(canonical)
    with pytest.raises(ValueError, match="changed"):
        adapter.evaluate(artifact, workspace, "s0", ("TaskA",))
    with pytest.raises(ValueError, match="changed"):
        adapter.evaluate(artifact, workspace, "s0", ("TaskB",))
    assert len(calls) == 1
    with adapter.cache.store() as store:
        assert store.db.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 1
        assert store.db.execute("SELECT count(*) FROM task_jobs").fetchone()[0] == 1


@pytest.mark.parametrize("staged_adapter", ["attested"], indirect=True)
def test_runtime_change_during_task_is_infrastructure_evidence_not_a_reward(staged_adapter):
    adapter, artifact, workspace, calls, _ = staged_adapter
    original = adapter.runner
    runtime = Path(adapter.config["androidworld_root"]) / "android_world" / "controller.py"
    changed = []
    def mutate_runtime(*args, **kwargs):
        result = original(*args, **kwargs)
        if not changed:
            changed.append(True)
            runtime.write_text("value = 1\n")
        return result
    adapter.runner = mutate_runtime
    with pytest.raises(TaskIncomplete, match="changed"):
        adapter.evaluate(artifact, workspace, "s0", ("TaskA",))
    with adapter.cache.store() as store:
        assert store.db.execute("SELECT state FROM task_jobs").fetchone()[0] == "infra_failed"
        assert store.db.execute("SELECT result FROM task_jobs").fetchone()[0] is None
        assert store.db.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 1
    runtime.write_text("value = 0\n")
    canonical = adapter.evaluate(artifact, workspace, "s0", ("TaskA",))
    assert canonical.outcomes == {"TaskA": False}
    assert len(calls) == 2
    with adapter.cache.store() as store:
        assert store.db.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 2
        row = store.db.execute("SELECT output FROM task_runs WHERE state='completed'").fetchone()
        record = json.loads((Path(row[0]) / "runtime_source_attestation.json").read_text())
        assert record == dict.fromkeys(("before", "after"), adapter.config["androidworld_source_attestation"])


def test_simultaneous_stages_share_one_compatible_task_execution(staged_adapter):
    adapter, artifact, workspace, calls, failures = staged_adapter
    peer = adapter.for_owner("trial-b")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(adapter.evaluate, artifact, workspace, "s0", ("TaskA", "TaskB"))
        second = pool.submit(peer.evaluate, artifact, workspace, "selection", ("TaskA", "TaskB", "TaskC"))
        s0, full = first.result(timeout=10), second.result(timeout=10)
    assert s0.evidence == full.evidence[:2]
    assert sorted(task for task, _, _ in calls) == ["TaskA", "TaskB", "TaskC"]
    with adapter.cache.store() as store:
        assert store.db.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 3


def test_search_dependency_promotes_queued_background_task_before_other_promotions():
    scheduler = DeviceScheduler(("d0",), autostart=False)
    scheduler.submit(job("needed", "promotion", 1, "selection"))
    scheduler.submit(job("other", "promotion", 100, "selection"))
    scheduler.promote_dependency("needed", "trial-a", "s0")
    scheduler.start()
    scheduler.close()
    assert [h["job"] for h in scheduler.history if h["event"] == "start"] == ["needed", "other"]


def test_late_concurrent_admission_rechecks_cache_before_scheduling(staged_adapter):
    adapter, artifact, workspace, calls, failures = staged_adapter
    adapter.evaluate(artifact, workspace, "s0", ("TaskA",))
    # Simulate a caller whose earlier cache check preceded completion.
    future = adapter._submit(adapter._identity(artifact, "TaskA"), "selection")
    assert future.result()["success"] is False
    assert len(calls) == 1


def test_run_resume_rejects_changed_evaluation_protocol(staged_adapter):
    from alpha_agents.tree_search.bridges.androidworld.staged_evaluator import (
        EvaluationRevision,
        StagedArtemisEvaluator,
    )
    adapter, artifact, workspace, calls, failures = staged_adapter
    changed = dict(adapter.config, seed=43)
    with pytest.raises(ValueError, match="frozen evaluation protocol"):
        StagedArtemisEvaluator(adapter.cache, adapter.artifacts, adapter.scheduler, changed, adapter.revision,
                               adapter.partitions, "other", runner=adapter.runner, health_check=adapter.health_check)
    changed_revision = EvaluationRevision("new-model-revision", adapter.revision.evaluator, adapter.revision.fixture)
    with pytest.raises(ValueError, match="frozen evaluation protocol"):
        StagedArtemisEvaluator(adapter.cache, adapter.artifacts, adapter.scheduler, adapter.config, changed_revision,
                               adapter.partitions, "other", runner=adapter.runner, health_check=adapter.health_check)


@pytest.mark.parametrize("staged_adapter", [None, "attested"], indirect=True)
def test_crash_after_trusted_task_completion_recovers_same_outcome_without_resampling(staged_adapter, monkeypatch):
    adapter, artifact, workspace, calls, failures = staged_adapter
    finish = adapter.cache.finish
    class Crash(BaseException):
        pass
    def crash_before_commit(*args, **kwargs):
        raise Crash("coordinator died before task DB commit")
    monkeypatch.setattr(adapter.cache, "finish", crash_before_commit)
    with pytest.raises(Crash):
        adapter.evaluate(artifact, workspace, "s0", ("TaskA",))
    monkeypatch.setattr(adapter.cache, "finish", finish)
    if adapter.config.get("androidworld_source_attestation"):
        with adapter.cache.store() as store:
            interrupted = dict(store.db.execute("SELECT * FROM task_runs WHERE state='running'").fetchone())
        marker = Path(interrupted["output"]) / "runtime_source_attestation.json"
        contents = marker.read_text()
        marker.unlink()
        assert adapter.recover_completed(interrupted) is None
        marker.write_text(json.dumps({"before": {}, "after": {}}))
        assert adapter.recover_completed(interrupted) is None
        marker.write_text(contents)
        assert adapter.recover_completed(interrupted) is not None
    def process_dead(path):
        assert path.parts[-2:] == ("runner", "process.json")
        process = json.loads(path.read_text())
        return process["status"] == "finished"
    adapter.reconcile_interrupted(process_dead)
    evidence = adapter.evaluate(artifact, workspace, "s0", ("TaskA",))
    assert evidence.outcomes == {"TaskA": False}
    assert len(calls) == 1
    with adapter.cache.store() as store:
        assert store.db.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 1
        cost = store.db.execute("SELECT classification,metadata FROM costs WHERE id LIKE 'lease:%'").fetchone()
        assert cost["classification"] == "upper_bound"
        assert json.loads(cost["metadata"])["recovered_from_checkpoint"]


def test_explicit_task_recovery_is_bounded_audited_and_idempotent(cache, tmp_path):
    ident = identity()
    cache.register("owner", ident.artifact_id, "s0", (ident,))
    with cache.store() as store:
        with pytest.raises(ValueError, match="inactive infrastructure"):
            store.grant_task_recovery(ident.key, "Fixture repaired", "round-one")
    for ordinal in range(2):
        run = cache.begin(ident, "d0", tmp_path / f"original-{ordinal}")
        cache.finish(run, None, "fixture unavailable", 1, "s0")
    with cache.store() as store:
        before = dict(store.db.execute("SELECT * FROM task_jobs WHERE id=?", (ident.key,)).fetchone())
        grant = store.grant_task_recovery(ident.key, "Fixture repaired", "round-one")
        assert grant["attempts"] == 2 and grant["first_ordinal"] == 3 and grant["round"] == 1
        assert store.grant_task_recovery(ident.key, "Fixture repaired", "round-one") == grant
        assert dict(store.db.execute("SELECT * FROM task_jobs WHERE id=?", (ident.key,)).fetchone()) == before
        with pytest.raises(ValueError, match="different grant"):
            store.grant_task_recovery(ident.key, "Different reason", "round-one")
        with pytest.raises(ValueError, match="must be exhausted"):
            store.grant_task_recovery(ident.key, "More recovery", "round-two")
    for ordinal in range(2):
        run = cache.begin(ident, "d0", tmp_path / f"recovery-{ordinal}")
        with cache.store() as store:
            with pytest.raises(ValueError, match="inactive infrastructure"):
                store.grant_task_recovery(ident.key, "More recovery", "round-two")
        cache.finish(run, None, "still unavailable", 1, "s0")
    with pytest.raises(TaskIncomplete, match="exhausted"):
        cache.begin(ident, "d0", tmp_path / "ungranted-fifth")
    with cache.store() as store:
        assert [row[0] for row in store.db.execute("SELECT ordinal FROM task_runs ORDER BY ordinal")] == [1, 2, 3, 4]
        assert len(store.status()["task_recovery_rounds"]) == 1
        grant = store.grant_task_recovery(ident.key, "Second fixture repair", "round-two")
        assert grant["first_ordinal"] == 5 and grant["round"] == 2


def test_task_recovery_cannot_replace_completed_first_outcome(cache, tmp_path):
    ident = identity()
    cache.register("owner", ident.artifact_id, "s0", (ident,))
    complete(cache, tmp_path, ident, success=False)
    before = cache.cached(ident)
    with cache.store() as store:
        with pytest.raises(ValueError, match="inactive infrastructure"):
            store.grant_task_recovery(ident.key, "Try for a success", "outcome-shopping")
    assert cache.cached(ident) == before and not before["success"]


def test_task_recovery_cli_grants_once_without_launching_work(cache, tmp_path):
    ident = identity()
    cache.register("owner", ident.artifact_id, "s0", (ident,))
    for ordinal in range(2):
        run = cache.begin(ident, "d0", tmp_path / f"attempt-{ordinal}")
        cache.finish(run, None, "fixture unavailable", 1, "s0")
    command = [sys.executable, "-m", "alpha_agents.tree_search", "--command", "grant-task-recovery",
        "--output", str(cache.directory), "--task-job", ident.key,
        "--recovery-id", "operator-round", "--recovery-reason", "Fixture restored"]
    results = [subprocess.run(command, cwd=Path(__file__).resolve().parents[3],
                             capture_output=True, text=True, timeout=10) for _ in range(2)]
    for result in results:
        assert result.returncode == 0, result.stderr
        status = json.loads(result.stdout)
        assert len(status["task_recovery_rounds"]) == 1
        assert status["incomplete_tasks"][0]["remaining_attempts"] == 2
        assert status["incomplete_tasks"][0]["consumed_attempts"] == 2
    with cache.store() as store:
        assert store.db.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 2
        assert store.budget()["spent"] == 0


def test_readonly_status_supports_runs_without_new_task_recovery_table(cache):
    from alpha_agents.tree_search.core.mcts.storage import TreeStore

    with cache.store() as store:
        store.db.execute("DROP TABLE task_recovery_rounds")
    inspected = TreeStore.readonly(cache.directory)
    try:
        assert inspected.status()["task_recovery_rounds"] == []
        assert not inspected.db.execute("SELECT 1 FROM sqlite_master WHERE name='task_recovery_rounds'").fetchone()
    finally:
        inspected.close()


def test_concurrent_task_recovery_grants_cannot_stack_unconsumed_rounds(cache, tmp_path):
    ident = identity()
    cache.register("owner", ident.artifact_id, "s0", (ident,))
    for ordinal in range(2):
        run = cache.begin(ident, "d0", tmp_path / f"original-{ordinal}")
        cache.finish(run, None, "fixture unavailable", 1, "s0")
    ready = threading.Barrier(2)
    def grant(number):
        with cache.store() as store:
            ready.wait(timeout=5)
            try:
                return store.grant_task_recovery(ident.key, "Fixture restored", f"round-{number}")
            except ValueError as error:
                return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(grant, (1, 2)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert any(isinstance(result, str) and "must be exhausted" in result for result in results)
    with cache.store() as store:
        assert len(store.status()["task_recovery_rounds"]) == 1
        assert store.status()["incomplete_tasks"][0]["remaining_attempts"] == 2


@pytest.mark.parametrize("unhealthy", [False, True])
def test_future_completion_observes_released_slot_and_settled_quarantine(unhealthy):
    scheduler = DeviceScheduler(("device",), autostart=False)
    def run(device):
        if unhealthy:
            raise UnhealthyDevice("cleanup failed")
        return True
    future = scheduler.submit(job("settled", "owner", 1, run=run))
    observed = []
    future.add_done_callback(lambda _: observed.append(scheduler.status()))
    scheduler.start()
    scheduler.close()
    assert sum(observed[0]["running"].values()) == 0
    assert bool(observed[0]["quarantined"]) == unhealthy
    if unhealthy:
        with pytest.raises(UnhealthyDevice):
            future.result()
    else:
        assert future.result()


def test_quarantine_completes_queued_futures_outside_scheduler_lock():
    scheduler = DeviceScheduler(("device",), autostart=False)
    def unhealthy(device):
        raise UnhealthyDevice("cleanup failed")
    failed = scheduler.submit(job("failed", "owner", 10, run=unhealthy))
    queued = scheduler.submit(job("queued", "owner", 1))
    observations = []
    def probe(_):
        read = threading.Event()
        def read_status():
            scheduler.status()
            read.set()
        thread = threading.Thread(target=read_status)
        thread.start()
        observations.append(read.wait(timeout=2))
        thread.join(timeout=2)
    queued.add_done_callback(probe)
    scheduler.start()
    scheduler.close()
    with pytest.raises(UnhealthyDevice):
        failed.result()
    with pytest.raises(DevicesUnavailable):
        queued.result()
    assert observations == [True]


def test_dispatch_recovery_preserves_unknown_duration_after_verified_process_death(cache, tmp_path):
    ident = identity()
    cache.register("owner", ident.artifact_id, "s0", (ident,))
    cache.register_dispatch("dispatch", ident.key, "old-session", 2, "owner", "s0")
    cache.start_dispatch("dispatch", "device", .2)
    cache.begin(ident, "device", tmp_path / "interrupted")
    with pytest.raises(TaskIncomplete, match="may still be live"):
        cache.reconcile_interrupted(lambda _: False)
    with cache.store() as store:
        assert store.db.execute("SELECT state FROM task_dispatches").fetchone()[0] == "running"
    cache.reconcile_interrupted(lambda _: True)
    with cache.store() as store:
        row = store.db.execute("SELECT * FROM task_dispatches").fetchone()
        assert row["state"] == "interrupted" and row["callback_seconds"] is None
        metrics = store.status()["scheduler_metrics"]
        assert metrics["unknown_callback_durations"] == 1
        assert metrics["sessions"][0]["assigned_slot_utilization"] is None
        assert metrics["mean_queue_delay_seconds"] == .2
