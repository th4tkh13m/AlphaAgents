"""Durable sequential task curriculum; each task owns a separate MCTS tree."""

import json
import threading
from contextlib import nullcontext
from pathlib import Path

from .artifacts import manifest
from .contracts import fingerprint
from ..shared.storage import run_lock, write_json


class TaskSequence:
    def __init__(self, output, source, tasks, identity, application_factory, monitor=None):
        if not tasks or len(tasks) != len(set(tasks)):
            raise ValueError("Freeze a nonempty, unique ordered failed-task list")
        self.directory, self.source = Path(output), Path(source)
        self.tasks, self.identity = list(tasks), identity
        self.application_factory = application_factory
        self.monitor = monitor
        self.stop, self.current = threading.Event(), None
        self.directory.mkdir(parents=True, exist_ok=True)

    def request_stop(self):
        self.stop.set()
        if self.current:
            self.current.request_stop()

    def close(self):
        if self.current:
            self.current.close()
            self.current = None

    def preflight(self):
        with run_lock(self.directory):
            step = self.directory / "tasks" / f"01-{self.tasks[0]}"
            try:
                self.current = self.application_factory(self.tasks[0], 0, self.source, step)
                return self.current.preflight()
            finally:
                self.close()

    def finalize(self):
        raise ValueError("Task sequences select winners automatically after each completed tree; use run/resume")

    def _save(self, state):
        write_json(self.directory / "sequence.json", state)
        lines = ["# Sequential single-task MCTS monitor", "",
                 f"State: **{state['state']}**. Frozen failed tasks: **{len(self.tasks)}**.", "",
                 "Four emulators; 3 plans × 3 implementation slots per node; depth 8; 30 rollouts per task.",
                 "Each tree evaluates only its named task. The selected exact harness becomes the next root.",
                 "Earlier-task retention and generalization are not measured by this experiment.", "",
                 "| Task | State | Tree directory | Winner |", "| --- | --- | --- | --- |"]
        for step in state["steps"]:
            lines.append(f"| {step['task']} | {step['state']} | `{step['directory']}` | `{step.get('winner', '')}` |")
        if state.get("error"):
            lines += ["", "Latest problem:", "", "```", state["error"], "```"]
        (self.directory / "MONITOR.md").write_text("\n".join(lines) + "\n")

    def run(self, *, resume=False, stop_after_trials=None, finalize=None):
        with self.monitor if self.monitor is not None else nullcontext():
            return self._run(resume=resume, stop_after_trials=stop_after_trials, finalize=finalize)

    def _run(self, *, resume=False, stop_after_trials=None, finalize=None):
        if stop_after_trials is not None or finalize is False:
            raise ValueError("Task sequences finish all configured rollouts before selecting and advancing")
        with run_lock(self.directory):
            identity = fingerprint({"configuration": self.identity, "tasks": self.tasks,
                                    "root_manifest": manifest(self.source)})
            path = self.directory / "sequence.json"
            if path.exists():
                if not resume:
                    raise ValueError("Existing task sequence requires --resume")
                state = json.loads(path.read_text())
                if state["identity"] != identity:
                    raise ValueError("Task sequence identity changed")
            else:
                state = {"identity": identity, "tasks": self.tasks, "state": "running", "steps": [
                    {"task": task, "state": "pending", "directory": f"tasks/{index + 1:02d}-{task}"}
                    for index, task in enumerate(self.tasks)]}
            self._save(state)
            source = self.source
            for index, step in enumerate(state["steps"]):
                directory = self.directory / step["directory"]
                if step["state"] == "completed":
                    source = directory / "winner"
                    if fingerprint(manifest(source)) != step["winner_manifest"]:
                        raise ValueError("Selected handoff artifact changed")
                    continue
                if self.stop.is_set():
                    break
                step["state"], state["state"] = "running", "running"
                state.pop("error", None)
                self._save(state)
                try:
                    self.current = self.application_factory(step["task"], index, source, directory)
                    if self.stop.is_set():
                        self.current.request_stop()
                        break
                    exists = (directory / "run_configuration.json").exists()
                    result = self.current.run(resume=exists, finalize=True)
                    final = result.get("final_selection")
                    if not final or final.get("selection_status") != "completed":
                        step["state"] = state["state"] = "recovery_pending"
                        state["error"] = json.dumps(result.get("errors", []))
                        self._save(state)
                        return state
                    winner = final["winner"]
                    source = directory / "winner"
                    step.update(state="completed", winner=winner["artifact_id"],
                                winner_manifest=fingerprint(manifest(source)),
                                selected_actions=final["selected_actions"],
                                completed_rollouts=final["completed_rollouts"])
                    self._save(state)
                except Exception as error:
                    step["state"] = state["state"] = "recovery_pending"
                    state["error"] = f"{type(error).__name__}: {error}"
                    self._save(state)
                    raise
                finally:
                    self.close()
            state["state"] = "completed" if all(step["state"] == "completed" for step in state["steps"]) else "stopped"
            if state["state"] == "completed":
                state["final_harness"] = str(source.resolve())
            self._save(state)
            return state
