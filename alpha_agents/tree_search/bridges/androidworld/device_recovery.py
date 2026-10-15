"""Device-local transport policy and functional infrastructure recovery probe.

This entry point never runs an agent, a task validator, or publishes rewards.
Its parent owns the device lock and supervises the entire subprocess deadline.
"""

import argparse
import json
import re
import socket
import subprocess
import sys
import time
import urllib.request  # noqa: F401 -- AndroidEnv expects urllib.request to be loaded.
import xml.etree.ElementTree as ET
from pathlib import Path


class DeviceTransportFailure(RuntimeError):
    """A failed device command must not restart the shared ADB daemon."""


FORWARDER_PACKAGE = "com.google.androidenv.accessibilityforwarder"
CLIPPER_ACTIVITY = "ca.zgrs.clipper/ca.zgrs.clipper.Main"


def focused_window(adb):
    output = adb("shell", "dumpsys", "activity", "activities", redact_stdout=True)
    match = re.search(r"mCurrentFocus=(.*)", output)
    return match.group(1).strip() if match else "unknown"


def dismiss_systemui_anr(adb, focus):
    """Only press Wait on the identified infrastructure SystemUI ANR dialog.

    This runs under the exclusive device lock, outside benchmark episodes.
    Other dialogs and application ANRs must not be dismissed by this probe.
    """
    if "Application Not Responding: com.android.systemui}" not in focus:
        return False
    path = "/data/local/tmp/alpha-readiness-ui.xml"
    try:
        adb("shell", "uiautomator", "dump", path, timeout=20)
        tree = ET.fromstring(adb("shell", "cat", path, redact_stdout=True))
    finally:
        adb("shell", "rm", "-f", path)
    buttons = [
        node
        for node in tree.iter("node")
        if node.get("package") == "android"
        and node.get("resource-id") == "android:id/aerr_wait"
    ]
    if len(buttons) != 1:
        raise RuntimeError("SystemUI ANR Wait control was not identified")
    # Recheck focus after collecting UI evidence, before sending any input.
    if focused_window(adb) != focus:
        raise RuntimeError("Focus changed while inspecting SystemUI ANR")
    bounds = re.fullmatch(
        r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", buttons[0].get("bounds", "")
    )
    if not bounds:
        raise RuntimeError("SystemUI ANR Wait control has invalid bounds")
    x0, y0, x1, y1 = map(int, bounds.groups())
    if x1 <= x0 or y1 <= y0:
        raise RuntimeError("SystemUI ANR Wait control has empty bounds")
    adb("shell", "input", "tap", str((x0 + x1) // 2), str((y0 + y1) // 2))
    return True


def focus_clipper(adb):
    """Bounded warm-up, allowing a slow cold start without accepting wrong focus."""
    for attempt in range(2):
        dismiss_systemui_anr(adb, focused_window(adb))
        try:
            adb("shell", "am", "start", "-W", "-n", CLIPPER_ACTIVITY, timeout=30)
        except subprocess.TimeoutExpired:
            if attempt == 1:
                raise
            continue
        for _ in range(6):
            focus = focused_window(adb)
            if CLIPPER_ACTIVITY in focus:
                return
            if dismiss_systemui_anr(adb, focus):
                break
            time.sleep(0.5)
    raise RuntimeError("Clipper did not acquire input focus")


def check_clipboard_readable(adb):
    """Check receiver responsiveness without writing benchmark-visible text.

    Restoring the clipboard after a write does not restore keyboard history.
    In particular, never insert a sentinel here. An empty successful read is
    valid. Clipper also returns result=0 with explicit empty data for a null
    clipboard; accept that only with verified input focus and read app-op.
    Actual writes remain the responsibility of task fixture initialization.
    """
    states = []
    for _ in range(3):
        focus_clipper(adb)
        result = adb(
            "shell",
            "am",
            "broadcast",
            "-a",
            "clipper.get",
            redact_stdout=True,
        )
        if "ALPHA_DEVICE_HEALTH_" in result:
            raise RuntimeError(
                "Legacy health-probe clipboard contamination; clean clipboard "
                "and keyboard history before reusing this device"
            )
        if CLIPPER_ACTIVITY not in focused_window(adb):
            raise RuntimeError("Clipper lost input focus during clipboard read")
        if re.search(r'Broadcast completed: result=-1, data="', result):
            states.append("text_read")
        elif re.search(r'Broadcast completed: result=0, data=""\s*$', result):
            permission = adb(
                "shell", "cmd", "appops", "get", "ca.zgrs.clipper", "READ_CLIPBOARD"
            )
            if not re.search(r"READ_CLIPBOARD: allow(?:;|\s|$)", permission):
                raise RuntimeError(
                    "Null clipboard response without allowed read app-op"
                )
            states.append("no_text_with_focus_and_read_permission")
        else:
            raise RuntimeError("Clipboard read probe failed")
    return states


def quiesce_forwarder(wrapper):
    """Stop forwarding before its host server closes; retain other services."""
    if getattr(wrapper, "_alpha_forwarder_quiesced", False):
        return
    command = wrapper._env._coordinator._adb_call_parser._adb_controller.execute_command
    key = "enabled_accessibility_services"
    current = (
        command(["shell", "settings", "get", "secure", key], timeout=5).decode().strip()
    )
    enabled = [s for s in current.split(":") if s and s != "null"]
    remaining = [s for s in enabled if s.split("/", 1)[0] != FORWARDER_PACKAGE]
    if remaining != enabled:
        if remaining:
            command(
                ["shell", "settings", "put", "secure", key, ":".join(remaining)],
                timeout=5,
            )
        else:
            command(["shell", "settings", "delete", "secure", key], timeout=5)
    command(["shell", "am", "force-stop", FORWARDER_PACKAGE], timeout=5)
    wrapper._alpha_forwarder_quiesced = True


def install_device_local_adb_policy(controller_type=None, wrapper_type=None):
    if controller_type is None:
        from android_env.components.adb_controller import AdbController
        from android_env.wrappers.a11y_grpc_wrapper import A11yGrpcWrapper

        controller_type = AdbController
        wrapper_type = A11yGrpcWrapper

    def reject_global_restart(self, timeout=None):
        raise DeviceTransportFailure(
            "Device ADB command failed; shared-server restart suppressed. "
            "Recovery belongs to the exclusive device scheduler."
        )

    controller_type._restart_server = reject_global_restart
    if wrapper_type is not None and not getattr(
        wrapper_type, "_alpha_close_policy", False
    ):
        original_close = wrapper_type.close

        def close(self):
            try:
                quiesce_forwarder(self)
            finally:
                original_close(self)

        wrapper_type.close = close
        wrapper_type._alpha_close_policy = True


def device_failure(summary):
    """Known trusted infrastructure exceptions; ordinary agent misses are excluded."""
    for episode in (summary or {}).get("task_results", []):
        error = episode.get("exception", "") or ""
        if (
            error.startswith(
                (
                    "DeviceOfflineError:",
                    "AdbControllerError:",
                    "DeviceTransportFailure:",
                )
            )
            or error == "RuntimeError: Could not get a11y tree."
        ):
            return error
    return None


def probe(args):
    socket.setdefaulttimeout(15)
    install_device_local_adb_policy()
    record = {
        "device": args.device,
        "mode": args.mode,
        "started": time.time(),
        "healthy": False,
        "checks": [],
        "clipboard_probe": "read-only; fixture initialization validates writes",
    }
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    def save():
        temporary = output / "health.json.tmp"
        temporary.write_text(json.dumps(record, indent=2) + "\n")
        temporary.replace(output / "health.json")

    def adb(*parts, timeout=10, redact_stdout=False):
        began = time.monotonic()
        result = subprocess.run(
            [args.adb, "-s", args.device, *parts],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        record["checks"].append(
            {
                "command": list(parts),
                "seconds": time.monotonic() - began,
                "returncode": result.returncode,
                "stdout": (
                    "[command output omitted]"
                    if redact_stdout
                    else result.stdout[:2000]
                ),
            }
        )
        save()
        result.check_returncode()
        return result.stdout

    controller = None
    try:
        if args.mode == "reboot":
            adb("reboot")
            time.sleep(3)
            deadline = time.monotonic() + 100
            while time.monotonic() < deadline:
                try:
                    if adb("shell", "getprop", "sys.boot_completed").strip() == "1":
                        break
                except (subprocess.SubprocessError, OSError):
                    pass
                time.sleep(2)
            else:
                raise TimeoutError("Device boot deadline exceeded")
            time.sleep(3)
        elif args.mode == "services":
            adb(
                "shell",
                "settings",
                "delete",
                "secure",
                "enabled_accessibility_services",
            )
            adb(
                "shell",
                "am",
                "force-stop",
                "com.google.androidenv.accessibilityforwarder",
            )
            adb("shell", "am", "force-stop", "ca.zgrs.clipper")
        record["clipboard_read_states"] = check_clipboard_readable(adb)
        sys.path.insert(0, args.androidworld)
        from android_world.env import android_world_controller

        controller = android_world_controller.get_controller(
            args.console_port, args.adb, args.grpc_port
        )
        for i in range(3):
            began = time.monotonic()
            forest = controller.get_a11y_forest()
            if forest.ByteSize() <= 0:
                raise RuntimeError("Accessibility probe returned an empty forest")
            record["checks"].append(
                {
                    "check": "accessibility_forest",
                    "index": i,
                    "bytes": forest.ByteSize(),
                    "seconds": time.monotonic() - began,
                }
            )
            save()
            time.sleep(0.5)
        adb("shell", "input", "keyevent", "3")
        record["healthy"] = True
    except Exception as error:
        record["error"] = f"{type(error).__name__}: {error}"
    finally:
        # A hanging close is still contained by the parent's subprocess deadline.
        if controller is not None:
            controller.close()
        record["finished"] = time.time()
        save()
    return record["healthy"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--adb", required=True)
    parser.add_argument("--androidworld", required=True)
    parser.add_argument("--console-port", required=True, type=int)
    parser.add_argument("--grpc-port", required=True, type=int)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--mode", choices=["probe", "services", "reboot"], required=True
    )
    raise SystemExit(0 if probe(parser.parse_args()) else 1)
