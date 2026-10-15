"""Tracing must be automatic, importable, read-only, and recover after errors."""

import gzip
import json
import threading
from unittest.mock import Mock

import pytest

from alpha_agents.tree_search.composition import SearchApplication
from alpha_agents.tree_search.core.mcts.contracts import FixedPUCTConfig, Objective
from alpha_agents.tree_search.core.mcts.storage import TreeStore
from alpha_agents.tree_search.infrastructure.perfetto import TraceWatcher
from alpha_agents.tree_search.infrastructure.trace_export import Trace, snapshot


def test_rounding_shared_endpoints_cannot_create_overlapping_slices():
    trace = Trace(0, 1966.2278455)
    trace.span(1, 1, "parent", 1733.8548024, None, "task")
    trace.span(1, 1, "child", 1782.2360981, None, "phase")
    parent, child = trace.events
    assert parent["ts"] + parent["dur"] == child["ts"] + child["dur"]


@pytest.fixture
def recorded(tmp_path):
    store = TreeStore(
        tmp_path,
        FixedPUCTConfig(screen_gate="execution_valid"),
        Objective("tasks", "rubric", "judge"),
    )
    contract = json.dumps(
        {"artifact_id": "artifact", "template": "TaskA", "sample_kind": "screen"}
    )
    store.db.execute(
        "INSERT INTO task_jobs VALUES('job',?,'running',2,NULL,NULL)", (contract,)
    )
    store.db.execute(
        "INSERT INTO task_runs VALUES('run','job',1,'emulator-5580','running',?,110,NULL,NULL)",
        (str(tmp_path / "task"),),
    )
    store.db.execute(
        "INSERT INTO task_dispatches VALUES('dispatch','job','session',1,'child','screen','running',105,110,NULL,'emulator-5580',5,NULL)"
    )
    store.db.execute(
        "INSERT INTO task_batches VALUES('batch','child','artifact','screen','[\"TaskA\"]','[\"job\"]')"
    )
    yield store
    store.close()


def test_export_without_launcher_preserves_database_and_counts_active_device(recorded):
    before = list(recorded.db.iterdump())
    trace = snapshot(
        recorded.directory, 120, [], ("emulator-5580", "emulator-5582"), 100
    )
    assert list(recorded.db.iterdump()) == before
    spans = [e for e in trace["traceEvents"] if e["ph"] == "X"]
    stage = next(e for e in spans if e["cat"] == "evaluation.stage")
    assert stage["dur"] == 15_000_000  # Full dispatch envelope includes its queue.
    assert stage["args"]["incomplete"]
    queue = next(e for e in spans if e["cat"] == "queue.device")
    assert queue["dur"] == 5_000_000
    assert next(e for e in spans if e["cat"] == "device.task")["args"]["incomplete"]
    counters = [e for e in trace["traceEvents"] if e["name"] == "Device occupancy"]
    assert counters[-1]["args"] == {
        "task": 1,
        "recovery": 0,
        "quarantine": 0,
        "idle": 1,
    }
    assert trace["otherData"]["search_start_unix_seconds"] == 100


def test_completed_task_and_stage_retain_measured_endpoints(recorded):
    recorded.db.execute(
        "UPDATE task_runs SET state='completed',finished=115,payload='{\"success\":true}'"
    )
    recorded.db.execute(
        "UPDATE task_dispatches SET state='completed',finished=117,callback_seconds=7"
    )
    recorded.db.execute("UPDATE task_jobs SET state='completed'")
    trace = snapshot(recorded.directory, 120, [], fallback_start=100)
    spans = [e for e in trace["traceEvents"] if e["ph"] == "X"]
    stage = next(e for e in spans if e["cat"] == "evaluation.stage")
    assert stage["dur"] == 12_000_000 and not stage["args"]["incomplete"]
    task = next(e for e in spans if e["cat"] == "android.task")
    assert task["dur"] == 5_000_000 and task["args"]["success"] == 1


def test_resource_counters_are_distinct_per_device(recorded):
    devices = ("emulator-5580", "emulator-5582")
    samples = [
        {
            "at": 119,
            "devices": {
                d: {"rss_mib": 100, "cpu_percent_one_core": 10} for d in devices
            },
            "host": {},
        }
    ]
    trace = snapshot(recorded.directory, 120, samples, devices, 100)
    counters = [e for e in trace["traceEvents"] if e["pid"] == 600 and e["ph"] == "C"]
    assert len({e["name"] for e in counters}) == 4


def test_watcher_rejects_duplicate_ownership_and_flushes_before_state_close(recorded):
    first = threading.Event()
    watcher = TraceWatcher(recorded.directory, interval=1)
    export = watcher.export

    def notify(**kwargs):
        export(**kwargs)
        first.set()

    watcher.export = notify
    app = SearchApplication(Mock(), Mock(), watcher)
    try:
        app.run()
        assert first.wait(5)
        assert watcher.start()  # Idempotent startup.
        duplicate = TraceWatcher(recorded.directory)
        assert not duplicate.start()
    finally:
        app.close()
    app.close()
    assert not watcher.thread.is_alive()
    status = json.loads((watcher.output / "watcher_status.json").read_text())
    assert status["status"] == "stopped"
    payload = (watcher.output / "live_trace.json").read_bytes()
    assert (
        gzip.decompress((watcher.output / "live_trace.json.gz").read_bytes()) == payload
    )
    assert json.loads(payload)["traceEvents"]
    assert list((watcher.output / "snapshots").glob("*.gz"))
    # The flock was released and resource samples can be resumed.
    resumed = TraceWatcher(recorded.directory)
    try:
        resumed._acquire()
        assert resumed.samples
    finally:
        resumed._release()


def test_failed_observation_recovers_without_stopping_search(tmp_path, monkeypatch):
    from alpha_agents.tree_search.infrastructure import perfetto

    watcher = TraceWatcher(tmp_path)
    watcher._acquire()
    try:
        monkeypatch.setattr(
            perfetto, "snapshot", Mock(side_effect=RuntimeError("busy"))
        )
        assert not watcher._safe_export()
        assert (
            json.loads((watcher.output / "watcher_status.json").read_text())["status"]
            == "error"
        )
        monkeypatch.setattr(
            perfetto,
            "snapshot",
            lambda *a: {"traceEvents": [], "otherData": {"observed_at_utc": "now"}},
        )
        assert watcher._safe_export()
        assert (
            json.loads((watcher.output / "watcher_status.json").read_text())["status"]
            == "watching"
        )
    finally:
        watcher._release()


@pytest.mark.parametrize("operation", ["preflight", "run", "finalize"])
def test_application_owns_profiler_lifecycle_even_on_failure(operation):
    events = []
    coordinator, scheduler, profiler = Mock(), Mock(), Mock()
    getattr(coordinator, operation).side_effect = RuntimeError("work failed")
    profiler.start.side_effect = lambda: events.append("start")
    scheduler.close.side_effect = lambda: events.append("drain")
    profiler.close.side_effect = lambda: events.append("flush")
    coordinator.close.side_effect = lambda: events.append("close")
    with pytest.raises(RuntimeError, match="work failed"):
        with SearchApplication(coordinator, scheduler, profiler) as app:
            getattr(app, operation)()
    assert events == ["start", "drain", "flush", "close"]
