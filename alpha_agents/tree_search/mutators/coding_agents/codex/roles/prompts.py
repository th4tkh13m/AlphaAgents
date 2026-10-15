"""Full, versioned role prompts and strict role-scoped input envelopes.

Source guidance: official Codex prompting and Astra initiative/follow-through.
https://learn.chatgpt.com/docs/prompting
https://developers.openai.com/api/docs/guides/latest-model#prompting-best-practices

Templates are original framework-specific instructions, not copied model docs.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from alpha_agents.tree_search.core.mcts.contracts import canonical_json, fingerprint

PROMPT_VERSION = "fixed-puct-role-prompts-v22"

PROMPT_DIRECTORY = Path(__file__).with_name("prompts")


def _load_prompt(name: str) -> str:
    return (PROMPT_DIRECTORY / name).read_text(encoding="utf-8")

SHARED = _load_prompt("shared.md") + "\n\n" + _load_prompt("scratch.md")

PROPOSER = _load_prompt("proposer.md")
PLAN_REPAIR_TEMPLATE = _load_prompt("plan_repair.md")

POLICY = _load_prompt("policy.md")

IMPLEMENTER = _load_prompt("implementer.md")

JUDGE_COMMON = _load_prompt("judge_common.md")

JUDGE_CODE = JUDGE_COMMON + _load_prompt("judge_code.md")

JUDGE_EXECUTION = JUDGE_COMMON + _load_prompt("judge_execution.md")

VALUE = _load_prompt("value.md")

GOAL_OBJECTIVE_TEMPLATE = _load_prompt("goal_objective.md")
CONTEXT_GUIDANCE = json.loads(_load_prompt("context_guidance.json"))
SINGLE_TASK = _load_prompt("single_task.md")


def _decorate_context(role: str, supplied: dict) -> dict:
    context = dict(supplied)
    guidance = json.loads(json.dumps(CONTEXT_GUIDANCE))
    for name in ("verification_scope_contract", "novelty_admission_contract"):
        if name in context:
            context[name] = {**context[name], **guidance[name]}
    if not role.startswith("judge_"):
        context["utility_context"] = {**context.get("utility_context", {}), **guidance["utility_context"]}
        context["trajectory_interpretation_contract"] = {
            **guidance["trajectory_interpretation_contract"],
            "sqlite": guidance["judge_reference_contract"]["sqlite"],
        }
        references = guidance["evidence_reference_contract"]
        references["available_snapshot_aliases"] = [
            f"context:{key}" for key, value in sorted(supplied.items())
            if key.endswith("_id") and isinstance(value, str) and value]
        context["evidence_reference_contract"] = references
    if role == "judge_execution" and "snapshot_id" in context.get("canonical_evidence", {}):
        canonical = context["canonical_evidence"]
        context["canonical_evidence"] = {**canonical,
            "canonical_evidence_id": canonical["snapshot_id"],
            "reference_contract": guidance["judge_reference_contract"]}
    if role == "value" and "continuation_role_protocol" in context:
        protocol = context["continuation_role_protocol"]
        context["continuation_role_protocol"] = {**protocol, "implementation_verification": {
            **protocol["implementation_verification"],
            "unconfigured_meaning": guidance["unconfigured_verification_meaning"]}}
    return context


TEMPLATES = {"proposer": PROPOSER, "policy": POLICY, "implementer": IMPLEMENTER,
             "judge_code": JUDGE_CODE, "judge_execution": JUDGE_EXECUTION, "value": VALUE}


@dataclass(frozen=True)
class RoleRequest:
    run_id: str
    request_id: str
    role: str
    artifact_id: str
    model: str
    effort: str
    context: dict[str, Any]
    timeout_seconds: int = 7200
    prompt_version: str = PROMPT_VERSION

    def __post_init__(self):
        object.__setattr__(self, "context", _decorate_context(self.role, self.context))
        if self.role not in TEMPLATES or not all((self.run_id, self.request_id, self.artifact_id, self.model, self.effort)):
            raise ValueError("Role request requires known role and exact runtime identities")
        if self.request_id in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]+", self.request_id):
            raise ValueError("Request ID must be one safe filesystem component")
        if self.timeout_seconds != 7200 or isinstance(self.timeout_seconds, bool):
            raise ValueError("Every role invocation requires its full two-hour deadline")
        if self.prompt_version != PROMPT_VERSION:
            raise ValueError("Unsupported role prompt version")
        canonical_json(self.context)
        if self.role.startswith("judge_"):
            allowed = {"artifact", "canonical_evidence", "rubric", "code_findings", "coverage_policy"}
            if set(self.context) - allowed:
                raise ValueError("Judge context must be artifact-only canonical scoring evidence")
            def inspect(value):
                if isinstance(value, dict):
                    forbidden = {"plan", "plan_id", "incoming_plan", "parent", "parent_artifact", "diff",
                                 "implementer_narrative", "plan_alignment", "previous_judge_score", "targeted_checks"}
                    if set(value) & forbidden:
                        raise ValueError("Judge scoring context contains excluded comparison/alignment material")
                    for item in value.values():
                        inspect(item)
                elif isinstance(value, list):
                    for item in value:
                        inspect(item)
            inspect(self.context)

    @property
    def permission_mode(self) -> str:
        return "workspace-write" if self.role == "implementer" else "read-only"

    @property
    def collaboration_mode(self) -> str:
        return "plan" if self.role == "proposer" else "default"

    @property
    def execution_mode(self) -> str:
        return "goal" if self.role == "implementer" else "plan" if self.role == "proposer" else "turn"

    def render(self) -> str:
        envelope = {**asdict(self), "permission_mode": self.permission_mode,
                    "collaboration_mode": self.collaboration_mode, "execution_mode": self.execution_mode,
                    "scratch_directory": str(self.scratch_directory)}
        single = self.context.get("utility_context", {}).get("evaluation_task") or (
            len(self.context.get("canonical_evidence", {}).get("outcomes", {})) == 1)
        protocol = "\n\n" + SINGLE_TASK.strip() if single else ""
        return SHARED.strip() + "\n\n" + TEMPLATES[self.role].strip() + protocol + "\n\n## Exact invocation inputs\n```json\n" + canonical_json(envelope) + "\n```\n"

    def to_record(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def scratch_directory(self) -> Path:
        return Path(self.run_id).resolve() / "role_requests" / self.request_id / "scratch"

    @property
    def prompt_hash(self) -> str:
        return fingerprint({"rendered": self.render()})

    @property
    def library_hash(self) -> str:
        return fingerprint({"version": PROMPT_VERSION, "shared": SHARED, "templates": TEMPLATES,
                            "goal_objective": GOAL_OBJECTIVE_TEMPLATE, "context_guidance": CONTEXT_GUIDANCE,
                            "plan_repair": PLAN_REPAIR_TEMPLATE, "single_task": SINGLE_TASK})
