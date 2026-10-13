"""Confirm or terminate owned subprocesses without confusing reused PIDs."""

import json
import os
import signal
import time
from pathlib import Path

from alpha_agents.tree_search.core.shared.storage import write_json
from alpha_agents.tree_search.infrastructure.process import process_birth


def _tagged(launch_id):
    if not launch_id:
        return []
    token = b"ALPHA_PROCESS_LAUNCH_ID=" + launch_id.encode()
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            status = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if status[0] == "Z":
                continue
            if token in (entry / "environ").read_bytes().split(b"\0"):
                found.append(int(entry.name))
        except (OSError, IndexError):
            continue
    return found


def _legacy_alive(pid, birth):
    if process_birth(pid) != birth:
        return False
    try:
        state = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except (OSError, IndexError):
        return False


def reconcile_process(path: Path, *, terminate=True):
    if not path.exists():
        # No launch checkpoint means this adapter has not spawned a process.
        return True
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    tagged = _tagged(record.get("launch_id"))
    pid = record.get("pid")
    legacy = False
    if not tagged and pid and process_birth(pid) == record.get("process_birth") and record.get("status") in {"running", "launching"}:
        # Older records must contain a verified birth time; a numeric PID alone
        # is never authority to terminate a process group.
        if not record.get("process_birth"):
            return False
        if not _legacy_alive(pid, record["process_birth"]):
            return True
        try:
            # Older checkpoints identify a session leader, not arbitrary members
            # of a potentially unrelated process group.
            if os.getpgid(pid) != pid:
                return False
        except ProcessLookupError:
            return True
        tagged = [pid]
        legacy = True
    if not tagged:
        return True
    if not terminate:
        return False
    for owned in tagged:
        try:
            group = os.getpgid(owned)
            if group == os.getpgrp():
                return False
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        alive = _legacy_alive(pid, record["process_birth"]) if legacy else bool(_tagged(record.get("launch_id")))
        if not alive:
            write_json(path.with_name("recovery.json"), {"status": "owned_processes_terminated", "launch_id": record.get("launch_id"),
                                                       "pids": tagged, "reclaimed_at": time.time()})
            return True
        time.sleep(.05)
    return False
