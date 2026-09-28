"""Run isolated AndroidWorld evaluations on multiple local Android emulators."""

import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

from android_world import registry


EVAL_DIR = Path(__file__).resolve().parent
ANDROID_WORLD_DIR = EVAL_DIR / "android_world"
SDK_DIR = EVAL_DIR / "android-sdk"
AVD_HOME = EVAL_DIR / ".android" / "avd"
ADB = SDK_DIR / "platform-tools" / "adb"
EMULATOR = SDK_DIR / "emulator" / "emulator"
AVDMANAGER = SDK_DIR / "cmdline-tools" / "latest" / "bin" / "avdmanager"
SYSTEM_IMAGE = "system-images;android-33;google_apis;x86_64"
APP_SETUP_WARNING = re.compile(r"Failed to automatically setup app[^\n]*")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--agent-name", required=True)
    parser.add_argument("--tasks", help="Comma-separated task names; default: full suite")
    parser.add_argument("--mode", choices=("shard", "replicate"), default="shard")
    parser.add_argument("--n-task-combinations", type=int, default=1)
    parser.add_argument("--task-random-seed", type=int, default=30)
    parser.add_argument("--base-console-port", type=int, default=5554)
    parser.add_argument("--base-grpc-port", type=int, default=8554)
    parser.add_argument("--avd-prefix", default="AndroidWorldWorker")
    parser.add_argument("--skip-app-setup", action="store_true")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    if args.n_task_combinations < 1:
        parser.error("--n-task-combinations must be positive")
    if args.base_console_port % 2 or not 5554 <= args.base_console_port <= 5682:
        parser.error("--base-console-port must be even and between 5554 and 5682")
    if args.base_console_port + 2 * (args.workers - 1) > 5682:
        parser.error("worker console ports exceed Android emulator's range")
    if not 1024 <= args.base_grpc_port <= 65535 - args.workers + 1:
        parser.error("gRPC port range is invalid")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", args.avd_prefix):
        parser.error("--avd-prefix must start with a letter and contain only letters, digits, _ or -")
    return args


def make_plan(args: argparse.Namespace) -> list[dict]:
    available = registry.TaskRegistry().get_registry(family="android_world")
    tasks = (
        [name.strip() for name in args.tasks.split(",") if name.strip()]
        if args.tasks
        else sorted(available)
    )
    if not tasks or len(tasks) != len(set(tasks)):
        raise ValueError("Task list must be nonempty and contain no duplicates")
    unknown = sorted(set(tasks) - set(available))
    if unknown:
        raise ValueError(f"Unknown AndroidWorld tasks: {', '.join(unknown)}")
    if args.mode == "shard" and args.workers > len(tasks):
        raise ValueError("Shard mode needs at least one task per worker")
    groups = [tasks[i:: args.workers] for i in range(args.workers)]
    if args.mode == "replicate":
        groups = [tasks[:] for _ in range(args.workers)]
    plan = []
    for index, group in enumerate(groups):
        console_port = args.base_console_port + 2 * index
        plan.append(
            {
                "worker": index + 1,
                "avd": f"{args.avd_prefix}{index + 1:02d}",
                "serial": f"emulator-{console_port}",
                "console_port": console_port,
                "adb_port": console_port + 1,
                "grpc_port": args.base_grpc_port + index,
                "tasks": group,
                "seed": args.task_random_seed + (index if args.mode == "replicate" else 0),
            }
        )
    ports = [p for worker in plan for p in (worker["console_port"], worker["adb_port"], worker["grpc_port"])]
    if len(ports) != len(set(ports)):
        raise ValueError("Console, ADB, and gRPC port ranges overlap")
    return plan


def check_port_available(port: int) -> None:
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        with socket.socket(family) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError as exc:
                raise RuntimeError(f"Port {port} is unavailable: {exc}") from exc


