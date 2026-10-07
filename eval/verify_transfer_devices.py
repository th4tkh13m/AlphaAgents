"""Check dedicated devices match the evaluated root's build and app versions."""

import concurrent.futures
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "runs/experiments/transfer_devices"
OLD = ROOT / "runs/experiments/artemis_sol_medium_50_20261002T181115Z"
ADB = str(ROOT / "eval/android-sdk/platform-tools/adb")


def verify(device, reference):
    serial = device["serial"]
    if serial not in ("emulator-5570", "emulator-5572", "emulator-5574"):
        raise ValueError("Not a dedicated transfer device")

    def shell(*args):
        return subprocess.check_output(
            [ADB, "-s", serial, "shell", *args], text=True, timeout=60
        )

    identity = {
        "build": {
            k: shell("getprop", k).strip() for k in reference["identity"]["build"]
        },
        "packages": {
            package: [
                line.strip()
                for line in shell("dumpsys", "package", package).splitlines()
                if line.strip().startswith(("versionCode=", "versionName="))
            ]
            for package in reference["identity"]["packages"]
        },
    }
    report = {
        "device": serial,
        "identity": identity,
        "matches_root": identity == reference["identity"],
        "expected_sha256": reference["sha256"],
    }
    (OUTPUT / device["name"] / "versions.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    if not report["matches_root"]:
        raise RuntimeError(f"Dedicated device build/app versions differ: {serial}")
    return {"device": serial, "matches_root": True, "sha256": reference["sha256"]}


if __name__ == "__main__":
    reference = json.loads((OLD / "device_fixture_versions.json").read_text())[0]
    devices = json.loads((OUTPUT / "devices.json").read_text())
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        result = list(pool.map(lambda d: verify(d, reference), devices))
    print(json.dumps(result, indent=2))
