"""Single-plan handoff, sequential expansion, and implementation JSON lineage."""
import copy
import json

import pytest

from alpha_agents.tree_search.core.mcts.design_documents import FORMAT, SECTIONS
from alpha_agents.tree_search.core.mcts.implementation_history import (
    implementation_history,
)
from alpha_agents.tree_search.core.mcts.responses import validate_response
from alpha_agents.tree_search.core.mcts.storage import TreeStore
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.prompts import (
    RoleRequest,
)
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.responses import (
    parse_native_plan,
)
from alpha_agents.tree_search.tests.test_mcts import (  # shared deterministic harness fixtures
    child_transition,
    draft,
    expand,
)

pytest_plugins = ["alpha_agents.tree_search.tests.test_mcts"]

def summary(slot=1):
    return {"id": f"proposal-{slot}", "title": f"Intervention {slot}",
            "summary": "Change a concrete mechanism based on observed behavior.",
            "intervention": {"target": "controller.py", "operation": f"operation {slot}",
                             "conditions": "On the motivating condition", "mechanism": f"mechanism {slot}"},
            "expected_benefit": "Improve task completion when the condition occurs.",
            "validation": "Exercise the branch locally and inspect execution evidence.",
            "risks_and_limitations": "Execution benefit remains unmeasured.", "evidence": ["controller.py"]}


def document(meta):
    return (f'# {meta["title"]}\n\n```json\n{json.dumps(meta)}\n```\n\n'
            + '\n\n'.join(f'## {heading}\nConcrete inspected design content.' for heading in SECTIONS))


def request(tmp_path, **context):
    return RoleRequest(str(tmp_path), "proposal-request", "proposer", "parent", "model", "high",
                       {"plan_format": FORMAT, **context})


def test_single_document_roundtrip_and_exact_evidence(tmp_path):
    (tmp_path / "controller.py").write_text("value = 0\n")
    req = request(tmp_path)
    result = parse_native_plan(document(summary()), req)
    assert validate_response(result, req, tmp_path) == result
    assert len(result["plans"]) == 1
    assert result["plans"][0]["document"] == document(summary())
    assert "archive_review" not in result
    assert result["plans"][0]["summary"] == summary()["summary"]


@pytest.mark.parametrize("mutation", [
    lambda text: text.replace('## Validation', '## Other section'),
    lambda text: text.replace('## Validation\nConcrete inspected design content.', '## Validation\n'),
    lambda text: text.replace('# Intervention 1\n', '# Wrong title\n'),
    lambda text: text + '\n```json\n{}\n```\n',
])
def test_invalid_design_document_is_rejected(tmp_path, mutation):
    with pytest.raises(ValueError):
        parse_native_plan(mutation(document(summary())), request(tmp_path))


@pytest.mark.parametrize("field,value", [("summary", ""), ("validation", []),
                                          ("evidence", []), ("intervention", {})])
def test_invalid_summary_is_rejected(tmp_path, field, value):
    meta = {**summary(), field: value}
    with pytest.raises(ValueError):
        parse_native_plan(document(meta), request(tmp_path))


def test_duplicate_existing_plan_is_rejected(tmp_path):
    (tmp_path / "controller.py").write_text("value = 0\n")
    req = request(tmp_path, existing_plans=[summary()])
    result = parse_native_plan(document(summary()), req)
    with pytest.raises(ValueError, match="duplicates"):
        validate_response(result, req, tmp_path)


