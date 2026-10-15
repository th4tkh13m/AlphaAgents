"""Continuously export observed experiment events; never write experiment state.

Only Python's standard library is required. The Chrome trace JSON snapshots
can be opened directly in Perfetto. Each export is complete and atomic.
"""

import datetime as dt
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def atomic(path, payload):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def process_stats(record):
    try:
        fields = (
            Path(f"/proc/{record['pid']}/stat").read_text().rsplit(")", 1)[1].split()
        )
        if fields[0] == "Z" or fields[19] != str(record["process_birth"]):
            return None
        return {
            "pid": record["pid"],
            "birth": fields[19],
            "cpu_seconds": (int(fields[11]) + int(fields[12]))
            / os.sysconf("SC_CLK_TCK"),
            "rss_mib": int(fields[21]) * os.sysconf("SC_PAGE_SIZE") / 1048576,
        }
    except (OSError, KeyError, IndexError):
        return None


def sample_host(run, now, previous):
    devices = read_json(run / "devices/status.json", {}).get("devices", [])
    samples = {}
    for device in devices:
        stat = process_stats(device)
        if stat is None:
            continue
        before = previous.get(device["serial"])
        if before and before["birth"] == stat["birth"] and now > before["at"]:
            stat["cpu_percent_one_core"] = max(
                0,
                100
                * (stat["cpu_seconds"] - before["cpu_seconds"])
                / (now - before["at"]),
            )
        stat["at"] = now
        samples[device["serial"]] = stat
    host = {}
    try:
        memory = dict(
            line.split(":", 1)
            for line in Path("/proc/meminfo").read_text().splitlines()
        )
        host["mem_available_mib"] = int(memory["MemAvailable"].split()[0]) / 1024
        for kind in ("cpu", "io", "memory"):
            for line in Path(f"/proc/pressure/{kind}").read_text().splitlines():
                parts = line.split()
                if parts[0] == "some":
                    host[kind + "_pressure_avg10_pct"] = float(
                        dict(p.split("=", 1) for p in parts[1:])["avg10"]
                    )
    except (OSError, ValueError, KeyError):
        pass
    return {"at": now, "devices": samples, "host": host}


class Trace:
    def __init__(self, origin, now):
        self.origin, self.now = origin, now
        self.events = []
        self.tracks = set()

    def timestamp(self, at):
        return max(0, round((at - self.origin) * 1_000_000))

    def track(self, pid, tid, process, name):
        if (pid, tid) in self.tracks:
            return
        self.tracks.add((pid, tid))
        self.events.extend(
            [
                {
                    "ph": "M",
                    "pid": pid,
                    "tid": 0,
                    "name": "process_name",
                    "args": {"name": process},
                },
                {
                    "ph": "M",
                    "pid": pid,
                    "tid": tid,
                    "name": "thread_name",
                    "args": {"name": name},
                },
                {
                    "ph": "M",
                    "pid": pid,
                    "tid": tid,
                    "name": "thread_sort_index",
                    "args": {"sort_index": tid},
                },
            ]
        )

    def span(self, pid, tid, name, start, end, category, **args):
        if start is None:
            return
        incomplete = end is None or bool(args.pop("incomplete", False))
        a, b = (
            max(start, self.origin),
            min(end if end is not None else self.now, self.now),
        )
        if b <= a:
            return
        self.events.append(
            {
                "ph": "X",
                "pid": pid,
                "tid": tid,
                "name": name + (" [ongoing]" if incomplete else ""),
                "cat": category,
                "ts": self.timestamp(a),
                # Round endpoints consistently. Rounding each duration can put
                # a child one microsecond beyond its parent's identical end.
                "dur": self.timestamp(b) - self.timestamp(a),
                "args": {"incomplete": incomplete, **args},
            }
        )

    def instant(self, pid, tid, name, at, **args):
        self.events.append(
            {
                "ph": "I",
                "s": "t",
                "pid": pid,
                "tid": tid,
                "name": name,
                "ts": self.timestamp(at),
                "args": args,
            }
        )

    def counter(self, pid, tid, name, at, **values):
        self.events.append(
            {
                "ph": "C",
                "pid": pid,
                "tid": tid,
                "name": name,
                "ts": self.timestamp(at),
                "args": values,
            }
        )


