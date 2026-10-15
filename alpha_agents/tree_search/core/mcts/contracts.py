"""Validated, serializable identities and score contracts for fixed PUCT."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

PREDICTION_VERSION = "fixed-puct-discounted-continuing-v4"
BACKUP_VERSION = "discounted-component-backup-v2"
ARTIFACT_RUBRIC_VERSION = "artifact-quality-rubric-v2"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{name} must be a finite number")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def unit(value: float, name: str) -> float:
    value = finite(value, name)
    if not 0 <= value <= 1:
        raise ValueError(f"{name} must be in [0, 1]")
    return value


@dataclass(frozen=True)
class FixedPUCTConfig:
    # Screen policy is intentionally a required experimental choice.
    screen_gate: str
    max_depth: int = 8
    discount_factor: float = 0.9
    max_trials: int = 30
    max_new_attempts_per_trial: int = 3
    max_attempts: int = 90
    max_plans: int = 3
    max_implementations: int = 3
    trial_workers: int = 2
    device_limit: int = 6
    implementor_limit: int = 9
    evaluation_task: str | None = None
    # Historical storage names; execution uses the generic accessors below.
    codex_invocation_cap: int = 2000
    codex_run_timeout_seconds: int = 7200
    shortlist_limit: int = 3
    c_puct: float = 1.0
    c_impl: float = 1.0
    prior_floor: float = 0.01

    @classmethod
    def from_mapping(cls, settings: Mapping[str, Any]) -> FixedPUCTConfig:
        """Accept generic names while freezing the original serialized schema."""
        settings = dict(settings)
        for generic, historical in (("role_invocation_cap", "codex_invocation_cap"),
                                    ("role_timeout_seconds", "codex_run_timeout_seconds")):
            if generic in settings:
                value = settings.pop(generic)
                if historical in settings and settings[historical] != value:
                    raise ValueError(f"Conflicting {generic} and {historical}")
                settings[historical] = value
        return cls(**settings)

    def __post_init__(self):
        if self.evaluation_task is not None and (not isinstance(self.evaluation_task, str) or not self.evaluation_task.strip()):
            raise ValueError("Single-task search requires a nonempty task name")
        if self.screen_gate not in {"strict_all_success", "execution_valid"}:
            raise ValueError("Explicit screen_gate must be strict_all_success or execution_valid")
        if self.max_plans != 3 or self.max_implementations != 3:
            raise ValueError("MCTS requires exactly three plans and three implementation slots per plan")
        for name in (
            "max_depth", "max_trials", "max_new_attempts_per_trial", "max_attempts",
            "max_plans", "max_implementations", "trial_workers", "device_limit",
            "codex_invocation_cap", "shortlist_limit", "implementor_limit",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        ceilings = {
            "max_depth": 8, "max_trials": 30, "max_new_attempts_per_trial": 8 if self.evaluation_task else 3,
            "max_attempts": 240 if self.evaluation_task else 90, "max_plans": 3, "max_implementations": 3,
            "trial_workers": 9, "device_limit": 9, "shortlist_limit": 3, "implementor_limit": 9,
        }
        for name, ceiling in ceilings.items():
            if getattr(self, name) > ceiling:
                raise ValueError(f"{name} exceeds the fixed v1 limit {ceiling}")
        if isinstance(self.codex_run_timeout_seconds, bool) or self.codex_run_timeout_seconds != 7200:
            raise ValueError("Every role invocation must receive the 7200-second deadline")
        for name in ("c_puct", "c_impl"):
            if finite(getattr(self, name), name) < 0:
                raise ValueError(f"{name} must be nonnegative")
        if not 0 < finite(self.prior_floor, "prior_floor") <= 1:
            raise ValueError("prior_floor must be in (0, 1]")
        discount(self.discount_factor)

    @property
    def role_invocation_cap(self) -> int:
        """Generic access; retain the historical field in serialized identities."""
        return self.codex_invocation_cap

    @property
    def role_timeout_seconds(self) -> int:
        return self.codex_run_timeout_seconds

    @property
    def identity(self) -> str:
        return fingerprint(asdict(self))


@dataclass(frozen=True)
class Objective:
    s0_manifest_id: str
    rubric_version: str
    judge_protocol_id: str
    judge_weight: float = 0.1
    version: str = "combined-harness-v1"

    def __post_init__(self):
        unit(self.judge_weight, "judge_weight")
        if not all((self.s0_manifest_id, self.rubric_version, self.judge_protocol_id, self.version)):
            raise ValueError("Objective requires subset, rubric, and judge protocol identities")

    @property
    def identity(self) -> str:
        return fingerprint(asdict(self))


@dataclass(frozen=True)
class CombinedScore:
    artifact_id: str
    u_snapshot_id: str
    j_snapshot_id: str
    objective: Objective
    u: float
    j: float

    def __post_init__(self):
        if not all((self.artifact_id, self.u_snapshot_id, self.j_snapshot_id)):
            raise ValueError("Score requires artifact and usable U/J snapshot IDs")
        unit(self.u, "Evaluation utility")
        unit(self.j, "judge score")

    @property
    def f(self) -> float:
        weight = self.objective.judge_weight
        return (1 - weight) * self.u + weight * self.j

    @property
    def identity(self) -> str:
        return fingerprint(asdict(self))

    def reward_to(self, child: CombinedScore) -> Reward:
        if child.objective != self.objective:
            raise ValueError("Cannot combine scores with different frozen objectives")
        weight = self.objective.judge_weight
        return Reward(
            self.identity, child.identity,
            (1 - weight) * (child.u - self.u), weight * (child.j - self.j),
        )


@dataclass(frozen=True)
class Reward:
    parent_snapshot: str
    child_snapshot: str
    androidworld: float
    judge: float

    @property
    def evaluation(self) -> float:
        """Measured component; the historical field name stays serialized."""
        return self.androidworld

    @property
    def total(self) -> float:
        return self.evaluation + self.judge


def judge_aggregate(dimensions: Mapping[str, Mapping[str, Any]]) -> tuple[float, tuple[str, ...]]:
    required = {"robustness", "decisions", "generalizable"}
    if set(dimensions) != required:
        raise ValueError("Completed judge review must contain all three harness dimensions")
    ratings, unknown = [], []
    for name in sorted(required):
        item = dimensions[name]
        if not item.get("rationale") or not item.get("confidence"):
            raise ValueError(f"{name} requires rationale and qualitative confidence")
        score = item.get("score")
        if score is None:
            if "score" not in item or not item.get("unknown_reason"):
                raise ValueError(f"{name} requires an explicit insufficient-evidence reason")
            unknown.append(name)
            ratings.append(0.5)
        else:
            score = unit(score, name)
            if score not in {0, 0.25, 0.5, 0.75, 1} or not item.get("evidence"):
                raise ValueError(f"{name} requires an ordinal rating and evidence")
            ratings.append(score)
    return math.fsum(ratings) / 3, tuple(unknown)


def discount(value: float) -> float:
    value = finite(value, "discount_factor")
    if not 0 < value < 1:
        raise ValueError("discount_factor must be strictly between zero and one")
    return value


def validate_value(score: CombinedScore, value: float) -> float:
    """Bound discounted future utility changes independently of search depth."""
    value = finite(value, "frontier value")
    if not -score.f <= value <= 1 - score.f:
        raise ValueError("Value must be a bounded net change in combined utility")
    return value


def reverse_returns(rewards: Sequence[Reward], value: float, discount_factor: float) -> list[dict[str, float]]:
    """G_t = r_t + gamma G_(t+1), preserving each discounted contribution."""
    value = finite(value, "frontier value")
    gamma = discount(discount_factor)
    evaluation = judge = 0.0
    predicted = value
    result = []
    for reward in reversed(rewards):
        evaluation = reward.evaluation + gamma * evaluation
        judge = reward.judge + gamma * judge
        predicted *= gamma
        result.append({
            # Keep the frozen backup record schema used by historical runs.
            "androidworld": evaluation, "judge": judge, "predicted": predicted,
            "total": evaluation + judge + predicted,
        })
    return list(reversed(result))
