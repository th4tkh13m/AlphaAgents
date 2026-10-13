"""Domain-neutral role instructions, quality anchors, and immutable rubric binding."""

import re
from dataclasses import replace

import pytest

from alpha_agents.tree_search.core.mcts.contracts import (
    ARTIFACT_RUBRIC_VERSION,
    CombinedScore,
    Objective,
    judge_aggregate,
)
from alpha_agents.tree_search.mutators.coding_agents.codex.roles import prompts


@pytest.mark.parametrize("role,marker", [
    ("proposer", "Assessment rubric: proposer-v1"),
    ("policy", "Assessment rubric: policy-v1"),
    ("implementer", "Assessment rubric: implementer-v1"),
    ("judge_code", "Assessment rubric: code-review-v1"),
    ("judge_execution", "Assessment rubric: execution-review-v1"),
    ("value", "Assessment rubric: value-v1"),
])
def test_each_rendered_role_integrates_its_rubric_and_preserves_supplied_semantics(tmp_path, role, marker):
    context = {"artifact": "artifact", "rubric": ARTIFACT_RUBRIC_VERSION,
               "canonical_evidence": {"snapshot_id": "canonical", "outcomes": {"case": True},
                                      "evidence": ["supplied-record"]}}
    request = prompts.RoleRequest(str(tmp_path), "request", role, "artifact", "model", "effort", context)
    rendered = request.render()
    assert marker in rendered
    assert "Static instructions and rubrics are domain-neutral" in rendered
    assert '"case":true' in rendered and '"supplied-record"' in rendered
    if role.startswith("judge_"):
        assert ARTIFACT_RUBRIC_VERSION in rendered
        assert "incoming plan" in rendered and "score=null" in rendered
        assert "0.5 means" in rendered
        assert "robustness" in rendered and "decisions" in rendered and "generalizable" in rendered
    else:
        assert "discounted continuing changes" in rendered


def test_all_instruction_assets_are_free_of_harness_domain_and_trace_layout_assumptions():
    forbidden = re.compile(r"\b(?:android(?:world)?|artemis|mobile|gui|adb|emulator|qwen|gemini|coordinates?)\b"
                           r"|data_engine\.db|pre_image_name|post_image_name", re.I)
    assets = [path for path in prompts.PROMPT_DIRECTORY.iterdir() if path.suffix in {".md", ".json"}]
    for path in assets:
        assert not forbidden.search(path.read_text()), path.name
    guidance = prompts.CONTEXT_GUIDANCE
    assert "discounted continuing" in guidance["utility_context"]["definition"]
    assert "stopping F minus" not in guidance["utility_context"]["definition"]
    assert "scratch-copy" in guidance["judge_reference_contract"]["sqlite"]
    assert "nonempty WAL" in guidance["judge_reference_contract"]["sqlite"]


def test_judge_rubric_defines_all_fifteen_dimension_anchors_and_distinguishes_unknown():
    common = prompts.JUDGE_COMMON
    rows = re.findall(r"^\| (0|0\.25|0\.5|0\.75|1) \|(.+)$", common, re.M)
    assert [score for score, _ in rows] == ["0", "0.25", "0.5", "0.75", "1"]
    for _, cells in rows:
        descriptions = [cell.strip() for cell in cells.split("|") if cell.strip()]
        assert len(descriptions) == 3
        assert all(len(description.split()) >= 10 for description in descriptions)
    dimensions = {key: {"score": .75, "rationale": "Direct bounded evidence", "confidence": "medium",
                         "evidence": ["supplied-record"]} for key in ("robustness", "decisions", "generalizable")}
    dimensions["generalizable"].update(score=None, unknown_reason="No supported variation evidence")
    aggregate, unknown = judge_aggregate(dimensions)
    assert aggregate == pytest.approx(2/3)
    assert unknown == ("generalizable",)
    assert "not multiplied into the score" in common


def test_quality_rubric_changes_are_bound_to_prompt_and_objective_identity(tmp_path, monkeypatch):
    req = prompts.RoleRequest(str(tmp_path), "request", "judge_code", "artifact", "model", "effort", {})
    old_library, old_prompt = req.library_hash, req.prompt_hash
    templates = dict(prompts.TEMPLATES)
    templates["judge_code"] += "\nChanged quality anchor.\n"
    monkeypatch.setattr(prompts, "TEMPLATES", templates)
    assert req.library_hash != old_library
    assert req.prompt_hash != old_prompt
    objective = Objective("subset", ARTIFACT_RUBRIC_VERSION, old_library)
    changed = replace(objective, rubric_version="different-rubric", judge_protocol_id=req.library_hash)
    parent = CombinedScore("parent", "u-parent", "j-parent", objective, .5, .5)
    child = CombinedScore("child", "u-child", "j-child", changed, .5, .5)
    with pytest.raises(ValueError, match="different frozen objectives"):
        parent.reward_to(child)


def test_adapter_evidence_can_carry_domain_semantics_without_static_prompt_assumptions(tmp_path):
    for semantics in ("numeric transformations with dimensional constraints", "record reconciliation with consistency constraints"):
        req = prompts.RoleRequest(str(tmp_path), "request", "proposer", "artifact", "model", "effort",
                                  {"supplied_semantics": semantics})
        assert semantics in req.render()
        assert semantics not in prompts.PROPOSER
        assert req.to_record()["context"]["supplied_semantics"] == semantics
    assert "Assessment rubric: plan-repair-v1" in prompts.PLAN_REPAIR_TEMPLATE
    assert set(prompts.TEMPLATES) == {"proposer", "policy", "implementer", "judge_code", "judge_execution", "value"}
