"""Live-discovered Codex guard scaffolding must not hide actual harness edits."""

import json
import shutil

import pytest

from alpha_agents.tree_search.core.mcts.artifacts import (
    ArtifactStore,
    register_runtime_workspace,
)
from alpha_agents.tree_search.core.mcts.contracts import fingerprint


def setup_workspace(tmp_path, original_guard=False):
    source = tmp_path / "source"
    source.mkdir()
    (source / "harness.py").write_text("value = 1\n")
    if original_guard:
        (source / ".codex").mkdir()
    store = ArtifactStore(tmp_path / "run" / "artifacts")
    parent = store.freeze(source)
    workspace = tmp_path / "run" / "workspaces" / "implementation"
    shutil.copytree(source, workspace)
    register_runtime_workspace(tmp_path / "run", workspace, parent)
    return store, parent, workspace


def test_only_registered_initially_absent_empty_guard_roots_are_ignored(tmp_path):
    store, parent, workspace = setup_workspace(tmp_path)
    for name in (".agents", ".aws", ".codex"):
        (workspace / name).mkdir()
    assert store.freeze(workspace) == parent
    # Runtime registration survives repeated/resumed invocation preparation.
    register_runtime_workspace(tmp_path / "run", workspace, parent)
    assert store.freeze(workspace) == parent
    (workspace / ".codex" / "config.toml").write_text("model = 'changed'\n")
    child = store.freeze(workspace)
    assert child != parent
    assert ".codex/config.toml" in {row["path"] for row in store.verify(child)}


def test_original_guard_directory_remains_part_of_artifact_identity(tmp_path):
    store, parent, workspace = setup_workspace(tmp_path, original_guard=True)
    assert store.freeze(workspace) == parent
    (workspace / ".codex").rmdir()
    assert store.freeze(workspace) != parent


def test_guard_symlink_is_not_silently_ignored(tmp_path):
    store, _, workspace = setup_workspace(tmp_path)
    (workspace / ".aws").symlink_to(tmp_path / "source", target_is_directory=True)
    with pytest.raises(ValueError, match="External harness symlink"):
        store.freeze(workspace)


def test_invalid_registration_cannot_exclude_arbitrary_or_parent_content(tmp_path):
    store, parent, workspace = setup_workspace(tmp_path, original_guard=True)
    path = tmp_path / "run" / "workspace_runtime" / (fingerprint(str(workspace.resolve())) + ".json")
    record = json.loads(path.read_text())
    record["initially_absent_guard_roots"] = ["harness.py"]
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="Invalid trusted"):
        store.freeze(workspace)
    record["initially_absent_guard_roots"] = [".codex"]
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="parent harness content"):
        store.freeze(workspace)
    assert store.verify(parent)


def test_root_source_symlink_is_still_rejected(tmp_path):
    store, _, workspace = setup_workspace(tmp_path)
    link = tmp_path / "workspace-link"
    link.symlink_to(workspace, target_is_directory=True)
    with pytest.raises(ValueError, match="real directory"):
        store.freeze(link)
