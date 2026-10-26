"""Continuously publish a human-readable sequence and active-tree report."""

import datetime
import json
import logging
import threading
from pathlib import Path

from .sqlite_snapshot import snapshot_connection

LOG = logging.getLogger(__name__)


class SequenceMonitor:
    def __init__(self, directory, interval=15):
        self.directory, self.interval = Path(directory), interval
        self.stop = threading.Event()
        self.thread = None
        self.previous = None

    def update(self):
        path = self.directory / "sequence.json"
        if not path.exists():
            return
        state = json.loads(path.read_text())
        limits = state.get("search_configuration", {})
        rollout_limit = limits.get("max_trials", "configured")
        depth_limit = limits.get("max_depth", "configured")
        plans = limits.get("max_plans", "configured")
        implementations = limits.get("max_implementations", "configured")
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        lines = ["# Live single-task experiment monitor", "", f"Updated: `{now}` (UTC).", "",
                 f"Sequence state: **{state['state']}**.", "",
                 f"Per-task limits: {rollout_limit} rollouts, depth ≤ {depth_limit}, {plans} plans × {implementations} implementation slots per plan. Qualified success may stop earlier.",
                 "Only the current task contributes Android utility. Unified engineering review follows canonical task evaluation.",
                 "Elapsed review, device execution, queues, and model calls may overlap; do not sum their durations.", "",
                 "| Task | State | Tree |", "| --- | --- | --- |"]
        for step in state["steps"]:
            status = step["state"] + (" (root passed; search skipped)" if step.get("search_skipped") else "")
            lines.append(f"| {step['task']} | {status} | [{step['directory']}]({step['directory']}) |")
        if state.get("root_success_policy"):
            lines += ["", "Fresh qualified passing roots bypass MCTS and carry the exact root forward. Failed roots receive bounded search; skips do not invent rollout completions."]
        errors = []
        for step in state["steps"]:
            if step["state"] not in {"running", "recovery_pending"}:
                continue
            tree = self.directory / step["directory"]
            database = tree / "tree.sqlite3"
            if not database.exists():
                continue
            with snapshot_connection(database) as connection:
                frozen = connection.execute("SELECT value FROM meta WHERE key='identity'").fetchone()
                active_limit = json.loads(frozen[0])["config"]["max_trials"] if frozen else rollout_limit
                def counts(table):
                    return dict(connection.execute(f"SELECT state,count(*) FROM {table} GROUP BY state"))
                trials, attempts = counts("trials"), counts("attempts")
                backups = connection.execute("SELECT count(*) FROM backups").fetchone()[0]
                depth = connection.execute("SELECT max(depth) FROM nodes").fetchone()[0]
                roles = dict(connection.execute("SELECT purpose,count(*) FROM invocations GROUP BY purpose"))
                lines += ["", f"## Active task: {step['task']}", "",
                          f"Trials: `{json.dumps(trials)}`. Backups: **{backups}/{active_limit}**. Maximum stored depth: **{depth}**.",
                          f"Implementations: `{json.dumps(attempts)}`.", f"Role invocations: `{json.dumps(roles)}`.", "",
                          "| Artifact | Depth | Observed U | Judge J | Combined F |", "| --- | ---: | ---: | ---: | ---: |"]
                for row in connection.execute("SELECT artifact,depth,score FROM nodes WHERE eligible=1 ORDER BY depth,id"):
                    score = json.loads(row["score"])
                    weight = score["objective"]["judge_weight"]
                    combined = (1 - weight) * score["u"] + weight * score["j"]
                    lines.append(f"| `{row['artifact'][:12]}` | {row['depth']} | {score['u']:.3f} | {score['j']:.4f} | {combined:.4f} |")
                for row in connection.execute("SELECT device,job,reason FROM device_faults ORDER BY recorded,id"):
                    errors.append(dict(row))
                lines += ["", f"Task execution attempts: `{json.dumps(counts('task_runs'))}`.", "",
                          "Measured assessment wall time, minutes per complete stage (overlapping phases):", "",
                          "| Cohort | Phase | Samples | Mean | Range |", "| --- | --- | ---: | ---: | --- |"]
                durations = {}
                for row in connection.execute("SELECT owner,state,events FROM assessment_timings"):
                    events = json.loads(row["events"])
                    cohort = "baseline" if row["owner"] == "baseline" else "child"
                    for name, start, finish in [("single-task S0", "android_start", "android_finish"),
                                                ("code review", "code_start", "code_finish"),
                                                ("trajectory judge", "execution_start", "execution_finish"),
                                                ("engineering review", "review_start", "review_finish"),
                                                ("after-S0 continuation latency", "s0_ready", "score_ready")]:
                        if start in events and finish in events and row["state"] == "completed":
                            durations.setdefault((cohort, name), []).append((events[finish]-events[start])/60)
                for (cohort, name), values in sorted(durations.items()):
                    lines.append(f"| {cohort} | {name} | {len(values)} | {sum(values)/len(values):.2f} | {min(values):.2f}–{max(values):.2f} |")
                if not durations:
                    lines.append("| — | No completed assessment stages | 0 | not measured | not measured |")
                queue = connection.execute("SELECT count(*),sum(queue_delay),sum(callback_seconds) FROM task_dispatches WHERE state='completed'").fetchone()
                lines += ["", "Screen and full Selection are not run in this mode. Baseline must finish before child search.",
                          "Stage elapsed time includes evaluation queue waits, permitted retries, and cleanup; do not add them again.",
                          f"Completed task dispatches: {queue[0]}; summed queue time: {(queue[1] or 0)/60:.2f} minutes; "
                          f"summed device callback execution: {(queue[2] or 0)/60:.2f} device-minutes.",
                          "Summed device time is not elapsed wall time; concurrent devices can overlap."]
            status_path = tree / "status.json"
            if status_path.exists():
                status = json.loads(status_path.read_text())
                lines += ["", "Device pool (latest coordinator observation):", "", "```json",
                          json.dumps(status.get("device_pool", {}), indent=2), "```"]
            phases = {}
            for metrics in (tree / "role_requests").glob("*/observed_metrics.json"):
                data = json.loads(metrics.read_text())
                phases.setdefault(data["role"], []).append(data["elapsed_seconds"] / 60)
            lines += ["", "Finished role-call wall time, minutes per invocation (including failed calls):", "",
                      "| Role | Samples | Mean | Range |", "| --- | ---: | ---: | --- |"]
            for role, values in sorted(phases.items()):
                lines.append(f"| {role} | {len(values)} | {sum(values)/len(values):.2f} | {min(values):.2f}–{max(values):.2f} |")
            failures = []
            for receipt in (tree / "role_requests").glob("*/failure.json"):
                failures.append({"request": receipt.parent.name, **json.loads(receipt.read_text())})
            errors += failures
            lines += ["", f"Perfetto: [{step['directory']}/profiling/live_trace.json]({step['directory']}/profiling/live_trace.json)."]
        if state.get("error"):
            errors.append({"sequence": state["error"]})
        lines += ["", "## Problems", "", "```json", json.dumps(errors, indent=2), "```"]
        temporary = self.directory / "LIVE_MONITOR.md.tmp"
        temporary.write_text("\n".join(lines) + "\n")
        temporary.replace(self.directory / "LIVE_MONITOR.md")
        signature = json.dumps(errors, sort_keys=True)
        if errors and signature != self.previous:
            with (self.directory / "INCIDENTS.md").open("a") as stream:
                stream.write(f"\n## {now}\n\n```json\n{json.dumps(errors, indent=2)}\n```\n")
        self.previous = signature

    def _watch(self):
        while not self.stop.wait(self.interval):
            try:
                self.update()
            except Exception:
                LOG.exception("Sequence monitoring sample failed; search continues")

    def __enter__(self):
        self.stop.clear()
        self.thread = threading.Thread(target=self._watch, name="sequence-monitor", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join()
        try:
            self.update()
        except Exception:
            LOG.exception("Final sequence monitoring sample failed")
