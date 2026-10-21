"""Process-isolated Codex gateway with persisted Goal execution for coding roles."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

from alpha_agents.tree_search.core.mcts.artifacts import register_runtime_workspace
from alpha_agents.tree_search.core.mcts.contracts import fingerprint
from alpha_agents.tree_search.core.mcts.ports import RoleAdapter
from alpha_agents.tree_search.core.mcts.responses import validate_response
from alpha_agents.tree_search.core.shared.storage import write_json
from alpha_agents.tree_search.infrastructure.process import execute
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.permissions import (
    permission_overrides,
    role_permissions,
    runtime_permissions,
)
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.prompts import (
    CONTEXT_GUIDANCE,
    RoleRequest,
)
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.responses import (
    output_schema,
    parse_json_response,
)
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.telemetry import (
    role_usage,
)


@dataclass(frozen=True)
class CodexRuntime:
    python: str
    codex_bin: str
    provider: str
    base_url: str
    auth_mode: str
    api_key_env: str | None = None
    verification_python: str | None = None

    def __post_init__(self):
        if not all((self.python, self.codex_bin, self.provider, self.base_url)):
            raise ValueError("Explicit Codex interpreter, binary, provider, and endpoint are required")
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]*", self.provider):
            raise ValueError("Invalid model-provider identifier")
        if urlparse(self.base_url).scheme not in {"http", "https"} or not urlparse(self.base_url).netloc:
            raise ValueError("Codex endpoint must be an explicit HTTP(S) URL")
        if self.auth_mode not in {"codex_login", "environment", "unauthenticated"}:
            raise ValueError("Select an explicit Codex authentication mode")
        if self.auth_mode == "environment" and (not self.api_key_env or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env)):
            raise ValueError("Environment authentication requires a variable name, never a secret value")

    def overrides(self) -> list[str]:
        # Codex reserves built-in identifiers such as 'openai'. Configure our
        # explicit endpoint under an owned name rather than overriding them.
        prefix = f"model_providers.{self.effective_provider}"
        values = {"approval_policy": "never", "model_provider": self.effective_provider, f"{prefix}.name": self.provider,
                  f"{prefix}.base_url": self.base_url, f"{prefix}.wire_api": "responses",
                  f"{prefix}.requires_openai_auth": self.auth_mode == "codex_login"}
        if self.auth_mode == "environment":
            values[f"{prefix}.env_key"] = self.api_key_env
        return [f"{key}={json.dumps(value)}" for key, value in values.items()]

    @property
    def effective_provider(self):
        return "alpha_agents_" + self.provider

    @property
    def record(self):
        record = asdict(self)
        if self.verification_python is None:
            record.pop("verification_python")
        return record

    def implementation_tools_context(self):
        if self.verification_python is None:
            return {}
        return {"trusted_verification_tools": {
            "command": shlex.join([self.verification_python, str(Path(__file__).with_name("verification.py").resolve())]),
            "purpose": CONTEXT_GUIDANCE["verification_tools_purpose"],
        }}

    @property
    def identity(self):
        return fingerprint({"runtime": self.record, "effective_provider": self.effective_provider,
                            "overrides": self.overrides()})




class CodexGateway(RoleAdapter):
    # Preserve historical production telemetry keys without naming providers in core.
    telemetry_name = "codex"
    supports_checks = True
    prepare_request = staticmethod(RoleRequest)
    observed_usage = staticmethod(role_usage)

    def __init__(self, runtime: CodexRuntime):
        self.runtime = runtime

    def enrich_context(self, role, context):
        context = dict(context)
        tools = self.runtime.implementation_tools_context() if self.runtime is not None else {}
        if role == "implementer":
            context.update(tools)
        if role == "value":
            context["continuation_role_protocol"] = {
                **context["continuation_role_protocol"],
                "proposer_mode": "native_goal", "implementer_mode": "native_goal",
                "implementation_verification": {"prepared_environment_configured": bool(tools), **tools},
            }
        return context

    def invoke_with_checks(self, request, workspace, service):
        from alpha_agents.tree_search.core.mcts.targeted_checks import CheckMonitor

        monitor = CheckMonitor(service)
        try:
            result = self.invoke(request, workspace)
        finally:
            # No new admissions after native Goal termination. Already admitted
            # device work drains independently without extending its Goal clock.
            monitor.close()
        if result.get("status") == "completed":
            service.validate_handoff(result)
        # A partial Goal may stop before it receives a draining diagnostic.
        # Retain that engineering status and its original unverified handoff;
        # the independent request ledger remains the diagnostic authority.
        return result

    def preflight(self, workspace: Path | None = None) -> dict:
        """Verify capabilities without generating a model turn or a goal."""
        if self.runtime.auth_mode == "environment" and not os.environ.get(self.runtime.api_key_env):
            raise RuntimeError(f"Missing configured Codex credential environment variable {self.runtime.api_key_env}")
        for path in (self.runtime.python, self.runtime.codex_bin):
            if not Path(path).is_file() or not os.access(path, os.X_OK):
                raise RuntimeError(f"Configured executable is missing or not executable: {path}")
        version = subprocess.run([self.runtime.codex_bin, "--version"], capture_output=True, text=True, timeout=30, check=True).stdout.strip()
        with tempfile.TemporaryDirectory(prefix="alpha-codex-schema-") as tmp:
            subprocess.run([self.runtime.codex_bin, "app-server", "generate-json-schema", "--experimental", "--out", tmp],
                           capture_output=True, text=True, timeout=30, check=True)
            files = list(Path(tmp).rglob("TurnStartParams.json"))
            if len(files) != 1:
                raise RuntimeError("Configured Codex runtime lacks the turn-start schema")
            thread_files = list(Path(tmp).rglob("ThreadStartParams.json"))
            if not thread_files or "permissions" not in json.loads(thread_files[0].read_text()).get("properties", {}):
                raise RuntimeError("Configured Codex runtime lacks named thread permission profiles")
            if "permissions" not in json.loads(files[0].read_text()).get("properties", {}):
                raise RuntimeError("Configured Codex runtime lacks named turn permission profiles")
            if not list(Path(tmp).rglob("ThreadGoalSetParams.json")):
                raise RuntimeError("Configured Codex runtime lacks native persisted Goal support")
            write_json(Path(tmp) / "runtime.json", self.runtime.record)
            env = os.environ.copy()
            package_root = str(Path(__file__).resolve().parents[6])
            permission_workspace = str(workspace.resolve()) if workspace is not None else package_root
            env["PYTHONPATH"] = package_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            # Query capabilities and initialize one ephemeral sandbox thread.
            # No goal or model turn is created by this preflight.
            report, _ = execute([self.runtime.python, "-m", "alpha_agents.tree_search.mutators.coding_agents.codex.worker", "role",
                                 "--preflight", "--runtime", str(Path(tmp) / "runtime.json"),
                                 "--workspace", permission_workspace, "--output", tmp],
                                Path(package_root), Path(tmp) / "preflight-process", 30, env)
            if report["timed_out"] or report["returncode"]:
                errors = (Path(tmp) / "preflight-process" / "stderr.txt").read_text()
                raise RuntimeError(f"Native Codex capability preflight failed: {errors[-2000:]}")
            capabilities = json.loads((Path(tmp) / "preflight.json").read_text())
        return {"runtime": self.runtime.record, "version": version, "proposer_mode": "native_goal", "native_goal": True,
                "capabilities": capabilities}

    def invoke(self, request: RoleRequest, workspace: Path) -> dict:
        invocation_started = time.monotonic()
        # Each artifact workspace can contain different project configuration.
        # Verify that workspace, rather than trusting only coordinator startup.
        self.preflight(workspace)
        directory = Path(request.run_id) / "role_requests" / request.request_id
        directory.mkdir(parents=True, exist_ok=True)
        if request.role == "implementer":
            register_runtime_workspace(Path(request.run_id), workspace, request.artifact_id)
        profile = runtime_permissions(role_permissions(request, workspace), self.runtime.codex_bin,
                                      self.runtime.verification_python if request.role == "implementer" else None)
        write_json(directory / "permissions.json", profile)
        stdin_path = None
        if request.role in {"proposer", "implementer"}:
            write_json(directory / "native_request.json", {**asdict(request), "_prompt_binding": {
                "version": "native-prompt-binding-v1", "library_hash": request.library_hash,
                "prompt_hash": request.prompt_hash}})
            write_json(directory / "runtime.json", self.runtime.record)
            command = [self.runtime.python, "-m", "alpha_agents.tree_search.mutators.coding_agents.codex.worker", "role",
                       "--request", str(directory / "native_request.json"), "--runtime", str(directory / "runtime.json"),
                       "--workspace", str(workspace), "--output", str(directory)]
        else:
            write_json(directory / "schema.json", output_schema(request))
            (directory / "input.md").write_text(request.render())
            # `exec` has its own configuration parser. Overrides before the
            # subcommand are dropped by the pinned CLI, including permissions,
            # approval policy and provider. Keep every override after `exec`.
            command = [self.runtime.codex_bin, "exec"]
            for override in [*self.runtime.overrides(), *permission_overrides(profile)]:
                command.extend(["-c", override])
            command.extend(["--model", request.model,
                            "-c", f"model_reasoning_effort={json.dumps(request.effort)}", "--json",
                            "--skip-git-repo-check", "--output-schema", str(directory / "schema.json"),
                            "--output-last-message", str(directory / "final.txt"), "-"])
            stdin_path = directory / "input.md"
        # The request's full timeout applies once, including all native Goal
        # continuation and app-server recovery; no inner loop resets this timer.
        env = os.environ.copy()
        package_root = str(Path(__file__).resolve().parents[6])
        env["PYTHONPATH"] = package_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        remaining = request.timeout_seconds - (time.monotonic() - invocation_started)
        if remaining <= 0:
            raise TimeoutError("Original role invocation deadline exhausted before launch")
        process, _ = execute(command, workspace, directory / "process", remaining, env, stdin_path=stdin_path)
        if process["timed_out"]:
            raise TimeoutError("Codex invocation reached its original two-hour deadline; partial evidence retained")
        if process["returncode"]:
            raise RuntimeError(f"Codex {request.execution_mode} worker failed; see {directory / 'process'}")
        if request.role in {"proposer", "implementer"}:
            result = json.loads((directory / "result.json").read_text())
        else:
            result = parse_json_response((directory / "final.txt").read_text())
        return validate_response(result, request, workspace)
