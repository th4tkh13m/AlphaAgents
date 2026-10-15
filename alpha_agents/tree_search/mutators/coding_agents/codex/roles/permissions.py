"""Explicit role filesystem profiles; never inherit broad legacy sandboxes."""

from __future__ import annotations

import json
import re
from pathlib import Path

from alpha_agents.tree_search.core.mcts.evidence_scope import continuation_bundle, value_task_roots

from alpha_agents.tree_search.mutators.coding_agents.codex.roles.prompts import (
    RoleRequest,
)

PROFILE = "alpha_agents_role"


def runtime_permissions(profile: dict, codex_bin: str, verification_python: str | None = None) -> dict:
    """Expose the trusted launcher itself, without its home or release directory."""
    executable = Path(codex_bin).resolve(strict=True)
    if not executable.is_file():
        raise ValueError("Codex runtime read grant must be an exact executable file")
    rules = {**profile["filesystem"], str(executable): "read"}
    if verification_python is not None:
        python = Path(verification_python).expanduser().absolute()
        environment = python.parent.parent.resolve(strict=True)
        if (python.parent.name != "bin" or not re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", python.name)
                or not python.is_file() or not (environment / "pyvenv.cfg").is_file()):
            raise ValueError("Verification tools must use an explicit virtual-environment Python")
        if environment in {Path("/"), Path("/tmp"), Path.home()}:
            raise ValueError("Verification environment cannot grant a broad filesystem root")
        rules[str(environment)] = "read"
        rules[str(Path(__file__).with_name("verification.py").resolve(strict=True))] = "read"
    return {**profile, "filesystem": rules}


def role_permissions(request: RoleRequest, workspace: Path) -> dict:
    # Use the same declaration validation as response citations, preserving the
    # existing read scope rather than granting paths discovered inside bundles.
    value_task_roots(request)
    continuation_bundle(request)
    workspace = workspace.resolve()
    run = Path(request.run_id).resolve()
    rules = {":minimal": "read", str(workspace): "write" if request.role == "implementer" else "read"}
    # These exact files contain this request only. Never grant the surrounding
    # role_requests directory, which holds other roles' prompts/conversations.
    request_dir = run / "role_requests" / request.request_id
    scratch = request.scratch_directory
    if scratch.resolve() != scratch or scratch.is_symlink():
        raise ValueError("Invocation scratch directory cannot use symbolic links")
    scratch.mkdir(parents=True, exist_ok=True)
    rules[str(scratch)] = "write"
    for name in ("input.md", "prompt.md", "goal_context.md"):
        rules[str(request_dir / name)] = "read"
    channel = request.context.get("device_check_channel")
    if channel:
        if (request.role != "implementer"
                or channel.get("request_path") != str(request_dir / "check_input" / "check_request.json")
                or channel.get("result_directory") != str(request_dir / "check_results")):
            raise ValueError("Device check channel must belong to this implementer invocation")
        inbox = request_dir / "check_input"
        if inbox.is_symlink() or Path(channel["request_path"]).is_symlink():
            raise ValueError("Device check inbox cannot use symbolic links")
        # Codex creates guard directories beneath writable roots. A writable
        # exact file is incorrectly treated as a directory by that helper.
        # Grant only this invocation's dedicated inbox, never its request root.
        rules[str(inbox)] = "write"
        rules[channel["result_directory"]] = "read"

    def visit(value):
        if isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)
        elif isinstance(value, str) and value.startswith("/"):
            path = Path(value)
            if not path.exists():
                return  # Symbolic evidence references are not filesystem grants.
            resolved = path.resolve()
            if resolved.is_relative_to(workspace):
                return
            if not resolved.is_relative_to(run):
                raise ValueError("Evidence read grant must belong to this run")
            relative = resolved.relative_to(run).parts
            if channel and str(resolved) in {channel["request_path"], channel["result_directory"]}:
                if path.is_symlink():
                    raise ValueError("Device check channel cannot use symbolic links")
                return
            task_evidence = len(relative) >= 4 and relative[0] == "tasks" and relative[3] == "evidence"
            archived_plan = len(relative) == 3 and relative[0] == "plan_archive" and relative[2] in {"PLAN.md", "PLAN.json"}
            archived_attempt = len(relative) == 3 and relative[0] == "evidence_archive" and relative[2] == "ATTEMPT.json"
            continuation = len(relative) == 3 and relative[0] == "evidence_archive" and relative[2] == "CONTINUATION.json"
            code_view = (len(relative) == 2 and relative[0] == "value_code_views" and len(relative[1]) == 64
                         and all(char in "0123456789abcdef" for char in relative[1]) and resolved.is_dir())
            if not task_evidence and not ((archived_plan or archived_attempt) and request.role not in {"judge_code", "judge_execution"}) and not ((continuation or code_view) and request.role == "value"):
                raise ValueError("Role context requested a forbidden filesystem read grant")
            if path.is_symlink():
                raise ValueError("Evidence read grant cannot use a symbolic link")
            rules[str(resolved)] = "read"

    # A judge can only gain external reads from its canonical evidence block,
    # never from arbitrary paths in earlier model-authored code findings.
    visit(request.context.get("canonical_evidence", {}) if request.role.startswith("judge_") else request.context)
    # Evidence manifests enumerate every file as well as their already granted
    # directory. Keep the same access while avoiding Linux's per-argument limit
    # when the complete profile is serialized into one CLI override.
    directories = [Path(path) for path, access in rules.items()
                   if path.startswith("/") and access == "read" and Path(path).is_dir()]
    rules = {path: access for path, access in rules.items()
             if not (path.startswith("/") and access == "read"
                     and any(Path(path) != parent and Path(path).is_relative_to(parent)
                             for parent in directories))}
    return {"name": PROFILE, "filesystem": rules, "network_enabled": False}


def permission_overrides(profile: dict) -> list[str]:
    prefix = f"permissions.{profile['name']}"
    result = [f"default_permissions={json.dumps(profile['name'])}", f"{prefix}.network.enabled=false"]
    # Codex's dotted -c keys are not TOML-parsed: quoted path components
    # become literal quote characters. Put path keys inside a TOML inline table.
    filesystem = ", ".join(f"{json.dumps(path)} = {json.dumps(access)}"
                           for path, access in sorted(profile["filesystem"].items()))
    result.append(f"{prefix}.filesystem={{ {filesystem} }}")
    return result


def reject_legacy_sandbox_layers(layers: list[dict]) -> None:
    """CLI default_permissions loses precedence to loaded sandbox_mode settings."""
    for layer in layers:
        config = layer.get("config")
        if not layer.get("disabledReason") and isinstance(config, dict) and config.get("sandbox_mode") is not None:
            raise RuntimeError("Loaded legacy sandbox_mode overrides role read restrictions; remove that setting before admission")


def validate_effective_permissions(config: dict, profile: dict) -> None:
    actual = config.get("permissions", {}).get(profile["name"], {})
    # config/read includes unset optional filesystem metadata as null.
    filesystem = {key: value for key, value in actual.get("filesystem", {}).items() if value is not None}
    if (config.get("default_permissions") != profile["name"]
            or filesystem != profile["filesystem"]
            or actual.get("extends") is not None or actual.get("workspace_roots")
            or actual.get("network", {}).get("enabled") is not False):
        raise RuntimeError("Effective role permission profile differs from the restricted configuration")
