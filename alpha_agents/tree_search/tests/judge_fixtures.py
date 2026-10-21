"""Complete deterministic review reports for orchestration tests."""

from alpha_agents.tree_search.core.mcts.judge_review import CRITERIA, RUBRIC_VERSION


def review_payload(artifact="artifact", canonical="canonical", rubric=RUBRIC_VERSION, score=3, evidence="controller.py"):
    return {"artifact_id": artifact, "canonical_evidence_id": canonical, "rubric_version": rubric,
        "status": "completed", "summary": "Inspected deterministic source and canonical behavior.", "findings": [],
        "dimensions": {name: {id: {"assessment": "rated", "score": score,
            "rationale": f"Inspected behavior relevant to {id}.", "confidence": "medium", "evidence": [evidence],
            "finding_ids": [], "unknown_reason": None, "not_applicable_reason": None} for id in ids}
            for name, ids in CRITERIA.items()},
        "integrity": {"generalization_assessment": "insufficient_evidence", "manipulation_assessment": "no_evidence_identified",
            "rationale": "No manipulation identified within inspected deterministic fixture.", "finding_ids": [],
            "evidence": [evidence], "limitations": ["Deterministic local fixture."], "controller_flag": "none"},
        "coverage": {"source_inspected": [evidence], "execution_inspected": [], "tests_inspected": [],
            "checks_run": [], "not_inspected": [], "limitations": ["Local fixture only."]}, "blockers": []}
