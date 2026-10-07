"""Provision dedicated AndroidWorld emulators without touching existing devices."""

import json
import os
import socket
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "eval"
SDK = EVAL / "android-sdk"
AVD = EVAL / ".android/avd"
OUTPUT = ROOT / "runs/experiments/transfer_devices"


def setup(index):
    name = f"CapabilityTransfer{index:02d}"
    console = 5570 + 2 * index
    grpc = 8570 + index
    serial = f"emulator-{console}"
    directory = OUTPUT / name
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / "ready.json"
    env = {
        **os.environ,
        "ANDROID_HOME": str(SDK),
        "ANDROID_SDK_ROOT": str(SDK),
        "ANDROID_AVD_HOME": str(AVD),
    }
    adb = str(SDK / "platform-tools/adb")
    devices = subprocess.check_output([adb, "devices"], text=True)
    if marker.exists() and serial + "\tdevice" in devices:
        return json.loads(marker.read_text())
    running = serial + "\tdevice" in devices
    if running:
        record = json.loads((directory / "process.json").read_text())
        cmdline = Path(f"/proc/{record['pid']}/cmdline").read_bytes()
        if name.encode() not in cmdline:
            raise RuntimeError("Existing device is not this provisioner's emulator")
    else:
        for port in (console, console + 1, grpc):
            with socket.socket() as s:
                try:
                    s.bind(("127.0.0.1", port))
                except OSError:
                    raise RuntimeError(
                        f"Port {port} is occupied; no process will be stopped"
                    )
    if not (AVD / (name + ".avd")).exists():
        subprocess.run(
            [
                str(SDK / "cmdline-tools/latest/bin/avdmanager"),
                "create",
                "avd",
                "-n",
                name,
                "-k",
                "system-images;android-33;google_apis;x86_64",
                "-d",
                "pixel_6",
            ],
            input="no\n",
            text=True,
            env=env,
            check=True,
        )
    process = None
    if not running:
        log = (directory / "emulator.log").open("ab")
        process = subprocess.Popen(
            [
                str(SDK / "emulator/emulator"),
                "-avd",
                name,
                "-port",
                str(console),
                "-grpc",
                str(grpc),
                "-no-snapshot",
                "-no-window",
                "-no-audio",
                "-no-metrics",
                "-no-boot-anim",
            ],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        record = {
            "name": name,
            "serial": serial,
            "console_port": console,
            "grpc_port": grpc,
            "pid": process.pid,
        }
        (directory / "process.json").write_text(json.dumps(record, indent=2))
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"Emulator exited: {directory}")
        boot = subprocess.run(
            [adb, "-s", serial, "shell", "getprop", "sys.boot_completed"],
            capture_output=True,
            text=True,
        )
        if boot.stdout.strip() == "1":
            break
        time.sleep(2)
    else:
        raise RuntimeError(f"Emulator boot timed out: {serial}")
    print(f"{serial}: booted, installing AndroidWorld apps", flush=True)
    code = (
        "from android_env.components import adb_controller; "
        "original_execute = adb_controller.AdbController.execute_command; "
        "adb_controller.AdbController.execute_command = "
        "lambda self,args,timeout=None,device_specific=True: "
        "original_execute(self,args,timeout=max(timeout or 0,60),"
        "device_specific=device_specific); "
        "adb_controller.AdbController._restart_server = lambda self,timeout=None: None; "
        "from android_world.env import env_launcher; "
        f"env=env_launcher.load_and_setup_env(console_port={console}, grpc_port={grpc},"
        f"adb_path={adb!r}, emulator_setup=True); env.close()"
    )
    with (directory / "app_setup.log").open("w") as handle:
        subprocess.run(
            [str(EVAL / "android_world/.venv/bin/python"), "-c", code],
            cwd=EVAL / "android_world",
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            check=True,
        )
    marker.write_text(json.dumps(record, indent=2))
    print(f"{serial}: ready", flush=True)
    return record


if __name__ == "__main__":
    OUTPUT.mkdir(parents=True, exist_ok=True)
    # Provision serially; cold app installs need longer than episode ADB calls.
    # The process-local setup shim never restarts the shared ADB server.
    devices = [setup(index) for index in range(3)]
    (OUTPUT / "devices.json").write_text(json.dumps(devices, indent=2))
    print(json.dumps(devices, indent=2), flush=True)
