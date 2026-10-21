"""Structured continuing-value forecasts and auditable numerical validation."""

from __future__ import annotations

import math

from alpha_agents.tree_search.core.mcts.contracts import finite, unit

FORMAT = "value-v2"
RUBRIC = tuple(f"V{i:02}" for i in range(1, 7))
CONTRACT = {"version": FORMAT, "arithmetic_tolerance": 1e-9,
            "aggregation": "sum(probability*(discounted_transitions+discounted_tail))",
            "tail": "remaining net change relative to final explicit state",
            "incomplete": "unavailable_without_backup"}


def forecast_schema():
    """Closed schema shared by model output and provider-independent validation."""
    string = {"type": "string"}
    strings = {"type": "array", "items": string}
    number = {"type": "number"}
    probability = {"type": "number", "minimum": 0, "maximum": 1}

    def obj(fields, nullable=False):
        return {"type": ["object", "null"] if nullable else "object", "properties": fields,
                "required": list(fields), "additionalProperties": False}

    def array(item):
        return {"type": "array", "items": item}

    claim = obj({"statement": string, "evidence_basis": {"type": "string", "enum": ["observed", "source_supported", "speculative"]},
                 "evidence": strings, "limitations": strings})
    assessment = obj({"conclusion": string, "claims": array(claim), "assumptions": strings,
                      "limitations": strings, "forecast_implications": string})
    transition = obj({"transition_index": {"type": "integer", "minimum": 0}, "description": string,
        "u": probability, "j": probability, "f": probability, "delta_f": number,
        "discounted_delta": number, "evidence": strings})
    tail = obj({"treatment": {"type": "string", "enum": ["constant_utility", "continued_search"]},
        "value": number, "discounted_contribution": number, "rationale": string, "assumptions": strings, "evidence": strings})
    scenario = obj({"id": string, "description": string, "claims": array(claim), "prerequisites": strings,
        "policy_and_capacity": string, "probability": probability, "probability_rationale": string,
        "trajectory": array(transition), "tail": tail, "discounted_return": number, "weighted_contribution": number})
    sensitivity = obj({"assumption": string, "alternative": string, "alternative_value": number,
                       "delta_from_main": number, "rationale": string, "evidence": strings})
    return obj({"forecast_version": {"type": "string", "enum": [FORMAT]}, "artifact_id": string,
        "status": {"type": "string", "enum": ["completed", "partial", "failed"]},
        **{key: string for key in ("score_snapshot_id", "capacity_snapshot_id", "objective_id", "continuation_policy_id")},
        "discount_factor": number,
        "baseline": obj({key: probability for key in ("u", "j", "f", "judge_weight")}, nullable=True),
        "rubric_assessments": obj({id: assessment for id in RUBRIC}), "scenarios": array(scenario),
        "value": {"type": ["number", "null"]}, "rationale": string, "uncertainty": string,
        "confidence": {"type": ["string", "null"], "enum": ["high", "medium", "low", None]},
        "sensitivity": array(sensitivity), "coverage": obj({"inspected": strings,
            "checks_run": array(obj({"command": string, "scope": string, "result": string, "limitations": strings})),
            "not_inspected": array(obj({"reference": string, "reason": string, "forecast_impact": string})),
            "unavailable": array(obj({"reference": string, "operation": string, "observed_error": string, "forecast_impact": string})),
            "limitations": strings}), "evidence": strings, "blockers": strings})


def _shape(value, schema, location="forecast"):
    types = schema["type"]
    types = types if isinstance(types, list) else [types]
    matches = {"null": value is None, "string": isinstance(value, str), "integer": type(value) is int,
               "number": type(value) in {int, float}, "object": isinstance(value, dict), "array": isinstance(value, list)}
    if not any(matches[kind] for kind in types):
        raise ValueError(f"{location}: invalid type")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{location}: invalid enum")
    if value is None:
        return
    if isinstance(value, str) and not value.strip():
        raise ValueError(f"{location}: empty text")
    if type(value) in {int, float}:
        finite(value, location)
        if not schema.get("minimum", value) <= value <= schema.get("maximum", value):
            raise ValueError(f"{location}: numeric bound violated")
    if isinstance(value, dict):
        if set(value) != set(schema["properties"]):
            raise ValueError(f"{location}: missing or extra fields")
        for key, child in value.items():
            _shape(child, schema["properties"][key], f"{location}.{key}")
    if isinstance(value, list):
        for index, child in enumerate(value):
            _shape(child, schema["items"], f"{location}[{index}]")


