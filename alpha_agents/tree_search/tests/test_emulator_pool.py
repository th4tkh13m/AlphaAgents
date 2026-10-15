"""Private daemon configuration and failure containment for operational launch."""

import socket
import subprocess

import pytest

from alpha_agents.tree_search.bridges.androidworld import emulator_pool as pool


def test_source_in_use_detects_name_and_avd_home_without_absolute_argv(tmp_path):
    source = tmp_path / "Worker.avd"
    assert pool.uses_avd(source, ["qemu-system-x86", "-avd", "Worker"], str(tmp_path))
    assert not pool.uses_avd(
        source, ["qemu-system-x86", "-avd", "Other"], str(tmp_path)
    )
    assert pool.uses_avd(source, ["qemu", "-datadir", str(source)], None)


def test_six_devices_have_disjoint_ports_and_private_environments(tmp_path):
    rows = pool.layout(6, 5580, 8580, 19001, 19101)
    envs = [
        pool.device_env(
            {"NETSIM_ARGS": "--debug-no-network", "NETSIM_HCI_PORT": "1"},
            tmp_path,
            tmp_path / "avd",
            tmp_path / r["serial"],
            r,
        )
        for r in rows
    ]
    assert len({e["TMPDIR"] for e in envs}) == 6
    assert len({e["NETSIM_INSTANCE"] for e in envs}) == 6
    assert len({e["NETSIM_GRPC_PORT"] for e in envs}) == 6
    for r, env in zip(rows, envs):
        assert "NETSIM_ARGS" not in env and "NETSIM_HCI_PORT" not in env
        daemon, emulator = pool.commands(tmp_path, r)
        assert emulator[-2:] == [
            "-packet-streamer-endpoint",
            f"127.0.0.1:{r['netsim_port']}",
        ]
        assert "-feature" not in emulator  # SDK Wi-Fi default is retained.
        assert daemon[daemon.index("--hci-port") + 1] == str(r["hci_port"])


@pytest.mark.parametrize(
    "args",
    [
        (0, 5580, 8580, 19001, 19101),
        (10, 5580, 8580, 19001, 19101),
        (2, 5581, 8580, 19001, 19101),
        (2, 5580, 5580, 19001, 19101),
        (2, 5580, 8580, 65535, 19101),
    ],
)
def test_invalid_layout_rejected(args):
    with pytest.raises(ValueError):
        pool.layout(*args)


def test_bound_port_rejected_before_any_process_start():
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        port = held.getsockname()[1]
        rows = [
            dict(console_port=port, grpc_port=12001, netsim_port=12002, hci_port=12003)
        ]
        with pytest.raises(OSError):
            pool.check_ports(rows)


def test_active_listener_rejected_even_when_address_reuse_is_enabled():
    with socket.socket() as held:
        held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        held.bind(("127.0.0.1", 0))
        held.listen()
        port = held.getsockname()[1]
        rows = [
            dict(console_port=port, grpc_port=12001, netsim_port=12002, hci_port=12003)
        ]
        with pytest.raises(OSError, match=f"Port {port} unavailable"):
            pool.check_ports(rows)


class Process:
    def __init__(self, code=None, timeout=False):
        self.code, self.timeout, self.calls = code, timeout, []

    def poll(self):
        return self.code

    def terminate(self):
        self.calls.append("terminate")

    def kill(self):
        self.calls.append("kill")
        self.code = -9

    def wait(self, timeout):
        self.calls.append("wait")
        if self.timeout and self.code is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        self.code = -15 if self.code is None else self.code
        return self.code


def test_stop_reaps_only_owned_process_and_escalates_after_deadline():
    process = Process(timeout=True)
    pool.stop_owned(process, 0.01)
    assert process.calls == ["terminate", "wait", "kill", "wait"]
    pool.stop_owned(process)
    assert process.calls == ["terminate", "wait", "kill", "wait"]


def test_daemon_death_stops_its_emulator_and_preserves_other_pair(
    tmp_path, monkeypatch
):
    supervisor = pool.PrivatePool(tmp_path, tmp_path, tmp_path, tmp_path, [])
    failed, healthy = dict(status="ready", ready=True), dict(status="ready", ready=True)
    dead_daemon, first_emulator = Process(1), Process()
    other_daemon, other_emulator = Process(), Process()
    supervisor.pairs = [
        (failed, dead_daemon, first_emulator),
        (healthy, other_daemon, other_emulator),
    ]
    saved = []
    monkeypatch.setattr(supervisor, "save", lambda **kw: saved.append(kw))
    supervisor.check_pairs()
    assert failed["status"] == "failed" and not failed["ready"]
    assert first_emulator.code == -15
    assert not other_daemon.calls and not other_emulator.calls
    assert healthy["ready"] and supervisor.state == "degraded" and saved


def test_failed_launch_cleans_all_started_children(tmp_path, monkeypatch):
    supervisor = pool.PrivatePool(tmp_path, tmp_path, tmp_path, tmp_path, [{}])
    daemon, emulator = Process(), Process()
    supervisor.processes = [daemon, emulator]
    monkeypatch.setattr(supervisor, "prepare", lambda: None)

    def fail(row):
        raise RuntimeError("boot failed")

    monkeypatch.setattr(supervisor, "start_device", fail)
    with pytest.raises(RuntimeError, match="boot failed"):
        supervisor.run()
    assert daemon.code == -15 and emulator.code == -15


def test_rejected_output_never_overwrites_existing_pool(tmp_path, monkeypatch):
    path = tmp_path / "status.json"
    path.write_text("historical evidence")
    (tmp_path / "pool.lock").touch()
    supervisor = pool.PrivatePool(tmp_path, tmp_path, tmp_path, tmp_path, [])

    def rejected():
        raise FileExistsError("previous pool")

    monkeypatch.setattr(supervisor, "prepare", rejected)
    with pytest.raises(FileExistsError):
        supervisor.run()
    assert path.read_text() == "historical evidence"
