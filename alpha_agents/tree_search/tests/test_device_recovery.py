"""Fault isolation and recovery must preserve rewards and device ownership."""

import json
import threading
from types import SimpleNamespace

import pytest

from alpha_agents.tree_search.bridges.androidworld.device_maintenance import (
    DeviceMaintenance,
)
from alpha_agents.tree_search.bridges.androidworld.device_recovery import (
    DeviceTransportFailure,
    device_failure,
    install_device_local_adb_policy,
)
from alpha_agents.tree_search.bridges.androidworld.scheduler import (
    DeviceScheduler,
    DevicesUnavailable,
    TaskJob,
    UnhealthyDevice,
)


def test_dependency_retry_cannot_restart_shared_adb():
    commands = []

    class Controller:
        def _restart_server(self, timeout=None):
            commands.append("kill-server")

    install_device_local_adb_policy(Controller)
    with pytest.raises(DeviceTransportFailure, match="shared-server"):
        Controller()._restart_server(timeout=10)
    assert commands == []


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_forwarder_is_stopped_before_server_close_without_disabling_other_services(
    cleanup_fails,
):
    events = []

    class Controller:
        pass

    def command(parts, timeout):
        assert timeout == 5
        events.append(parts)
        if cleanup_fails:
            raise TimeoutError("device stalled")
        return b"com.google.androidenv.accessibilityforwarder/.AccessibilityForwarder:other/.Service\n"

    class Wrapper:
        def close(self):
            events.append("server-close")

    install_device_local_adb_policy(Controller, Wrapper)
    wrapper = Wrapper()
    wrapper._env = SimpleNamespace(
        _coordinator=SimpleNamespace(
            _adb_call_parser=SimpleNamespace(
                _adb_controller=SimpleNamespace(execute_command=command)
            )
        )
    )
    if cleanup_fails:
        with pytest.raises(TimeoutError):
            wrapper.close()
    else:
        wrapper.close()
        assert [
            "shell",
            "settings",
            "put",
            "secure",
            "enabled_accessibility_services",
            "other/.Service",
        ] in events
        assert [
            "shell",
            "am",
            "force-stop",
            "com.google.androidenv.accessibilityforwarder",
        ] in events
        before = len(events)
        wrapper.close()
        assert (
            len(events) == before + 1
        )  # Idempotent device cleanup; server close remains safe.
    assert events[-1] == "server-close"


@pytest.mark.parametrize(
    "error",
    [
        "DeviceOfflineError: lost",
        "AdbControllerError: timeout",
        "RuntimeError: Could not get a11y tree.",
        "DeviceTransportFailure: blocked",
    ],
)
def test_broken_infrastructure_is_not_a_responsive_device(error):
    assert device_failure({"task_results": [{"exception": error}]}) == error


@pytest.mark.parametrize(
    "error",
    [
        None,
        "TimeoutError: LLM call timed out",
        "GraphRecursionError: limit",
        "RuntimeError: bad generated harness code",
    ],
)
def test_agent_failure_is_not_a_reason_to_reboot_device(error):
    assert device_failure({"task_results": [{"exception": error}]}) is None


def test_single_device_recovery_readmits_only_after_functional_success():
    entered, finish = threading.Event(), threading.Event()

    def recovery(device, reason):
        entered.set()
        assert finish.wait(5)
        return True

    scheduler = DeviceScheduler(("d0",), autostart=False, recover_device=recovery)

    def bad(device):
        raise UnhealthyDevice("accessibility failed")

    failed = scheduler.submit(TaskJob("bad", "trial", "screen", 10, bad))
    next_job = scheduler.submit(TaskJob("next", "trial", "screen", 1, lambda d: d))
    scheduler.start()
    assert entered.wait(5)
    status = scheduler.status()
    assert status["recovering"] == ["d0"]
    assert status["active_device_count"] == 0
    assert status["idle_healthy_device_count"] == 0
    assert not next_job.done()
    finish.set()
    with pytest.raises(UnhealthyDevice):
        failed.result(timeout=5)
    assert next_job.result(timeout=5) == "d0"
    scheduler.close()
    assert not scheduler.status()["quarantined"]


