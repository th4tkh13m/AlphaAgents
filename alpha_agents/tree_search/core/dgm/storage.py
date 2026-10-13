"""DGM candidate records and authoritative JSON search checkpoints."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from ..shared.storage import read_json, run_lock, write_json
from .contracts import Candidate, Evaluation


class RunStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def save_candidate(
        self,
        candidate: Candidate,
        evaluation: Evaluation,
        status: str,
        mutation=None,
        validation=None,
    ):
        value = {
            "id": candidate.id,
            "parent_id": candidate.parent_id,
            "base_commit": candidate.base_commit,
            "status": status,
            "evaluation": asdict(evaluation),
            "mutation": mutation,
            "validation": validation or [],
        }
        write_json(candidate.directory / "candidate.json", value)
        return value

    def candidate(self, id: str) -> Candidate:
        directory = self.root / id
        record = read_json(directory / "candidate.json")
        return Candidate(id, record["parent_id"], directory, record["base_commit"])

    def checkpoint(self, state: dict) -> None:
        write_json(self.root / "state.json", state)

    def lock(self):
        return run_lock(self.root)
