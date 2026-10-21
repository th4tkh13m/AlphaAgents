"""Retire derived storage only after durable, validated completion checkpoints."""
from __future__ import annotations

import errno
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

from alpha_agents.tree_search.core.mcts.recovery import reconcile_process
from alpha_agents.tree_search.core.shared.storage import write_json

LOG = logging.getLogger(__name__)
VERSION = "role-storage-retention-v1"


def readonly_projection(source: Path, destination: Path) -> dict:
    """Share bytes only between sealed evidence and a read-only role grant.

    This is deliberately never used for mutable harness workspaces. Directory
    entries remain independent, so deleting a projection cannot delete evidence.
    """
    if source.is_symlink() or any(path.is_symlink() for path in source.rglob("*")):
        raise ValueError("Read-only evidence projection cannot contain symlinks")
    counts = {"hardlinked_files": 0, "copied_files": 0}

    def link_file(src, dst):
        try:
            os.link(src, dst, follow_symlinks=False)
            counts["hardlinked_files"] += 1
        except OSError as error:
            if error.errno not in {errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP}:
                raise
            shutil.copy2(src, dst)
            counts["copied_files"] += 1
        return dst

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".projection-", dir=destination.parent) as temporary:
        staging = Path(temporary) / "content"
        shutil.copytree(source, staging, copy_function=link_file)
        os.rename(staging, destination)
    return {"mode": "read-only-evidence-projection-v1", **counts}


def cleanup_sqlite_scratch(request_dir: Path) -> None:
    """Keep scripts/logs; evict bulky derived DBs after the role has exited."""
    try:
        _cleanup_sqlite_scratch(request_dir)
    except OSError as error:
        LOG.warning("Derived SQLite cleanup incomplete for %s: %s", request_dir, error)


def _cleanup_sqlite_scratch(request_dir: Path) -> None:
    scratch = request_dir / "scratch"
    if not scratch.is_dir() or scratch.is_symlink():
        return
    if not reconcile_process(request_dir / "process/process.json", terminate=False):
        return
    selected = set()
    for directory, _, files in os.walk(scratch, followlinks=False):
        for name in files:
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            with path.open("rb") as stream:
                database = stream.read(16) == b"SQLite format 3\0"
            if database:
                selected.add(path)
                selected.update(p for suffix in ("-wal", "-shm", "-journal")
                                if (p := Path(str(path) + suffix)).is_file() and not p.is_symlink())
    if not selected:
        return
    receipt = request_dir / "scratch_retention.json"
    entries = [{"path": str(path.relative_to(scratch)), "size": path.stat().st_size}
               for path in sorted(selected)]
    write_json(receipt, {"version": VERSION, "status": "evicting-derived-sqlite", "files": entries})
    for path in selected:
        path.unlink()
    write_json(receipt, {"version": VERSION, "status": "derived-sqlite-evicted", "files": entries})


def verification_files(workspace: Path, result: dict) -> set[Path]:
    """Preserve exact local citations and ignored test logs at their old paths."""
    keep = set()
    root = workspace.resolve()
    cache = root / ".pytest_cache"
    if cache.is_dir() and not cache.is_symlink():
        keep.add(cache)

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.endswith("evidence") and isinstance(child, list):
                    for reference in child:
                        if not isinstance(reference, str):
                            continue
                        text = re.sub(r":\d+(?::\d+)?$", "", reference.removeprefix("code:"))
                        path = root / text
                        if path.is_file() and path.resolve().is_relative_to(root):
                            keep.add(path.absolute())
                            keep.add(path.resolve())
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(result)
    return keep


def retire_workspace(run: Path, workspace: Path, artifacts, artifact: str,
                     receipt: Path, preserve=()) -> None:
    """Caller must first publish the checkpoint that replaces this workspace.

    A failed deletion is observable but cannot invalidate an already published
    benchmark result. Identity mismatches are not cleanup errors and fail closed.
    """
    run, workspace = run.resolve(), workspace.absolute()
    relative = workspace.relative_to(run).parts
    allowed = ((len(relative) == 2 and relative[0] in {"workspaces", "targeted_workspaces"})
               or (len(relative) == 4 and relative[0] == "tasks" and relative[-1] == "workspace"))
    if not allowed or workspace.is_symlink() or workspace.resolve() != workspace:
        raise ValueError("Cleanup requires an owned disposable workspace")
    if not workspace.exists():
        return
    if artifacts.freeze(workspace) != artifact:
        raise ValueError("Cannot retire a workspace that differs from its frozen checkpoint")
    keep = {Path(path).absolute() for path in preserve}
    if any(not path.is_relative_to(workspace) or not path.resolve().is_relative_to(workspace) for path in keep):
        raise ValueError("Verification retention path escapes workspace")
    record = {"version": "checkpoint-workspace-retention-v1", "workspace": str(workspace),
              "artifact_id": artifact, "status": "evicting", "preserved": sorted(str(p.relative_to(workspace)) for p in keep)}
    write_json(receipt, record)

    def prune(path):
        if any(path == p or path.is_relative_to(p) for p in keep):
            return
        if any(p.is_relative_to(path) for p in keep):
            for child in path.iterdir():
                prune(child)
        elif path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()

    try:
        prune(workspace)
    except OSError as error:
        write_json(receipt, {**record, "status": "cleanup-incomplete", "error": str(error)})
        LOG.warning("Workspace cleanup incomplete: %s", workspace)
        return
    write_json(receipt, {**record, "status": "source-evicted"})
