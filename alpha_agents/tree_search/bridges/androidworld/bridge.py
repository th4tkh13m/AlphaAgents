"""AndroidWorld task stages, exclusive devices, and mobile evidence policy."""

from __future__ import annotations

import queue
import sys
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from ...core.contracts import Candidate, Evaluation, MutationContext
from .task_sets import STAGES, load_sets, stage_tasks


class AndroidWorldBridge:
    def __init__(self, source: Path, config: dict):
        self.source = source.resolve()
        defaults = {
            "python": sys.executable,
            "seed": 42,
            "task_file": str(Path(__file__).with_name("tasks.json")),
            "evaluation_runner": "mobile_agent_http",
            "score_stage": "selection",
            "max_steps": 50,
            "task_timeout": 3600,
            "stage_timeout": 3600,
            "evaluation_task_attempts": 3,
            "evaluation_recovery_interval": 15,
            "androidworld_devices": [],
        }
        self.config = {**defaults, **config, "harness_dir": str(self.source)}
        if sys.platform == "win32":
            raise RuntimeError("AndroidWorld bridge requires Linux/WSL")
        required = ["base_url", "model"]
        if self.config["evaluation_runner"] == "artemis_local":
            required += ["androidworld_root", "adb_path"]
        else:
            required += ["androidworld_api_url"]
        for key in required:
            if not self.config.get(key):
                raise ValueError(f"AndroidWorld requires explicit {key}")
        self.config["task_file"] = str(Path(self.config["task_file"]).resolve())
        if not self.source.is_dir() or not Path(self.config["task_file"]).is_file():
            raise ValueError("AndroidWorld source and task file must exist")
        if "score_stage" not in config and "selection" not in load_sets(
            self.config["task_file"]
        ):
            self.config["score_stage"] = "evaluation"
        if self.config["score_stage"] not in STAGES:
            raise ValueError("Unknown AndroidWorld score stage")
        stage_tasks(self.config["task_file"], self.config["score_stage"])
        if self.config["score_stage"] == "confirmation" and "seed" not in config:
            raise ValueError("Confirmation requires an explicit fresh evaluation seed")
        devices = self.config["androidworld_devices"]
        if isinstance(devices, str):
            devices = [value.strip() for value in devices.split(",") if value.strip()]
        if len(devices) != len(set(devices)):
            raise ValueError("Device IDs must be unique")
        self.config["androidworld_devices"] = devices
        if self.config["evaluation_runner"] == "artemis_local":
            from .devices import local_device_ports

            self.config["androidworld_device_ports"] = local_device_ports(
                devices, self.config
            )
            for key in ("androidworld_root", "adb_path"):
                self.config[key] = str(Path(self.config[key]).resolve())
            from .artemis import preserve_source

            self.source = preserve_source(self.source)
        self.pool = queue.Queue()
        for device in devices or [None]:
            self.pool.put(device)

    def identity(self):
        return {
            "type": "androidworld",
            "config": self.config,
            "task_sets": load_sets(self.config["task_file"]),
        }

    @contextmanager
    def lease(self):
        device = self.pool.get()
        config = deepcopy(self.config)
        if device:
            config.update(
                androidworld_assigned_device=device, androidworld_devices=[device]
            )
            if config["evaluation_runner"] == "artemis_local":
                ports = config["androidworld_device_ports"][device]
                config.update(ports)
                config["androidworld_device_ports"] = {device: ports}
        try:
            yield config
        finally:
            self.pool.put(device)

    def prepare(self, candidate: Candidate, parent: Candidate | None, resource):
        from . import runtime

        if parent is not None and resource["score_stage"] == "confirmation":
            raise ValueError("Confirmation is held out; use Selection for DGM mutation")
        runtime.expose_agent_venv(
            candidate.workspace, resource.get("venv_source", str(self.source / ".venv"))
        )
        if resource["evaluation_runner"] == "artemis_local":
            from .artemis import expose_preserved_modules, mutation_context

            expose_preserved_modules(candidate)
            return mutation_context(candidate, parent, resource)
        return MutationContext(
            "Investigate parent evidence and improve the mobile harness's general reliability.",
            "AndroidWorld guidance:\n"
            "The bridge evaluates benchmark success after mutation.\n"
            "Do not add task-specific app rules or fixed coordinate sequences. "
            "Keep improvements general across tasks.\n"
            "Candidate-owned mobile-agent role logic, prompts, workflows, tools, "
            "action parsing, recovery, and state handling may be improved.\n\n"
            + "Assigned device: "
            + str(resource.get("androidworld_assigned_device"))
            + ". Use this assigned resource for the representative harness test "
            "run during mutation. No prescribed experiment-summary file is required. "
            "The bridge performs independent benchmark evaluation afterward.",
            {
                "parent_id": parent.id if parent else None,
                "parent_evidence": ".dgm_parent_evidence",
                "resource": resource.get("androidworld_assigned_device"),
            },
            tuple(resource.get("protected_paths", [])),
        )

    def evaluate(self, candidate: Candidate, resource):
        from . import runtime

        # Every scored candidate uses the same task split. Scores from a small
        # screening subset must not compete with full-suite scores.
        stage = resource["score_stage"]
        summary = runtime.run_stage(
            candidate.workspace,
            candidate.directory / "stages" / stage,
            stage,
            resource,
            candidate.directory.parent,
        )
        value = runtime.performance(summary, stage)
        evidence = (str(candidate.directory / "stages"),)
        metrics = value
        if resource["evaluation_runner"] == "artemis_local":
            metrics = {
                **value,
                "task_results": summary.get("task_results", []),
                "errors": summary.get("errors", []),
                "model": resource["model"],
            }
        if (
            value.get("evaluation_status") != "completed"
            or value.get("total_submitted_instances", 0) <= 0
        ):
            return Evaluation(
                "failed",
                metrics=metrics,
                evidence=evidence,
                error="AndroidWorld evaluation did not complete",
                diagnostic=True,
            )
        return Evaluation(
            "completed",
            value["accuracy_score"],
            metrics,
            evidence,
        )
