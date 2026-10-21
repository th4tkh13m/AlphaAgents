"""Value report arithmetic, evidence boundaries, and safe publication."""

import copy
from dataclasses import asdict

import pytest

from alpha_agents.tree_search.core.mcts.contracts import CombinedScore, Objective
from alpha_agents.tree_search.core.mcts.responses import validate_response
from alpha_agents.tree_search.core.mcts.value_forecast import (
    CONTRACT,
    validate_forecast,
)
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.prompts import (
    RoleRequest,
)
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.responses import (
    output_schema,
)
from alpha_agents.tree_search.tests.value_fixtures import forecast_payload


def context():
    objective = Objective("subset", "rubric", "review")
    score = CombinedScore("artifact", "u", "j", objective, .6, .4)
    return {"score": asdict(score), "score_snapshot_id": score.identity, "capacity_snapshot_id": "capacity",
            "objective_id": objective.identity, "continuation_policy_id": "policy", "discount_factor": .9,
            "value_report_contract": CONTRACT, "capacity": {"exhausted": False}}


def scenarios_payload():
    ctx = context()
    report = forecast_payload(ctx, "artifact")
    template = report["scenarios"][0]
    paths = [(.5, [.58, .68]), (.3, [.58]), (.2, [.53])]
    report["scenarios"] = []
    for index, (probability, states) in enumerate(paths):
        scenario = copy.deepcopy(template)
        scenario.update(id=f"S{index}", probability=probability, trajectory=[])
        previous = report["baseline"]["f"]
        for t, f in enumerate(states):
            delta = f - previous
            scenario["trajectory"].append({"transition_index": t, "description": "Conditional test transition.",
                "u": f, "j": f, "f": f, "delta_f": delta, "discounted_delta": .9 ** t * delta,
                "evidence": ["controller.py"]})
            previous = f
        scenario["discounted_return"] = sum(t["discounted_delta"] for t in scenario["trajectory"])
        scenario["weighted_contribution"] = probability * scenario["discounted_return"]
        report["scenarios"].append(scenario)
    report["value"] = sum(s["weighted_contribution"] for s in report["scenarios"])
    report["sensitivity"][0]["delta_from_main"] = -report["value"]
    return ctx, report


def test_three_scenario_example_reconstructs_probability_weighted_value():
    ctx, report = scenarios_payload()
    validate_forecast(report, ctx, "artifact")
    assert report["value"] == pytest.approx(.035)


@pytest.mark.parametrize("value", ["lower", -.1, 0, .1, "upper"])
def test_signed_values_and_baseline_bounds(value):
    ctx = context()
    f = .9 * ctx["score"]["u"] + .1 * ctx["score"]["j"]
    value = -f if value == "lower" else 1 - f if value == "upper" else value
    validate_forecast(forecast_payload(ctx, "artifact", value), ctx, "artifact")


def test_continued_search_tail_is_relative_to_final_state_and_discounted():
    ctx = context()
    report = forecast_payload(ctx, "artifact", .02)
    s = report["scenarios"][0]
    s["tail"].update(treatment="continued_search", value=.1, discounted_contribution=.09)
    s.update(discounted_return=.11, weighted_contribution=.11)
    report["value"] = .11
    report["sensitivity"][0]["delta_from_main"] = -.11
    validate_forecast(report, ctx, "artifact")
    s["tail"]["discounted_contribution"] = .1
    with pytest.raises(ValueError, match="tail discount"):
        validate_forecast(report, ctx, "artifact")


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(value=True),
    lambda p: p.update(value=float("nan")),
    lambda p: p.update(value=float("inf")),
    lambda p: p.update(value=.6),
    lambda p: p["baseline"].update(j=.8),
    lambda p: p["baseline"].update(judge_weight=.5),
    lambda p: p["rubric_assessments"].pop("V06"),
    lambda p: p["rubric_assessments"]["V01"].update(claims=[]),
    lambda p: p["scenarios"][0].update(probability=.8),
    lambda p: p["scenarios"][0].update(weighted_contribution=.1),
    lambda p: p["scenarios"][0].update(discounted_return=.1),
    lambda p: p["scenarios"][0]["trajectory"][0].update(transition_index=True),
    lambda p: p["scenarios"][0]["trajectory"][0].update(transition_index=1),
    lambda p: p["scenarios"][0]["trajectory"][0].update(f=.9),
    lambda p: p["scenarios"][0]["trajectory"][0].update(delta_f=.1),
    lambda p: p["scenarios"][0]["trajectory"][0].update(discounted_delta=.1),
    lambda p: p["scenarios"][0]["tail"].update(value=.1),
    lambda p: p["scenarios"][0].update(trajectory=[]),
    lambda p: p.update(blockers=["Required evidence unavailable."]),
    lambda p: p.update(confidence=None),
])
def test_malformed_or_inconsistent_completed_forecasts_are_rejected(mutation):
    ctx = context()
    report = forecast_payload(ctx, "artifact")
    mutation(report)
    with pytest.raises(ValueError):
        validate_forecast(report, ctx, "artifact")


