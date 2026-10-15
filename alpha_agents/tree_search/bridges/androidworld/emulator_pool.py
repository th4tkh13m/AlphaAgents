"""Launch fresh AVD copies with one supervised, private netsim per emulator.

This operational launcher preserves the SDK's Wi-Fi backend. It does not run
benchmark tasks, restart a failed device, or change an existing experiment.
Run ``python -m alpha_agents.tree_search.bridges.androidworld.emulator_pool --help``.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path


def birth(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def uses_avd(source: Path, argv: list[str], avd_home: str | None) -> bool:
    if any(str(source) in argument for argument in argv):
        return True
    if avd_home and "-avd" in argv:
        index = argv.index("-avd") + 1
        if index < len(argv):
            return (
                Path(avd_home) / (argv[index] + ".avd")
            ).resolve() == source.resolve()
    return False


def layout(count: int, console: int, control: int, netsim: int, hci: int) -> list[dict]:
    if not 1 <= count <= 9:
        raise ValueError("device count must be between one and nine")
    if console % 2:
        raise ValueError("emulator console base must be even")
    rows = [
        dict(
            avd=f"AlphaPrivate{i:02}",
            serial=f"emulator-{console + 2 * i}",
            console_port=console + 2 * i,
            grpc_port=control + 2 * i,
            netsim_port=netsim + i,
            hci_port=hci + i,
            netsim_instance=101 + i,
        )
        for i in range(count)
    ]
    ports = [
        p
        for r in rows
        for p in (
            r["console_port"],
            r["console_port"] + 1,
            r["grpc_port"],
            r["netsim_port"],
            r["hci_port"],
        )
    ]
    if any(not 1024 <= p <= 65535 for p in ports) or len(set(ports)) != len(ports):
        raise ValueError("all assigned ports must be distinct and within 1024..65535")
    return rows


def check_ports(rows: list[dict]) -> None:
    # Reserve every selected port together during preflight. Popen startup still
    # checks process health because releasing these sockets leaves a bind race.
    sockets = []
    try:
        for r in rows:
            for port in (
                r["console_port"],
                r["console_port"] + 1,
                r["grpc_port"],
                r["netsim_port"],
                r["hci_port"],
            ):
                sock = socket.socket()
                sockets.append(sock)
                # Recently stopped ADB connections can remain in TIME_WAIT.
                # Match server reuse behavior while still rejecting listeners.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind(("127.0.0.1", port))
                except OSError as exc:
                    raise OSError(
                        exc.errno, f"Port {port} unavailable: {exc.strerror}"
                    ) from exc
    finally:
        for sock in sockets:
            sock.close()


def device_env(base: dict, sdk: Path, avds: Path, directory: Path, row: dict) -> dict:
    env = base.copy()
    # These inherited values can redirect instances or add unrelated flags.
    env.pop("NETSIM_ARGS", None)
    env.pop("NETSIM_HCI_PORT", None)
    env.update(
        ANDROID_HOME=str(sdk),
        ANDROID_SDK_ROOT=str(sdk),
        ANDROID_AVD_HOME=str(avds),
        TMPDIR=str(directory / "tmp"),
        NETSIM_INSTANCE=str(row["netsim_instance"]),
        NETSIM_GRPC_PORT=str(row["netsim_port"]),
    )
    return env


def commands(sdk: Path, row: dict) -> tuple[list[str], list[str]]:
    daemon = [
        str(sdk / "emulator/netsimd"),
        "--instance",
        str(row["netsim_instance"]),
        "--hci-port",
        str(row["hci_port"]),
        "--no-web-ui",
        "--logtostderr",
    ]
    emulator = [
        str(sdk / "emulator/emulator"),
        "-avd",
        row["avd"],
        "-port",
        str(row["console_port"]),
        "-grpc",
        str(row["grpc_port"]),
        "-no-snapshot",
        "-no-window",
        "-no-audio",
        "-no-metrics",
        "-no-boot-anim",
        "-cores",
        "2",
        "-memory",
        "2048",
        "-packet-streamer-endpoint",
        f"127.0.0.1:{row['netsim_port']}",
    ]
    return daemon, emulator


def stop_owned(process: subprocess.Popen, timeout: float = 10) -> None:
    # Retain Popen ownership: never search by process name or signal a recorded
    # PID from a previous invocation. poll()/wait() also reap terminated children.
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


class PrivatePool:
    def __init__(
        self,
        sdk: Path,
        adb: Path,
        source: Path,
        output: Path,
        rows: list[dict],
        boot_timeout: float = 480,
    ):
        self.sdk, self.adb, self.source, self.output = sdk, adb, source, output
        self.rows, self.boot_timeout = rows, boot_timeout
        self.stop = threading.Event()
        self.processes: list[subprocess.Popen] = []
        self.pairs: list[tuple[dict, subprocess.Popen, subprocess.Popen]] = []
        self.state = "preparing"
        self.owns_output = False

    def save(self, **extra) -> None:
        data = dict(
            observed_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            status=self.state,
            supervisor_pid=os.getpid(),
            supervisor_birth=birth(os.getpid()),
            source_avd=str(self.source),
            sdk=str(self.sdk),
            adb=str(self.adb),
            wifi_backend="sdk_default",
            netsim_binary_sha256=hashlib.sha256(
                (self.sdk / "emulator/netsimd").read_bytes()
            ).hexdigest(),
            devices=self.rows,
            **extra,
        )
        tmp = self.output / "status.json.tmp"
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(self.output / "status.json")

    def prepare(self) -> None:
        check_ports(self.rows)
        if not (self.source / "config.ini").is_file():
            raise ValueError("source must be a stopped, provisioned .avd directory")
        for p in Path("/proc").iterdir():
            if not p.name.isdigit():
                continue
            try:
                argv = (p / "cmdline").read_bytes().decode(errors="replace").split("\0")
                if not argv or "qemu-system" not in argv[0]:
                    continue
                environment = (
                    (p / "environ").read_bytes().decode(errors="replace").split("\0")
                )
                avd_home = next(
                    (
                        v.partition("=")[2]
                        for v in environment
                        if v.startswith("ANDROID_AVD_HOME=")
                    ),
                    None,
                )
                if uses_avd(self.source, argv, avd_home):
                    raise ValueError("source AVD is in use by a live emulator")
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                pass
        for binary in (
            self.adb,
            self.sdk / "emulator/netsimd",
            self.sdk / "emulator/emulator",
        ):
            if not os.access(binary, os.X_OK):
                raise ValueError(f"missing executable: {binary}")
        self.output.mkdir(parents=True, exist_ok=True)
        # A fresh output directory is mandatory; old manifests/AVDs are evidence.
        (self.output / "pool.lock").touch(exist_ok=False)
        self.owns_output = True
        self.save()
        avds = self.output / "avd"
        avds.mkdir()
        for row in self.rows:
            if self.stop.is_set():
                raise InterruptedError("pool preparation interrupted")
            target = avds / (row["avd"] + ".avd")
            target.mkdir()
            subprocess.run(
                [
                    "cp",
                    "-a",
                    "--reflink=auto",
                    "--sparse=always",
                    str(self.source) + "/.",
                    str(target),
                ],
                check=True,
            )
            for p in [
                *target.rglob("*.lock"),
                target / "hardware-qemu.ini",
                target / "emu-launch-params.txt",
            ]:
                if p.is_dir():
                    shutil.rmtree(p)
                elif p.exists():
                    p.unlink()
            (avds / (row["avd"] + ".ini")).write_text(
                f"avd.ini.encoding=UTF-8\npath={target}\ntarget=android-33\n"
            )
            directory = self.output / row["serial"]
            (directory / "tmp").mkdir(parents=True)
            row.update(directory=str(directory), prepared=True, ready=False)
            self.save()

    def start_process(self, cmd: list[str], env: dict, log: Path) -> subprocess.Popen:
        with log.open("wb") as stream:
            process = subprocess.Popen(
                cmd,
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.processes.append(process)
        return process

    def adb_shell(self, row: dict, *args: str) -> str:
        result = subprocess.run(
            [str(self.adb), "-s", row["serial"], "shell", *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode:
            raise RuntimeError(result.stderr[-1000:])
        return result.stdout.strip()

    def start_device(self, row: dict) -> None:
        import grpc  # Only the operational launcher needs the Android runtime dependency.

        directory = Path(row["directory"])
        env = device_env(os.environ, self.sdk, self.output / "avd", directory, row)
        daemon_cmd, emulator_cmd = commands(self.sdk, row)
        daemon = self.start_process(daemon_cmd, env, directory / "netsim.log")
        row.update(
            netsim_pid=daemon.pid,
            netsim_birth=birth(daemon.pid),
            netsim_command=daemon_cmd,
            command=emulator_cmd,
            tmpdir=env["TMPDIR"],
            status="starting_netsim",
        )
        self.save()
        channel = grpc.insecure_channel(f"127.0.0.1:{row['netsim_port']}")
        future = grpc.channel_ready_future(channel)
        try:
            deadline = time.monotonic() + 30
            while True:
                if daemon.poll() is not None or self.stop.is_set():
                    raise RuntimeError(
                        "private netsim exited or launch was interrupted"
                    )
                try:
                    future.result(timeout=1)
                    break
                except grpc.FutureTimeoutError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("private netsim gRPC readiness deadline")
        finally:
            future.cancel()
            channel.close()
        emulator = self.start_process(emulator_cmd, env, directory / "emulator.log")
        row.update(
            pid=emulator.pid, process_birth=birth(emulator.pid), status="booting"
        )
        self.pairs.append((row, daemon, emulator))
        self.save()
        deadline = time.monotonic() + self.boot_timeout
        while time.monotonic() < deadline:
            if (
                self.stop.is_set()
                or daemon.poll() is not None
                or emulator.poll() is not None
            ):
                raise RuntimeError(
                    "device pair exited or launch was interrupted during boot"
                )
            try:
                boot = self.adb_shell(row, "getprop", "sys.boot_completed")
                if boot == "1":
                    with socket.create_connection(
                        ("127.0.0.1", row["grpc_port"]), timeout=2
                    ):
                        pass
                    packages = sorted(
                        self.adb_shell(row, "pm", "list", "packages").splitlines()
                    )
                    if len(packages) < 100:
                        raise RuntimeError("provisioned app inventory is missing")
                    row.update(
                        ready=True,
                        status="ready",
                        package_count=len(packages),
                        package_inventory_sha256=hashlib.sha256(
                            "\n".join(packages).encode()
                        ).hexdigest(),
                    )
                    self.save()
                    print(
                        f"Ready {row['serial']} -> private netsim {row['netsim_port']}",
                        flush=True,
                    )
                    return
            except (subprocess.TimeoutExpired, OSError, RuntimeError):
                pass
            self.stop.wait(2)
        raise TimeoutError(f"{row['serial']} boot readiness deadline")

    def check_pairs(self) -> None:
        for row, daemon, emulator in self.pairs:
            if row["status"] == "failed":
                continue
            if daemon.poll() is not None or emulator.poll() is not None:
                row.update(
                    ready=False,
                    status="failed",
                    netsim_exit=daemon.poll(),
                    emulator_exit=emulator.poll(),
                )
                # A failed daemon must not leave its emulator apparently healthy.
                # Other pairs keep running; no hidden restart changes task state.
                stop_owned(emulator)
                stop_owned(daemon)
                self.state = "degraded"
                self.save()

    def run(self, ready_seconds: float | None = None) -> None:
        try:
            self.prepare()
            self.state = "starting"
            for row in self.rows:
                self.check_pairs()
                self.start_device(row)
            if len({r["package_inventory_sha256"] for r in self.rows}) != 1:
                raise RuntimeError("device app inventories differ")
            self.check_pairs()
            if not all(r["ready"] for r in self.rows):
                raise RuntimeError("a device pair failed before pool readiness")
            self.state = "ready"
            self.save()
            deadline = (
                None if ready_seconds is None else time.monotonic() + ready_seconds
            )
            while not self.stop.wait(1):
                self.check_pairs()
                if not any(r["ready"] for r in self.rows):
                    raise RuntimeError("all device pairs failed")
                if deadline is not None and time.monotonic() >= deadline:
                    break
        except Exception as exc:
            self.state = "failed"
            if self.owns_output:
                self.save(error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            for process in reversed(self.processes):
                stop_owned(process)
            if self.owns_output:
                for row in self.rows:
                    row["ready"] = False
                failed = self.state == "failed"
                self.state = "failed" if failed else "stopped"
                self.save()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--adb", type=Path, required=True)
    parser.add_argument("--source-avd", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=6)
    parser.add_argument("--console-base", type=int, default=5580)
    parser.add_argument("--control-base", type=int, default=8580)
    parser.add_argument("--netsim-base", type=int, default=19001)
    parser.add_argument("--hci-base", type=int, default=19101)
    parser.add_argument("--boot-timeout", type=float, default=480)
    parser.add_argument(
        "--ready-seconds",
        type=float,
        help="stop after this many ready seconds; omit for persistent supervision",
    )
    args = parser.parse_args()
    rows = layout(
        args.count,
        args.console_base,
        args.control_base,
        args.netsim_base,
        args.hci_base,
    )
    pool = PrivatePool(
        args.sdk.resolve(),
        args.adb.resolve(),
        args.source_avd.resolve(),
        args.output.resolve(),
        rows,
        args.boot_timeout,
    )
    signal.signal(signal.SIGTERM, lambda *_: pool.stop.set())
    signal.signal(signal.SIGINT, lambda *_: pool.stop.set())
    pool.run(args.ready_seconds)


if __name__ == "__main__":
    main()
