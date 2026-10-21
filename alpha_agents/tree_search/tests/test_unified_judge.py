"""Unified judge scoring, report integrity, and runtime boundary contracts."""

import copy

import pytest

from alpha_agents.tree_search.core.mcts.judge_review import (
    CRITERIA,
    RUBRIC_VERSION,
    criterion_aggregate,
    review_score,
    validate_review,
)
from alpha_agents.tree_search.core.mcts.responses import validate_response
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.prompts import (
    RoleRequest,
)
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.responses import (
    output_schema,
)
from alpha_agents.tree_search.tests.judge_fixtures import review_payload


@pytest.mark.parametrize("rating", range(5))
def test_normalization_endpoints_and_anchors(rating):
    payload = review_payload(score=rating)
    validate_review(payload)
    assert review_score(payload) == (rating / 4, ())


def test_individual_criterion_changes_have_finer_resolution():
    payload = review_payload()
    payload["dimensions"]["correctness"]["C01"]["score"] = 2
    assert review_score(payload)[0] == pytest.approx(119 / 160)


def test_unknown_and_na_are_excluded_without_implicit_points():
    payload = review_payload(score=4)
    group = payload["dimensions"]["correctness"]
    group["C01"].update(assessment="unknown", score=None, confidence=None, evidence=[], unknown_reason="Missing activation trace.")
    group["C02"].update(assessment="not_applicable", score=None, confidence=None, not_applicable_reason="No routing obligation at this interface.")
    payload["dimensions"]["reliability"]["R01"]["score"] = 0
    validate_review(payload)
    score, unknown = review_score(payload)
    assert score == pytest.approx(37 / 38)
    assert unknown == ("C01",)


def test_no_ratings_is_unavailable_but_can_be_an_honest_partial_report():
    payload = review_payload()
    for group in payload["dimensions"].values():
        for item in group.values():
            item.update(assessment="unknown", score=None, confidence=None, unknown_reason="Source unavailable.", evidence=[])
    with pytest.raises(ValueError, match="no rated criteria"):
        review_score(payload)
    payload.update(status="partial", blockers=["Required source inaccessible."])
    validate_review(payload)
    with pytest.raises(ValueError, match="Incomplete"):
        review_score(payload)


@pytest.mark.parametrize("score", [True, False, 2.0, .75, -1, 5, "3", float("nan"), float("inf")])
def test_malformed_criterion_scores_are_rejected(score):
    payload = review_payload()
    payload["dimensions"]["correctness"]["C01"]["score"] = score
    with pytest.raises(ValueError):
        review_score(payload)


@pytest.mark.parametrize("mutation", [
    lambda p: p["dimensions"].pop("correctness"),
    lambda p: p["dimensions"]["correctness"].pop("C01"),
    lambda p: p["dimensions"]["correctness"].update(C11={}),
    lambda p: p["dimensions"]["correctness"]["C01"].update(finding_ids=["missing"]),
    lambda p: p["dimensions"]["correctness"]["C01"].update(evidence=[]),
    lambda p: p["dimensions"]["correctness"]["C01"].update(unknown_reason="Not allowed on a rated criterion."),
    lambda p: p["integrity"].update(controller_flag="confirmed_manipulation"),
    lambda p: p.update(phase="execution"),
    lambda p: p.update(status="partial"),
])
def test_inconsistent_reports_are_rejected(mutation):
    payload = review_payload()
    mutation(payload)
    with pytest.raises(ValueError):
        validate_review(payload)


def confirmed_payload():
    payload = review_payload(score=4)
    payload["findings"] = [{"id": "F001", "kind": "defect", "title": "Direct modification of evaluator success state.",
        "severity": "critical", "confidence": "high", "evidence_basis": "source_supported",
        "source_evidence": ["controller.py"], "execution_evidence": [], "trigger": "Completion branch.",
        "mechanism": "Writes evaluator state instead of performing the required action.",
        "consequence": "Invalid success measurement.", "affected_criteria": ["G04", "G06"],
        "uncertainty": "Source establishes the prohibited write; activation not observed."}]
    payload["integrity"].update(manipulation_assessment="confirmed", controller_flag="confirmed_manipulation",
                                  finding_ids=["F001"])
    return payload


def test_confirmed_manipulation_has_independent_effective_score():
    payload = confirmed_payload()
    assert criterion_aggregate(payload["dimensions"])[0] == 1
    assert review_score(payload)[0] == 0
    payload["integrity"].update(manipulation_assessment="suspected", controller_flag="none")
    assert review_score(payload)[0] == 1


