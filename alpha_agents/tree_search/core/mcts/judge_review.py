"""Unified review schema, semantic validation, and frozen criterion scoring."""

from __future__ import annotations

import math

RUBRIC_VERSION = "artifact-quality-rubric-v3"
CRITERIA = {name: tuple(f"{prefix}{i:02}" for i in range(1, 11)) for name, prefix in (
    ("correctness", "C"), ("reliability", "R"), ("maintainability", "M"),
    ("generalizability_integrity", "G"))}
SCORING_CONTRACT = {
    "version": "criterion-normalization-v1",
    "criteria": CRITERIA,
    "criterion_range": [0, 4],
    "aggregation": "sum(rated_scores)/(4*rated_count)",
    "unknown": "excluded_and_reported",
    "not_applicable": "excluded_and_reported",
    "no_rated_criteria": "unavailable",
    "confirmed_manipulation": "effective_j_zero_raw_j_retained",
}


def review_schema(evidence=None):
    """Closed provider schema; cross-field constraints are validated separately."""
    string = {"type": "string"}
    strings = {"type": "array", "items": string}
    evidence = evidence or strings

    def obj(fields):
        return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}

    def enum(*values):
        return {"type": "string", "enum": list(values)}

    nullable = {"type": ["string", "null"]}
    confidence = enum("high", "medium", "low")
    criterion = obj({"assessment": enum("rated", "unknown", "not_applicable"),
        "score": {"type": ["integer", "null"], "minimum": 0, "maximum": 4},
        "rationale": string, "confidence": {"type": ["string", "null"], "enum": ["high", "medium", "low", None]},
        "evidence": evidence, "finding_ids": strings, "unknown_reason": nullable, "not_applicable_reason": nullable})
    finding = obj({"id": string, "kind": enum("strength", "defect", "risk"), "title": string,
        "severity": enum("critical", "major", "minor", "informational"), "confidence": confidence,
        "evidence_basis": enum("observed", "source_supported", "inferred"),
        "source_evidence": evidence, "execution_evidence": evidence, "trigger": string,
        "mechanism": string, "consequence": string, "affected_criteria": strings, "uncertainty": string})
    return obj({"artifact_id": string, "canonical_evidence_id": nullable, "rubric_version": string,
        "status": enum("completed", "partial", "failed"), "summary": string,
        "findings": {"type": "array", "items": finding},
        "dimensions": obj({name: obj({id: criterion for id in ids}) for name, ids in CRITERIA.items()}),
        "integrity": obj({"generalization_assessment": enum("supported_within_reviewed_scope", "limited_transfer", "insufficient_evidence"),
            "manipulation_assessment": enum("no_evidence_identified", "suspected", "confirmed", "insufficient_evidence"),
            "rationale": string, "finding_ids": strings, "evidence": evidence, "limitations": strings,
            "controller_flag": enum("confirmed_manipulation", "none")}),
        "coverage": obj({"source_inspected": evidence, "execution_inspected": evidence, "tests_inspected": evidence,
            "checks_run": {"type": "array", "items": obj({"command": string, "scope": string, "result": string, "limitations": strings})},
            "not_inspected": {"type": "array", "items": obj({"reference": string, "reason": string, "affected_criteria": strings})},
            "limitations": strings}), "blockers": strings})


def _shape(value, schema):
    """Validate our small JSON-schema subset without a provider dependency."""
    types = schema["type"]
    types = types if isinstance(types, list) else [types]
    matches = {"null": value is None, "string": isinstance(value, str),
        "integer": type(value) is int, "object": isinstance(value, dict), "array": isinstance(value, list)}
    if not any(matches.get(kind, False) for kind in types):
        raise ValueError("Invalid unified review field type")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("Invalid unified review enum")
    if isinstance(value, str) and not value.strip():
        raise ValueError("Unified review strings must be nonempty")
    if type(value) is int and not schema.get("minimum", value) <= value <= schema.get("maximum", value):
        raise ValueError("Criterion score must be an integer from 0 through 4")
    if isinstance(value, dict):
        if set(value) != set(schema["properties"]):
            raise ValueError("Unified review fields do not match the closed contract")
        for key, item in value.items():
            _shape(item, schema["properties"][key])
    if isinstance(value, list):
        for item in value:
            _shape(item, schema["items"])


