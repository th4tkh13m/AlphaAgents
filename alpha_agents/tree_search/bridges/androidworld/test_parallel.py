"""Exercise four concurrent candidate lifecycles through the real controller."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from alpha_agents.tree_search.bridges.androidworld import artemis
from alpha_agents.tree_search.bridges.androidworld.bridge import AndroidWorldBridge
from alpha_agents.tree_search.bridges.androidworld.devices import local_device_ports
from alpha_agents.tree_search.core.contracts import MutationOutcome
from alpha_agents.tree_search.core.controller import DGMController, SearchConfig


def ports():
    return {
        f"emulator-{5560 + 2 * i}": {
            "console_port": 5560 + 2 * i,
            "grpc_port": 8560 + i,
        }
        for i in range(4)
    }


def make_bridge(tmp_path):
    source = tmp_path / "harness"
    source.mkdir()
    (source / "agent.py").write_text("value = 0\n")
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps({"selection": ["ClockStopWatchPausedVerify"]}))
    mapping = ports()
    return AndroidWorldBridge(
        source,
        {
            "evaluation_runner": "artemis_local",
            "androidworld_devices": list(mapping),
            "androidworld_device_ports": mapping,
            "androidworld_root": str(tmp_path),
            "adb_path": "/bin/true",
            "python": "python",
            "model": "test",
            "base_url": "http://model/v1",
            "task_file": str(tasks),
        },
    )


def test_four_leases_are_distinct_private_and_released_on_error(tmp_path):
    bridge = make_bridge(tmp_path)
    barrier = threading.Barrier(4)

    def lease(_):
        with bridge.lease() as resource:
            device = resource["androidworld_assigned_device"]
            assert resource["console_port"] == ports()[device]["console_port"]
            assert resource["grpc_port"] == ports()[device]["grpc_port"]
            assert list(resource["androidworld_device_ports"]) == [device]
            resource["androidworld_device_ports"][device]["grpc_port"] = 9999
            barrier.wait(timeout=15)
            return device

    with ThreadPoolExecutor(max_workers=4) as executor:
        devices = list(executor.map(lease, range(4)))
    assert set(devices) == set(ports())
    assert bridge.config["androidworld_device_ports"] == ports()
    assert bridge.pool.qsize() == 4
    with pytest.raises(RuntimeError):
        with bridge.lease():
            raise RuntimeError("failed mutation")
    assert bridge.pool.qsize() == 4


@pytest.mark.parametrize(
    "defect", ["missing", "same_grpc", "adb_collision", "wrong_serial", "odd_console"]
)
def test_invalid_device_assignments_rejected(defect):
    mapping = ports()
    devices = list(mapping)
    if defect == "missing":
        del mapping[devices[-1]]
    elif defect == "same_grpc":
        mapping[devices[1]]["grpc_port"] = mapping[devices[0]]["grpc_port"]
    elif defect == "adb_collision":
        mapping[devices[1]]["grpc_port"] = 5561
    elif defect == "wrong_serial":
        mapping[devices[0]]["console_port"] = 5580
    else:
        mapping[devices[0]]["console_port"] = 5561
    with pytest.raises(ValueError):
        local_device_ports(devices, {"androidworld_device_ports": mapping})


@pytest.mark.parametrize("scheduling", ["synchronous", "asynchronous"])
def test_real_dgm_controller_runs_four_children_on_isolated_devices(
    tmp_path, monkeypatch, scheduling
):
    bridge = make_bridge(tmp_path)
    mutation_barrier = threading.Barrier(4)
    evaluation_barrier = threading.Barrier(4)
    mutation_devices = {}
    evaluation_devices = {}
    lock = threading.Lock()

    class Mutator:
        def identity(self):
            return {"type": "parallel-contract-test"}

        def mutate(self, candidate, context):
            device = context.evidence["resource"]
            assert context.evidence["parent_evaluation"]["score"] == 1
            command = context.evidence["experiment_command"]
            assert command[command.index("--serial") + 1] == device
            assert (
                int(command[command.index("--grpc-port") + 1])
                == ports()[device]["grpc_port"]
            )
            with lock:
                mutation_devices[candidate.id] = device
            mutation_barrier.wait(timeout=15)
            (candidate.workspace / "agent.py").write_text(f'value = "{candidate.id}"\n')
            return MutationOutcome("completed")

    def execute(command, workspace, artifacts, timeout, env):
        candidate_id = workspace.parent.name
        device = command[command.index("--serial") + 1]
        assert (
            int(command[command.index("--console-port") + 1])
            == ports()[device]["console_port"]
        )
        assert (
            int(command[command.index("--grpc-port") + 1])
            == ports()[device]["grpc_port"]
        )
        assert env["DGM_ARTEMIS_WORKTREE"] == str(workspace.resolve())
        assert Path(env["ARTEMIS_TRACES_DIR"]).is_relative_to(artifacts.parent)
        if candidate_id != "initial":
            with lock:
                evaluation_devices[candidate_id] = device
            evaluation_barrier.wait(timeout=15)
        manifest = {
            "status": "completed",
            "model": "test",
            "model_base_url": "http://model/v1",
            "seed": 42,
            "tasks": ["ClockStopWatchPausedVerify"],
            "combinations": 1,
            "episodes": [
                {
                    "template": "ClockStopWatchPausedVerify",
                    "index": 0,
                    "success": 1,
                    "androidworld_reward": 1,
                    "artemis_status": "completed",
                }
            ],
        }
        (artifacts.parent / "manifest.json").write_text(json.dumps(manifest))
        return {"returncode": 0, "timed_out": False}, ""

    monkeypatch.setattr(artemis, "execute", execute)
    controller = DGMController(
        bridge,
        Mutator(),
        tmp_path / "search",
        SearchConfig(max_children=4, workers=4, batch_size=4, scheduling=scheduling),
    )
    state = controller.run()
    assert state["completed_children"] == 4
    assert len(state["archive"]) == 5
    assert len(set(mutation_devices.values())) == 4
    assert mutation_devices == evaluation_devices
    assert bridge.pool.qsize() == 4
    assert (tmp_path / "harness/agent.py").read_text() == "value = 0\n"
