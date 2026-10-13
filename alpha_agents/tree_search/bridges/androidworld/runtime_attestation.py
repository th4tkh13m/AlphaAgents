"""Observed AndroidWorld runtime source identity; never benchmark outcomes."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

from alpha_agents.tree_search.core.mcts.artifacts import manifest
from alpha_agents.tree_search.core.mcts.contracts import fingerprint


def observe_androidworld(root: Path, expected_revision: str) -> dict:
    root = root.resolve()
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", expected_revision):
        raise ValueError("androidworld_revision must be the full expected Git commit SHA")
    checkout = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel", "HEAD"],
                              capture_output=True, text=True, timeout=10)
    lines = checkout.stdout.splitlines()
    if checkout.returncode or len(lines) != 2 or Path(lines[0]).resolve() != root:
        raise ValueError("Configured AndroidWorld root must be its own Git checkout")
    if lines[1] != expected_revision:
        raise ValueError("AndroidWorld checkout HEAD differs from the configured revision")
    package = root / "android_world"
    if package.is_symlink() or not (package / "__init__.py").is_file():
        raise ValueError("Configured AndroidWorld checkout has no real runtime package")
    # Include generated/untracked package code and actual dirty file contents.
    # Exclude root-level test_runs/results: measurements are not source identity.
    records = manifest(package)
    entrypoints = []
    paths = sorted(set(root.glob("*.py")) | {root / "pyproject.toml", root / "requirements.txt"})
    for path in paths:
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError("Runtime entrypoints cannot use symbolic or non-file paths")
        entrypoints.append({"path": path.name, "mode": path.stat().st_mode & 0o777,
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    identity = {"version": "androidworld-runtime-source-v1", "git_commit": lines[1],
                "package_manifest": records, "entrypoints": entrypoints}
    return {"version": identity["version"], "git_commit": lines[1], "source_hash": fingerprint(identity),
            "package_records": len(records), "entrypoint_records": len(entrypoints),
            "scope": "android_world runtime package and root Python/dependency entrypoints; excludes measurement outputs"}


def verify_androidworld(root: Path, expected: dict) -> dict:
    observed = observe_androidworld(root, expected["git_commit"])
    if observed != expected:
        raise ValueError("AndroidWorld runtime source changed after execution identity was frozen")
    return observed


def verify_imports(root: Path, source: Path | None, dependencies: dict):
    imported = dependencies.get("android_world")
    if not imported or Path(imported).resolve() != (root.resolve() / "android_world" / "__init__.py").resolve():
        raise ValueError("Dependency preflight imported AndroidWorld outside the configured checkout")
    if source is not None:
        harness = dependencies.get("artemis")
        if not harness or not Path(harness).resolve().is_relative_to(source.resolve()):
            raise ValueError("Dependency preflight imported Artemis outside the assigned harness")
