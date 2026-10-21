"""Proposer document parsing and assessment JSON schema construction."""
from __future__ import annotations

import json
import re

from alpha_agents.tree_search.core.mcts.responses import file_reference_catalog
from alpha_agents.tree_search.core.mcts.role_contracts import RoleRequest


def parse_json_response(text: str) -> dict:
    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("Role output must be one JSON object")
    return result


def _json_metadata(section: str) -> dict:
    blocks = re.findall(r"^```json\s*\n(.*?)^```\s*$", section, re.MULTILINE | re.DOTALL | re.IGNORECASE)
    if len(blocks) != 1:
        raise ValueError("Each native plan section needs exactly one JSON metadata block")
    return parse_json_response(blocks[0])


def parse_native_plan(text: str, request: RoleRequest) -> dict:
    """Parse a complete Goal document or a historical native Plan item."""
    if request.role != "proposer" or request.execution_mode not in {"goal", "plan"}:
        raise ValueError("Plan document output belongs only to the proposer")
    from alpha_agents.tree_search.core.mcts.design_documents import (
        FORMAT,
        parse_design_document,
    )
    if request.context.get("plan_format") == FORMAT:
        metadata = parse_design_document(text)
        return {"artifact_id": request.artifact_id, "status": "completed", "plan_format": FORMAT,
                "plans": [{**metadata, "document": text}], "native_document": text}
    sections = []
    fence = None
    start = None
    heading = None
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        match = re.match(r"^(`{3,}|~{3,})", stripped)
        if match:
            if fence is None:
                fence = match.group(1)[0]
            elif stripped.startswith(fence * 3):
                fence = None
        if fence is None and line.startswith("## "):
            if start is not None:
                sections.append((heading, text[start:offset]))
            heading, start = stripped[3:].strip(), offset
        offset += len(line)
    if fence is not None:
        raise ValueError("Native plan contains an unclosed code fence")
    if start is not None:
        sections.append((heading, text[start:]))
    if not sections or sections[0][0] != "Archive review" or sections[-1][0] != "Diversity audit":
        raise ValueError("Native plan must begin with Archive review and end with Diversity audit")
    if len({title for title, _ in sections}) != len(sections):
        raise ValueError("Duplicate native plan section")
    archive = _json_metadata(sections[0][1])
    plans = []
    for title, section in sections[1:-1]:
        if not title.startswith("Candidate ") or not title.removeprefix("Candidate ").strip():
            raise ValueError("Unexpected native plan section")
        metadata = _json_metadata(section)
        candidate_id = title.removeprefix("Candidate ").strip()
        inline_id = re.fullmatch(r"(`+)([^`\n]+)\1", candidate_id)
        if inline_id:
            candidate_id = inline_id.group(2)
        if metadata.get("id") != candidate_id:
            raise ValueError("Candidate heading and metadata IDs do not agree")
        plans.append({**metadata, "document": section})
    audit = sections[-1][1].removeprefix("## Diversity audit").strip()
    if not audit:
        raise ValueError("Pairwise diversity audit cannot be empty")
    return {"artifact_id": request.artifact_id, "status": "completed", "plans": plans,
            "archive_review": archive, "zero_reason": archive.get("zero_reason"),
            "diversity_audit": audit, "native_document": text}


def output_schema(request: RoleRequest) -> dict:
    """Closed JSON schemas for assessment turns, not the native plan document."""
    string = {"type": "string"}
    strings = {"type": "array", "items": string}
    def obj(properties):
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
    artifact = {"type": "string", "enum": [request.artifact_id]}
    def identity(key):
        value = request.context.get(key)
        return {"type": "string", "enum": [value]} if isinstance(value, str) else string
    if request.role == "policy":
        priors = obj({p["id"]: {"type": "number", "minimum": 0} for p in request.context["plans"]})
        return obj({"artifact_id": artifact, "evidence_snapshot_id": identity("evidence_snapshot_id"), "plan_ids": strings,
                    "priors": priors, "comparisons": string, "uncertainty": string, "evidence": strings})
    if request.role == "value":
        from alpha_agents.tree_search.core.mcts.value_forecast import forecast_schema
        schema = forecast_schema()
        schema["properties"]["artifact_id"] = artifact
        for key in ("score_snapshot_id", "capacity_snapshot_id", "objective_id", "continuation_policy_id"):
            schema["properties"][key] = identity(key)
        schema["properties"]["discount_factor"] = {"type": "number", "enum": [request.context["discount_factor"]]}
        return schema
    if request.role.startswith("judge_"):
        catalog = file_reference_catalog(request)
        definitions = {}
        evidence = strings
        if catalog:
            definitions["file_reference"] = {"type": "string", "enum": sorted(catalog)}
            evidence = {"type": "array", "items": {"anyOf": [
                {"$ref": "#/$defs/file_reference"},
                {"type": "string", "pattern": r"^(?:code:)?(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.[A-Za-z0-9_-]+(?::[0-9]+(?::[0-9]+)?)?$"},
                {"type": "string", "enum": [request.context["canonical_evidence"]["snapshot_id"]]}]}}
        if request.role == "judge_review":
            from alpha_agents.tree_search.core.mcts.judge_review import review_schema
            schema = review_schema(evidence)
            schema["properties"]["artifact_id"] = artifact
            schema["properties"]["rubric_version"] = {"type": "string", "enum": [request.context["rubric"]]}
            schema["properties"]["canonical_evidence_id"] = {"type": "string", "enum": [request.context["canonical_evidence"]["snapshot_id"]]}
            if definitions:
                schema["$defs"] = definitions
            return schema
    raise ValueError("Native Plan/Goal execution does not use an assessment turn schema")