def validate_forecast(report, context, artifact_id, evidence_check=None):
    """Reject inconsistent model arithmetic before persistence or backup.

    Arithmetic checks establish consistency, not the empirical calibration of
    subjective probabilities, causal claims, or assumed constant-utility tails.
    """
    _shape(report, forecast_schema())
    if report["artifact_id"] != artifact_id:
        raise ValueError("Value artifact identity mismatch")
    for key in ("score_snapshot_id", "capacity_snapshot_id", "objective_id", "continuation_policy_id", "discount_factor"):
        if report[key] != context[key]:
            raise ValueError(f"Value {key} mismatch")
    declared = context.get("value_report_contract")
    if declared is not None and declared != CONTRACT:
        raise ValueError("Unsupported value report contract")
    score = context["score"]
    weight = unit(score["objective"]["judge_weight"], "judge weight")
    current_u, current_j = unit(score["u"], "baseline U"), unit(score["j"], "baseline J")
    current_f = (1 - weight) * current_u + weight * current_j
    gamma = finite(report["discount_factor"], "discount")
    if not 0 < gamma < 1:
        raise ValueError("Invalid value discount factor")

    def equal(actual, expected, name):
        if not math.isclose(actual, expected, abs_tol=CONTRACT["arithmetic_tolerance"], rel_tol=0):
            raise ValueError(f"Value {name} arithmetic mismatch")

    def bounded(value, baseline, name):
        if not -baseline <= value <= 1 - baseline:
            raise ValueError(f"Value {name} is outside baseline-relative bounds")

    baseline = report["baseline"]
    if baseline is not None:
        for key, expected in {"u": current_u, "j": current_j, "f": current_f, "judge_weight": weight}.items():
            equal(baseline[key], expected, f"baseline {key}")
    completed = report["status"] == "completed"
    if completed:
        if baseline is None or not report["scenarios"] or not report["evidence"] or report["blockers"] or report["confidence"] is None:
            raise ValueError("Completed value requires baseline, scenarios, evidence, confidence and no blockers")
        if report["value"] is None:
            raise ValueError("Completed value requires a finite scalar")
    elif report["value"] is not None or report["confidence"] is not None or not report["blockers"]:
        raise ValueError("Incomplete value requires null value/confidence and concrete blockers")

    def evidence_visit(value):
        if isinstance(value, dict):
            if "evidence_basis" in value and not value["evidence"]:
                raise ValueError("Forecast claims require supporting evidence")
            for key, child in value.items():
                if key in {"evidence", "inspected"} and isinstance(child, list) and child and evidence_check:
                    evidence_check(child)
                elif key not in {"not_inspected", "unavailable"}:
                    evidence_visit(child)
        elif isinstance(value, list):
            for child in value:
                evidence_visit(child)
    evidence_visit(report)
    if completed and any(not item["claims"] for item in report["rubric_assessments"].values()):
        raise ValueError("Completed value requires claims for all six rubric assessments")
    ids, probabilities, contributions = set(), [], []
    exhausted = context.get("capacity", {}).get("exhausted", False)
    if completed and not exhausted and not report["sensitivity"]:
        raise ValueError("Completed nonterminal forecast requires sensitivity analysis")
    for scenario in report["scenarios"]:
        if scenario["id"] in ids or not scenario["claims"]:
            raise ValueError("Scenarios require unique IDs and evidence-backed claims")
        ids.add(scenario["id"])
        probabilities.append(scenario["probability"])
        previous_f = current_f
        rewards = []
        for t, transition in enumerate(scenario["trajectory"]):
            if transition["transition_index"] != t:
                raise ValueError("Value transition indices must be consecutive from zero")
            expected_f = (1 - weight) * transition["u"] + weight * transition["j"]
            equal(transition["f"], expected_f, "transition F")
            delta = expected_f - previous_f
            equal(transition["delta_f"], delta, "transition delta")
            discounted = gamma ** t * delta
            equal(transition["discounted_delta"], discounted, "transition discount")
            rewards.append(discounted)
            previous_f = expected_f
        tail = scenario["tail"]
        if not scenario["trajectory"] and not exhausted:
            raise ValueError("Nonterminal scenario requires an explicit transition")
        if exhausted and (scenario["trajectory"] or tail["treatment"] != "constant_utility"):
            raise ValueError("Exhausted continuation must be absorbing")
        bounded(tail["value"], previous_f, "tail")
        if tail["treatment"] == "constant_utility" and tail["value"] != 0:
            raise ValueError("Constant utility requires zero tail value")
        tail_discount = gamma ** len(rewards) * tail["value"]
        equal(tail["discounted_contribution"], tail_discount, "tail discount")
        expected_return = math.fsum(rewards) + tail_discount
        bounded(expected_return, current_f, "scenario return")
        equal(scenario["discounted_return"], expected_return, "scenario return")
        contribution = scenario["probability"] * expected_return
        equal(scenario["weighted_contribution"], contribution, "scenario contribution")
        contributions.append(contribution)
    if completed or report["scenarios"]:
        equal(math.fsum(probabilities), 1, "probability sum")
    if completed:
        equal(report["value"], math.fsum(contributions), "final aggregation")
        bounded(report["value"], current_f, "forecast")
        if exhausted and report["value"] != 0:
            raise ValueError("Exhausted continuation has zero value")
    for case in report["sensitivity"]:
        if not completed:
            raise ValueError("Incomplete value cannot give sensitivity relative to an unavailable scalar")
        bounded(case["alternative_value"], current_f, "sensitivity")
        equal(case["delta_from_main"], case["alternative_value"] - report["value"], "sensitivity delta")
    return report
