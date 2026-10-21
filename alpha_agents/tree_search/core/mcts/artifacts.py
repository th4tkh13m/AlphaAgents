"""Content-addressed harness snapshots and binary parent-relative patches.

Manifest identity covers paths, file bytes, executable modes, directories,
symlink targets, and permissions. Runtime work always uses separate copies.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from alpha_agents.tree_search.core.mcts.contracts import canonical_json, fingerprint
from alpha_agents.tree_search.core.shared.storage import write_json
from alpha_agents.tree_search.core.shared.workspace import git

# Do not ignore output* names: Artemis includes required source modules there.
DEFAULT_EXCLUDES = frozenset({
    ".git", "gitdir", "__pycache__", ".pytest_cache", ".venv", "venv",
    ".dgm_parent_evidence", ".dgm_goal_context.md",
})
RUNTIME_GUARD_ROOTS = frozenset({".agents", ".aws", ".codex"})


def register_runtime_workspace(run: Path, workspace: Path, parent: str) -> None:
    """Record absent guard directories before Codex can initialize its sandbox."""
    workspace = workspace.resolve()
    directory = run.resolve() / "workspace_runtime"
    directory.mkdir(parents=True, exist_ok=True)
    record = directory / (fingerprint(str(workspace)) + ".json")
    if record.exists():
        previous = json.loads(record.read_text())
        if previous.get("workspace") != str(workspace) or previous.get("parent") != parent:
            raise ValueError("Runtime workspace registration changed identity")
        return  # Recovery must preserve the original pre-invocation inventory.
    missing = sorted(name for name in RUNTIME_GUARD_ROOTS if not (workspace / name).exists()
                     and not (workspace / name).is_symlink())
    write_json(record, {"workspace": str(workspace), "parent": parent,
                        "initially_absent_guard_roots": missing})


def _empty_guard(path: Path, source: Path, roots: frozenset[str]) -> bool:
    return (path.parent == source and path.name in roots and not path.is_symlink()
            and path.is_dir() and next(path.iterdir(), None) is None)


def manifest(source: Path, excludes: frozenset[str] = DEFAULT_EXCLUDES,
             empty_guard_roots: frozenset[str] = frozenset()) -> list[dict]:
    if source.is_symlink() or not source.is_dir():
        raise ValueError("Harness source must be a real directory")
    records = []
    def walk(directory: Path):
        for path in sorted(directory.iterdir(), key=lambda p: p.name):
            if path.name in excludes:
                continue
            if _empty_guard(path, source, empty_guard_roots):
                continue
            info = path.lstat()
            item = {"path": path.relative_to(source).as_posix(), "mode": stat.S_IMODE(info.st_mode)}
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                # A frozen artifact cannot inherit mutable external dependencies
                # through a symlink; runtime dependency links belong to execution.
                resolved = (path.parent / target).resolve()
                if Path(target).is_absolute() or not resolved.is_relative_to(source.resolve()):
                    raise ValueError(f"External harness symlink: {item['path']}")
                item.update(kind="symlink", target=target)
            elif stat.S_ISDIR(info.st_mode):
                item.update(kind="directory")
            elif stat.S_ISREG(info.st_mode):
                item.update(kind="file", sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            else:
                raise ValueError(f"Unsupported artifact entry: {item['path']}")
            records.append(item)
            if item["kind"] == "directory":
                walk(path)
    walk(source)
    return records


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def freeze(self, source: Path) -> str:
        if source.is_symlink():
            raise ValueError("Harness source must be a real directory")
        source = source.resolve()
        empty_guards = self._runtime_empty_guards(source)
        records = manifest(source, empty_guard_roots=empty_guards)
        id = fingerprint(records)
        target = self.root / id
        if target.exists():
            self.verify(id)
            return id
        staging = Path(tempfile.mkdtemp(prefix="freeze-", dir=self.root))
        try:
            shutil.copytree(source, staging / "content", symlinks=True,
                            ignore=lambda directory, names: DEFAULT_EXCLUDES.intersection(names) | {
                                name for name in names if _empty_guard(Path(directory) / name, source, empty_guards)})
            if manifest(staging / "content") != records:
                raise ValueError("Harness changed during artifact freezing")
            (staging / "manifest.json").write_text(canonical_json(records) + "\n")
            try:
                os.rename(staging, target)
            except OSError:
                if not target.is_dir():
                    raise
                self.verify(id)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return id

    def _runtime_empty_guards(self, source: Path) -> frozenset[str]:
        registration = self.root.parent / "workspace_runtime" / (fingerprint(str(source)) + ".json")
        if not registration.exists():
            return frozenset()
        record = json.loads(registration.read_text())
        missing = record.get("initially_absent_guard_roots")
        if (record.get("workspace") != str(source) or not isinstance(missing, list)
                or any(name not in RUNTIME_GUARD_ROOTS for name in missing)):
            raise ValueError("Invalid trusted runtime workspace registration")
        parent = {row["path"] for row in self.verify(record["parent"])}
        if any(name in parent for name in missing):
            raise ValueError("Runtime guard exclusion would omit parent harness content")
        return frozenset(missing)

    def _directory(self, id: str) -> Path:
        if len(id) != 64 or any(c not in "0123456789abcdef" for c in id):
            raise ValueError("Artifact ID must be a SHA-256 content identity")
        return self.root / id

    def verify(self, id: str) -> list[dict]:
        directory = self._directory(id)
        records = json.loads((directory / "manifest.json").read_text())
        if fingerprint(records) != id or manifest(directory / "content") != records:
            raise ValueError(f"Frozen artifact corruption: {id}")
        return records

    def materialize(self, id: str, destination: Path):
        records = self.verify(id)
        shutil.copytree(self._directory(id) / "content", destination, symlinks=True)
        if manifest(destination) != records:
            raise ValueError("Materialized artifact does not match its manifest")

    def readonly_view(self, id: str, root: Path | None = None) -> Path:
        """One independent code copy per artifact, shared only with read roles.

        Never hardlink writable source files to immutable artifacts. Keep the
        established value_code_views location so permissions and old frozen
        continuation contexts retain the same exact paths.
        """
        root = root if root is not None else self.root.parent / "value_code_views"
        if root.is_symlink():
            raise ValueError("Read-only code views cannot use symbolic roots")
        root.mkdir(parents=True, exist_ok=True)
        records = self.verify(id)
        destination = root / id
        if destination.is_symlink():
            raise ValueError("Read-only code view cannot be a symbolic link")
        if not destination.exists():
            with tempfile.TemporaryDirectory(prefix=".view-", dir=root) as temporary:
                staging = Path(temporary) / "content"
                self.materialize(id, staging)
                try:
                    os.rename(staging, destination)
                except OSError:
                    if not destination.is_dir() or destination.is_symlink():
                        raise
        if manifest(destination) != records:
            raise ValueError("Read-only code view differs from its frozen artifact")
        return destination

    def binary_patch(self, parent: str, child: str) -> bytes:
        """Produce the exact parent-relative Git patch, including new/binary files."""
        self.verify(parent)
        self.verify(child)
        with tempfile.TemporaryDirectory(prefix="alpha-patch-") as tmp:
            workspace = Path(tmp) / "worktree"
            self.materialize(parent, workspace)
            git(workspace, "init", "--initial-branch=main")
            # Artifact scope is authoritative; .gitignore cannot hide source.
            git(workspace, "add", "--all", "--force")
            git(workspace, "-c", "user.name=AlphaAgents", "-c", "user.email=alpha@example.invalid",
                "commit", "--allow-empty", "-m", "frozen parent")
            for path in workspace.iterdir():
                if path.name != ".git":
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
            shutil.copytree(self._directory(child) / "content", workspace, dirs_exist_ok=True, symlinks=True)
            git(workspace, "add", "--all", "--force")
            result = subprocess.run(["git", "-C", str(workspace), "diff", "--binary", "HEAD"],
                                    capture_output=True, check=True)
            return result.stdout

    def reconstruct(self, parent: str, child: str, patch: bytes, destination: Path):
        """Verify patch execution as well as non-Git metadata (e.g. empty dirs)."""
        self.materialize(parent, destination)
        result = subprocess.run(["git", "-C", str(destination), "apply", "--binary", "--whitespace=nowarn", "-"],
                                input=patch, capture_output=True)
        if result.returncode and patch.strip():
            raise ValueError(result.stderr.decode(errors="replace"))
        records = self.verify(child)
        expected = {item["path"]: item for item in records}
        # Git does not carry empty directories or arbitrary non-executable modes.
        # The immutable child manifest supplies these, but never file content.
        for item in reversed(manifest(destination)):
            path = destination / item["path"]
            if item["kind"] == "directory" and item["path"] not in expected:
                path.rmdir()
        for item in records:
            path = destination / item["path"]
            if item["kind"] == "directory":
                path.mkdir(parents=True, exist_ok=True)
            if item["kind"] != "symlink":
                path.chmod(item["mode"])
        if manifest(destination) != records:
            raise ValueError("Parent-relative patch reconstruction failed child manifest verification")
