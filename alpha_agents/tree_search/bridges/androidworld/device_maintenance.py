"""Bounded device recovery, with independent receipts and resource accounting."""

import json
import os
import time
from pathlib import Path

from alpha_agents.tree_search.bridges.androidworld import device_recovery
from alpha_agents.tree_search.bridges.androidworld.devices import local_device_ports
from alpha_agents.tree_search.bridges.androidworld.runtime import evaluation_lock
from alpha_agents.tree_search.core.mcts.contracts import canonical_json
from alpha_agents.tree_search.core.shared.storage import write_json
from alpha_agents.tree_search.infrastructure.process import execute


class DeviceMaintenance:
    """At most two interventions per device per run, including across restarts.

    First try helper-service recovery; then a device reboot. Each subprocess
    has a whole-process deadline and an exclusive per-device lock. No shared
    ADB server operation or benchmark result is permitted here.
    """

    def __init__(self, directory, config, devices, *, execute_process=execute):
        self.directory, self.config = Path(directory), dict(config)
        self.ports = local_device_ports(list(devices), config)
        self.execute = execute_process
        self.cache = None

    def __call__(self, device, reason):
        root = self.directory / "device_recovery" / device
        root.mkdir(parents=True, exist_ok=True)
        with evaluation_lock(self.directory, device, blocking=False):
            for ordinal, mode, deadline in [(1, "services", 90), (2, "reboot", 180)]:
                output = root / str(ordinal)
                if output.exists():
                    continue  # A prior interrupted intervention consumes its allowance.
                output.mkdir()
                start = time.time()
                write_json(
                    output / "intervention.json",
                    {
                        "device": device,
                        "mode": mode,
                        "reason": reason,
                        "started": start,
                        "timeout_seconds": deadline,
                        "state": "running",
                    },
                )
                env = os.environ.copy()
                env["TMPDIR"] = str(output / "tmp")
                Path(env["TMPDIR"]).mkdir()
                command = [
                    self.config["python"],
                    str(Path(device_recovery.__file__).resolve()),
                    "--device",
                    device,
                    "--adb",
                    self.config["adb_path"],
                    "--androidworld",
                    self.config["androidworld_root"],
                    "--console-port",
                    str(self.ports[device]["console_port"]),
                    "--grpc-port",
                    str(self.ports[device]["grpc_port"]),
                    "--output",
                    str(output),
                    "--mode",
                    mode,
                ]
                healthy, error = False, None
                try:
                    process, _ = self.execute(
                        command, output, output / "process", env=env, timeout=deadline
                    )
                    report = json.loads((output / "health.json").read_text())
                    healthy = (
                        process.get("returncode") == 0
                        and process.get("status") == "finished"
                        and report.get("healthy") is True
                        and report.get("finished") is not None
                    )
                except Exception as failure:
                    error = f"{type(failure).__name__}: {failure}"
                finished = time.time()
                receipt = {
                    "device": device,
                    "mode": mode,
                    "reason": reason,
                    "started": start,
                    "finished": finished,
                    "seconds": finished - start,
                    "healthy": healthy,
                    "timeout_seconds": deadline,
                    "error": error,
                    "state": "completed",
                }
                write_json(output / "intervention.json", receipt)
                if self.cache is not None:
                    with self.cache.store() as store, store.transaction():
                        store.db.execute(
                            "INSERT INTO costs VALUES(?,?,?,?,?,'observed',?)",
                            (
                                f"device-recovery:{device}:{ordinal}",
                                device,
                                "device_recovery",
                                "device",
                                finished - start,
                                canonical_json(receipt),
                            ),
                        )
                if healthy:
                    return True
        return False