def test_duplicate_scenario_ids_and_unsupported_tail_bounds():
    ctx, report = scenarios_payload()
    report["scenarios"][1]["id"] = report["scenarios"][0]["id"]
    with pytest.raises(ValueError, match="unique"):
        validate_forecast(report, ctx, "artifact")
    ctx, report = scenarios_payload()
    report["scenarios"][0]["tail"].update(treatment="continued_search", value=1)
    with pytest.raises(ValueError, match="tail.*bounds"):
        validate_forecast(report, ctx, "artifact")


def test_sensitivity_is_conditional_and_does_not_add_another_reward():
    ctx = context()
    report = forecast_payload(ctx, "artifact", .1)
    report["sensitivity"] = [{"assumption": "Implementation succeeds.", "alternative": "Stagnation.",
        "alternative_value": 0, "delta_from_main": -.1, "rationale": "Conditional downside check.", "evidence": ["controller.py"]}]
    validate_forecast(report, ctx, "artifact")
    report["sensitivity"][0]["delta_from_main"] = .1
    with pytest.raises(ValueError, match="sensitivity delta"):
        validate_forecast(report, ctx, "artifact")


def test_incomplete_report_cannot_publish_a_scalar_or_trigger_role_success(tmp_path):
    ctx = context()
    report = forecast_payload(ctx, "artifact")
    report.update(status="partial", value=None, confidence=None, scenarios=[], sensitivity=[], blockers=["Required trace inaccessible."])
    validate_forecast(report, ctx, "artifact")
    (tmp_path / "controller.py").write_text("value = 0\n")
    request = RoleRequest(str(tmp_path), "value", "value", "artifact", "model", "low", ctx)
    with pytest.raises(ValueError, match="incomplete"):
        validate_response(report, request, tmp_path)


def test_depth_cutoff_is_not_absorbing_but_structural_exhaustion_is():
    ctx = context()
    ctx["capacity"]["terminal"] = True
    validate_forecast(forecast_payload(ctx, "artifact", .1), ctx, "artifact")
    ctx["capacity"]["exhausted"] = True
    report = forecast_payload(ctx, "artifact")
    with pytest.raises(ValueError, match="absorbing"):
        validate_forecast(report, ctx, "artifact")
    report["scenarios"][0]["trajectory"] = []
    validate_forecast(report, ctx, "artifact")


def test_live_contract_binds_schema_ids_and_nested_evidence(tmp_path):
    ctx = context()
    (tmp_path / "controller.py").write_text("value = 0\n")
    request = RoleRequest(str(tmp_path), "value", "value", "artifact", "model", "low", ctx)
    schema = output_schema(request)
    assert schema["properties"]["forecast_version"]["enum"] == ["value-v2"]
    report = forecast_payload(ctx, "artifact")
    validate_response(report, request, tmp_path)
    report["scenarios"][0]["claims"][0]["evidence"] = ["/private/answers.json"]
    with pytest.raises(ValueError, match="out-of-scope"):
        validate_response(report, request, tmp_path)
    for field in ("artifact_id", "score_snapshot_id", "capacity_snapshot_id", "objective_id", "continuation_policy_id", "discount_factor"):
        report = forecast_payload(ctx, "artifact")
        report[field] = .5 if field == "discount_factor" else "other"
        with pytest.raises(ValueError, match="mismatch"):
            validate_forecast(report, ctx, "artifact")



def test_ancestry_report_citations_do_not_authorize_embedded_private_paths(tmp_path):
    ctx = context()
    ctx["prior_implementations"] = {"snapshot_id": "history", "implementations": [
        {"report_id": "report", "implementation": {"verification": [{"evidence": ["/private/answers.json"]}]}}]}
    (tmp_path / "controller.py").write_text("value = 0\n")
    request = RoleRequest(str(tmp_path), "value", "value", "artifact", "model", "low", ctx)
    report = forecast_payload(ctx, "artifact", evidence="report")
    validate_response(report, request, tmp_path)
    report["evidence"] = ["/private/answers.json"]
    with pytest.raises(ValueError, match="out-of-scope"):
        validate_response(report, request, tmp_path)


def test_ancestry_observation_ids_are_citable_without_transitive_path_access(tmp_path):
    ctx = context()
    ctx["prior_implementations"] = {"snapshot_id": "history", "implementations": [{
        "report_id": "report", "canonical_outcome": {"u_snapshot_id": "canonical-u", "j_snapshot_id": "canonical-j"},
        "implementation": {"check_requests": [{"snapshot_id": "diagnostic-snapshot", "artifact_id": "diagnostic-artifact"}],
                           "verification": [{"evidence": ["unprovided-id", "/private/answers.json"]}]}}]}
    (tmp_path / "controller.py").write_text("value = 0\n")
    request = RoleRequest(str(tmp_path), "value", "value", "artifact", "model", "low", ctx)
    report = forecast_payload(ctx, "artifact")
    for ref in ["canonical-u", "canonical-j", "diagnostic-snapshot", "diagnostic-artifact"]:
        report["scenarios"][0]["claims"][0]["evidence"] = [ref]
        validate_response(report, request, tmp_path)
    for ref in ["unprovided-id", "/private/answers.json"]:
        report["scenarios"][0]["claims"][0]["evidence"] = [ref]
        with pytest.raises(ValueError, match="out-of-scope"):
            validate_response(report, request, tmp_path)