def test_confirmation_cannot_be_only_an_inferred_risk():
    payload = confirmed_payload()
    payload["findings"][0].update(kind="risk", evidence_basis="inferred")
    with pytest.raises(ValueError, match="established defect"):
        validate_review(payload)


def test_live_schema_and_validation_bind_exact_identity_and_evidence(tmp_path):
    (tmp_path / "controller.py").write_text("value = 1\n")
    request = RoleRequest(str(tmp_path), "review", "judge_review", "artifact", "model", "low",
        {"artifact": "artifact", "rubric": RUBRIC_VERSION,
         "canonical_evidence": {"snapshot_id": "canonical", "evidence": ["trace"]}})
    schema = output_schema(request)
    assert set(schema["properties"]["dimensions"]["properties"]) == set(CRITERIA)
    assert "phase" not in schema["properties"]
    assert schema["properties"]["rubric_version"]["enum"] == [RUBRIC_VERSION]
    payload = review_payload()
    validate_response(payload, request, tmp_path)
    bad = copy.deepcopy(payload)
    bad["dimensions"]["correctness"]["C01"]["evidence"] = ["/private/answers.json"]
    with pytest.raises(ValueError, match="out-of-scope"):
        validate_response(bad, request, tmp_path)
    for key in ("artifact_id", "canonical_evidence_id", "rubric_version"):
        bad = copy.deepcopy(payload)
        bad[key] = "different"
        with pytest.raises(ValueError):
            validate_response(bad, request, tmp_path)


def test_unified_judge_context_excludes_prior_review_and_parent(tmp_path):
    for key in ("code_findings", "parent", "incoming_plan", "previous_judge_score"):
        with pytest.raises(ValueError):
            RoleRequest(str(tmp_path), "review", "judge_review", "artifact", "model", "low", {key: "bias"})


@pytest.mark.parametrize("manipulation", [False, True])
def test_v3_root_and_child_publish_normalized_scores_and_integrity_reports(tmp_path, manipulation):
    from alpha_agents.tree_search.core.mcts.contracts import FixedPUCTConfig, Objective
    from alpha_agents.tree_search.core.mcts.coordinator import FixedPUCTCoordinator
    from alpha_agents.tree_search.core.mcts.ports import RoleSettings
    from alpha_agents.tree_search.core.mcts.reporting import score_breakdowns
    from alpha_agents.tree_search.tests.test_mcts_coordinator import Evaluator, Gateway

    source = tmp_path / "source"
    source.mkdir()
    (source / "controller.py").write_text("value = 0\n")
    evaluator = Evaluator()

    class ReviewingGateway(Gateway):
        def invoke(self, request, workspace):
            result = super().invoke(request, workspace)
            if request.role == "judge_review":
                assert any(artifact == request.artifact_id and stage == "s0" for artifact, stage, _ in evaluator.calls)
                assert "code_findings" not in request.context and "parent" not in request.context
                if manipulation:
                    result = confirmed_payload()
                    result.update(artifact_id=request.artifact_id,
                                  canonical_evidence_id=request.context["canonical_evidence"]["snapshot_id"])
            return result

    gateway = ReviewingGateway()
    config = FixedPUCTConfig(screen_gate="execution_valid", max_trials=1, max_depth=1,
                             max_new_attempts_per_trial=1, trial_workers=1)
    subset = {"tasks": [{"task": "a", "weight": {"display": 1.0}}]}
    search = FixedPUCTCoordinator(source, tmp_path / "run", config, Objective("subset", RUBRIC_VERSION, "review-v3"),
        gateway, evaluator, RoleSettings("model", "low"), ("screen",), {"a": 1.0}, ("a",), subset)
    try:
        result = search.run(stop_after_trials=1)
        assert result["errors"] == []
        reviews = [request for request in gateway.calls if request.role.startswith("judge_")]
        assert len(reviews) == 2 and all(request.role == "judge_review" for request in reviews)
        assert search.store.db.execute("SELECT count(*) FROM judge_snapshots").fetchone()[0] == 2
        for node in score_breakdowns(search.store, subset):
            assert node["j"] == (0 if manipulation else .75)
            assert node["judge_raw_score"] == (1 if manipulation else .75)
            assert node["judge_rated_criteria"] == 40
            assert sum(d["aggregate_contribution"] for d in node["judge_dimensions"].values()) == pytest.approx(node["j"])
        assert search.store.budget()["reserved"] == 0
        search.finalize()
        assert "Raw criterion score" in (search.directory / "FINAL_REPORT.md").read_text()
    finally:
        search.close()
