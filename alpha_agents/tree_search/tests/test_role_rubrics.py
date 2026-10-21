"""Domain-neutral role instructions, quality anchors, and immutable rubric binding."""

import re
from dataclasses import replace

import pytest

from alpha_agents.tree_search.core.mcts.contracts import (
    ARTIFACT_RUBRIC_VERSION,
    CombinedScore,
    Objective,
)
from alpha_agents.tree_search.mutators.coding_agents.codex.roles import prompts


@pytest.mark.parametrize("role,marker", [
    ("proposer", "Required technical design document"),
    ("policy", "Assessment rubric: policy-v1"),
    ("implementer", "Completion criteria"),
    ("judge_review", "Engineering review workflow"),
    ("value", "Assessment rubric: value-v2"),
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
    if role == "judge_review":
        assert "forty" in rendered and "generalizability_integrity" in rendered
        assert "unknown_reason" in rendered and "confirmed_manipulation" in rendered
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


@pytest.mark.parametrize("role", ["judge_code", "judge_execution"])
def test_removed_judge_roles_cannot_be_dispatched(tmp_path, role):
    with pytest.raises(ValueError, match="known role"):
        prompts.RoleRequest(str(tmp_path), "request", role, "artifact", "model", "effort", {})


def test_quality_rubric_changes_are_bound_to_prompt_and_objective_identity(tmp_path, monkeypatch):
    req = prompts.RoleRequest(str(tmp_path), "request", "judge_review", "artifact", "model", "effort", {})
    old_library, old_prompt = req.library_hash, req.prompt_hash
    templates = dict(prompts.TEMPLATES)
    templates["judge_review"] += "\nChanged quality anchor.\n"
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
    assert "Technical design document requirements" in prompts.PLAN_REPAIR_TEMPLATE
    assert set(prompts.TEMPLATES) == {"proposer", "policy", "implementer", "judge_review", "value"}


def test_structured_value_forecast_is_complete_and_active(tmp_path, monkeypatch):
    text = prompts.VALUE_FORECAST
    assert re.findall(r"^## (\d+)\.", text, re.M) == [str(i) for i in range(1, 11)]
    assert re.findall(r"^### (V\d{2}) —", text, re.M) == [f"V{i:02}" for i in range(1, 7)]
    for requirement in ("observed", "source_supported", "speculative", "scenario_return",
                        "weighted_contribution", "continued_search", "gamma^n * V_tail",
                        "alternative_value", "curated implementation summaries", "null otherwise"):
        assert requirement in text
    request = prompts.RoleRequest(str(tmp_path), "value", "value", "artifact", "model", "low", {})
    assert "Assessment rubric: value-v2" in request.render()
    assert "Assessment rubric: value-v1" not in request.render()
    old_library = request.library_hash
    templates = dict(prompts.TEMPLATES)
    templates["value"] = text + "\nChanged forecast contract.\n"
    monkeypatch.setattr(prompts, "TEMPLATES", templates)
    assert request.library_hash != old_library
