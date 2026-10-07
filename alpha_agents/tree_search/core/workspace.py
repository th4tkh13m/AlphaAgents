"""Generic candidate Git workspaces; no benchmark or SDK dependencies."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from .contracts import Candidate


def git(workspace: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(workspace), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def materialize(
    source: Path, root: Path, id: str, parent: Candidate | None
) -> Candidate:
    directory = root / id
    workspace = directory / "worktree"
    if parent is None:
        origin = source
    else:
        # Reconstruct from the immutable baseline, never from an evaluator-
        # modified parent workspace. Each child patch is relative to its parent.
        origin = root / "source"
    shutil.copytree(
        origin,
        workspace,
        ignore=shutil.ignore_patterns(
            ".git",
            "gitdir",
            "__pycache__",
            ".pytest_cache",
            ".venv",
            "venv",
            ".dgm_parent_evidence",
            ".dgm_goal_context.md",
            ".dgm_archive",
        ),
    )
    git(
        workspace,
        "init",
        "--initial-branch=main",
        f"--separate-git-dir={directory / 'gitdir'}",
    )
    git(workspace, "add", "--all")
    git(
        workspace,
        "-c",
        "user.name=DGM",
        "-c",
        "user.email=dgm@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "baseline",
    )
    if parent:
        lineage = []
        current = parent
        from .storage import read_json

        while current.parent_id is not None:
            lineage.append(current.patch)
            record = read_json(root / current.parent_id / "candidate.json")
            current = Candidate(
                record["id"],
                record["parent_id"],
                root / record["id"],
                record["base_commit"],
            )
        for patch in reversed(lineage):
            if patch.exists() and patch.stat().st_size:
                git(workspace, "apply", "--whitespace=nowarn", str(patch))
        git(workspace, "add", "--all")
        git(
            workspace,
            "-c",
            "user.name=DGM",
            "-c",
            "user.email=dgm@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "parent",
        )
        evidence = workspace / ".dgm_parent_evidence"
        shutil.copytree(
            parent.directory,
            evidence,
            ignore=shutil.ignore_patterns(
                "worktree", "gitdir", ".dgm_archive", "archive_snapshot.json",
                "mutation_context.json", "self_evo.md", "self_evolution.md",
                "mutation", "transfer_report.json", "transfer_assessment.json",
            ),
        )
    exclude = directory / "gitdir" / "info" / "exclude"
    with exclude.open("a", encoding="utf-8") as stream:
        stream.write("\n.dgm_parent_evidence/\n.dgm_goal_context.md\n.dgm_archive/\n")
    base_commit = git(workspace, "rev-parse", "HEAD")
    from .storage import write_json

    write_json(directory / "workspace.json", {"base_commit": base_commit})
    return Candidate(id, parent.id if parent else None, directory, base_commit)


def recover_patch(candidate: Candidate) -> bool:
    # Include newly created files but exclude ignored evidence/context.
    git(candidate.workspace, "add", "--all")
    result = subprocess.run(
        [
            "git",
            "-C",
            str(candidate.workspace),
            "diff",
            "--binary",
            candidate.base_commit,
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace"))
    candidate.patch.write_bytes(result.stdout)
    return bool(result.stdout.strip())


def source_digest(source: Path) -> str:
    """Fingerprint the immutable snapshot, including file names and contents."""
    digest = hashlib.sha256()
    for path in sorted(source.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(source)).replace("\\", "/").encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()
