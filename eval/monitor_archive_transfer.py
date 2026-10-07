"""Read-only persistent search monitor. Never interrupts or restarts a run."""

import datetime
import hashlib
import json
import sys
import time
from pathlib import Path


def read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def main(directory):
    directory = Path(directory)
    launch = read(directory / "launch.json")
    for _ in range(20):
        if "pid" in launch and "start_ticks" in launch:
            break
        time.sleep(0.25)
        launch = read(directory / "launch.json")
    else:
        raise RuntimeError("Missing controller identity in launch metadata")
    output = directory / "attempt_001"
    previous = None
    while True:
        proc = Path(f"/proc/{launch['pid']}/stat")
        try:
            stat = proc.read_text().split(") ", 1)[1].split()
            alive = stat[19] == launch["start_ticks"] and stat[0] != "Z"
        except OSError:
            alive = False
        state = read(output / "state.json")
        records = state.get("records", {})
        scored = [
            (records[k].get("evaluation", {}).get("score", 0), k)
            for k in state.get("archive", [])
            if records[k].get("evaluation", {}).get("status") == "completed"
        ]
        assessments = {
            p.parent.name: read(p)
            for p in output.glob("child_*/transfer_assessment.json")
        }
        integrity_alerts = []
        for catalog in output.glob("child_*/worktree/.dgm_archive/catalog.json"):
            snapshot = catalog.parents[2] / "archive_snapshot.json"
            if (
                snapshot.is_file()
                and hashlib.sha256(catalog.read_bytes()).digest()
                != hashlib.sha256(snapshot.read_bytes()).digest()
            ):
                integrity_alerts.append(str(catalog))
        summary = {
            "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "controller_alive": alive,
            "completed_children": sum(
                k != "initial" and r.get("status") == "completed"
                for k, r in records.items()
            ),
            "records": len(records),
            "settled_children": len(records) - int("initial" in records),
            "pending": state.get("pending"),
            "best": max(scored, key=lambda item: item[0], default=None),
            "final_evaluation": state.get("final_evaluation"),
            "helper_requests": {
                p.parent.parent.parent.name: len(list(p.glob("*.json")))
                for p in output.glob("child_*/worktree/.dgm_archive/requests")
            },
            "assessments": {
                k: {
                    f: v.get(f)
                    for f in ("status", "target_task", "regressions", "reasons")
                }
                for k, v in assessments.items()
            },
            "archive_integrity_alerts": integrity_alerts,
            "root_reuse": read(output / "initial/root_reuse.json").get("status"),
        }
        key = json.dumps(
            {k: v for k, v in summary.items() if k != "time"}, sort_keys=True
        )
        if key != previous or not alive:
            print(json.dumps(summary), flush=True)
            previous = key
        (directory / "monitor_status.json").write_text(
            json.dumps(summary, indent=2) + "\n"
        )
        if not alive:
            return
        time.sleep(30)


if __name__ == "__main__":
    main(sys.argv[1])
