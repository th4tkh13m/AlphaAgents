"""Publish one verified standalone inspection database per completed execution.

Original DB/WAL/SHM bytes are never opened by SQLite or changed. Inspection
snapshots are derived files, not additional task observations. Publication must
precede the task evidence inventory and its immutable cache commit.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from alpha_agents.tree_search.infrastructure.sqlite_snapshot import snapshot_connection

DIRECTORY = ".alpha_inspection_sqlite"
VERSION = "sealed-sqlite-inspection-v1"


def file_record(path: Path, root: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return {"path": path.relative_to(root).as_posix(), "size": path.stat().st_size,
            "sha256": digest.hexdigest()}


def prepare_inspection_databases(root: Path) -> None:
    # The lock is outside the sealed evidence tree. Concurrent/recovered
    # publishers cannot observe an index whose snapshots are still being built.
    import fcntl
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Inspection source must be a real evidence directory")
    with (root.parent / ".sqlite-inspection.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _prepare_inspection_databases(root)


def _prepare_inspection_databases(root: Path) -> None:
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Inspection source must be a real evidence directory")
    root = root.resolve()
    sources = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("SQLite inspection cannot traverse evidence symlinks")
        if DIRECTORY in path.relative_to(root).parts or not path.is_file():
            continue
        with path.open("rb") as stream:
            if stream.read(16) == b"SQLite format 3\0":
                sources.append(path)
    if not sources:
        if (root / DIRECTORY).exists():
            raise ValueError("Inspection metadata has no original SQLite source")
        return
    target = root / DIRECTORY
    with tempfile.TemporaryDirectory(prefix=".inspection-build-", dir=root.parent) as temporary:
        staging = Path(temporary) / DIRECTORY
        staging.mkdir()
        databases = []
        for index, source in enumerate(sources):
            paths = [source, *(Path(str(source) + suffix) for suffix in ("-wal", "-shm", "-journal"))]
            before = [file_record(path, root) for path in paths if path.exists()]
            destination = staging / f"{index:04d}.sqlite"
            unavailable = None
            try:
                with snapshot_connection(source) as connection, closing(sqlite3.connect(destination)) as backup:
                    connection.backup(backup)
                    backup.execute("PRAGMA journal_mode=DELETE")
                    if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise sqlite3.DatabaseError("Inspection SQLite integrity check failed")
            except (sqlite3.Error, RuntimeError) as error:
                unavailable = f"{type(error).__name__}: {error}"
            after = [file_record(path, root) for path in paths if path.exists()]
            if before != after:
                raise ValueError("Original database changed during inspection publication")
            if unavailable:
                for path in staging.glob(destination.name + "*"):
                    path.unlink()
            record = None if unavailable else file_record(destination, Path(temporary))
            databases.append({"source_file": source.relative_to(root).as_posix(),
                              "source_files": before, "standalone": record,
                              "status": "unavailable" if unavailable else "ready", "error": unavailable})
        payload = {"version": VERSION, "databases": databases}
        (staging / "index.json").write_text(json.dumps(payload, indent=2) + "\n")
        if target.exists():
            # Rebuild first: an existing file authored by the harness is not a
            # trusted checkpoint merely because it claims matching hashes.
            if target.is_symlink() or json.loads((target / "index.json").read_text()) != payload:
                raise ValueError("Conflicting SQLite inspection publication")
            expected = {"index.json", *(Path(d["standalone"]["path"]).name for d in databases if d["standalone"])}
            if {p.name for p in target.iterdir()} != expected:
                raise ValueError("Unexpected SQLite inspection files")
            for database in databases:
                item = database["standalone"]
                if item and file_record(root / item["path"], root) != item:
                    raise ValueError("Published SQLite inspection changed")
        else:
            os.rename(staging, target)


def inspection_catalog(root: Path, inventory: list[dict]) -> list[dict]:
    """Expose only snapshot declarations bound to the sealed file inventory."""
    root = Path(root)
    items = {item["path"]: item for item in inventory}
    index_path = f"{DIRECTORY}/index.json"
    if index_path not in items:
        return []  # Historical evidence keeps the scratch-copy fallback.
    if file_record(root / index_path, root) != items[index_path]:
        raise ValueError("SQLite inspection index does not match sealed evidence")
    payload = json.loads((root / index_path).read_text())
    if payload.get("version") != VERSION:
        raise ValueError("Unsupported SQLite inspection version")
    result = []
    for entry in payload["databases"]:
        source = entry["source_file"]
        snapshot = entry["standalone"]
        for item in [*entry["source_files"], *([snapshot] if snapshot else [])]:
            if items.get(item["path"]) != item:
                raise ValueError("SQLite inspection is not bound to its source inventory")
        expected_sources = {path for path in (source, *(source + suffix for suffix in ("-wal", "-shm", "-journal"))) if path in items}
        if {item["path"] for item in entry["source_files"]} != expected_sources:
            raise ValueError("SQLite inspection has unaccounted source sidecars")
        if snapshot is None:
            if entry.get("status") != "unavailable" or not entry.get("error") or source not in items:
                raise ValueError("Unavailable inspection requires a bound source and concrete error")
            result.append({"version": VERSION, "status": "unavailable", "source_file": str(root / source),
                           "error": entry["error"]})
            continue
        relative = Path(snapshot["path"])
        if (source not in items or relative.parent != Path(DIRECTORY)
                or relative.suffix != ".sqlite" or relative.is_absolute()
                or any(str(relative) + suffix in items for suffix in ("-wal", "-shm"))):
            raise ValueError("Invalid standalone SQLite inspection path")
        path = root / relative
        result.append({"version": VERSION, "status": "ready", "source_file": str(root / source),
                       "standalone_file": str(path), "sha256": snapshot["sha256"],
                       "size": snapshot["size"], "read_uri": path.as_uri() + "?mode=ro&immutable=1"})
    return result
