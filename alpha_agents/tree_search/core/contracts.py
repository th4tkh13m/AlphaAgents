"""The boundary between search policy, mutation, and harness execution.

Bridges own task semantics and resource lifecycles. Scores must be finite,
normalized to [0, 1], and higher is better; failed evaluations have no score.
"""

from __future__ import annotations

import math
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class Candidate:
    id: str
    parent_id: str | None
    directory: Path
    base_commit: str

    @property
    def workspace(self) -> Path:
        return self.directory / "worktree"

    @property
    def patch(self) -> Path:
        return self.directory / "model_patch.diff"

    @property
    def mutation_context(self) -> Path:
        """Canonical context serialized by the controller before mutation."""
        return self.directory / "mutation_context.json"


@dataclass(frozen=True)
class Evaluation:
    status: Literal["completed", "failed"]
    score: float | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()
    error: str | None = None
    diagnostic: bool = False

    def __post_init__(self):
        if self.status not in {"completed", "failed"}:
            raise ValueError(f"Unknown evaluation status: {self.status}")
        if self.status == "completed":
            if (
                self.score is None
                or isinstance(self.score, bool)
                or not math.isfinite(self.score)
                or not 0 <= self.score <= 1
            ):
                raise ValueError(
                    "Completed evaluations require a finite score in [0, 1]"
                )
        elif self.score is not None:
            raise ValueError("Failed evaluations cannot carry a score")


@dataclass(frozen=True)
class MutationContext:
    objective: str
    instructions: str
    evidence: dict[str, Any] = field(default_factory=dict)
    protected_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class MutationOutcome:
    status: Literal["completed", "failed"]
    error: str | None = None

    def __post_init__(self):
        if self.status not in {"completed", "failed"}:
            raise ValueError(f"Unknown mutation status: {self.status}")


class HarnessBridge(Protocol):
    """Implementations must support concurrent calls under distinct leases.

    Evaluation must use a trusted evaluator outside the editable candidate.
    prepare() may attach ignored context or dependencies; it must not edit the
    candidate's tracked source. Evaluation errors are returned or raised.
    """

    @property
    def source(self) -> Path: ...

    def identity(self) -> dict[str, Any]:
        """JSON-serializable evaluator configuration, used to validate resume."""
        ...

    def lease(self) -> AbstractContextManager[Any]: ...

    def prepare(
        self, candidate: Candidate, parent: Candidate | None, resource: Any
    ) -> MutationContext: ...

    def evaluate(self, candidate: Candidate, resource: Any) -> Evaluation: ...


class MutationBackend(Protocol):
    """Edit candidate.workspace using the supplied context.

    The controller also serializes context to candidate.mutation_context for
    process-based backends. Backends own editing telemetry; the controller
    owns context serialization and patch capture, including partial edits.
    """

    def identity(self) -> dict[str, Any]: ...

    def mutate(
        self, candidate: Candidate, context: MutationContext
    ) -> MutationOutcome: ...


@dataclass(frozen=True)
class ValidationReport:
    """Engineering evidence; never substitutes for a benchmark score."""

    name: str
    status: Literal["completed", "failed", "skipped"]
    details: dict[str, Any] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()
    error: str | None = None

    def __post_init__(self):
        if self.status not in {"completed", "failed", "skipped"}:
            raise ValueError(f"Unknown validation status: {self.status}")


class CandidateValidator(Protocol):
    """Optional engineering checks, independent of mutation providers."""

    def identity(self) -> dict[str, Any]: ...

    def validate(
        self, candidate: Candidate, context: MutationContext
    ) -> ValidationReport: ...
