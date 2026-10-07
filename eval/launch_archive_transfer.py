"""Launch a full archive-enabled search after dedicated devices pass preflight."""

import argparse
import datetime
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLD = ROOT / "runs/experiments/artemis_sol_medium_50_20261002T181115Z/attempt_005"


def publish(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = args.output or ROOT / "runs/experiments" / (
        "cap_transfer_sol_medium_50_" + stamp
    )
    directory = directory.resolve()
    devices = json.loads(
        (ROOT / "runs/experiments/transfer_devices/devices.json").read_text()
    )
    for device in devices:
        fixture = (
            ROOT
            / "runs/experiments/transfer_devices"
            / device["name"]
            / "fixtures/report.json"
        )
        readiness = fixture.parent / "ready.json"
        if (
            json.loads((readiness if readiness.exists() else fixture).read_text())[
                "status"
            ]
            != "ready"
        ):
            raise RuntimeError(f"Device fixture preflight did not pass: {fixture}")
        versions = json.loads((fixture.parents[1] / "versions.json").read_text())
        if not versions["matches_root"]:
            raise RuntimeError(f"Device build/app versions differ: {device['serial']}")
    directory.mkdir(parents=True, exist_ok=False)
    shutil.copytree(
        ROOT / "alpha_agents",
        directory / "infrastructure_snapshot/alpha_agents",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (directory / "implementation.diff").write_bytes(
        subprocess.check_output(["git", "diff", "--binary", "HEAD"], cwd=ROOT)
    )
    verification = directory / "verification"
    verification.mkdir()
    with (verification / "pytest.txt").open("wb") as log:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "alpha_agents/tree_search/tests",
                "alpha_agents/tree_search/bridges/androidworld",
                "-q",
            ],
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
    shutil.copytree(
        ROOT / "runs/experiments/transfer_devices",
        verification / "devices",
        ignore=shutil.ignore_patterns(
            "emulator.log", "app_setup.log", "tmp", "repair_tmp"
        ),
    )
    config = json.loads(
        (OLD.parent / "config_full_benchmark_300s_3devices.json").read_text()
    )
    bridge = config["harness"]["config"]
    bridge.update(
        console_port=devices[0]["console_port"],
        grpc_port=devices[0]["grpc_port"],
        androidworld_devices=[d["serial"] for d in devices],
        androidworld_device_ports={
            d["serial"]: {k: d[k] for k in ("console_port", "grpc_port")}
            for d in devices
        },
        reuse_root_from=str(OLD),
        task_metadata_file=str(ROOT / "androidworld_sets_manifest.json"),
    )
    config["archive_access"] = {
        "enabled": True,
        "import_runs": [str(OLD)],
        "include_current_run": True,
        "allowed_stages": ["selection"],
    }
    config["validation"] = [
        {"type": "changed_pytest", "python": bridge["python"], "timeout": 300}
    ]
    config_path = directory / "config.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    command = [
        sys.executable,
        "-u",
        "-m",
        "alpha_agents.tree_search",
        "--config",
        str(config_path),
        "--output",
        str(directory / "attempt_001"),
    ]
    with (directory / "controller.log").open("wb") as log:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    start_ticks = (
        Path(f"/proc/{process.pid}/stat").read_text().split(") ", 1)[1].split()[19]
    )
    metadata = {
        "pid": process.pid,
        "start_ticks": start_ticks,
        "started_at": stamp,
        "command": command,
        "directory": str(directory),
        "root_reused_from": str(OLD),
        "max_children": 50,
        "git_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
    }
    publish(directory / "launch.json", metadata)
    with (directory / "monitor.log").open("wb") as log:
        monitor = subprocess.Popen(
            [
                sys.executable,
                "-u",
                str(ROOT / "eval/monitor_archive_transfer.py"),
                str(directory),
            ],
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    metadata["monitor_pid"] = monitor.pid
    publish(directory / "launch.json", metadata)
    publish(ROOT / "runs/experiments/current_cap_transfer_run.json", metadata)
    print(json.dumps(metadata, indent=2), flush=True)


if __name__ == "__main__":
    main()