def test_three_calls_receive_incremental_summaries_and_publish_full_documents(engine_factory):
    tree, executor, gateway, evaluator = engine_factory()
    original = gateway.invoke
    calls = []

    def invoke(req, workspace):
        if req.role != "proposer":
            return original(req, workspace)
        calls.append(req)
        return parse_native_plan(document(summary(req.context["proposal_slot"])), req)

    gateway.invoke = invoke
    executor._expand("root", "worker")
    assert [req.context["proposal_slot"] for req in calls] == [1, 2, 3]
    assert [len(req.context["existing_plans"]) for req in calls] == [0, 1, 2]
    for req in calls:
        assert req.context["required_plan_count"] == 1
        assert req.context["prior_implementations"]["implementations"] == []
        assert "plan_archive" not in req.context
        assert "discount_factor" not in req.context
        assert "novelty_admission_contract" not in req.context
    plans = list(tree.db.execute("SELECT * FROM plans ORDER BY slot"))
    assert len(plans) == 3
    for slot, plan in enumerate(plans, 1):
        meta = json.loads(plan["metadata"])
        assert meta["plan_format"] == FORMAT
        assert meta["existing_plan_ids"] == [prior["id"] for prior in plans[:slot-1]]
        assert plan["document"] == document(summary(slot))
    assert tree.budget()["reserved"] == 0
    assert tree.db.execute("SELECT count(*) FROM invocations WHERE purpose='proposal'").fetchone()[0] == 3
    tree.close()


def test_recovery_reuses_earlier_slots_without_charging_new_calls(engine_factory, monkeypatch):
    from alpha_agents.tree_search.core.mcts.controller import AssessmentPending
    tree, executor, gateway, evaluator = engine_factory()
    original = gateway.invoke
    fail = True
    calls = []

    def invoke(req, workspace):
        if req.role != "proposer":
            return original(req, workspace)
        slot = req.context["proposal_slot"]
        calls.append(slot)
        if slot == 2 and fail:
            raise RuntimeError("temporary outage")
        return parse_native_plan(document(summary(slot)), req)

    gateway.invoke = invoke
    with pytest.raises(AssessmentPending):
        executor._expand("root", "worker")
    assert not tree._row("nodes", "root")["closed"]
    assert tree.db.execute("SELECT count(*) FROM plans").fetchone()[0] == 0
    fail = False
    # Commit on a peer while the cached response is being inspected. Keeping
    # the invocation scan cursor live would produce SQLITE_BUSY_SNAPSHOT when
    # the controller subsequently records reuse.
    from alpha_agents.tree_search.core.mcts import controller
    validate = controller.validate_response
    peer = TreeStore(tree.directory, tree.config, tree.objective)
    committed = []
    def interleaved(result, req, workspace):
        if req.request_id == "validate-before-launch" and not committed:
            peer.db.execute("INSERT INTO meta VALUES('peer-during-cache-read','committed')")
            committed.append(True)
        return validate(result, req, workspace)
    monkeypatch.setattr(controller, "validate_response", interleaved)
    executor._expand("root", "worker")
    assert committed
    peer.close()
    assert calls == [1, 2, 2, 2, 3]  # slot 1 is reused from its validated persisted response
    assert tree.db.execute("SELECT count(*) FROM role_reuses WHERE purpose='proposal'").fetchone()[0] == 1
    tree.close()


def test_lineage_is_curated_from_json_excludes_root_and_siblings_and_survives_reload(store):
    expand(store)
    trial = store.reserve_trial()
    selection = store.select_and_reserve(trial)
    report = {"artifact_id": "root-artifact", "plan_id": selection["plan"],
              "attempt_id": selection["attempt"], "status": "completed",
              "changes": [{"file": "controller.py", "reason": "Parent implementation"}],
              "verification": [{"ran": True, "status": "completed", "evidence": ["controller.py"]}],
              "check_requests": [], "evidence": ["controller.py"], "blockers": [],
              "private_extra": "not part of the curated summary"}
    store.save_implementation_report(selection["attempt"], report)
    child_transition(store, trial, selection, id="child")
    expand(store, "child", [draft("child-plan")])
    next_selection = store.select_and_reserve(trial)
    child_transition(store, trial, next_selection, id="grandchild")
    # A separate attempt under the root is unrelated to the selected lineage.
    other_trial = store.reserve_trial()
    sibling_selection = store.select_and_reserve(other_trial)
    child_transition(store, other_trial, sibling_selection, id="sibling")
    history = implementation_history(store, "grandchild")
    entries = history["implementations"]
    assert [entry["node_id"] for entry in entries] == ["child", "grandchild"]
    assert entries[0]["implementation"]["changes"] == report["changes"]
    assert "private_extra" not in entries[0]["implementation"]
    assert entries[0]["canonical_outcome"]["u"] == .7
    assert entries[1]["availability"] == "unavailable"
    changed = copy.deepcopy(report)
    changed["changes"] = "different content"
    with pytest.raises(ValueError, match="immutable"):
        store.save_implementation_report(selection["attempt"], changed)
    reloaded = TreeStore(store.directory, store.config, store.objective)
    assert implementation_history(reloaded, "grandchild") == history
    assert implementation_history(reloaded, "root")["implementations"] == []
    reloaded.close()


