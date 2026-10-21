"""Deterministic, numerically complete forecasts for controller test ports."""

from alpha_agents.tree_search.core.mcts.value_forecast import FORMAT, RUBRIC


def forecast_payload(context, artifact_id, value=0, evidence="controller.py"):
    score = context["score"]
    weight = score["objective"]["judge_weight"]
    f = (1 - weight) * score["u"] + weight * score["j"]
    future = f + value
    claim = {"statement": "A hypothetical continuation produces the specified test-state change.",
             "evidence_basis": "speculative", "evidence": [evidence], "limitations": ["Deterministic test fixture."]}
    return {"forecast_version": FORMAT, "artifact_id": artifact_id, "status": "completed",
        **{key: context[key] for key in ("score_snapshot_id", "capacity_snapshot_id", "objective_id", "continuation_policy_id", "discount_factor")},
        "baseline": {"u": score["u"], "j": score["j"], "f": f, "judge_weight": weight},
        "rubric_assessments": {id: {"conclusion": f"Inspected deterministic conditions for {id}.",
            "claims": [dict(claim)], "assumptions": ["Deterministic continuation."], "limitations": [],
            "forecast_implications": "The fixture specifies an immediate change followed by constant utility."} for id in RUBRIC},
        "scenarios": [{"id": "S01", "description": "Deterministic immediate change.", "claims": [dict(claim)],
            "prerequisites": [], "policy_and_capacity": "Uses an available continuation transition.",
            "probability": 1, "probability_rationale": "Deterministic test port.",
            "trajectory": [{"transition_index": 0, "description": "Reach the test state.", "u": future,
                "j": future, "f": future, "delta_f": value, "discounted_delta": value, "evidence": [evidence]}],
            "tail": {"treatment": "constant_utility", "value": 0, "discounted_contribution": 0,
                "rationale": "The fixture assumes later stagnation, not structural termination.", "assumptions": [], "evidence": [evidence]},
            "discounted_return": value, "weighted_contribution": value}],
        "value": value, "rationale": "Expected change under the deterministic test continuation.",
        "uncertainty": "Fixture only; no empirical calibration claim.", "confidence": "medium",
        "sensitivity": [{"assumption": "The prescribed effect is obtained.", "alternative": "No utility effect.",
            "alternative_value": 0, "delta_from_main": -value, "rationale": "Conditional stagnation check.", "evidence": [evidence]}],
        "coverage": {"inspected": [evidence], "checks_run": [], "not_inspected": [], "unavailable": [],
                     "limitations": ["Deterministic test fixture."]}, "evidence": [evidence], "blockers": []}
