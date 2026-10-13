"""Atomic JSON persistence and exclusive run locking for both searches."""

from __future__ import annotations

import json
import os
from pathlib import Path


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


def run_lock(root: Path):
    """Exclusive writer lock, released by the OS on crash."""
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    from contextlib import contextmanager

    @contextmanager
    def acquire():
        with (root / ".search.lock").open("a+b") as stream:
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
