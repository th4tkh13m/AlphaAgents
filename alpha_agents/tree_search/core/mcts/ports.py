"""Contracts between the fixed search protocol and external integrations.

Adapters own their runtime details. The default base classes are useful for
uncached, in-process integrations; durable evaluators must override verification
and reconciliation instead of accepting these no-op defaults.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .contracts import fingerprint
from .role_contracts import RoleRequest


@dataclass(frozen=True)
class RoleSettings:
    model: str
    effort: str

    def __post_init__(self):
        if not self.model or not self.effort:
            raise ValueError("Explicit model and reasoning effort are required")


class DiagnosticService(Protocol):
    """Invocation-scoped diagnostics, serviced only during implementation."""

    root: Path

    @property
    def channel(self) -> dict[str, Any]: ...
    def handle(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def validate_handoff(self, result: dict[str, Any]) -> None: ...


class RoleGateway(Protocol):
    telemetry_name: str
    supports_checks: bool

    def preflight(self) -> dict[str, Any]: ...
    def enrich_context(self, role: str, context: dict[str, Any]) -> dict[str, Any]: ...
    def prepare_request(self, run_id: str, request_id: str, role: str, artifact_id: str,
                        model: str, effort: str, context: dict[str, Any]) -> RoleRequest: ...
    def observed_usage(self, directory: Path) -> dict[str, Any]: ...
    def invoke(self, request: RoleRequest, workspace: Path) -> dict[str, Any]: ...
    def invoke_with_checks(self, request: RoleRequest, workspace: Path,
                           service: DiagnosticService) -> dict[str, Any]: ...


class RoleAdapter(ABC):
    """Explicit defaults for integrations without preflight or diagnostics."""

    telemetry_name = "role_execution"
    supports_checks = False

    def preflight(self) -> dict[str, Any]:
        return {}

    def enrich_context(self, role: str, context: dict[str, Any]) -> dict[str, Any]:
        context = dict(context)
        if role == "value":
            context["continuation_role_protocol"] = {
                **context["continuation_role_protocol"],
                "proposer_mode": "in_process", "implementer_mode": "in_process",
                "implementation_verification": {"prepared_environment_configured": False},
            }
        return context

    def observed_usage(self, directory: Path) -> dict[str, Any]:
        return {}

    @abstractmethod
    def prepare_request(self, run_id: str, request_id: str, role: str, artifact_id: str,
                        model: str, effort: str, context: dict[str, Any]) -> RoleRequest:
        raise NotImplementedError

    @abstractmethod
    def invoke(self, request: RoleRequest, workspace: Path) -> dict[str, Any]:
        raise NotImplementedError

    def invoke_with_checks(self, request: RoleRequest, workspace: Path,
                           service: DiagnosticService) -> dict[str, Any]:
        raise NotImplementedError("This role adapter does not support implementation diagnostics")


@dataclass(frozen=True)
class StageEvidence:
    artifact_id: str
    stage: str
    outcomes: Mapping[str, bool]
    evidence: tuple[str, ...]
    samples: Mapping[str, tuple[bool, ...]] | None = None

    def __post_init__(self):
        if not self.artifact_id or self.stage not in {"screen", "s0", "selection", "targeted"}:
            raise ValueError("Exact artifact and permitted evaluation stage required")
        if any(not isinstance(outcome, bool) for outcome in self.outcomes.values()):
            raise ValueError("Stage evidence contains normalized binary successes only")
        if self.samples is not None:
            if set(self.samples) != set(self.outcomes):
                raise ValueError("Repetition samples must cover exactly the stage tasks")
            for task, samples in self.samples.items():
                if not samples or any(not isinstance(value, bool) for value in samples):
                    raise ValueError("Repetition outcomes must be nonempty binary samples")
                if self.outcomes[task] != all(samples):
                    raise ValueError("Stage success requires all repetitions to pass")
        if not self.evidence:
            raise ValueError("A completed stage requires trusted evidence references")

    @property
    def rates(self):
        return {task: sum(values) / len(values) for task, values in self.samples.items()} if self.samples is not None else dict(self.outcomes)

    def validate_repetitions(self, count):
        if count == 1 and self.samples is None:
            return
        if self.samples is None or any(
            not (len(values) == count or (0 < len(values) < count and values[-1] is False and all(values[:-1])))
            for values in self.samples.values()
        ):
            raise ValueError("Stage lacks the required fresh repetition outcomes")
        if len(self.evidence) != sum(len(values) for values in self.samples.values()) or len(set(self.evidence)) != len(self.evidence):
            raise ValueError("Repetitions require distinct trusted execution evidence roots")

    def validate_tasks(self, tasks):
        if set(self.outcomes) != set(tasks) or len(tasks) != len(set(tasks)):
            raise ValueError("Exactly one valid outcome per requested stage task is required")

    @property
    def identity(self) -> str:
        return fingerprint(asdict(self))


class StagedEvaluator(Protocol):
    telemetry_name: str
    supports_checks: bool

    def implementation_session(self, owner: str): ...
    def scheduling_status(self) -> dict[str, Any]: ...

    def preflight(self, source: Path) -> dict[str, Any]: ...
    def for_owner(self, owner: str) -> StagedEvaluator: ...
    def prepare(self, artifact: str, stages: Mapping[str, tuple[str, ...]]) -> None: ...
    def evaluate(self, artifact: str, workspace: Path, stage: str,
                 tasks: tuple[str, ...]) -> StageEvidence: ...
    def verify_stage(self, evidence: StageEvidence) -> None: ...
    def reconcile_interrupted(self, process_dead: Callable[[Path], bool]) -> None: ...


class EvaluationAdapter(ABC):
    """Defaults for an uncached evaluator with no external background work.

Implementations must support concurrent calls or return owner-specific sessions.
Evidence roots follow the requested task order; shared bundles may have fewer
references than tasks. Persistent integrations must verify reused evidence and
reconcile their own processes and task state.
"""

    telemetry_name = "evaluation"
    supports_checks = False

    def scheduling_status(self) -> dict[str, Any]:
        return {}

    @contextmanager
    def implementation_session(self, owner: str):
        """Adapters without physical devices require no testing admission."""
        yield self

    def preflight(self, source: Path) -> dict[str, Any]:
        return {}

    def for_owner(self, owner: str) -> StagedEvaluator:
        return self

    def prepare(self, artifact: str, stages: Mapping[str, tuple[str, ...]]) -> None:
        pass

    @abstractmethod
    def evaluate(self, artifact: str, workspace: Path, stage: str,
                 tasks: tuple[str, ...]) -> StageEvidence:
        raise NotImplementedError

    def verify_stage(self, evidence: StageEvidence) -> None:
        pass

    def reconcile_interrupted(self, process_dead: Callable[[Path], bool]) -> None:
        pass