@pytest.mark.parametrize("failure", [False, RuntimeError("recovery failed")])
def test_failed_recovery_keeps_quarantine_and_settles_queue(failure):
    def recovery(*args):
        if isinstance(failure, Exception):
            raise failure
        return failure

    scheduler = DeviceScheduler(("d0",), autostart=False, recover_device=recovery)

    def bad(device):
        raise UnhealthyDevice("broken")

    failed = scheduler.submit(TaskJob("bad", "trial", "screen", 10, bad))
    queued = scheduler.submit(TaskJob("next", "trial", "screen", 1, lambda d: d))
    scheduler.start()
    with pytest.raises(UnhealthyDevice):
        failed.result(timeout=5)
    with pytest.raises(DevicesUnavailable):
        queued.result(timeout=5)
    scheduler.close()
    assert scheduler.status()["quarantined"]


def test_recovery_does_not_block_other_device_work():
    recovering, release, healthy_work = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )

    def recovery(*args):
        recovering.set()
        assert release.wait(5)
        return True

    scheduler = DeviceScheduler(("d0", "d1"), autostart=False, recover_device=recovery)
    lock = threading.Lock()
    first = []

    def work(device):
        with lock:
            fail = not first
            first.append(device)
        if fail:
            raise UnhealthyDevice("one device")
        assert recovering.wait(5)
        healthy_work.set()

    futures = [
        scheduler.submit(TaskJob(str(i), "trial", "screen", 1, work)) for i in range(3)
    ]
    scheduler.start()
    assert healthy_work.wait(5)
    release.set()
    scheduler.close()
    assert sum(f.exception() is not None for f in futures) == 1


def test_intervention_budget_persists_across_manager_recreation(tmp_path):
    config = {
        "python": "/python",
        "adb_path": "/adb",
        "androidworld_root": "/androidworld",
        "console_port": 5580,
        "grpc_port": 8580,
    }
    calls = []

    def execute(command, workspace, artifacts, *, env, timeout):
        calls.append((command[-1], timeout))
        (workspace / "health.json").write_text(
            json.dumps({"healthy": False, "finished": 1})
        )
        return {"status": "finished", "returncode": 1}, {}

    manager = DeviceMaintenance(
        tmp_path, config, ("emulator-5580",), execute_process=execute
    )
    assert manager("emulator-5580", "broken") is False
    manager = DeviceMaintenance(
        tmp_path, config, ("emulator-5580",), execute_process=execute
    )
    assert manager("emulator-5580", "still broken") is False
    assert calls == [("services", 90), ("reboot", 180)]


def test_all_nine_devices_recover_without_stranding_queues_or_admitting_early():
    started = threading.Barrier(10)
    healing = threading.Barrier(10)
    release = threading.Event()
    admitted = threading.Event()

    def recovery(device, reason):
        healing.wait(timeout=5)
        assert release.wait(5)
        return True

    def fail(device):
        started.wait(timeout=5)
        raise UnhealthyDevice("injected accessibility failure")

    scheduler = DeviceScheduler(
        tuple(f"d{i}" for i in range(9)), autostart=False, recover_device=recovery
    )
    failed = [
        scheduler.submit(TaskJob(f"bad-{i}", "trial", "screen", 10, fail))
        for i in range(9)
    ]
    scheduler.start()
    started.wait(timeout=5)
    healing.wait(timeout=5)
    assert len(scheduler.status()["recovering"]) == 9
    assert scheduler.status()["active_device_count"] == 0
    # Submission waits for recovery instead of declaring the entire pool dead.
    pending = scheduler.submit(
        TaskJob("after-recovery", "trial", "screen", 1, lambda d: d)
    )

    def implementor():
        with scheduler.implementation("owner"):
            admitted.set()

    worker = threading.Thread(target=implementor)
    worker.start()
    assert not admitted.is_set()
    assert not pending.done()
    release.set()
    for future in failed:
        with pytest.raises(UnhealthyDevice):
            future.result(timeout=5)
    assert pending.result(timeout=5).startswith("d")
    worker.join(timeout=5)
    assert not worker.is_alive() and admitted.is_set()
    scheduler.close()
    assert not scheduler.status()["quarantined"]


def test_incomplete_or_timed_out_health_receipt_cannot_readmit(tmp_path):
    config = {
        "python": "/python",
        "adb_path": "/adb",
        "androidworld_root": "/androidworld",
        "console_port": 5580,
        "grpc_port": 8580,
    }

    def execute(command, workspace, artifacts, *, env, timeout):
        (workspace / "health.json").write_text(json.dumps({"healthy": True}))
        return {"status": "timed_out", "returncode": -9}, {}

    manager = DeviceMaintenance(
        tmp_path, config, ("emulator-5580",), execute_process=execute
    )
    assert manager("emulator-5580", "broken") is False


