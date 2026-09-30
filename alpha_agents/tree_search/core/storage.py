"""Atomic search snapshots and candidate records."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from .contracts import Candidate, Evaluation


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


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
        """Exclusive writer lock, released by the OS on crash."""
        from contextlib import contextmanager

        @contextmanager
        def acquire():
            with (self.root / ".search.lock").open("a+b") as stream:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    if os.fstat(stream.fileno()).st_size == 0:
                        stream.write(b"0")
                        stream.flush()
                    stream.seek(0)
                    try:
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    except OSError as error:
                        raise RuntimeError(
                            "Another controller owns this run"
                        ) from error
                else:
                    import fcntl

                    try:
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError as error:
                        raise RuntimeError(
                            "Another controller owns this run"
                        ) from error
                try:
                    yield
                finally:
                    if os.name == "nt":
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream, fcntl.LOCK_UN)

        return acquire()