def snapshot(run, now, samples, configured_devices=(), fallback_start=None):
    launcher = read_json(run / "launcher.json", {})
    stop = read_json(run / "observer_stop.json", {})
    now = min(now, stop.get("stopped_at", now))
    with closing(
        sqlite3.connect(
            (run / "tree.sqlite3").as_uri() + "?mode=ro", uri=True, timeout=2
        )
    ) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        tables = {
            t: [dict(x) for x in db.execute("SELECT * FROM " + t)]
            for t in (
                "task_dispatches",
                "invocations",
                "assessment_timings",
                "device_faults",
                "task_batches",
            )
        }
        tables["task_jobs"] = [
            dict(x) for x in db.execute("SELECT id,contract,state FROM task_jobs")
        ]
        tables["task_runs"] = [
            dict(x)
            for x in db.execute(
                "SELECT id,job,ordinal,device,state,output,started,finished,"
                "json_extract(payload,'$.success') AS success FROM task_runs"
            )
        ]
    recorded_starts = [
        r["started"]
        for table in ("invocations", "task_runs", "assessment_timings")
        for r in tables[table]
        if r["started"] is not None
    ]
    launched = launcher.get("launched_at_utc")
    search_start = (
        dt.datetime.fromisoformat(launched).timestamp()
        if launched
        else min(recorded_starts + [fallback_start or now])
    )
    manager = read_json(run / "devices/launcher.json", {})
    origin = (
        min(
            search_start,
            dt.datetime.fromisoformat(manager["started_at_utc"]).timestamp(),
        )
        if manager.get("started_at_utc")
        else search_start
    )
    jobs = {
        j["id"]: {**j, "contract": json.loads(j["contract"])}
        for j in tables["task_jobs"]
    }
    trace = Trace(origin, now)
    trace.track(
        80, 1, "Preparation (before search launch)", "Device preparation and checks"
    )
    trace.span(
        80,
        1,
        "Preparation: image copies / staggered boot / functional checks",
        origin,
        search_start,
        "preparation",
    )
    trace.instant(
        80,
        1,
        "Search observation begins",
        search_start,
        cached_root_tasks=launcher.get("cached_root_tasks", "not recorded"),
    )
    repairs = [
        read_json(p)
        for p in sorted((run / "device_recovery").glob("*/*/intervention.json"))
    ]
    repairs = [r for r in repairs if r]
    functional = read_json(run / "devices/functional_readiness.json", {})
    for i, check in enumerate(functional.get("results", []), 1):
        trace.track(
            81, i, "Functional readiness (not benchmark tasks)", check["device"]
        )
        trace.span(
            81,
            i,
            "Clipboard + accessibility checks",
            check["started"],
            check["finished"],
            "readiness",
            healthy=check["healthy"],
            recovered=check["recovered"],
        )
    devices = sorted(
        set(configured_devices)
        | {
            d["serial"]
            for d in read_json(run / "devices/status.json", {}).get("devices", [])
        }
        | {d["device"] for d in tables["task_dispatches"] if d["device"]}
        | {r["device"] for r in repairs}
    )
    transitions = {search_start, now}
    device_states = {}
    for index, device in enumerate(devices, 1):
        trace.track(100, index, "Devices: task assignment", device)
        trace.track(110, index, "Device recovery (includes startup)", device)
        task_spans = [
            d
            for d in tables["task_dispatches"]
            if d["device"] == device and d["started"] is not None
        ]
        maintenance = [r for r in repairs if r["device"] == device]
        quarantine = []
        for fault in [f for f in tables["device_faults"] if f["device"] == device]:
            ends = [
                r["finished"]
                for r in maintenance
                if r.get("healthy")
                and r.get("finished")
                and r["finished"] >= fault["recorded"]
            ]
            quarantine.append((fault["recorded"], min(ends) if ends else now))
        for repair in maintenance:
            trace.span(
                110,
                index,
                repair["mode"],
                repair["started"],
                repair.get("finished"),
                "recovery",
                healthy=repair.get("healthy"),
                completed=repair.get("state") == "completed",
            )
        edges = {search_start, now}
        for a, b in (
            [(d["started"], d["finished"] or now) for d in task_spans]
            + [(r["started"], r.get("finished") or now) for r in maintenance]
            + quarantine
        ):
            edges.update(
                [max(search_start, min(now, a)), max(search_start, min(now, b))]
            )
        spans = []
        for a, b in zip(sorted(edges), sorted(edges)[1:]):
            if b <= a:
                continue
            midpoint = (a + b) / 2
            active = next(
                (
                    d
                    for d in task_spans
                    if d["started"] <= midpoint < (d["finished"] or now)
                ),
                None,
            )
            recovery = next(
                (
                    r
                    for r in maintenance
                    if r["started"] <= midpoint < (r.get("finished") or now)
                ),
                None,
            )
            state = (
                "task"
                if active
                else "recovery"
                if recovery
                else "quarantine"
                if any(x <= midpoint < y for x, y in quarantine)
                else "idle"
            )
            args = {"state": state}
            name = state.capitalize()
            if active:
                contract = jobs[active["job"]]["contract"]
                name = f"{active['stage']}: {contract['template']}"
                args.update(
                    job_id=active["job"],
                    artifact=contract["artifact_id"],
                    dispatch=active["id"],
                    callback_state=active["state"],
                    sample_kind=contract["sample_kind"],
                    incomplete=active["finished"] is None and b == now,
                )
            trace.span(100, index, name, a, b, "device." + state, **args)
            spans.append((a, b, state))
        device_states[device] = spans
        transitions.update(edges)
    trace.track(500, 1, "Experiment counters", "Device occupancy")
    for at in sorted(transitions):
        query_at = at - 0.000001 if at == now else at
        counts = dict(task=0, recovery=0, quarantine=0, idle=0)
        for spans in device_states.values():
            state = next((s for a, b, s in spans if a <= query_at < b), "idle")
            counts[state] += 1
        trace.counter(500, 1, "Device occupancy", at, **counts)
    # Task phases are isolated from assignment tracks, preventing invalid partial overlaps.
    wall_offset = time.time() - time.monotonic()
    for task in tables["task_runs"]:
        tid = 1 + next(
            (i for i, d in enumerate(devices) if d == task["device"]), len(devices)
        )
        trace.track(120, tid, "Android task envelopes and subphases", task["device"])
        contract = jobs[task["job"]]["contract"]
        trace.span(
            120,
            tid,
            contract["template"],
            task["started"],
            task["finished"],
            "android.task",
            run_id=task["id"],
            state=task["state"],
            attempt=task["ordinal"],
            success=task["success"],
        )
        phases = read_json(Path(task["output"]) / "phases.json", {}).get("events", [])
        for i, phase in enumerate(phases):
            if phase["phase"] == "finished":
                continue
            end = (
                phases[i + 1]["entered"] + wall_offset
                if i + 1 < len(phases)
                else task["finished"]
            )
            a = max(task["started"], phase["entered"] + wall_offset)
            b = min(end or now, task["finished"] or now)
            trace.span(
                120,
                tid,
                phase["phase"],
                a,
                b,
                "android.phase",
                run_id=task["id"],
                clock_source="same-host monotonic phases mapped by current realtime-minus-monotonic anchor",
                incomplete=i + 1 == len(phases) and task["finished"] is None,
            )
    active_roles = 0
    for tid, call in enumerate(
        sorted(tables["invocations"], key=lambda c: (c["started"], c["id"])), 1
    ):
        directory = run / "role_requests" / call["id"]
        metrics_path = directory / "observed_metrics.json"
        metrics = read_json(metrics_path, {})
        process_path = directory / "process/process.json"
        process = read_json(process_path, {})
        request = read_json(directory / "request.json", {})
        end = metrics_path.stat().st_mtime if metrics else None
        if process and process_stats(process):
            active_roles += 1
        trace.track(
            200,
            tid,
            "Codex role lifecycle (includes admission/setup)",
            call["purpose"] + " / " + call["id"][-8:],
        )
        trace.span(
            200,
            tid,
            call["purpose"],
            call["started"],
            end,
            "codex.lifecycle",
            request_id=call["id"],
            owner=call["owner"],
            model=request.get("model"),
            effort=request.get("effort"),
            status=metrics.get("status", "pending"),
            measured_invocation_seconds=metrics.get("elapsed_seconds"),
            clock_source="reservation timestamp to metrics-publication file timestamp",
        )
        admission = read_json(directory / "device_admission.json", {})
        if admission:
            trace.track(210, tid, "Codex admission queues", call["id"][-8:])
            trace.span(
                210,
                tid,
                "Implementor admission queue",
                admission["queued_at"],
                admission.get("admitted_at"),
                "queue.implementor",
            )
        if process:
            native_end = (
                process_path.stat().st_mtime
                if process.get("status") != "running"
                else None
            )
            native_start = process.get("started_at")
            if (
                native_start is None
                and native_end is not None
                and process.get("elapsed_seconds") is not None
            ):
                native_start = native_end - process["elapsed_seconds"]
            trace.track(
                201,
                tid,
                "Native Codex worker envelopes",
                call["purpose"] + " / " + call["id"][-8:],
            )
            trace.span(
                201,
                tid,
                call["purpose"],
                native_start,
                native_end,
                "codex.native",
                request_id=call["id"],
                clock_source="active: recorded started_at; terminal: process metadata mtime minus measured elapsed (approximate endpoints)",
            )
    for tid, d in enumerate(tables["task_dispatches"], 1):
        trace.track(
            220, tid, "Device scheduler queues", d["stage"] + " / " + d["id"][-8:]
        )
        trace.span(
            220,
            tid,
            "Waiting for device: " + d["stage"],
            d["queued"],
            d["started"] or d["finished"],
            "queue.device",
            dispatch=d["id"],
            job_id=d["job"],
            measured_queue_seconds=d["queue_delay"],
        )
    for index, a in enumerate(tables["assessment_timings"], 1):
        events = json.loads(a["events"])
        for phase, start_key, end_key in [
            ("Code review", "code_start", "code_finish"),
            ("Android evaluation/cache", "android_start", "android_finish"),
            ("Trajectory review", "execution_start", "execution_finish"),
        ]:
            if start_key not in events:
                continue
            tid = (
                index * 10
                + {
                    "Code review": 1,
                    "Android evaluation/cache": 2,
                    "Trajectory review": 3,
                }[phase]
            )
            trace.track(
                300,
                tid,
                "Assessments (overlapping phases on separate tracks)",
                a["owner"] + " / " + phase,
            )
            trace.span(
                300,
                tid,
                phase,
                a["started"] + events[start_key],
                a["started"] + events[end_key] if end_key in events else a["finished"],
                "assessment",
                artifact=a["artifact"],
                baseline=a["owner"] == "baseline",
                assessment_state=a["state"],
            )
    for index, batch in enumerate(tables["task_batches"], 1):
        ids = set(json.loads(batch["jobs"]))
        dispatches = [
            d
            for d in tables["task_dispatches"]
            if d["job"] in ids and d["owner"] == batch["owner"]
        ]
        if not dispatches:
            continue
        complete = all(jobs[j]["state"] == "completed" for j in ids) and all(
            d["finished"] is not None for d in dispatches
        )
        trace.track(
            310,
            index,
            "Evaluation stage dispatch envelopes",
            batch["artifact"][:12] + " / " + batch["stage"],
        )
        trace.span(
            310,
            index,
            batch["stage"],
            min(d["queued"] for d in dispatches),
            max(d["finished"] for d in dispatches) if complete else None,
            "evaluation.stage",
            artifact=batch["artifact"],
            required_tasks=len(ids),
            completed_tasks=sum(jobs[j]["state"] == "completed" for j in ids),
        )
    for sample in samples:
        at = sample["at"]
        if at > now:
            continue
        for tid, device in enumerate(devices, 1):
            values = sample["devices"].get(device, {})
            trace.track(600, tid, "Sampled emulator host resources", device)
            if "cpu_percent_one_core" in values:
                trace.counter(
                    600,
                    tid,
                    device + ": CPU percent of one host core",
                    at,
                    value=values["cpu_percent_one_core"],
                )
            if "rss_mib" in values:
                trace.counter(
                    600, tid, device + ": RSS MiB", at, value=values["rss_mib"]
                )
        trace.track(610, 1, "Sampled host resources", "Memory and pressure")
        for key, value in sample["host"].items():
            trace.counter(610, 1, key, at, value=value)
    trace.counter(500, 1, "Live native Codex workers", now, value=active_roles)
    metadata = {
        "run": run.name,
        "observed_at_utc": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
        "origin_unix_seconds": origin,
        "search_start_unix_seconds": search_start,
        "scope": "Logical wall-time events and sampled host resources; not kernel/GPU call-stack profiling",
        "incomplete_slices": "Clipped at observation and labeled ongoing; not completed runtime samples",
        "clock_notes": "Microsecond offsets from origin; task monotonic phases use a same-host current clock anchor. Terminal native worker endpoints reconstructed from metadata mtime are approximate.",
        "sampled_resources": "CPU/RSS/pressure only from watcher start; historical CPU usage is not fabricated",
    }
    # Longer equal-start slices must precede children for the JSON importer.
    trace.events.sort(
        key=lambda e: (e.get("ts", 0), 0 if e["ph"] == "M" else 1, -e.get("dur", 0))
    )
    return {"traceEvents": trace.events, "displayTimeUnit": "ms", "otherData": metadata}
