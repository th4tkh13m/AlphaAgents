"""AndroidWorld task stages, exclusive devices, and mobile evidence policy."""

from __future__ import annotations

import queue
import sys
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import shutil

from ...core.contracts import Candidate, Evaluation, MutationContext, RequiredEvaluationError
from ...core.storage import write_json
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
            "evaluation_workers": 1,
            "full_benchmark": False,
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
        workers = self.config["evaluation_workers"]
        if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= len(devices or [None]):
            raise ValueError("Evaluation workers must fit the available devices")
        if (workers > 1 or self.config["full_benchmark"]) and self.config["evaluation_runner"] != "artemis_local":
            raise ValueError("Parallel and full benchmark audits require the local Artemis runner")
        if self.config["full_benchmark"]:
            if self.config["score_stage"] != "selection":
                raise ValueError("Full audits rank candidates only on Selection")
            if not self.config.get("benchmark_cache_dir"):
                raise ValueError("Full audits require an explicit persistent benchmark_cache_dir")
            self.config["benchmark_cache_dir"] = str(Path(self.config["benchmark_cache_dir"]).resolve())
            if Path(self.config["benchmark_cache_dir"]).is_relative_to(self.source):
                raise ValueError("Benchmark cache must be outside the original harness")
            confirmation_seed = self.config.get("confirmation_seed")
            if isinstance(confirmation_seed, bool) or not isinstance(confirmation_seed, int) or confirmation_seed == self.config["seed"]:
                raise ValueError("Full audits require an explicit fresh confirmation_seed")
            for stage in ("screen", "selection", "confirmation"):
                stage_tasks(self.config["task_file"], stage)
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

    @contextmanager
    def evaluation_resources(self, resource):
        """Borrow idle devices without blocking while already holding a lease."""
        configs = [resource]
        borrowed = []
        try:
            for _ in range(self.config["evaluation_workers"] - 1):
                try:
                    device = self.pool.get_nowait()
                except queue.Empty:
                    break
                borrowed.append(device)
                config = deepcopy(self.config)
                config.update(androidworld_assigned_device=device, androidworld_devices=[device])
                config.update(config["androidworld_device_ports"][device])
                configs.append(config)
            yield configs
        finally:
            for device in borrowed:
                self.pool.put(device)

    def evaluate(self, candidate: Candidate, resource):
        from . import runtime

        # Every scored candidate uses the same task split. Scores from a small
        # screening subset must not compete with full-suite scores.
        stage = resource["score_stage"]
        output = candidate.directory / "stages" / stage
        with self.evaluation_resources(resource) as configs:
            if (
                candidate.parent_id is not None
                and stage == "selection"
                and "screen" in load_sets(resource["task_file"])
            ):
                screen_output = candidate.directory / "stages" / "screen"
                if len(configs) > 1:
                    from .benchmark import run_parallel_stage

                    screen = run_parallel_stage(
                        candidate.workspace, screen_output, "screen", configs,
                        candidate.directory.parent,
                    )
                else:
                    screen = runtime.run_stage(
                        candidate.workspace, screen_output, "screen", resource,
                        candidate.directory.parent,
                    )
                screen_value = runtime.performance(screen, "screen")
                if screen.get("status") != "completed":
                    return Evaluation(
                        "failed", metrics=screen_value,
                        evidence=(str(screen_output),),
                        error="AndroidWorld Screen evaluation did not complete",
                        diagnostic=True,
                    )
                if (
                    screen_value["total_submitted_instances"] <= 0
                    or screen_value["accuracy_score"] != 1.0
                ):
                    return Evaluation(
                        "failed",
                        metrics={
                            **screen_value,
                            "screen_gate": "failed",
                            "task_results": screen.get("task_results", []),
                            "errors": screen.get("errors", []),
                        },
                        evidence=(str(screen_output),),
                        error="Candidate did not pass all Screen tasks; Selection was not run",
                        diagnostic=True,
                    )
            if resource["full_benchmark"] and candidate.parent_id is None:
                from .benchmark import cache_identity, full_benchmark

                summary, report, cache = full_benchmark(
                    candidate, configs, resource["benchmark_cache_dir"],
                    cache_identity(candidate.workspace, resource),
                    report_path=candidate.directory.parent / "root_full_benchmark.json",
                )
                # Only Selection evidence enters the parent directory. The full
                # report and Confirmation trajectories stay in the private cache.
                shutil.copytree(cache / "selection", output, dirs_exist_ok=True)
                write_json(candidate.directory.parent / "root_full_benchmark.json", report)
            elif len(configs) > 1:
                from .benchmark import run_parallel_stage

                summary = run_parallel_stage(candidate.workspace, output, stage, configs, candidate.directory.parent)
            else:
                summary = runtime.run_stage(candidate.workspace, output, stage, resource, candidate.directory.parent)
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

    def finalize(self, root, records, archive):
        """Audit the Selection winner only after child generation has finished."""
        if not self.config["full_benchmark"]:
            return None
        if not archive:
            raise RequiredEvaluationError("No valid Selection candidate exists for final full benchmark")
        best = max(archive, key=lambda id: records[id]["evaluation"]["score"])
        record = records[best]
        candidate = Candidate(best, record["parent_id"], root / best, record["base_commit"])
        from .benchmark import cache_identity, full_benchmark

        with self.lease() as resource:
            with self.evaluation_resources(resource) as configs:
                _, report, _ = full_benchmark(candidate, configs, resource["benchmark_cache_dir"], cache_identity(candidate.workspace, resource), report_path=root / "final_full_benchmark.json")
        write_json(root / "final_full_benchmark.json", report)
        return report