def test_implemented_child_persists_actual_report(engine_factory):
    tree, executor, gateway, evaluator = engine_factory()
    original = gateway.invoke
    def invoke(req, workspace):
        if req.role == "proposer":
            return parse_native_plan(document(summary(req.context["proposal_slot"])), req)
        return original(req, workspace)
    gateway.invoke = invoke
    trial = tree.reserve_trial()
    executor.run(trial)
    child = tree.db.execute("SELECT child FROM attempts WHERE child IS NOT NULL").fetchone()[0]
    history = implementation_history(tree, child)
    assert history["implementations"][0]["implementation"]["status"] == "completed"
    assert history["implementations"][0]["implementation"]["changes"] == ["Increment deterministic harness state"]
    tree.close()


def test_summary_paths_do_not_grant_access_to_other_role_requests(tmp_path):
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.permissions import (
        role_permissions,
    )
    workspace = tmp_path / "harness"
    workspace.mkdir()
    private = tmp_path / "role_requests" / "other" / "response.json"
    private.parent.mkdir(parents=True)
    private.write_text('{"private": true}')
    req = request(tmp_path, prior_implementations={"implementation": {"changes": str(private)}},
                  existing_plans=[{**summary(), "evidence": [str(private)]}])
    rules = role_permissions(req, workspace)["filesystem"]
    assert str(private) not in rules
    assert str(private.parent) not in rules


def test_mismatched_json_index_and_document_are_rejected(tmp_path):
    (tmp_path / "controller.py").write_text("value = 0\n")
    req = request(tmp_path)
    response = parse_native_plan(document(summary()), req)
    response["plans"][0]["summary"] = "A different summary that was not in the frozen document."
    with pytest.raises(ValueError, match="differs"):
        validate_response(response, req, tmp_path)


def test_existing_plan_section_is_added_only_to_later_proposer_calls(tmp_path):
    first = request(tmp_path, existing_plans=[])
    second = request(tmp_path, existing_plans=[summary()])
    assert "## Existing plans for this invocation" not in first.render()
    assert "## Existing plans for this invocation" in second.render()
    assert "Your intervention must not overlap" in second.render()


def test_absolute_source_citations_rebase_with_embedded_json_and_survive_new_workspace(tmp_path):
    from alpha_agents.tree_search.core.mcts.responses import canonical_code_evidence
    original_workspace = tmp_path / "original"
    original_workspace.mkdir()
    source = original_workspace / "controller.py"
    source.write_text("value = 0\n")
    meta = {**summary(), "evidence": [f"code:{source}:1"]}
    req = request(tmp_path)
    raw = parse_native_plan(document(meta), req)
    canonical, mappings = canonical_code_evidence(raw, original_workspace)
    assert raw["plans"][0]["evidence"] == [f"code:{source}:1"]
    assert canonical["plans"][0]["evidence"] == ["code:controller.py:1"]
    assert canonical["native_document"] == canonical["plans"][0]["document"]
    assert mappings == [{"original": f"code:{source}:1", "canonical": "code:controller.py:1"}]
    fresh_workspace = tmp_path / "fresh"
    fresh_workspace.mkdir()
    (fresh_workspace / "controller.py").write_text(source.read_text())
    assert validate_response(canonical, req, fresh_workspace) == canonical
