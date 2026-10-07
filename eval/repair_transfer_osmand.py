"""Reproduce the evaluated root's initialized-empty OsmAnd app snapshot."""

import hashlib
import json
import random
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval/android_world"))


def repair(device):
    from android_world import registry
    from android_world.env import adb_utils, env_launcher
    from android_world.utils import app_snapshot

    from alpha_agents.tree_search.bridges.androidworld.fixture_preflight import (
        check_task_fixtures,
    )
    from alpha_agents.tree_search.bridges.androidworld.task_sets import load_sets

    serial = device["serial"]
    if serial not in ("emulator-5570", "emulator-5572", "emulator-5574"):
        raise ValueError("Not a dedicated transfer device")
    directory = ROOT / "runs/experiments/transfer_devices" / device["name"]
    original = json.loads((directory / "fixtures/report.json").read_text())
    if original["status"] == "running":
        raise RuntimeError("Wait for fixture checks before repairing app snapshot")
    sets = load_sets(ROOT / "alpha_agents/tree_search/bridges/androidworld/tasks.json")
    expected = {task for tasks in sets.values() for task in tasks}
    if {
        r["task"] for r in original["fixture_checks"]
    } != expected or "finished_at" not in original:
        raise RuntimeError("Original fixture preflight is incomplete")
    failures = [r for r in original["fixture_checks"] if r["status"] != "ready"]
    if "OsmAndMarker" not in {r["task"] for r in failures} or any(
        r["task"] != "OsmAndMarker"
        and not r.get("error", "").startswith("FileNotFoundError:")
        for r in failures
    ):
        raise RuntimeError(f"Unexpected fixture failures: {failures}")
    temporary = directory / "fixtures/repair_tmp"
    temporary.mkdir(exist_ok=True)
    tempfile.tempdir = str(temporary)
    adb = str(ROOT / "eval/android-sdk/platform-tools/adb")
    # Later fixtures restore the original empty snapshot. Launch the official
    # app once more so its normal startup creates the marker schema.
    launch = subprocess.run(
        [
            adb,
            "-s",
            serial,
            "shell",
            "am",
            "start",
            "-W",
            "-n",
            "net.osmand/net.osmand.plus.activities.MapActivity",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
    )
    (directory / "fixtures/osmand_launch.txt").write_bytes(launch.stdout)
    time.sleep(15)
    with tempfile.TemporaryDirectory(prefix="transfer-osmand-schema-") as tmp:
        db = Path(tmp) / "database.db"
        subprocess.run(
            [
                adb,
                "-s",
                serial,
                "pull",
                "/data/data/net.osmand/databases/map_markers_db",
                str(db),
            ],
            check=True,
        )
        with sqlite3.connect(
            db.resolve().as_uri() + "?mode=ro", uri=True
        ) as connection:
            if (
                connection.execute("SELECT count(*) FROM map_markers").fetchone()[0]
                != 0
            ):
                raise RuntimeError("Refuse to snapshot a nonempty marker database")
    backup = "/data/local/tmp/transfer_osmand_before_" + str(time.time_ns())
    subprocess.run(
        [
            adb,
            "-s",
            serial,
            "shell",
            "cp",
            "-a",
            "/data/data/android_world/snapshots/net.osmand",
            backup,
        ],
        check=True,
    )
    env = env_launcher.load_and_setup_env(
        console_port=device["console_port"],
        grpc_port=device["grpc_port"],
        adb_path=adb,
        emulator_setup=False,
        freeze_datetime=False,
    )
    try:
        adb_utils.close_app("osmand", env.controller)
        app_snapshot.save_snapshot("osmand", env.controller)
        classes = registry.TaskRegistry().get_registry(family="android_world")
        results = []
        for failure in failures:
            task = failure["task"]
            base = 1042 if task in sets["confirmation"] else 42
            seed = (
                int(hashlib.sha256(f"{base}_{task}_0".encode()).hexdigest(), 16) % 2**32
            )
            for _ in range(2):
                random.seed(seed)
                params = classes[task].generate_random_params()
                params["seed"] = seed
                classes[task].set_device_time(env)
                result = check_task_fixtures({task: classes[task](params)}, env)
                results.append(result)
                if result["status"] != "ready":
                    raise RuntimeError(f"Repaired fixture failed: {result}")
        report = {
            "status": "ready",
            "device": serial,
            "original_fixture_report": str(directory / "fixtures/report.json"),
            "resolved_failures": failures,
            "repair_checks": results,
            "old_snapshot_backup": backup,
            "repair": "Snapshot naturally initialized empty OsmAnd database, as in root experiment",
            "harness_modified": False,
            "benchmark_modified": False,
        }
        (directory / "fixtures/ready.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )
        print(json.dumps(report), flush=True)
    finally:
        env.close()


if __name__ == "__main__":
    for device in json.loads(
        (ROOT / "runs/experiments/transfer_devices/devices.json").read_text()
    ):
        repair(device)