def wait_for_boot(worker: dict, process: subprocess.Popen, timeout: int = 240) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{worker['avd']} exited before boot; see emulator.log")
        try:
            result = subprocess.run(
                [str(ADB), "-s", worker["serial"], "shell", "getprop", "sys.boot_completed"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except subprocess.TimeoutExpired:
            result = None
        if result and result.returncode == 0 and result.stdout.strip() == "1":
            api = subprocess.run(
                [str(ADB), "-s", worker["serial"], "shell", "getprop", "ro.build.version.sdk"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout.strip()
            if api != "33":
                raise RuntimeError(f"{worker['avd']} booted API {api}; expected 33")
            return
        time.sleep(2)
    raise TimeoutError(f"{worker['avd']} did not finish booting in {timeout}s")


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def main() -> int:
    args = parse_args()
    plan = make_plan(args)
    if args.dry_run:
        print(json.dumps(plan, indent=2))
        return 0
    for required in (ADB, EMULATOR, AVDMANAGER):
        if not required.exists():
            raise RuntimeError(f"Missing {required}; run ./eval/setup.sh --with-emulator")

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = args.run_dir or EVAL_DIR / "results" / "android_world_parallel" / f"run_{timestamp}_{os.getpid()}"
    run_dir = run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    AVD_HOME.mkdir(parents=True, exist_ok=True)
    locks_dir = EVAL_DIR / "results" / "android_world_locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update(
        ANDROID_HOME=str(SDK_DIR),
        ANDROID_SDK_ROOT=str(SDK_DIR),
        ANDROID_AVD_HOME=str(AVD_HOME),
    )
    locks = []
    emulators = []
    runners = []
    logs = []
    try:
        for worker in plan:
            lock = (locks_dir / f"{worker['avd']}.lock").open("w")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                lock.close()
                raise RuntimeError(f"{worker['avd']} is already in use; choose another --avd-prefix") from exc
            locks.append(lock)
            for port in (worker["console_port"], worker["adb_port"], worker["grpc_port"]):
                check_port_available(port)

        (run_dir / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
        for worker in plan:
            worker_dir = run_dir / f"worker_{worker['worker']:02d}"
            worker_dir.mkdir()
            temp_dir = worker_dir / "tmp"
            temp_dir.mkdir()
            worker_env = environment | {"TMPDIR": str(temp_dir)}
            avd_dir = AVD_HOME / f"{worker['avd']}.avd"
            if not (avd_dir / "config.ini").exists():
                with (worker_dir / "avd_setup.log").open("w") as log:
                    subprocess.run(
                        [str(AVDMANAGER), "create", "avd", "-n", worker["avd"],
                         "-k", SYSTEM_IMAGE, "-d", "pixel_6"],
                        input="no\n", text=True, stdout=log, stderr=subprocess.STDOUT,
                        env=worker_env, check=True,
                    )
            emulator_log = (worker_dir / "emulator.log").open("w")
            logs.append(emulator_log)
            process = subprocess.Popen(
                [str(EMULATOR), "-avd", worker["avd"], "-port", str(worker["console_port"]),
                 "-grpc", str(worker["grpc_port"]), "-no-window", "-no-audio",
                 "-no-snapshot", "-no-boot-anim", "-no-metrics"],
                stdout=emulator_log, stderr=subprocess.STDOUT, env=worker_env,
            )
            emulators.append(process)
            print(f"Booting {worker['avd']} ({worker['serial']}, gRPC {worker['grpc_port']})", flush=True)
            wait_for_boot(worker, process)

            marker = avd_dir / ".android_world_apps_ready"
            if not args.skip_app_setup and not marker.exists():
                print(f"Installing AndroidWorld apps on {worker['avd']}", flush=True)
                setup_code = (
                    "from android_world.env import env_launcher; "
                    f"env=env_launcher.load_and_setup_env(console_port={worker['console_port']}, "
                    f"grpc_port={worker['grpc_port']}, adb_path={str(ADB)!r}, "
                    "emulator_setup=True); env.close()"
                )
                setup_log = worker_dir / "app_setup.log"
                with setup_log.open("w") as log:
                    subprocess.run(
                        [sys.executable, "-c", setup_code], cwd=ANDROID_WORLD_DIR,
                        stdout=log, stderr=subprocess.STDOUT, env=worker_env, check=True,
                    )
                warnings = APP_SETUP_WARNING.findall(setup_log.read_text(errors="replace"))
                marker.write_text(json.dumps({"setup_attempted": True, "warnings": warnings}, indent=2) + "\n")
            if not args.skip_app_setup and marker.exists():
                try:
                    warnings = json.loads(marker.read_text()).get("warnings", [])
                except (ValueError, AttributeError):
                    warnings = ["App setup status predates warning tracking; inspect the original setup log."]
                for warning in warnings:
                    print(f"{worker['avd']} app setup warning: {warning}", flush=True)

            worker["output_path"] = str(worker_dir / "runs")
            worker["tmpdir"] = str(temp_dir)
            worker["environment"] = worker_env

        (run_dir / "plan.json").write_text(
            json.dumps([{k: v for k, v in worker.items() if k != "environment"} for worker in plan], indent=2) + "\n"
        )
        for worker in plan:
            worker_dir = run_dir / f"worker_{worker['worker']:02d}"
            runner_log = (worker_dir / "runner.log").open("w")
            logs.append(runner_log)
            command = [
                sys.executable, str(ANDROID_WORLD_DIR / "run.py"),
                "--suite_family=android_world",
                f"--agent_name={args.agent_name}",
                f"--adb_path={ADB}",
                f"--console_port={worker['console_port']}",
                f"--grpc_port={worker['grpc_port']}",
                f"--tasks={','.join(worker['tasks'])}",
                f"--n_task_combinations={args.n_task_combinations}",
                f"--task_random_seed={worker['seed']}",
                f"--output_path={worker['output_path']}",
            ]
            process = subprocess.Popen(
                command, cwd=ANDROID_WORLD_DIR, env=worker["environment"],
                stdout=runner_log, stderr=subprocess.STDOUT,
            )
            runners.append(process)
            print(f"Worker {worker['worker']} running {len(worker['tasks'])} task(s): {worker_dir}", flush=True)

        exit_codes = [process.wait() for process in runners]
        print(f"Results: {run_dir}")
        print(f"Worker exit codes: {exit_codes}")
        return 0 if all(code == 0 for code in exit_codes) else 1
    finally:
        for process in runners:
            stop_process(process)
        for process in emulators:
            stop_process(process)
        for log in logs:
            log.close()
        for lock in locks:
            lock.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Parallel AndroidWorld setup failed: {exc}", file=sys.stderr)
        sys.exit(1)