@pytest.mark.parametrize("mode", ["probe", "services", "reboot"])
@pytest.mark.parametrize("clipboard", ["", "existing task text"])
def test_health_probe_never_writes_clipboard_or_records_its_contents(
    tmp_path, monkeypatch, mode, clipboard
):
    import subprocess
    import sys

    from alpha_agents.tree_search.bridges.androidworld import device_recovery

    commands = []
    closed = []

    def run(command, **kwargs):
        parts = command[3:]
        commands.append(parts)
        assert "clipper.set" not in parts
        if "dumpsys" in parts:
            output = "mCurrentFocus=Window{123 ca.zgrs.clipper/ca.zgrs.clipper.Main}\n"
        elif "clipper.get" in parts:
            output = f'Broadcast completed: result=-1, data="{clipboard}"\n'
        elif "sys.boot_completed" in parts:
            output = "1\n"
        else:
            output = "ok\n"
        return subprocess.CompletedProcess(command, 0, stdout=output)

    controller = SimpleNamespace(
        get_a11y_forest=lambda: SimpleNamespace(ByteSize=lambda: 1),
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(
        device_recovery, "install_device_local_adb_policy", lambda: None
    )
    monkeypatch.setattr(device_recovery.subprocess, "run", run)
    monkeypatch.setattr(device_recovery.time, "sleep", lambda _: None)
    monkeypatch.setitem(sys.modules, "android_world", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "android_world.env",
        SimpleNamespace(
            android_world_controller=SimpleNamespace(
                get_controller=lambda *args: controller,
            )
        ),
    )
    args = SimpleNamespace(
        mode=mode,
        device="emulator-test",
        adb="adb",
        androidworld=str(tmp_path),
        output=str(tmp_path),
        console_port=5580,
        grpc_port=8580,
    )
    assert device_recovery.probe(args)
    record = json.loads((tmp_path / "health.json").read_text())
    assert record["healthy"] and record["finished"]
    assert closed == [True]
    assert sum("clipper.get" in command for command in commands) == 3
    assert not any("ALPHA_DEVICE_HEALTH" in str(command) for command in commands)
    assert "existing task text" not in json.dumps(record)
    assert all(
        check["stdout"] == "[command output omitted]"
        for check in record["checks"]
        if "clipper.get" in check.get("command", [])
    )


@pytest.mark.parametrize(
    "output",
    [
        "Broadcast completed: result=0",
        "Broadcast completed: result=-1",
        'Broadcast completed: result=1, data="text"',
        "unexpected output",
    ],
)
def test_read_only_clipboard_check_rejects_unconfirmed_reads(
    monkeypatch, output, clipper_focus
):
    from alpha_agents.tree_search.bridges.androidworld import device_recovery

    monkeypatch.setattr(device_recovery.time, "sleep", lambda _: None)
    calls = []

    def adb(*parts, **kwargs):
        calls.append(parts)
        return output

    with pytest.raises(RuntimeError, match="Clipboard read probe failed"):
        device_recovery.check_clipboard_readable(adb)
    assert not any("clipper.set" in parts for parts in calls)


def test_read_only_clipboard_timeout_propagates_without_cleanup_writes(
    monkeypatch, clipper_focus
):
    from alpha_agents.tree_search.bridges.androidworld import device_recovery

    monkeypatch.setattr(device_recovery.time, "sleep", lambda _: None)
    calls = []

    def adb(*parts, **kwargs):
        calls.append(parts)
        if "clipper.get" in parts:
            raise TimeoutError("unresponsive receiver")
        return "ok"

    with pytest.raises(TimeoutError):
        device_recovery.check_clipboard_readable(adb)
    assert not any("clipper.set" in parts for parts in calls)


def test_read_only_probe_rejects_known_legacy_contamination(monkeypatch, clipper_focus):
    from alpha_agents.tree_search.bridges.androidworld import device_recovery

    monkeypatch.setattr(device_recovery.time, "sleep", lambda _: None)

    def adb(*parts, **kwargs):
        assert "clipper.set" not in parts
        return 'Broadcast completed: result=-1, data="ALPHA_DEVICE_HEALTH_2"'

    with pytest.raises(
        RuntimeError, match="Legacy health-probe clipboard contamination"
    ):
        device_recovery.check_clipboard_readable(adb)


@pytest.fixture
def clipper_focus(monkeypatch):
    from alpha_agents.tree_search.bridges.androidworld import (
        device_recovery as recovery,
    )

    monkeypatch.setattr(recovery, "focus_clipper", lambda adb: None)
    monkeypatch.setattr(
        recovery, "focused_window", lambda adb: recovery.CLIPPER_ACTIVITY
    )


@pytest.mark.parametrize("permission", ["allow", "deny", "ignore", "default"])
def test_null_clipboard_requires_focus_and_explicit_read_permission(
    clipper_focus, permission
):
    from alpha_agents.tree_search.bridges.androidworld import (
        device_recovery as recovery,
    )

    commands = []

    def adb(*parts, **kwargs):
        commands.append(parts)
        if "clipper.get" in parts:
            return 'Broadcast completed: result=0, data=""\n'
        if "appops" in parts:
            return "READ_CLIPBOARD: " + permission + "; time=old\n"
        raise AssertionError(parts)

    if permission == "allow":
        assert (
            recovery.check_clipboard_readable(adb)
            == ["no_text_with_focus_and_read_permission"] * 3
        )
    else:
        with pytest.raises(RuntimeError, match="read app-op"):
            recovery.check_clipboard_readable(adb)
    assert not any("clipper.set" in parts for parts in commands)


def test_clipboard_read_cannot_pass_after_losing_focus(monkeypatch, clipper_focus):
    from alpha_agents.tree_search.bridges.androidworld import (
        device_recovery as recovery,
    )

    monkeypatch.setattr(recovery, "focused_window", lambda adb: "another.app")
    with pytest.raises(RuntimeError, match="lost input focus"):
        recovery.check_clipboard_readable(
            lambda *args, **kwargs: 'Broadcast completed: result=0, data=""'
        )


def test_systemui_anr_wait_is_targeted_and_focus_is_rechecked(monkeypatch):
    from alpha_agents.tree_search.bridges.androidworld import (
        device_recovery as recovery,
    )

    focus = "Window{123 Application Not Responding: com.android.systemui}"
    commands = []

    def adb(*parts, **kwargs):
        nonlocal focus
        commands.append(parts)
        if "dumpsys" in parts:
            return "mCurrentFocus=" + focus
        if "cat" in parts:
            return '<hierarchy><node package="android" resource-id="android:id/aerr_wait" bounds="[70,1300][1010,1426]"/></hierarchy>'
        if "tap" in parts:
            focus = "Window{456 " + recovery.CLIPPER_ACTIVITY + "}"
        return "ok"

    recovery.focus_clipper(adb)
    assert [c for c in commands if "tap" in c] == [
        ("shell", "input", "tap", "540", "1363")
    ]
    assert not any("clipper.set" in c for c in commands)


@pytest.mark.parametrize(
    "focus",
    [
        "Window{123 Application Not Responding: benchmark.app}",
        "unknown",
        "Window{123 benchmark.app/.Main}",
    ],
)
def test_unknown_focus_or_other_app_anr_cannot_be_dismissed(monkeypatch, focus):
    from alpha_agents.tree_search.bridges.androidworld import (
        device_recovery as recovery,
    )

    monkeypatch.setattr(recovery.time, "sleep", lambda _: None)
    commands = []

    def adb(*parts, **kwargs):
        commands.append(parts)
        return "mCurrentFocus=" + focus

    with pytest.raises(RuntimeError, match="did not acquire input focus"):
        recovery.focus_clipper(adb)
    assert not any("tap" in c or "uiautomator" in c for c in commands)


@pytest.mark.parametrize("changed", [False, True])
def test_anr_missing_control_or_changed_focus_cannot_trigger_tap(changed):
    from alpha_agents.tree_search.bridges.androidworld import (
        device_recovery as recovery,
    )

    focus = "Window{123 Application Not Responding: com.android.systemui}"
    commands = []

    def adb(*parts, **kwargs):
        commands.append(parts)
        if "cat" in parts:
            if changed:
                return '<hierarchy><node package="android" resource-id="android:id/aerr_wait" bounds="[0,0][10,10]"/></hierarchy>'
            return "<hierarchy/>"
        if "dumpsys" in parts:
            return "mCurrentFocus=Window{456 another.app}"
        return "ok"

    with pytest.raises(RuntimeError, match="Focus changed|not identified"):
        recovery.dismiss_systemui_anr(adb, focus)
    assert not any("tap" in c for c in commands)


def test_cold_clipper_launch_gets_one_bounded_retry(monkeypatch):
    import subprocess

    from alpha_agents.tree_search.bridges.androidworld import (
        device_recovery as recovery,
    )

    calls = []

    def adb(*parts, **kwargs):
        if "start" in parts:
            calls.append(kwargs["timeout"])
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(parts, kwargs["timeout"])
        return "mCurrentFocus=Window{456 " + recovery.CLIPPER_ACTIVITY + "}"

    recovery.focus_clipper(adb)
    assert calls == [30, 30]
