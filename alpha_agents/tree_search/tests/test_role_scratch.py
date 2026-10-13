"""Private inspection writes must not widen canonical/source permissions."""

import json
from pathlib import Path

import pytest

from alpha_agents.tree_search.mutators.coding_agents.codex.roles.permissions import (
    role_permissions,
)
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.prompts import (
    RoleRequest,
)


@pytest.mark.parametrize("role", ["proposer", "policy", "implementer", "judge_code", "judge_execution", "value"])
def test_invocation_has_private_scratch_without_other_request_or_evidence_write_access(tmp_path, role):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    canonical = tmp_path / "tasks/task/execution/evidence"
    canonical.mkdir(parents=True)
    context = {"canonical_evidence": {"evidence": [str(canonical)]}}
    request = RoleRequest(str(tmp_path), "call-first", role, "artifact", "gpt-6-luna", "low", context)
    profile = role_permissions(request, workspace)
    scratch = request.scratch_directory
    assert scratch.is_dir() and scratch.parent == tmp_path / "role_requests/call-first"
    assert profile["filesystem"][str(scratch)] == "write"
    assert profile["filesystem"][str(canonical)] == "read"
    assert profile["filesystem"][str(workspace)] == ("write" if role == "implementer" else "read")
    assert str(scratch.parent) not in profile["filesystem"]
    assert str(tmp_path / "role_requests/call-other/scratch") not in profile["filesystem"]
    assert str(tmp_path) not in profile["filesystem"] and ":root" not in profile["filesystem"]
    envelope = json.loads(request.render().split("## Exact invocation inputs\n```json\n", 1)[1].split("\n```", 1)[0])
    assert envelope["scratch_directory"] == str(scratch)


@pytest.mark.parametrize("location", ["scratch", "request"])
def test_scratch_symlink_cannot_turn_an_evidence_directory_into_a_write_grant(tmp_path, location):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    evidence = tmp_path / "tasks/task/execution/evidence"
    evidence.mkdir(parents=True)
    request = RoleRequest(str(tmp_path), "call-first", "judge_execution", "artifact", "gpt-6-luna", "low", {})
    target = request.scratch_directory if location == "scratch" else request.scratch_directory.parent
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(evidence, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic links"):
        role_permissions(request, workspace)
    assert not (evidence / "scratch").exists()


@pytest.mark.parametrize("request_id", [".", "..", "../other", "call/other", "call\\other"])
def test_request_id_cannot_escape_its_private_directory(tmp_path, request_id):
    with pytest.raises(ValueError, match="safe filesystem component"):
        RoleRequest(str(tmp_path), request_id, "policy", "artifact", "gpt-6-luna", "low", {})


def test_scratch_guidance_is_external_and_bound_to_library_identity(tmp_path, monkeypatch):
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles import prompts

    request = RoleRequest(str(tmp_path), "call-first", "value", "artifact", "gpt-6-luna", "low", {})
    guidance = (Path(prompts.__file__).with_name("prompts") / "scratch.md").read_text()
    assert guidance.strip() in request.render()
    before = request.library_hash
    monkeypatch.setattr(prompts, "SHARED", prompts.SHARED + "\nChanged scratch instructions\n")
    assert request.library_hash != before
