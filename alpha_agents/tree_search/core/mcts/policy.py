"""Pure two-level PUCT policy; all reservations are made by the store."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from alpha_agents.tree_search.core.mcts.contracts import finite


@dataclass(frozen=True)
class PriorAssessment:
    raw: dict[str, float]
    normalized: dict[str, float]
    effective: dict[str, float]
    floor: float


def prepare_priors(raw: Mapping[str, float], floor: float = 0.01) -> PriorAssessment:
    floor = finite(floor, "prior floor")
    if not raw or not 0 < floor <= 1:
        raise ValueError("Nonempty priors and a positive floor are required")
    values = {key: finite(value, f"prior {key}") for key, value in sorted(raw.items())}
    if any(value < 0 for value in values.values()):
        raise ValueError("Priors must be nonnegative")
    # Scaling avoids overflow for valid finite but very large priors.
    maximum = max(values.values())
    if maximum <= 0:
        raise ValueError("All-zero priors are invalid")
    scaled = {key: value / maximum for key, value in values.items()}
    total = math.fsum(scaled.values())
    normalized = {key: value / total for key, value in scaled.items()}
    floored = {key: max(value, floor) for key, value in normalized.items()}
    mass = math.fsum(floored.values())
    return PriorAssessment(values, normalized, {key: v / mass for key, v in floored.items()}, floor)


@dataclass(frozen=True)
class Edge:
    id: str
    visits: int
    total: float
    prior: float

    def __post_init__(self):
        if isinstance(self.visits, bool) or not isinstance(self.visits, int) or self.visits < 0:
            raise ValueError("Edge visits must be a nonnegative integer")
        finite(self.total, "edge total")
        if not 0 <= finite(self.prior, "edge prior") <= 1:
            raise ValueError("Edge prior must lie in [0, 1]")
        if self.visits == 0 and self.total != 0:
            raise ValueError("Unvisited edges cannot carry completed returns")

    @property
    def q(self) -> float:
        return self.total / self.visits if self.visits else 0.0


def select_edge(edges: Sequence[Edge], parent_visits: int, coefficient: float) -> Edge:
    if not edges:
        raise ValueError("No eligible edge")
    if isinstance(parent_visits, bool) or not isinstance(parent_visits, int) or parent_visits < 0:
        raise ValueError("Parent visits must be a nonnegative integer")
    coefficient = finite(coefficient, "exploration coefficient")
    if coefficient < 0:
        raise ValueError("Exploration coefficient must be nonnegative")
    if parent_visits == 0:
        return min(edges, key=lambda edge: (-edge.prior, edge.id))
    return min(edges, key=lambda edge: (
        -(edge.q + coefficient * edge.prior * math.sqrt(parent_visits) / (1 + edge.visits)),
        edge.id,
    ))