def criterion_aggregate(dimensions, *, allow_empty=False):
    """Equal weight per rated criterion; unknown/NA earn no invented points."""
    if not isinstance(dimensions, dict) or set(dimensions) != set(CRITERIA):
        raise ValueError("Unified review requires all four dimensions")
    scores, unknown = [], []
    for name, ids in CRITERIA.items():
        group = dimensions[name]
        if not isinstance(group, dict) or set(group) != set(ids):
            raise ValueError("Unified review requires exactly forty criteria")
        for id, item in group.items():
            if not isinstance(item, dict) or not item.get("rationale"):
                raise ValueError("Criterion requires a rationale")
            state, score = item.get("assessment"), item.get("score")
            if state == "rated":
                if type(score) is not int or not 0 <= score <= 4 or not item.get("evidence"):
                    raise ValueError("Rated criterion requires integer 0..4 and evidence")
                if item.get("confidence") not in {"high", "medium", "low"}:
                    raise ValueError("Rated criterion requires qualitative confidence")
                if item.get("unknown_reason") is not None or item.get("not_applicable_reason") is not None:
                    raise ValueError("Rated criterion cannot have a null-state reason")
                scores.append(score)
            elif state in {"unknown", "not_applicable"}:
                reason = "unknown_reason" if state == "unknown" else "not_applicable_reason"
                other = "not_applicable_reason" if state == "unknown" else "unknown_reason"
                if score is not None or "score" not in item or item.get("confidence") is not None or not item.get(reason) or item.get(other) is not None:
                    raise ValueError("Unknown/NA criterion requires null score/confidence and its specific reason")
                if state == "not_applicable" and not item.get("evidence"):
                    raise ValueError("Non-applicability requires evidence")
                if state == "unknown":
                    unknown.append(id)
            else:
                raise ValueError("Invalid criterion assessment")
    if not scores and allow_empty:
        return None, tuple(unknown)
    if not scores:
        raise ValueError("Judge review has no rated criteria")
    return math.fsum(scores) / (4 * len(scores)), tuple(unknown)


def validate_review(payload, evidence_check=None):
    """Validate both persisted and live reports, including integrity consistency."""
    _shape(payload, review_schema())
    all_ids = {id for group in CRITERIA.values() for id in group}
    findings = {}

    def refs(values):
        if values and evidence_check:
            evidence_check(values)

    for finding in payload["findings"]:
        id = finding["id"]
        if id in findings:
            raise ValueError("Duplicate finding ID")
        findings[id] = finding
        if not finding["affected_criteria"] or not set(finding["affected_criteria"]) <= all_ids:
            raise ValueError("Finding references invalid criteria")
        if not finding["source_evidence"] and not finding["execution_evidence"]:
            raise ValueError("Finding requires inspected evidence")
        if finding["kind"] == "strength" and finding["severity"] != "informational":
            raise ValueError("Strength findings use informational severity")
        refs(finding["source_evidence"])
        refs(finding["execution_evidence"])
    for group in payload["dimensions"].values():
        for item in group.values():
            if not set(item["finding_ids"]) <= findings.keys():
                raise ValueError("Criterion references an unknown finding")
            refs(item["evidence"])
    integrity = payload["integrity"]
    if not set(integrity["finding_ids"]) <= findings.keys():
        raise ValueError("Integrity references an unknown finding")
    confirmed = integrity["manipulation_assessment"] == "confirmed"
    if (integrity["controller_flag"] == "confirmed_manipulation") != confirmed:
        raise ValueError("Confirmed manipulation and controller flag disagree")
    if integrity["manipulation_assessment"] in {"suspected", "confirmed"}:
        if not integrity["evidence"] or not integrity["finding_ids"]:
            raise ValueError("Manipulation assessment requires findings and evidence")
        if not any(findings[id]["kind"] in {"defect", "risk"} and
                   any(criterion.startswith("G") for criterion in findings[id]["affected_criteria"])
                   for id in integrity["finding_ids"]):
            raise ValueError("Manipulation assessment requires an integrity finding")
    if confirmed and not any(findings[id]["kind"] == "defect" and
                             findings[id]["evidence_basis"] != "inferred" and
                             any(criterion.startswith("G") for criterion in findings[id]["affected_criteria"])
                             for id in integrity["finding_ids"]):
        raise ValueError("Confirmed manipulation requires an established defect")
    refs(integrity["evidence"])
    coverage = payload["coverage"]
    for key in ("source_inspected", "execution_inspected", "tests_inspected"):
        refs(coverage[key])
    for item in coverage["not_inspected"]:
        if not set(item["affected_criteria"]) <= all_ids:
            raise ValueError("Coverage references invalid criteria")
        refs([item["reference"]])
    if payload["status"] in {"partial", "failed"} and not payload["blockers"]:
        raise ValueError("Incomplete review requires blockers")
    criterion_aggregate(payload["dimensions"], allow_empty=payload["status"] != "completed")
    if payload["status"] == "completed":
        if payload["blockers"]:
            raise ValueError("Completed review cannot have completion blockers")


def review_score(payload):
    """Effective J; confirmed manipulation cannot be hidden by high averages."""
    if payload.get("rubric_version") != RUBRIC_VERSION and set(payload.get("dimensions", {})) != set(CRITERIA):
        # Historical three-dimension snapshots retain their original semantics.
        from alpha_agents.tree_search.core.mcts.contracts import judge_aggregate
        return judge_aggregate(payload["dimensions"])
    validate_review(payload)
    if payload["status"] != "completed":
        raise ValueError("Incomplete judge review is unavailable for scoring")
    score, unknown = criterion_aggregate(payload["dimensions"])
    return (0.0 if payload["integrity"]["controller_flag"] == "confirmed_manipulation" else score), unknown
