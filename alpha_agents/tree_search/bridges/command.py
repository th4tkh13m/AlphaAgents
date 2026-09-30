"""Bridge for any trusted evaluator implementing the JSON result contract."""

from __future__ import annotations

import json
import queue
from contextlib import contextmanager
from pathlib import Path

from ..core.contracts import Candidate, Evaluation, MutationContext
from ..infrastructure.commands import expand_command
from ..infrastructure.process import execute


class CommandBridge:
    def __init__(
        self,
        source: Path,
        evaluate_command: list[str],
        *,
        instructions: str = "",
        resources: tuple[str, ...] = ("local",),
        timeout: float = 300,
        evaluator_cwd: Path | None = None,
        protected_paths: tuple[str, ...] = (),
        evaluator_version: str = "",
    ):
        self.source = source.resolve()
        self.command = list(evaluate_command)
        self.evaluator_cwd = (evaluator_cwd or self.source.parent).resolve()
        self.instructions = instructions
        self.protected_paths = tuple(protected_paths)
        self.evaluator_version = evaluator_version
        self.timeout = timeout
        if (
            not self.source.is_dir()
            or not self.command
            or not resources
            or timeout <= 0
        ):
            raise ValueError(
                "Command bridge requires a source directory, evaluator, resources, and positive timeout"
            )
        if len(resources) != len(set(resources)):
            raise ValueError("Resource IDs must be unique")
        self.resources = tuple(resources)
        self.pool = queue.Queue()
        for resource in resources:
            self.pool.put(resource)

    def identity(self):
        return {
            "type": "command",
            "source": str(self.source),
            "evaluate_command": self.command,
            "evaluator_cwd": str(self.evaluator_cwd),
            "instructions": self.instructions,
            "resources": list(self.resources),
            "timeout": self.timeout,
            "protected_paths": list(self.protected_paths),
            "evaluator_version": self.evaluator_version,
        }

    @contextmanager
    def lease(self):
        resource = self.pool.get()
        try:
            yield resource
        finally:
            self.pool.put(resource)

    def prepare(self, candidate: Candidate, parent: Candidate | None, resource):
        return MutationContext(
            "Improve this harness using the parent evaluation evidence. Preserve the evaluator's meaning.",
            self.instructions + "\nOnly use assigned resource: " + str(resource),
            {
                "parent_id": parent.id if parent else None,
                "parent_evidence": ".dgm_parent_evidence",
                "evaluator_command": self.command,
            },
            self.protected_paths,
        )

    def evaluate(self, candidate: Candidate, resource):
        artifacts = candidate.directory / "evaluation"
        values = {
            "workspace": str(candidate.workspace),
            "artifacts": str(artifacts),
            "resource": str(resource),
        }
        command = expand_command(self.command, values)
        report, stdout = execute(command, self.evaluator_cwd, artifacts, self.timeout)
        evidence = (str(artifacts),)
        if report["timed_out"] or report["returncode"]:
            return Evaluation(
                "failed",
                evidence=evidence,
                error="Evaluator timed out"
                if report["timed_out"]
                else "Evaluator exited unsuccessfully",
            )
        try:
            # Evaluator writes exactly one JSON object to stdout; logs go to stderr.
            result = json.loads(stdout)
            evaluation = Evaluation(
                result["status"],
                result.get("score"),
                result.get("metrics", {}),
                evidence,
                result.get("error"),
                result.get("diagnostic", False),
            )
        except (ValueError, TypeError, KeyError) as error:
            return Evaluation(
                "failed", evidence=evidence, error=f"Invalid evaluator result: {error}"
            )
        return evaluation
