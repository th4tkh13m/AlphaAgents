"""AndroidWorld task stages, exclusive devices, and mobile evidence policy."""

from __future__ import annotations

import queue
import sys
from contextlib import contextmanager
from pathlib import Path

from ...core.contracts import Candidate, Evaluation, MutationContext


class AndroidWorldBridge:
    def __init__(self, source: Path, config: dict):
        self.source = source.resolve()
        defaults = {
            "python": sys.executable,
            "seed": 42,
            "task_file": str(Path(__file__).with_name("tasks.json")),
            "evaluation_runner": "mobile_agent_http",
            "score_stage": "evaluation",
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
        for key in ("androidworld_api_url", "base_url", "model"):
            if not self.config.get(key):
                raise ValueError(f"AndroidWorld requires explicit {key}")
        self.config["task_file"] = str(Path(self.config["task_file"]).resolve())
        if not self.source.is_dir() or not Path(self.config["task_file"]).is_file():
            raise ValueError("AndroidWorld source and task file must exist")
        if self.config["score_stage"] not in {"screen", "selection", "evaluation"}:
            raise ValueError("Unknown AndroidWorld score stage")
        devices = self.config["androidworld_devices"]
        if isinstance(devices, str):
            devices = [value.strip() for value in devices.split(",") if value.strip()]
        if len(devices) != len(set(devices)):
            raise ValueError("Device IDs must be unique")
        self.config["androidworld_devices"] = devices
        self.pool = queue.Queue()
        for device in devices or [None]:
            self.pool.put(device)

    def identity(self):
        return {"type": "androidworld", "config": self.config}

    @contextmanager
    def lease(self):
        device = self.pool.get()
        config = dict(self.config)
        if device:
            config.update(
                androidworld_assigned_device=device, androidworld_devices=[device]
            )
        try:
            yield config
        finally:
            self.pool.put(device)

    def prepare(self, candidate: Candidate, parent: Candidate | None, resource):
        from . import runtime

        runtime.expose_agent_venv(
            candidate.workspace, resource.get("venv_source", str(self.source / ".venv"))
        )
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
