"""Acceptance tests for the fixed-PUCT foundation, independent of live devices."""

from __future__ import annotations

import copy
import json
import math
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from alpha_agents.tree_search.bridges.androidworld.prepare_subset import prepare
from alpha_agents.tree_search.core.mcts.artifacts import ArtifactStore, manifest
from alpha_agents.tree_search.core.mcts.contracts import (
    CombinedScore,
    FixedPUCTConfig,
    Objective,
    judge_aggregate,
    reverse_returns,
    validate_value,
)
from alpha_agents.tree_search.core.mcts.policy import Edge, prepare_priors, select_edge
from alpha_agents.tree_search.core.mcts.ports import EvaluationAdapter, RoleAdapter
from alpha_agents.tree_search.core.mcts.storage import (
    CapacityError,
    OwnershipError,
    TreeStore,
)
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.prompts import (
    RoleRequest,
)
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.telemetry import (
    role_usage,
)

OBJECTIVE = Objective("s0", "rubric", "judge-protocol")
REPOSITORY = Path(__file__).resolve().parents[3]


def test_judge_orders_failed_tasks_and_preserves_sealed_identity_after_reload():
    from dataclasses import asdict

    from alpha_agents.tree_search.core.mcts.contracts import canonical_json
    from alpha_agents.tree_search.core.mcts.controller import (
        StageEvidence,
        canonical_judge_context,
    )

    tasks = ("success-z", "failure-z", "success-a", "failure-a")
    stage = StageEvidence("artifact", "s0", dict(zip(tasks, [True, False, True, False])),
                          tuple(f"root/{task}" for task in tasks))
    sealed = canonical_json(asdict(stage))
    data = json.loads(sealed)
    reloaded = StageEvidence(data["artifact_id"], data["stage"], data["outcomes"], tuple(data["evidence"]))
    context = canonical_judge_context(reloaded, tasks)
    expected = ["failure-a", "failure-z", "success-a", "success-z"]
    assert [row["task"] for row in context["task_review_order"]] == expected
    assert context["evidence"] == [f"root/{task}" for task in expected]
    assert all(row["evidence"] == [f"root/{row['task']}"] for row in context["task_review_order"])
    assert context["snapshot_id"] == stage.identity == reloaded.identity
    assert context["outcomes"] == stage.outcomes
    assert canonical_json(asdict(reloaded)) == sealed


def test_judge_shared_bundle_does_not_invent_task_specific_roots():
    from alpha_agents.tree_search.core.mcts.controller import (
        StageEvidence,
        canonical_judge_context,
    )

    stage = StageEvidence("artifact", "s0", {"pass": True, "fail": False}, ("shared-trace",))
    context = canonical_judge_context(stage, ("pass", "fail"))
    assert context["evidence"] == ["shared-trace"]
    assert context["task_review_order"] == [
        {"task": "fail", "success": False, "evidence": ["shared-trace"]},
        {"task": "pass", "success": True, "evidence": ["shared-trace"]},
    ]
    with pytest.raises(ValueError, match="Exactly one"):
        canonical_judge_context(stage, ("pass", "missing"))


def test_judge_file_catalog_preserves_exact_execution_paths_and_sealed_identity(tmp_path):
    from dataclasses import asdict

    from alpha_agents.tree_search.core.mcts.contracts import canonical_json
    from alpha_agents.tree_search.core.mcts.controller import (
        StageEvidence,
        canonical_judge_context,
    )
    from alpha_agents.tree_search.core.mcts.responses import _evidence
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.prompts import (
        RoleRequest,
    )

    evidence = tmp_path / "tasks" / ("a" * 64) / ("b" * 32) / "evidence"
    database = evidence / "runtime" / "data_engine.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"sealed database fixture")
    ledger = evidence / "runtime" / "exact-session" / "check_ledger.jsonl"
    ledger.parent.mkdir()
    ledger.write_text('{}\n')
    stage = StageEvidence("artifact", "s0", {"task": False}, (str(evidence),))
    sealed = canonical_json(asdict(stage))
    context = canonical_judge_context(stage, ("task",))
    catalog = context["file_catalog"][0]
    assert catalog["evidence_root_path"] == str(evidence)
    assert {item["absolute_file"] for item in catalog["files"]} == {str(database), str(ledger)}
    assert all(item["size"] > 0 and len(item["sha256"]) == 64 for item in catalog["files"])
    assert canonical_json(asdict(stage)) == sealed
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = RoleRequest(str(tmp_path), "request", "judge_execution", "artifact", "gpt-6-luna", "low",
                          {"canonical_evidence": context})
    _evidence([str(database), str(ledger)], request, workspace)
    shortened = str(database).replace("b" * 32, "b" * 20)
    with pytest.raises(ValueError, match="out-of-scope"):
        _evidence([shortened], request, workspace)
    ledger.unlink()
    with pytest.raises(ValueError, match="out-of-scope"):
        _evidence([str(ledger)], request, workspace)


def score(artifact, u=.6, j=.4, objective=OBJECTIVE):
    return CombinedScore(artifact, f"u:{artifact}", f"j:{artifact}", objective, u, j)


def draft(id="plan-a", mechanism="bounded recovery"):
    return {"id": id, "document": "## Evidence\nInspect failed GUI verification.\n## Intervention\nImplement bounded recovery.\n",
            "intervention": {"target": "controller", "operation": "add recovery", "conditions": "verified failure",
                             "parameters": {"retries": 2}, "scope": ["controller.py"], "mechanism": mechanism},
            "novelty": "A different executable recovery condition, not a renamed hypothesis.",
            "diversity_audit": "Compared mechanisms rather than titles.", "evidence": ["screen/failure"],
            "closest_plan_ids": []}


def expand(store, node="root", drafts=None):
    drafts = [draft()] if drafts is None else drafts
    op = store.claim_operation(node, "expansion", "worker", f"evidence:{node}")
    catalog = [r[0] for r in store.db.execute("SELECT id FROM plans")]
    store.publish_plans(node, "worker", op["token"], drafts,
                        {d["id"]: 1 for d in drafts},
                        {"inspected_plan_ids": catalog, "catalog_plan_ids": catalog, "gaps": []},
                        zero_reason="No further supported interventions" if not drafts else None)


@pytest.fixture
def store(tmp_path):
    s = TreeStore(tmp_path / "run", FixedPUCTConfig(screen_gate="execution_valid"), OBJECTIVE)
    s.add_root("root", score("root-artifact"))
    yield s
    s.close()


def child_transition(store, trial, selection, id="child", u=.7, j=.8):
    store.register_child(selection["attempt"], id, f"artifact:{id}")
    store.publish_score(selection["attempt"], score(f"artifact:{id}", u, j, store.objective))
    store.append_transition(trial, selection["position"])


@pytest.mark.parametrize("kwargs", [
    {"screen_gate": "guess"}, {"max_trials": 31}, {"max_depth": True},
    {"c_puct": float("nan")}, {"prior_floor": 0}, {"codex_run_timeout_seconds": 600},
    {"trial_workers": 10}, {"device_limit": 10}, {"max_plans": 2}, {"max_implementations": 2},
    {"discount_factor": 0}, {"discount_factor": 1}, {"discount_factor": True},
    {"discount_factor": -.1}, {"discount_factor": float("nan")}, {"discount_factor": float("inf")},
])
def test_config_rejects_invalid_and_out_of_protocol_defaults(kwargs):
    args = {"screen_gate": "execution_valid", **kwargs}
    with pytest.raises(ValueError):
        FixedPUCTConfig(**args)


def test_nine_worker_experiment_preserves_branching_and_role_deadlines():
    config = FixedPUCTConfig(screen_gate="execution_valid", trial_workers=9, device_limit=9)
    assert config.trial_workers == config.device_limit == 9
    assert config.max_plans == config.max_implementations == 3
    assert config.role_timeout_seconds == 7200


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), -.1, 1.1])
def test_scores_are_real_bounded_numbers(bad):
    with pytest.raises(ValueError):
        score("a", bad)


def test_combined_reward_and_original_return_example():
    reward = score("parent").reward_to(score("child", .7, .8))
    assert reward.total == pytest.approx(.13)
    assert reward.androidworld == pytest.approx(.09)
    assert reward.judge == pytest.approx(.04)
    assert score("parent").reward_to(score("parent")).total == 0
    objective = replace(OBJECTIVE, judge_weight=0)
    nodes = [score(str(i), u, .5, objective) for i, u in enumerate([.6, .65, .62, .7])]
    returns = reverse_returns([a.reward_to(b) for a, b in zip(nodes, nodes[1:])], .04, .9)
    assert [r["total"] for r in returns] == pytest.approx([.11696, .0744, .116])
    assert sum(r.total for r in [nodes[0].reward_to(nodes[1]), nodes[1].reward_to(nodes[2])]) == pytest.approx(.02)
    with pytest.raises(ValueError, match="objectives"):
        score("a").reward_to(score("b", objective=objective))


def test_value_bounds_are_combined_utility_not_android_only():
    s = score("a")
    assert s.f == pytest.approx(.58)
    assert validate_value(s, -.58) == -.58
    for value in [-.59, .43, float("nan"), True]:
        with pytest.raises(ValueError):
            validate_value(s, value)
    assert validate_value(s, .1) == .1


@pytest.mark.parametrize("gamma", [.1, .5, .9, .99])
def test_discounted_returns_preserve_bounds_and_separate_all_contributions(gamma):
    nodes = [score(str(i), u, j) for i, (u, j) in enumerate([(0, .2), (.8, .6), (.1, .3), (.7, .9)])]
    rewards = [a.reward_to(b) for a, b in zip(nodes, nodes[1:])]
    prediction = -.2
    returns = reverse_returns(rewards, prediction, gamma)
    expected = prediction
    for index in reversed(range(len(rewards))):
        expected = rewards[index].total + gamma * expected
        row = returns[index]
        assert row["total"] == pytest.approx(expected)
        assert row["predicted"] == pytest.approx(gamma ** (len(rewards) - index) * prediction)
        assert row["androidworld"] == pytest.approx(sum(gamma ** (j-index) * rewards[j].evaluation
                                                       for j in range(index, len(rewards))))
        assert row["judge"] == pytest.approx(sum(gamma ** (j-index) * rewards[j].judge
                                                for j in range(index, len(rewards))))
        assert row["total"] == pytest.approx(row["androidworld"] + row["judge"] + row["predicted"])
        assert -nodes[index].f <= row["total"] <= 1-nodes[index].f


@pytest.mark.parametrize("key,bad", [("discount_factor", .8), ("continuation_policy_id", "other-policy")])
def test_persisted_value_rejects_a_different_discount_or_policy(store, key, bad):
    expand(store)
    trial = store.reserve_trial()
    selection = store.select_and_reserve(trial)
    child_transition(store, trial, selection)
    cap = store.snapshot_frontier(trial)
    with pytest.raises(ValueError, match=f"Value {key} mismatch"):
        store.persist_value(trial, .1, cap, {key: bad})
    assert store.db.execute("SELECT count(*) FROM values_assessed").fetchone()[0] == 0


@pytest.mark.parametrize("incompatibility", ["discount", "prediction-version", "backup-version", "legacy"])
def test_incompatible_return_contract_can_be_inspected_but_never_resumed(tmp_path, incompatibility):
    config = FixedPUCTConfig(screen_gate="execution_valid")
    s = TreeStore(tmp_path, config, OBJECTIVE)
    s.add_root("root", score("root-artifact"))
    expand(s)
    trial = s.reserve_trial()
    selected = s.select_and_reserve(trial)
    child_transition(s, trial, selected)
    cap = s.snapshot_frontier(trial)
    vid = s.persist_value(trial, .1, cap, {"test": True})
    s.backup(trial, vid)
    frozen = json.loads(s.db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()[0])
    if incompatibility == "discount":
        frozen["config"]["discount_factor"] = .8
    elif incompatibility == "legacy":
        frozen["config"].pop("discount_factor")
        frozen["config"]["value_horizon"] = 1
        frozen.pop("prediction_version")
        frozen.pop("backup_version")
    else:
        frozen[incompatibility.replace("-", "_")] = "older-contract"
    encoded = json.dumps(frozen, sort_keys=True, separators=(",", ":"))
    s.db.execute("UPDATE meta SET value=? WHERE key='identity'", (encoded,))
    statistics = [tuple(row) for row in s.db.execute("SELECT id,n,w FROM plans ORDER BY id")]
    backup = s.db.execute("SELECT payload FROM backups WHERE trial=?", (trial,)).fetchone()[0]
    s.close()
    inspected = TreeStore.readonly(tmp_path)
    try:
        status = inspected.status()
        assert status["backups"] == 1
        assert status["retained_performance"]["best_s0"]["artifact_id"] == "artifact:child"
        assert status["return_contract"]["discount_factor"] == frozen["config"].get("discount_factor")
    finally:
        inspected.close()
    with pytest.raises(ValueError, match="identical frozen"):
        TreeStore(tmp_path, config, OBJECTIVE)
    connection = sqlite3.connect(tmp_path / "tree.sqlite3")
    try:
        assert connection.execute("SELECT value FROM meta WHERE key='identity'").fetchone()[0] == encoded
        assert connection.execute("SELECT id,n,w FROM plans ORDER BY id").fetchall() == statistics
        assert connection.execute("SELECT payload FROM backups WHERE trial=?", (trial,)).fetchone()[0] == backup
    finally:
        connection.close()


def test_judge_unknown_is_dimension_only_neutral():
    dimensions = {name: {"score": .75, "rationale": "Observed safe behavior", "confidence": "medium",
                         "evidence": ["task/trace/1"]} for name in ["robustness", "decisions", "generalizable"]}
    dimensions["generalizable"].update(score=None, unknown_reason="Transfer evidence unavailable")
    aggregate, unknown = judge_aggregate(dimensions)
    assert aggregate == pytest.approx(2 / 3)
    assert unknown == ("generalizable",)
    with pytest.raises(ValueError):
        judge_aggregate({})
    dimensions["decisions"]["score"] = .6
    with pytest.raises(ValueError):
        judge_aggregate(dimensions)


def test_floor_validation_and_puct_ties():
    p = prepare_priors({"a": 1, "b": 0})
    assert p.effective["b"] > 0
    assert math.fsum(p.effective.values()) == pytest.approx(1)
    assert prepare_priors({"a": 1e308, "b": 1e308}).effective == {"a": .5, "b": .5}
    for raw in ({"a": 0}, {"a": -1}, {"a": True}, {"a": float("nan")}):
        with pytest.raises(ValueError):
            prepare_priors(raw)
    edges = [Edge("b", 0, 0, .5), Edge("a", 0, 0, .5)]
    assert select_edge(edges, 0, 1).id == "a"
    assert select_edge([Edge("a", 5, 1, .6), Edge("b", 0, 0, .4)], 5, 1).id == "b"


def test_completion_reserve_cannot_be_spent_by_admission(tmp_path):
    config = FixedPUCTConfig(screen_gate="execution_valid", codex_invocation_cap=3)
    s = TreeStore(tmp_path, config, OBJECTIVE)
    s.add_root("root", score("a"))
    trial = s.reserve_trial()
    assert s.budget() == {"cap": 3, "spent": 0, "reserved": 2, "available": 1}
    with pytest.raises(CapacityError):
        s.claim_operation("root", "expansion", "other", "e")
    with pytest.raises(CapacityError):
        s.consume_call("other", "value")
    assert s.consume_call(trial, "value")
    assert s.consume_call(trial, "value")
    assert s.budget()["spent"] == 2
    s.close()


def test_expansion_owner_and_fencing(store):
    op = store.claim_operation("root", "expansion", "worker-1", "e", now=0)
    assert store.claim_operation("root", "expansion", "worker-2", "e", now=500) is None
    with pytest.raises(OwnershipError, match="still live"):
        store.reclaim_operation("root", op["token"], lambda _: False, now=121)
    store.reclaim_operation("root", op["token"], lambda _: True, now=121)
    new = store.claim_operation("root", "expansion", "worker-2", "e", now=122)
    assert new["token"] > op["token"]
    with pytest.raises(OwnershipError):
        store.publish_plans("root", "worker-1", op["token"], [], {},
                            {"catalog_plan_ids": [], "inspected_plan_ids": []}, "none")


def test_heartbeat_race_prevents_reclamation(store):
    op = store.claim_operation("root", "expansion", "w", "e", now=0)
    def verify_dead(_):
        store.heartbeat("root", "w", op["token"], now=121)
        return True
    with pytest.raises(OwnershipError, match="heartbeat changed"):
        store.reclaim_operation("root", op["token"], verify_dead, now=122)


def test_archive_publication_reservation_spans_nodes_and_survives_reopen(store):
    store.db.execute("INSERT INTO nodes(id,artifact,parent,depth,score,eligible) "
        "SELECT 'sibling',artifact,'root',1,score,1 FROM nodes WHERE id='root'")
    operation = store.claim_operation("root", "expansion", "first", "root-evidence", now=0)
    peer = TreeStore(store.directory, store.config, store.objective)
    try:
        before = peer.budget()
        assert peer.claim_operation("sibling", "expansion", "second", "sibling-evidence", now=121) is None
        assert peer.budget() == before
        # Staleness alone cannot release the shared archive reservation.
        with pytest.raises(OwnershipError, match="still live"):
            peer.reclaim_operation("root", operation["token"], lambda _: False, now=121)
        assert peer.claim_operation("sibling", "expansion", "second", "sibling-evidence") is None
        peer.reclaim_operation("root", operation["token"], lambda _: True, now=121)
        replacement = peer.claim_operation("sibling", "expansion", "second", "sibling-evidence")
        assert replacement is not None
        with pytest.raises(OwnershipError):
            store.publish_plans("root", "first", operation["token"], [], {},
                {"catalog_plan_ids": [], "inspected_plan_ids": []}, "none")
        peer.fail_operation("sibling", "second", replacement["token"], "joined")
        assert peer.budget()["reserved"] == 0
    finally:
        peer.close()


def test_same_parent_exact_duplicate_rejected_atomically(store):
    op = store.claim_operation("root", "expansion", "w", "e")
    one, two = draft("one"), draft("two")
    two["document"] = "A cosmetically rephrased plan"
    with pytest.raises(sqlite3.IntegrityError):
        store.publish_plans("root", "w", op["token"], [one, two], {"one": 1, "two": 1},
                            {"catalog_plan_ids": [], "inspected_plan_ids": []})
    assert store.db.execute("SELECT count(*) FROM plans").fetchone()[0] == 0
    assert not store.capacity("root")["closed"]


@pytest.mark.parametrize("classification", ["scope_repair_only", "new_action"])
def test_declared_duplicate_cannot_be_published_with_changed_test_scope(store, classification):
    op = store.claim_operation("root", "expansion", "w", "e")
    candidate = draft()
    candidate["novelty_status"] = classification
    candidate["novelty"] = "Same-parent duplicate of archived old: the only difference is test scope."
    candidate["intervention"]["scope"].append("tests/test_controller.py")
    with pytest.raises(ValueError, match="duplicate"):
        store.publish_plans("root", "w", op["token"], [candidate], {candidate["id"]: 1},
                            {"catalog_plan_ids": [], "inspected_plan_ids": []})
    assert store.db.execute("SELECT count(*) FROM plans").fetchone()[0] == 0
    assert not store.capacity("root")["closed"]


def test_archive_gaps_and_stale_catalog_rejected(store):
    op = store.claim_operation("root", "expansion", "w", "e")
    with pytest.raises(ValueError, match="archive review"):
        store.publish_plans("root", "w", op["token"], [], {}, {"inspected_plan_ids": [], "gaps": ["inaccessible"]}, "none")
    with pytest.raises(OwnershipError, match="catalog"):
        store.publish_plans("root", "w", op["token"], [draft()], {"plan-a": 1},
                            {"inspected_plan_ids": [], "catalog_plan_ids": ["nonexistent"]})


def test_negative_reward_continues_and_two_levels_receive_same_return(store):
    expand(store)
    trial = store.reserve_trial()
    selection = store.select_and_reserve(trial)
    child_transition(store, trial, selection, u=.4, j=.4)
    assert store.select_and_reserve(trial)["kind"] == "expand"
    cap = store.snapshot_frontier(trial)
    vid = store.persist_value(trial, .08, cap, {"assessment": "model-call"})
    result = store.backup(trial, vid)
    assert result["returns"][0]["total"] == pytest.approx(-.108)
    p = store.db.execute("SELECT n,w FROM plans").fetchone()
    a = store.db.execute("SELECT n,w FROM attempts").fetchone()
    assert tuple(p) == tuple(a)
    assert p["n"] == 1
    assert store.budget()["reserved"] == 0
    assert store.backup(trial, vid) == result
    assert store.db.execute("SELECT n FROM plans").fetchone()[0] == 1


def test_missing_or_forged_value_cannot_be_zero_substitute(store):
    expand(store)
    trial = store.reserve_trial()
    selection = store.select_and_reserve(trial)
    child_transition(store, trial, selection)
    with pytest.raises(ValueError, match="persisted value"):
        store.backup(trial)
    cap = store.snapshot_frontier(trial)
    forged = {**cap, "terminal": True}
    with pytest.raises(ValueError, match="unchanged persisted"):
        store.persist_value(trial, 0, forged, {"reason": "pretend exhaustion"})
    store.awaiting(trial, "value")
    replacement = store.reserve_trial()
    assert replacement != trial
    assert store.db.execute("SELECT count(*) FROM backups").fetchone()[0] == 0


def test_empty_failed_trial_consumes_slot_no_q_update(store):
    expand(store)
    trial = store.reserve_trial()
    selection = store.select_and_reserve(trial)
    store.fail_attempt(selection["attempt"], "empty patch")
    with pytest.raises(ValueError, match="continuing"):
        store.select_and_reserve(trial)
    result = store.backup(trial)
    assert result["returns"] == []
    assert store.db.execute("SELECT n FROM plans").fetchone()[0] == 0
    assert store.capacity("root")["plans"][0]["remaining"] == 2


def test_unappended_scored_attempt_cannot_be_silently_dropped(store):
    expand(store)
    trial = store.reserve_trial()
    selection = store.select_and_reserve(trial)
    store.register_child(selection["attempt"], "child", "child-artifact")
    store.publish_score(selection["attempt"], score("child-artifact"))
    with pytest.raises(ValueError, match="settled"):
        store.backup(trial)


def test_stored_children_survive_generation_exhaustion(store):
    expand(store)
    for i in range(3):
        trial = store.reserve_trial()
        selection = store.select_and_reserve(trial)
        child_transition(store, trial, selection, id=f"child-{i}")
        cap = store.snapshot_frontier(trial)
        store.backup(trial, store.persist_value(trial, 0, cap, {"test": True}))
    assert store.capacity("root")["plans"][0]["remaining"] == 0
    assert not store.capacity("root")["terminal"]
    trial = store.reserve_trial()
    selected = store.select_and_reserve(trial)
    assert selected["kind"] == "stored"
    assert store.db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 3
    store.append_transition(trial, selected["position"])
    cap = store.snapshot_frontier(trial)
    store.backup(trial, store.persist_value(trial, .1, cap, {"test": True}))
    assert store.db.execute("SELECT sum(n) FROM attempts").fetchone()[0] == 4


def test_no_plan_absorbing_stop_is_terminal_not_budget_cutoff(store):
    expand(store, drafts=[])
    assert store.capacity("root")["terminal"]
    trial = store.reserve_trial()
    assert store.select_and_reserve(trial)["kind"] == "terminal"
    store.backup(trial)


def test_workers_reserve_distinct_slots_under_same_plan(store):
    expand(store)
    first, second = store.reserve_trial(), store.reserve_trial()
    s1 = store.select_and_reserve(first)
    s2 = store.select_and_reserve(second)
    assert s2["kind"] == "implement"
    assert s1["plan"] == s2["plan"]
    assert s1["attempt"] != s2["attempt"]
    assert store._row("attempts", s1["attempt"])["slot"] == 1
    assert store._row("attempts", s2["attempt"])["slot"] == 2
    store.fail_attempt(s1["attempt"], "invalid")
    store.backup(first)
    with pytest.raises(ValueError, match="unfinished selection"):
        store.select_and_reserve(second)


def test_nine_workers_atomically_fill_three_slots_per_plan(tmp_path):
    config = FixedPUCTConfig(screen_gate="execution_valid", trial_workers=9, device_limit=9)
    directory = tmp_path / "run"
    with closing(TreeStore(directory, config, OBJECTIVE)) as store:
        store.add_root("root", score("root-artifact"))
        expand(store, drafts=[draft(f"plan-{i}", mechanism=f"mechanism-{i}") for i in range(3)])
        trials = [store.reserve_trial() for _ in range(9)]
        ready = Barrier(9)

        def select(trial):
            with closing(TreeStore(directory, config, OBJECTIVE)) as worker:
                ready.wait(5)
                return worker.select_and_reserve(trial)

        with ThreadPoolExecutor(max_workers=9) as pool:
            selections = list(pool.map(select, trials))
        assert all(s["kind"] == "implement" for s in selections)
        assert len({s["attempt"] for s in selections}) == 9
        for plan in store.db.execute("SELECT id FROM plans"):
            assert [a[0] for a in store.db.execute("SELECT slot FROM attempts WHERE plan=? ORDER BY slot", (plan[0],))] == [1, 2, 3]
        assert store.db.execute("SELECT sum(n) FROM plans").fetchone()[0] == 0
        assert store.db.execute("SELECT sum(w) FROM plans").fetchone()[0] == 0
        assert store.db.execute("SELECT count(*) FROM backups").fetchone()[0] == 0


def test_thirty_trials_never_become_thirty_one(store):
    expand(store, drafts=[])
    for _ in range(30):
        trial = store.reserve_trial()
        store.backup(trial)
    with pytest.raises(CapacityError, match="Trial limit"):
        store.reserve_trial()
    assert store.db.execute("SELECT count(*) FROM trials").fetchone()[0] == 30


@pytest.mark.parametrize("worker_count", [2, 9])
def test_concurrent_trial_admission_atomic(tmp_path, worker_count):
    config = FixedPUCTConfig(screen_gate="execution_valid", trial_workers=worker_count,
                             codex_invocation_cap=worker_count * 2)
    s = TreeStore(tmp_path, config, OBJECTIVE)
    s.add_root("root", score("a"))
    barrier = Barrier(worker_count + 1)
    def admit(_):
        worker = TreeStore(tmp_path, config, OBJECTIVE)
        barrier.wait()
        try:
            return worker.reserve_trial()
        except CapacityError:
            return None
        finally:
            worker.close()
    with ThreadPoolExecutor(max_workers=worker_count + 1) as pool:
        admitted = list(pool.map(admit, range(worker_count + 1)))
    assert sum(id is not None for id in admitted) == worker_count
    assert s.budget()["reserved"] == worker_count * 2
    assert s.budget()["available"] == 0
    s.close()


def test_backup_crash_transaction_rolls_back_all_statistics(store):
    expand(store)
    trial = store.reserve_trial()
    selection = store.select_and_reserve(trial)
    child_transition(store, trial, selection)
    cap = store.snapshot_frontier(trial)
    vid = store.persist_value(trial, .08, cap, {"test": True})
    store.db.execute("CREATE TRIGGER inject_crash BEFORE INSERT ON backups BEGIN SELECT RAISE(ABORT, 'injected crash'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.backup(trial, vid)
    assert store.db.execute("SELECT n FROM plans").fetchone()[0] == 0
    assert store.db.execute("SELECT state FROM trials").fetchone()[0] == "active"
    store.db.execute("DROP TRIGGER inject_crash")
    expected = store.backup(trial, vid)
    directory, config, objective = store.directory, store.config, store.objective
    resumed = TreeStore(directory, config, objective)
    assert resumed.backup(trial, vid) == expected
    assert resumed.db.execute("SELECT n FROM plans").fetchone()[0] == 1
    resumed.close()


def test_single_writer_lock_and_resume_identity(store):
    with store.lock():
        with pytest.raises(RuntimeError, match="Another controller"):
            with store.lock():
                pass
    with pytest.raises(ValueError, match="Resume"):
        TreeStore(store.directory, replace(store.config, screen_gate="strict_all_success"), OBJECTIVE)


def test_score_cannot_overwrite_or_change_artifact(store):
    expand(store)
    t = store.reserve_trial()
    selected = store.select_and_reserve(t)
    store.register_child(selected["attempt"], "child", "artifact-a")
    with pytest.raises(ValueError, match="exact artifact"):
        store.publish_score(selected["attempt"], score("artifact-b"))
    store.publish_score(selected["attempt"], score("artifact-a"))
    with pytest.raises(ValueError):
        store.publish_score(selected["attempt"], score("artifact-a", .9, .9))


def test_artifact_binary_patch_modes_symlinks_empty_dirs_and_output_modules(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "run.sh").write_text("#!/bin/sh\nexit 0\n")
    (source / "run.sh").chmod(0o755)
    (source / "binary").write_bytes(bytes(range(256)))
    (source / "output_module.py").write_text("required = True\n")
    (source / "empty").mkdir()
    (source / "link").symlink_to("binary")
    (source / "__pycache__").mkdir()
    (source / "__pycache__/ignored.pyc").write_bytes(b"runtime")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    root = artifacts.freeze(source)
    assert "output_module.py" in {r["path"] for r in artifacts.verify(root)}
    assert not any("pycache" in r["path"] for r in artifacts.verify(root))
    (source / "binary").write_bytes(b"\x00\xffchanged")
    (source / "empty").rmdir()
    (source / "new_empty").mkdir()
    (source / "run.sh").chmod(0o644)
    (source / "new.dat").write_bytes(b"\x00\x01new binary")
    child = artifacts.freeze(source)
    patch = artifacts.binary_patch(root, child)
    rebuilt = tmp_path / "reconstructed"
    artifacts.reconstruct(root, child, patch, rebuilt)
    assert manifest(rebuilt) == artifacts.verify(child)
    assert os.readlink(rebuilt / "link") == "binary"
    assert (rebuilt / "run.sh").stat().st_mode & 0o777 == 0o644
    (rebuilt / "binary").write_bytes(b"modified by evaluator")
    artifacts.verify(child)
    canonical = artifacts.root / child / "content/binary"
    canonical.write_bytes(b"corruption")
    with pytest.raises(ValueError, match="corruption"):
        artifacts.materialize(child, tmp_path / "reject")


def test_artifact_rejects_external_symlinks(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "escape").symlink_to("../outside")
    with pytest.raises(ValueError, match="External"):
        ArtifactStore(tmp_path / "artifacts").freeze(source)


def test_subset_reproduces_golden_and_is_independent_of_historical_outcomes():
    manifest_data = json.loads((REPOSITORY / "androidworld_sets_manifest.json").read_text())
    golden = json.loads((REPOSITORY / "alpha_agents/tree_search/bridges/androidworld/s0_golden.json").read_text())
    actual = prepare(manifest_data)
    assert actual == golden
    expected = {"AudioRecorderRecordAudioWithFileName", "ContactsAddContact", "SimpleSmsSendClipboardContent",
                "BrowserMultiply", "MarkorCreateNoteAndSms", "SaveCopyOfReceiptTaskEval", "RetroCreatePlaylist",
                "SimpleCalendarDeleteEventsOnRelativeDay", "NotesMeetingAttendeeCount", "TasksHighPriorityTasks"}
    assert {r["task"] for r in actual["tasks"]} == expected
    changed = copy.deepcopy(manifest_data)
    changed["rows"].reverse()
    for row in changed["rows"]:
        row.update(artemis_success=0, artemis_seconds=1e12, artemis_outcome="fiction")
    assert prepare(changed) == actual
    assert actual["coverage"]["app_labels"]["selected"] == 10
    assert actual["coverage"]["app_labels"]["full"] == 16
    assert actual["coverage"]["tags"]["selected"] == actual["coverage"]["tags"]["full"] == 12
    assert actual["coverage"]["effective_weighted_tasks"] == pytest.approx(9.7959183673)
    assert len(actual["complement"]) == 10
    assert not expected & set(actual["protected_confirmation"])


def test_two_hour_prompt_permissions_and_judge_input_isolation():
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.prompts import (
        TEMPLATES,
        RoleRequest,
    )
    rendered = {}
    for role in TEMPLATES:
        request = RoleRequest("run", "request", role, "artifact", "configured-model", "configured-effort", {})
        rendered[role] = request.render()
        assert request.permission_mode == ("workspace-write" if role == "implementer" else "read-only")
        assert request.collaboration_mode == ("plan" if role == "proposer" else "default")
        assert request.prompt_hash
        assert request.library_hash
        assert '"timeout_seconds":7200' in request.render()
    assert "closest relevant documents" in rendered["proposer"]
    assert "Do not replace it with an easier intervention" in rendered["implementer"]
    with pytest.raises(ValueError, match="artifact-only"):
        RoleRequest("run", "request", "judge_execution", "artifact", "model", "effort", {"plan": "biased"})
    with pytest.raises(ValueError, match="excluded"):
        RoleRequest("run", "request", "judge_execution", "artifact", "model", "effort",
                    {"canonical_evidence": {"plan_alignment": True}})
    with pytest.raises(ValueError, match="two-hour"):
        RoleRequest("run", "request", "value", "artifact", "model", "effort", {}, timeout_seconds=600)


def test_prior_refresh_preserves_stats_and_selection_time_version(store):
    expand(store)
    trial = store.reserve_trial()
    selected = store.select_and_reserve(trial)
    original = selected["prior_version"]
    child_transition(store, trial, selected)
    cap = store.snapshot_frontier(trial)
    store.backup(trial, store.persist_value(trial, .08, cap, {"test": True}))
    op = store.claim_operation("root", "refresh", "worker", "newer-evidence")
    refreshed = store.refresh_priors("root", "worker", op["token"], {"plan-a": 5})
    assert refreshed != original
    assert store.db.execute("SELECT n FROM plans").fetchone()[0] == 1
    assert store.db.execute("SELECT prior_version FROM selections").fetchone()[0] == original
    assert store.capacity("root")["prior_version"] == refreshed


def test_policy_reasoning_is_atomic_immutable_and_bound_to_each_prior_version(store):
    from alpha_agents.tree_search.core.mcts.contracts import fingerprint

    op = store.claim_operation("root", "expansion", "worker", "first-evidence")
    invocation = store.consume_call(op["request"], "policy")
    response = {"artifact_id": "root-artifact", "evidence_snapshot_id": "first-evidence",
                "plan_ids": ["plan-a"], "priors": {"plan-a": 1},
                "comparisons": "Recovery is preferable given the observed failure mechanism.",
                "uncertainty": "The failed trace does not establish broader app coverage.",
                "evidence": ["code:controller.py:5"],
                "controller_provenance": {"request_id": invocation, "model": "configured-model",
                                          "effort": "high", "prompt_hash": "prompt", "library_hash": "library"}}
    archive = {"inspected_plan_ids": [], "catalog_plan_ids": [], "gaps": []}
    for change in [{"artifact_id": "foreign"}, {"evidence_snapshot_id": "later"},
                   {"priors": {"plan-a": 2}}, {"plan_ids": ["foreign"]},
                   {"controller_provenance": {"request_id": "missing"}}]:
        with pytest.raises(ValueError, match="Policy"):
            store.publish_plans("root", "worker", op["token"], [draft()], {"plan-a": 1}, archive,
                                policy_response={**response, **change})
        assert store.db.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
        assert store.db.execute("SELECT COUNT(*) FROM priors").fetchone()[0] == 0
        assert store.db.execute("SELECT active FROM operations WHERE node='root'").fetchone()[0] == 1
    store.publish_plans("root", "worker", op["token"], [draft()], {"plan-a": 1}, archive,
                        policy_response=response)
    first = store.capacity("root")["prior_version"]
    stored = json.loads(store._row("priors", first)["assessment"])
    assert stored["policy_response"] == response
    assert stored["policy_response_hash"] == fingerprint(response)
    response["comparisons"] = "A later edit must not rewrite a stored version."
    refresh = store.claim_operation("root", "refresh", "worker", "second-evidence")
    refresh_invocation = store.consume_call(refresh["request"], "policy")
    newer = {**response, "evidence_snapshot_id": "second-evidence", "priors": {"plan-a": 5},
             "controller_provenance": {**response["controller_provenance"], "request_id": refresh_invocation}}
    second = store.refresh_priors("root", "worker", refresh["token"], newer["priors"],
                                 policy_response=newer)
    assert second != first
    assert json.loads(store._row("priors", first)["assessment"]) == stored
    assert json.loads(store._row("priors", second)["assessment"])["policy_response"] == newer
    snapshot = store.snapshot_frontier(store.reserve_trial())
    assert snapshot["reachable_evidence"]["root"]["prior"]["assessment"]["policy_response"] == newer


def test_attempt_cap_still_allows_cached_children_but_stops_admission_of_new_ones(tmp_path):
    config = FixedPUCTConfig(screen_gate="execution_valid", max_attempts=1)
    s = TreeStore(tmp_path, config, OBJECTIVE)
    s.add_root("root", score("root-artifact"))
    expand(s)
    trial = s.reserve_trial()
    selection = s.select_and_reserve(trial)
    child_transition(s, trial, selection)
    cap = s.snapshot_frontier(trial)
    s.backup(trial, s.persist_value(trial, 0, cap, {"test": True}))
    trial = s.reserve_trial()
    assert s.select_and_reserve(trial)["kind"] == "stored"
    s.close()


def test_depth_limit_never_expands_but_requires_nonzero_capable_forecast(tmp_path):
    config = FixedPUCTConfig(screen_gate="execution_valid", max_depth=1)
    s = TreeStore(tmp_path, config, OBJECTIVE)
    s.add_root("root", score("root-artifact"))
    expand(s)
    trial = s.reserve_trial()
    selected = s.select_and_reserve(trial)
    assert s.capacity("root")["pending"]
    assert not s.capacity("root")["terminal"]
    child_transition(s, trial, selected)
    assert s.select_and_reserve(trial)["kind"] == "terminal"
    with pytest.raises(ValueError, match="Ineligible"):
        s.claim_operation("child", "expansion", "worker", "evidence")
    cap = s.snapshot_frontier(trial)
    assert cap["terminal"] and not cap["exhausted"]
    assert cap["discount_factor"] == .9
    assert "remaining_depth" not in cap
    assert "value_horizon" not in cap
    assert cap["continuation_policy_id"]
    with pytest.raises(ValueError, match="persisted value"):
        s.backup(trial)
    vid = s.persist_value(trial, .1, cap, {"rationale": "Further bounded recovery"})
    result = s.backup(trial, vid)
    assert result["returns"][0]["predicted"] == pytest.approx(.09)
    assert result["returns"][0]["total"] == pytest.approx(.13 + .09)
    assert s.backup(trial) == result
    s.close()


def test_consumed_invalid_generation_slots_make_controller_absorbing(store):
    expand(store)
    for _ in range(3):
        trial = store.reserve_trial()
        selection = store.select_and_reserve(trial)
        store.fail_attempt(selection["attempt"], "invalid patch")
        store.backup(trial)
    cap = store.capacity("root")
    assert cap["terminal"]
    assert cap["depth"] == 0
    assert cap["plans"][0]["remaining"] == 0


def test_structural_exhaustion_still_has_zero_continuation_after_valid_transition(store):
    expand(store)
    trial = store.reserve_trial()
    selected = store.select_and_reserve(trial)
    child_transition(store, trial, selected)
    expand(store, node="child", drafts=[])
    cap = store.snapshot_frontier(trial)
    assert cap["exhausted"] and cap["terminal"]
    assert cap["depth"] < store.config.max_depth
    with pytest.raises(ValueError, match="exhausted continuation"):
        store.persist_value(trial, .1, cap, {"rationale": "Unsupported prediction"})
    result = store.backup(trial)
    assert result["value_id"] is None
    assert result["returns"][0]["predicted"] == 0
    assert result["returns"][0]["total"] == pytest.approx(.13)


def test_informational_device_cost_and_audited_model_recovery_grant(tmp_path):
    config = FixedPUCTConfig(screen_gate="execution_valid", codex_invocation_cap=2)
    s = TreeStore(tmp_path, config, OBJECTIVE)
    s.add_root("root", score("root-artifact"))
    trial = s.reserve_trial()
    s.consume_call(trial, "value")
    s.consume_call(trial, "value")
    s.awaiting(trial, "value")
    with pytest.raises(CapacityError):
        s.reserve_value_recovery(trial)
    s.record_cost("device-run", trial, "s0", "device", 1e9)
    s.record_cost("device-run", trial, "s0", "device", 1e9)
    assert s.status()["costs"][0]["seconds"] == 1e9
    s.grant_invocations(4, "Operator-authorized recovery of the original frontier")
    s.reserve_value_recovery(trial)
    assert s.budget()["reserved"] == 2
    assert s.budget()["spent"] == 2
    assert s.db.execute("SELECT reason FROM invocation_grants").fetchone()[0]
    resumed = TreeStore(tmp_path, config, OBJECTIVE)
    assert resumed.budget()["cap"] == 4
    resumed.close()
    s.close()


def test_manifest_only_directory_and_permission_change_reconstructs(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_text("same bytes")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    parent = artifacts.freeze(source)
    (source / "new_empty").mkdir()
    (source / "file").chmod(0o600)
    child = artifacts.freeze(source)
    patch = artifacts.binary_patch(parent, child)
    destination = tmp_path / "child"
    artifacts.reconstruct(parent, child, patch, destination)
    assert manifest(destination) == artifacts.verify(child)


def contract_response(request, response):
    """Deterministic doubles provide the same evidence contract as live roles."""
    role, context = request.role, request.context
    if role == "proposer":
        for plan in response["plans"]:
            plan["evidence"] = ["controller.py"]
            if context.get("novelty_admission_contract"):
                plan.setdefault("novelty_status", "new_action")
    elif role == "policy":
        response.update(evidence_snapshot_id=context["evidence_snapshot_id"],
                        plan_ids=[p["id"] for p in context["plans"]],
                        comparisons="Compare permitted bounded mechanisms", uncertainty="Deterministic fixture",
                        evidence=["controller.py"])
    elif role == "implementer":
        response.update(plan_id=context["plan_id"], attempt_id=context["attempt_id"],
                        changes=["Increment deterministic harness state"], blockers=[], evidence=["controller.py"],
                        verification=[{"ran": True, "status": "completed", "evidence": ["controller.py"]}])
    elif role.startswith("judge_"):
        response.update(rubric_version=context["rubric"], findings=["Inspect bounded harness behavior"],
                        coverage={"inspected": ["controller.py"], "unseen": []})
    elif role == "value":
        response.setdefault("status", "completed")
        response.update(objective_id=context["objective_id"], discount_factor=context["discount_factor"],
                        continuation_policy_id=context["continuation_policy_id"],
                        rationale="Continue under frozen capacity", uncertainty="Deterministic fixture",
                        evidence=["controller.py"])
    return response


def test_executable_trial_three_attempts_then_bootstrap_with_real_workspaces(tmp_path):
    from alpha_agents.tree_search.core.mcts.controller import (
        RoleSettings,
        StageEvidence,
        TrialExecutor,
    )
    source = tmp_path / "harness"
    source.mkdir()
    (source / "controller.py").write_text("value = 0\n")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    root_artifact = artifacts.freeze(source)
    s = TreeStore(tmp_path / "run", FixedPUCTConfig(screen_gate="execution_valid"), OBJECTIVE)
    s.add_root("root", score(root_artifact))
    class Gateway(RoleAdapter):
        prepare_request = staticmethod(RoleRequest)
        observed_usage = staticmethod(role_usage)
        def __init__(self):
            self.calls = []
        def invoke(self, req, workspace):
            self.calls.append(req)
            response = {"artifact_id": req.artifact_id}
            if req.role == "proposer":
                candidates = [draft(f"plan-{len(self.calls)}-{i}", mechanism=f"recovery {len(self.calls)} mechanism {i}") for i in range(3)]
                catalog = req.context["plan_archive"]["plan_ids"]
                response.update(plans=candidates, archive_review={"catalog_plan_ids": catalog, "inspected_plan_ids": catalog, "gaps": []})
            elif req.role == "policy":
                response["priors"] = {p["id"]: 1 for p in req.context["plans"]}
            elif req.role == "implementer":
                f = workspace / "controller.py"
                value = int(f.read_text().split("=")[1])
                f.write_text(f"value = {value + 1}\n")
                response["status"] = "completed"
            elif req.role == "judge_code":
                response.update(status="completed", phase="code", findings=["No unsafe paths"])
            elif req.role == "judge_execution":
                response.update(status="completed", phase="execution", canonical_evidence_id=req.context["canonical_evidence"]["snapshot_id"],
                                rubric_version=req.context["rubric"], dimensions={name: {"score": .75, "rationale": "Verified available traces",
                                "confidence": "medium", "evidence": ["trace"]} for name in ["robustness", "decisions", "generalizable"]})
            elif req.role == "value":
                response.update(value=.08, capacity_snapshot_id=req.context["capacity_snapshot_id"], score_snapshot_id=req.context["score_snapshot_id"])
            return contract_response(req, response)
    class Evaluator(EvaluationAdapter):
        def __init__(self):
            self.calls = []
        def evaluate(self, artifact, workspace, stage, tasks):
            self.calls.append((artifact, stage))
            # Real code drives deterministic evidence rather than fabricated
            # evaluator results copied from the parent or implementer narrative.
            value = int((workspace / "controller.py").read_text().split("=")[1])
            successes = {task: stage == "screen" or i < value for i, task in enumerate(tasks)}
            return StageEvidence(artifact, stage, successes, ("trace",))
    gateway, evaluator = Gateway(), Evaluator()
    executor = TrialExecutor(s, artifacts, gateway, evaluator, RoleSettings("configured-model", "configured-effort"),
                             ("screen-task",), {f"task-{i}": .25 for i in range(4)})
    trial = s.reserve_trial()
    result = executor.run(trial)
    assert len(result["path"]) == 3
    assert s.db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 3
    assert len([r for r in gateway.calls if r.role == "proposer"]) == 3
    assert len([r for r in gateway.calls if r.role == "value"]) == 1
    assert all(r.timeout_seconds == 7200 for r in gateway.calls)
    assert all(r.collaboration_mode == "plan" for r in gateway.calls if r.role == "proposer")
    assert len(evaluator.calls) == 6
    assert s.budget()["reserved"] == 0
    assert len(list((s.directory / "attempts").glob("*/model_patch.diff"))) == 3
    assert len(list((s.directory / "role_requests").glob("*/prompt.md"))) == len(gateway.calls)
    assert result["returns"][0]["total"] == pytest.approx(-.28 + .9 * (.225 + .9 * (.225 + .9 * .08)))
    s.close()


@pytest.fixture
def engine_factory(tmp_path):
    from alpha_agents.tree_search.core.mcts.controller import (
        RoleSettings,
        StageEvidence,
        TrialExecutor,
    )
    def build(gate="execution_valid", fail_judge=False, fail_value=False, screen_success=True, partial_patch=False,
              max_depth=8, discount_factor=.9):
        source = tmp_path / "source-engine"
        source.mkdir()
        (source / "controller.py").write_text("value = 0\n")
        artifacts = ArtifactStore(tmp_path / "engine-artifacts")
        artifact = artifacts.freeze(source)
        config = FixedPUCTConfig(screen_gate=gate, max_new_attempts_per_trial=1,
                                 max_depth=max_depth, discount_factor=discount_factor)
        store = TreeStore(tmp_path / "engine-run", config, OBJECTIVE)
        store.add_root("root", score(artifact))
        class Gateway(RoleAdapter):
            prepare_request = staticmethod(RoleRequest)
            observed_usage = staticmethod(role_usage)
            def __init__(self):
                self.calls = []
            def invoke(self, req, workspace):
                self.calls.append(req.role)
                result = {"artifact_id": req.artifact_id}
                if req.role == "proposer":
                    result.update(plans=[draft(f"plan-{i}", mechanism=f"mechanism-{i}") for i in range(3)], archive_review={"catalog_plan_ids": [], "inspected_plan_ids": [], "gaps": []})
                elif req.role == "policy":
                    result["priors"] = {p["id"]: 1 for p in req.context["plans"]}
                elif req.role == "implementer":
                    (workspace / "controller.py").write_text("value = 1\n")
                    if partial_patch:
                        raise TimeoutError("Invocation cancelled; permitted partial patch preserved")
                    result["status"] = "completed"
                elif req.role == "judge_code":
                    result.update(phase="code", status="completed", findings=["bounded recovery"])
                elif req.role == "judge_execution":
                    if fail_judge:
                        raise TimeoutError("Final review unavailable")
                    result.update(phase="execution", status="completed", canonical_evidence_id=req.context["canonical_evidence"]["snapshot_id"],
                                  rubric_version=req.context["rubric"], dimensions={name: {"score": .75,
                                  "rationale": "Inspected code and trajectories", "confidence": "medium", "evidence": ["trace"]}
                                  for name in ["robustness", "decisions", "generalizable"]})
                elif req.role == "value":
                    if fail_value:
                        raise TimeoutError("Value unavailable")
                    result.update(value=0, score_snapshot_id=req.context["score_snapshot_id"], capacity_snapshot_id=req.context["capacity_snapshot_id"])
                return contract_response(req, result)
        class Evaluator(EvaluationAdapter):
            def __init__(self):
                self.calls = []
            def evaluate(self, artifact, workspace, stage, tasks):
                self.calls.append(stage)
                return StageEvidence(artifact, stage, {task: screen_success if stage == "screen" else True for task in tasks}, ("trace",))
        gateway, evaluator = Gateway(), Evaluator()
        executor = TrialExecutor(store, artifacts, gateway, evaluator, RoleSettings("configured-model", "configured-effort"),
                                 ("screen-task",), {"s0-task": 1.0})
        return store, executor, gateway, evaluator
    return build


@pytest.mark.parametrize("prediction", [.02, -.05])
@pytest.mark.parametrize("discount_factor", [.5, .9])
def test_executor_estimates_beyond_depth_limit_without_generating_children(engine_factory, prediction, discount_factor):
    store, executor, gateway, evaluator = engine_factory(max_depth=1, discount_factor=discount_factor)
    invoke = gateway.invoke
    contexts = []

    def forecast(req, workspace):
        result = invoke(req, workspace)
        if req.role == "value":
            contexts.append(req.context)
            result["value"] = prediction
        return result

    gateway.invoke = forecast
    try:
        trial = store.reserve_trial()
        result = executor.run(trial)
        assert len(contexts) == 1
        cap = contexts[0]["capacity"]
        assert cap["depth"] == 1 and cap["terminal"] and not cap["exhausted"]
        assert cap["discount_factor"] == contexts[0]["discount_factor"] == discount_factor
        assert cap["continuation_policy_id"] == contexts[0]["continuation_policy_id"]
        assert "remaining_depth" not in contexts[0]
        assert "remaining_depth" not in cap
        assert result["value_id"] is not None
        assert result["returns"][0]["predicted"] == pytest.approx(discount_factor * prediction)
        assert result["returns"][0]["total"] == pytest.approx(.975 - .58 + discount_factor * prediction)
        assert store.db.execute("SELECT max(depth) FROM nodes").fetchone()[0] == 1
        assert gateway.calls.count("implementer") == 1
        assert store.budget()["reserved"] == 0
        # Replaying a settled trial neither calls the estimator nor backs up twice.
        assert executor.run(trial) == result
        assert gateway.calls.count("value") == 1
    finally:
        store.close()


def test_depth_eight_frontier_estimates_continuing_value_and_discounts_entire_path(engine_factory, tmp_path):
    store, executor, gateway, evaluator = engine_factory()
    invoke = gateway.invoke
    contexts = []

    def forecast(req, workspace):
        result = invoke(req, workspace)
        if req.role == "value":
            contexts.append(req.context)
            result["value"] = .1
        return result

    gateway.invoke = forecast
    try:
        # Build a valid eight-transition lineage through the public store APIs.
        # Exhaust unused slots with invalid attempts so later trials reuse the
        # scored prefix, preserving the controller's generation-first rule.
        for depth in range(1, 9):
            trial = store.reserve_trial()
            for _ in range(depth-1):
                selected = store.select_and_reserve(trial)
                assert selected["kind"] == "stored"
                store.append_transition(trial, selected["position"])
            selected = store.select_and_reserve(trial)
            assert selected["kind"] == "expand"
            expand(store, selected["node"], [draft(f"depth-{depth}", f"mechanism-{depth}")])
            selected = store.select_and_reserve(trial)
            assert selected["kind"] == "implement"
            source = tmp_path / f"depth-{depth}"
            source.mkdir()
            (source / "controller.py").write_text(f"value = {depth}\n")
            artifact = executor.artifacts.freeze(source)
            child = f"depth-{depth}"
            store.register_child(selected["attempt"], child, artifact)
            store.publish_score(selected["attempt"], score(artifact, .6 + .02*depth, .4))
            store.append_transition(trial, selected["position"])
            if depth < 8:
                cap = store.snapshot_frontier(trial)
                store.backup(trial, store.persist_value(trial, 0, cap, {"test": True}))
                for _ in range(2):
                    failed = store.reserve_trial()
                    for _ in range(depth-1):
                        prefix = store.select_and_reserve(failed)
                        assert prefix["kind"] == "stored"
                        store.append_transition(failed, prefix["position"])
                    unused = store.select_and_reserve(failed)
                    assert unused["kind"] == "implement"
                    store.fail_attempt(unused["attempt"], "Invalid fixture implementation")
                    cap = store.snapshot_frontier(failed)
                    vid = store.persist_value(failed, 0, cap, {"test": True}) if depth > 1 else None
                    store.backup(failed, vid)
        result = executor.run(trial)
        assert contexts[0]["capacity"]["depth"] == store.config.max_depth == 8
        assert contexts[0]["discount_factor"] == .9
        assert "remaining_depth" not in contexts[0]
        assert gateway.calls == ["value"]
        assert evaluator.calls == []
        assert len(result["returns"]) == 8
        assert result["returns"][0]["predicted"] == pytest.approx(.9**8 * .1)
        assert result["returns"][0]["total"] == pytest.approx(.018 * sum(.9**i for i in range(8)) + .9**8*.1)
        assert store.db.execute("SELECT max(depth) FROM nodes").fetchone()[0] == 8
        assert store.status()["backups"] == 22
        calls = store.budget()["spent"]
        assert executor.run(trial) == result
        assert store.budget()["spent"] == calls
        assert store.status()["backups"] == 22
    finally:
        store.close()


def test_verification_outputs_outside_scope_reject_patch_with_exact_paths(engine_factory):
    store, executor, gateway, evaluator = engine_factory()
    invoke = gateway.invoke
    def with_test_outputs(req, workspace):
        result = invoke(req, workspace)
        if req.role == "implementer":
            (workspace / "scratch").mkdir()
            (workspace / "scratch" / "mcp_stderr.log").write_text("test output\n")
            (workspace / "traces").mkdir()
            (workspace / "traces" / "data_engine.db").write_bytes(b"test database")
        return result
    gateway.invoke = with_test_outputs
    trial = store.reserve_trial()
    result = executor.run(trial)
    attempt = store.db.execute("SELECT state,child,error FROM attempts").fetchone()
    assert attempt["state"] == "invalid" and attempt["child"] is None
    assert attempt["error"] == (
        "Patch exceeds the frozen intervention edit scope: "
        "scratch, scratch/mcp_stderr.log, traces, traces/data_engine.db")
    assert evaluator.calls == []
    assert "judge_code" not in gateway.calls and "judge_execution" not in gateway.calls
    assert store.db.execute("SELECT count(*) FROM nodes").fetchone()[0] == 1
    assert store.db.execute("SELECT count(*) FROM evaluation_snapshots").fetchone()[0] == 0
    assert result["empty_failed"] and store.status()["backups"] == 1
    store.close()


def test_strict_screen_failure_never_retries_for_success_or_runs_s0(engine_factory):
    store, executor, gateway, evaluator = engine_factory(gate="strict_all_success", screen_success=False)
    trial = store.reserve_trial()
    result = executor.run(trial)
    assert evaluator.calls == ["screen"]
    assert "judge_execution" not in gateway.calls
    assert result["empty_failed"]
    assert store.status()["attempts"] == {"invalid": 1}
    assert store.status()["backups"] == 1
    assert store.db.execute("SELECT count(*) FROM evaluation_snapshots").fetchone()[0] == 1
    store.close()


@pytest.mark.parametrize("max_depth", [1, 8])
def test_pending_value_recovery_uses_original_capacity_and_renewed_reserve(engine_factory, max_depth):
    from alpha_agents.tree_search.core.mcts.controller import AssessmentPending
    store, executor, gateway, evaluator = engine_factory(fail_value=True, max_depth=max_depth)
    trial = store.reserve_trial()
    with pytest.raises(AssessmentPending):
        executor.run(trial)
    frozen = json.loads(store.db.execute("SELECT snapshot FROM frontier_snapshots WHERE trial=? ORDER BY rowid DESC LIMIT 1", (trial,)).fetchone()[0])
    assert frozen["discount_factor"] == .9
    assert frozen["continuation_policy_id"]
    assert store.status()["backups"] == 0
    assert gateway.calls.count("value") == 2
    # Another completed worker can change tree statistics during the outage.
    with store.transaction():
        store.db.execute("UPDATE nodes SET n=n+1 WHERE id=?", (frozen["node"],))
    invoke = gateway.invoke
    contexts = []
    def repaired(req, workspace):
        if req.role != "value":
            return invoke(req, workspace)
        contexts.append(req.context)
        return contract_response(req, {"artifact_id": req.artifact_id, "value": 0,
            "score_snapshot_id": req.context["score_snapshot_id"], "capacity_snapshot_id": req.context["capacity_snapshot_id"]})
    gateway.invoke = repaired
    executor.stop_admission.clear()
    executor.recover(trial)
    assert contexts[0]["capacity"] == {key: value for key, value in frozen.items() if key != "reachable_evidence"}
    assert json.loads(Path(contexts[0]["continuation_evidence"]["evidence_bundle_path"]).read_text()) == frozen
    assert contexts[0]["capacity_snapshot_id"] == frozen["snapshot_id"]
    assert store.status()["backups"] == 1
    assert store.db.execute("SELECT count(*) FROM recovery_funding").fetchone()[0] == 1
    assert store.budget()["reserved"] == 0
    store.close()


def test_role_preparation_capacity_snapshot_does_not_admit_a_trial(engine_factory):
    store, executor, gateway, evaluator = engine_factory()
    try:
        budget = store.budget()
        before = {table: store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("trials", "frontier_snapshots", "backups", "values_assessed")}
        snapshot = store.snapshot_frontier(None, node="root")
        assert snapshot["node"] == "root"
        assert snapshot["discount_factor"] == store.config.discount_factor
        assert "remaining_depth" not in snapshot
        assert snapshot["remaining_plan_slots"] == store.config.max_plans
        assert snapshot["reachable_evidence"]["root"]["canonical_evidence"]["s0_snapshot_id"] == store.score("root").u_snapshot_id
        assert snapshot == store.snapshot_frontier(None, node="root")
        assert store.budget() == budget
        assert before == {table: store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in before}
        assert gateway.calls == [] and evaluator.calls == []
        with pytest.raises(ValueError, match="exactly one"):
            store.snapshot_frontier(None)
        with pytest.raises(ValueError, match="exactly one"):
            store.snapshot_frontier("unreserved-trial", node="root")
    finally:
        store.close()


def test_pending_judge_recovery_reuses_android_without_regenerating_code(engine_factory):
    from alpha_agents.tree_search.core.mcts.controller import AssessmentPending
    store, executor, gateway, evaluator = engine_factory(fail_judge=True)
    trial = store.reserve_trial()
    with pytest.raises(AssessmentPending):
        executor.run(trial)
    original = gateway.invoke
    def repaired(req, workspace):
        if req.role != "judge_execution":
            return original(req, workspace)
        assert req.context["canonical_evidence"]["reference_contract"]["version"] == "judge-exact-evidence-references-v2"
        assert req.context["canonical_evidence"]["canonical_evidence_id"] == req.context["canonical_evidence"]["snapshot_id"]
        result = {"artifact_id": req.artifact_id, "phase": "execution", "status": "completed",
            "canonical_evidence_id": req.context["canonical_evidence"]["snapshot_id"],
            "dimensions": {key: {"score": .75, "rationale": "Canonical evidence inspected", "confidence": "medium",
            "evidence": ["trace"]} for key in ["robustness", "decisions", "generalizable"]}}
        return contract_response(req, result)
    gateway.invoke = repaired
    executor.stop_admission.clear()
    executor.recover(trial)
    assert evaluator.calls == ["screen", "s0"]
    assert gateway.calls.count("implementer") == 1
    assert store.status()["backups"] == 1
    assert store.budget()["reserved"] == 0
    store.close()


def test_interrupted_unfrozen_implementation_preserves_partial_but_consumes_invalid_slot(engine_factory):
    store, executor, gateway, evaluator = engine_factory()
    original = gateway.invoke
    class Crash(BaseException):
        pass
    def interrupted(req, workspace):
        result = original(req, workspace)
        if req.role == "implementer":
            raise Crash("Interrupted before freeze checkpoint")
        return result
    gateway.invoke = interrupted
    trial = store.reserve_trial()
    with pytest.raises(Crash):
        executor.run(trial)
    gateway.invoke = original
    executor.recover(trial)
    assert evaluator.calls == []
    assert gateway.calls.count("implementer") == 1
    assert store.status()["attempts"] == {"invalid": 1}
    assert len(list((store.directory / "attempts").glob("*/interrupted_partial.json"))) == 1
    assert store.status()["backups"] == 1
    assert store.db.execute("SELECT sum(n) FROM plans").fetchone()[0] == 0
    store.close()


def test_frozen_attempt_checkpoint_recovers_after_crash_before_child_registration(engine_factory, monkeypatch):
    store, executor, gateway, evaluator = engine_factory()
    promoted = []
    executor.on_score = promoted.append
    original = store.register_child
    class Crash(BaseException):
        pass
    def interrupted(*args):
        raise Crash("Frozen checkpoint committed before node transaction")
    monkeypatch.setattr(store, "register_child", interrupted)
    trial = store.reserve_trial()
    with pytest.raises(Crash):
        executor.run(trial)
    monkeypatch.setattr(store, "register_child", original)
    executor.recover(trial)
    assert gateway.calls.count("implementer") == 1
    assert evaluator.calls == ["screen", "s0"]
    assert store.status()["backups"] == 1
    assert store.status()["attempts"] == {"eligible": 1}
    assert promoted == [store.db.execute("SELECT child FROM attempts").fetchone()[0]]
    store.close()


def test_final_judge_outage_keeps_android_evidence_and_never_fabricates_j(engine_factory):
    from alpha_agents.tree_search.core.mcts.controller import AssessmentPending
    store, executor, gateway, evaluator = engine_factory(fail_judge=True)
    trial = store.reserve_trial()
    with pytest.raises(AssessmentPending):
        executor.run(trial)
    assert evaluator.calls == ["screen", "s0"]
    assert gateway.calls.count("judge_execution") == 1
    assert "value" not in gateway.calls
    assert store.status()["trials"] == {"awaiting_judge": 1}
    assert store.db.execute("SELECT count(*) FROM evaluation_snapshots").fetchone()[0] == 2
    assert store.db.execute("SELECT score FROM nodes WHERE parent IS NOT NULL").fetchone()[0] is None
    assert store.status()["backups"] == 0
    # Recovery retains its funding, but frees the worker for another trial.
    reserved = store.budget()["reserved"]
    replacement = store.reserve_trial()
    assert store.budget()["reserved"] == reserved + 2
    assert not executor.stop_admission.is_set()
    store.stop_trial(replacement, "test_cleanup")
    store.backup(replacement)
    store.close()


def test_value_outage_uses_protected_retry_and_retains_unbacked_path(engine_factory):
    from alpha_agents.tree_search.core.mcts.controller import AssessmentPending
    store, executor, gateway, evaluator = engine_factory(fail_value=True)
    trial = store.reserve_trial()
    with pytest.raises(AssessmentPending):
        executor.run(trial)
    assert gateway.calls.count("value") == 2
    assert store.status()["trials"] == {"awaiting_value": 1}
    assert store.status()["backups"] == 0
    assert store.db.execute("SELECT count(*) FROM path").fetchone()[0] == 1
    assert store.db.execute("SELECT n FROM plans").fetchone()[0] == 0
    assert store.db.execute("SELECT eligible FROM nodes WHERE parent IS NOT NULL").fetchone()[0] == 1
    store.close()


def test_partial_timeout_patch_can_be_evaluated_without_relabeling_implementation(engine_factory):
    store, executor, gateway, evaluator = engine_factory(partial_patch=True)
    trial = store.reserve_trial()
    result = executor.run(trial)
    assert len(result["path"]) == 1
    record = next((store.directory / "attempts").glob("*/implementation.json"))
    assert json.loads(record.read_text())["status"] == "failed"
    assert evaluator.calls == ["screen", "s0"]
    store.close()


def test_trial_resume_reuses_valid_prediction_after_backup_crash(engine_factory):
    store, executor, gateway, evaluator = engine_factory()
    trial = store.reserve_trial()
    store.db.execute("CREATE TRIGGER crash BEFORE INSERT ON backups BEGIN SELECT RAISE(ABORT,'crash'); END")
    with pytest.raises(sqlite3.IntegrityError):
        executor.run(trial)
    assert gateway.calls.count("value") == 1
    assert store.db.execute("SELECT count(*) FROM values_assessed").fetchone()[0] == 1
    store.db.execute("DROP TRIGGER crash")
    recovered = executor.run(trial)
    assert recovered == executor.run(trial)
    assert gateway.calls.count("value") == 1
    assert gateway.calls.count("implementer") == 1
    assert store.status()["backups"] == 1
    store.close()


def test_frozen_plan_archive_is_complete_recoverable_and_detects_tampering(store):
    from alpha_agents.tree_search.core.mcts.plan_archive import (
        document_hash,
        freeze_document,
    )

    expand(store)
    row = store.db.execute("SELECT document,metadata FROM plans WHERE id='plan-a'").fetchone()
    metadata = json.loads(row["metadata"])
    assert metadata["document_hash"] == document_hash(row["document"])
    exported = freeze_document(store.directory, "plan-a", row["document"], metadata["document_hash"])
    path = Path(exported["document_path"])
    assert path.name == "PLAN.md"
    assert path.read_text() == row["document"]
    assert path.stat().st_mode & 0o222 == 0
    path.unlink()  # Crash/lost derived export: SQLite retains the exact plan.
    assert freeze_document(store.directory, "plan-a", row["document"]) == exported
    path.chmod(0o644)
    path.write_text("different intervention")
    with pytest.raises(ValueError, match="corrupt"):
        freeze_document(store.directory, "plan-a", row["document"])
    assert path.read_text() == "different intervention"


def test_plan_archive_concurrent_export_and_untrusted_id_are_safe(tmp_path):
    from alpha_agents.tree_search.core.mcts.plan_archive import freeze_document

    with ThreadPoolExecutor(max_workers=4) as pool:
        exports = list(pool.map(lambda _: freeze_document(tmp_path, "../../escape", "Full plan\n"), range(8)))
    assert all(export == exports[0] for export in exports)
    path = Path(exports[0]["document_path"])
    assert path.is_relative_to(tmp_path / "plan_archive")
    assert path.read_text() == "Full plan\n"
    assert not list(path.parent.glob(".plan-*"))
    with pytest.raises(ValueError, match="hash mismatch"):
        freeze_document(tmp_path, "../../escape", "Full plan\n", "wrong-hash")


def test_judge_recovery_reuses_absolute_code_evidence_in_a_fresh_workspace(engine_factory):
    from alpha_agents.tree_search.core.mcts.controller import AssessmentPending

    store, executor, gateway, evaluator = engine_factory(fail_judge=True)
    original = gateway.invoke
    def absolute_code_review(request, workspace):
        response = original(request, workspace)
        if request.role == "judge_code":
            response["evidence"] = [str(workspace / "controller.py") + ":1"]
        return response
    gateway.invoke = absolute_code_review
    trial = store.reserve_trial()
    with pytest.raises(AssessmentPending):
        executor.run(trial)
    record = next(path for path in (store.directory / "role_requests").glob("*/request.json")
                  if json.loads(path.read_text())["role"] == "judge_code")
    directory = record.parent
    assert json.loads((directory / "response.json").read_text())["evidence"] == ["code:controller.py:1"]
    assert json.loads((directory / "raw_response.json").read_text())["evidence"][0].startswith("/")
    assert json.loads((directory / "code_evidence_provenance.json").read_text())["artifact_id"] != store.score("root").artifact_id
    def repaired(request, workspace):
        if request.role != "judge_execution":
            return absolute_code_review(request, workspace)
        assert request.context["code_findings"]["evidence"] == ["code:controller.py:1"]
        return contract_response(request, {"artifact_id": request.artifact_id, "status": "completed", "phase": "execution",
            "canonical_evidence_id": request.context["canonical_evidence"]["snapshot_id"],
            "dimensions": {name: {"score": .75, "rationale": "Verified canonical evidence", "confidence": "medium",
                "evidence": ["trace"]} for name in ("robustness", "decisions", "generalizable")}})
    gateway.invoke = repaired
    executor.stop_admission.clear()
    executor.recover(trial)
    assert gateway.calls.count("judge_code") == 1
    assert gateway.calls.count("implementer") == 1
    assert evaluator.calls == ["screen", "s0"]
    assert store.status()["backups"] == 1
    assert store.status()["role_response_reuses"] >= 1
    costs = list(store.db.execute("SELECT * FROM costs WHERE resource='role_execution'"))
    assert len(costs) == store.budget()["spent"]
    assert all(row["classification"] == "observed" and row["seconds"] >= 0 for row in costs)
    assert all(json.loads(row["metadata"])["usage"]["usage"] is None for row in costs)
    store.close()


def test_score_report_handles_unequal_task_weights_and_unknown_judge_dimension(tmp_path):
    from alpha_agents.tree_search.core.mcts.controller import StageEvidence
    from alpha_agents.tree_search.core.mcts.reporting import score_breakdowns

    store = TreeStore(tmp_path / "score-report", FixedPUCTConfig(screen_gate="execution_valid"), OBJECTIVE)
    try:
        evidence = StageEvidence("artifact", "s0", {"easy": True, "hard": False}, ("canonical-trace",))
        store.save_evaluation(evidence.identity, "artifact", "s0", {
            "artifact_id": "artifact", "stage": "s0", "outcomes": dict(evidence.outcomes), "evidence": list(evidence.evidence)})
        dimensions = {"robustness": {"score": None, "rationale": "No recovery behavior witnessed",
            "confidence": "low", "unknown_reason": "No recovery trajectory supplied"},
            "decisions": {"score": .25, "rationale": "Weak grounding", "confidence": "medium", "evidence": ["canonical-trace"]},
            "generalizable": {"score": .75, "rationale": "Reusable mechanism", "confidence": "medium", "evidence": ["canonical-trace"]}}
        judge_id = store.save_judge("artifact", evidence.identity, {"artifact_id": "artifact",
            "canonical_evidence_id": evidence.identity, "phase": "execution",
            "rubric_version": OBJECTIVE.rubric_version, "dimensions": dimensions})
        store.add_root("root", CombinedScore("artifact", evidence.identity, judge_id, OBJECTIVE, .25, .5))
        subset = {"tasks": [{"task": "easy", "weight": {"display": .25}}, {"task": "hard", "weight": {"display": .75}}]}
        node = score_breakdowns(store, subset)[0]
        assert node["u"] == .25 and node["j"] == .5
        assert node["weighted_tasks"] == [
            {"task": "easy", "weight": .25, "success": True, "contribution": .25},
            {"task": "hard", "weight": .75, "success": False, "contribution": 0}]
        assert node["unknown_dimensions"] == ["robustness"]
        assert node["judge_dimensions"]["robustness"]["score"] is None
        assert node["judge_dimensions"]["robustness"]["effective_rating"] == .5
        assert node["judge_dimensions"]["robustness"]["aggregate_contribution"] == pytest.approx(1 / 6)
        wrong_weights = {"tasks": [{"task": "easy", "weight": {"display": .5}}, {"task": "hard", "weight": {"display": .5}}]}
        with pytest.raises(ValueError, match="disagree"):
            score_breakdowns(store, wrong_weights)
    finally:
        store.close()


def test_backup_reports_original_value_capacity_after_later_state_changes(store):
    expand(store)
    trial = store.reserve_trial()
    selection = store.select_and_reserve(trial)
    child_transition(store, trial, selection)
    frozen = store.snapshot_frontier(trial)
    value = store.persist_value(trial, .08, frozen, {"assessment": "frozen-context"})
    store.db.execute("UPDATE nodes SET n=n+5 WHERE id=?", (frozen["node"],))
    store.stop_trial(trial, "computational_cutoff")
    backup = store.backup(trial, value)
    assert backup["capacity"] == frozen
    assert backup["capacity_at_backup"]["visits"] == frozen["visits"] + 5
    assert backup["stop_reason"] == "computational_cutoff"
    assert store.backup(trial, value) == backup


def test_proposer_catalog_keeps_all_plans_and_full_readable_records_without_inline_bodies(engine_factory):
    import hashlib

    store, executor, gateway, _ = engine_factory()
    original = gateway.invoke
    marker = "Complete archived mechanism analysis. " * 1000
    def detailed_plan(request, workspace):
        response = original(request, workspace)
        if request.role == "proposer":
            response["plans"][0]["document"] += marker
        return response
    gateway.invoke = detailed_plan
    executor._expand("root", "archive-worker")
    catalog = executor._catalog()
    assert catalog["plan_ids"] == [row[0] for row in store.db.execute("SELECT id FROM plans ORDER BY id")]
    assert marker not in json.dumps(catalog)
    entry = catalog["plans"][0]
    document = Path(entry["document_path"])
    metadata = Path(entry["metadata_path"])
    assert marker in document.read_text()
    record = json.loads(metadata.read_text())
    assert record["document"] == document.read_text()
    assert record["novelty"] and record["diversity_audit"] and record["archive_review"]
    assert hashlib.sha256(document.read_bytes()).hexdigest() == entry["document_hash"]
    assert hashlib.sha256(metadata.read_bytes()).hexdigest() == entry["metadata_hash"]
    assert not metadata.stat().st_mode & 0o222
    assert executor._catalog() == catalog
    metadata.chmod(0o644)
    metadata.write_text('{"different": "intervention"}')
    with pytest.raises(ValueError, match="corrupt"):
        executor._catalog()
    store.close()


def test_expansion_rechecks_stale_archive_with_reserved_native_plan_retry(engine_factory, monkeypatch):
    store, executor, gateway, evaluator = engine_factory()
    store.db.execute("INSERT INTO nodes(id,artifact,parent,depth,score,eligible) "
        "SELECT 'sibling',artifact,'root',1,score,1 FROM nodes WHERE id='root'")
    expand(store, "sibling", [draft("sibling-plan", mechanism="different recovery condition")])
    catalog = executor._catalog
    snapshots = []
    def stale_once():
        result = catalog()
        snapshots.append(result)
        if len(snapshots) == 1:
            return {**result, "plan_ids": [], "plans": []}
        return result
    monkeypatch.setattr(executor, "_catalog", stale_once)
    invoke = gateway.invoke
    requests = []
    def reviewed(request, workspace):
        requests.append(request)
        result = invoke(request, workspace)
        if request.role == "proposer":
            ids = request.context["plan_archive"]["plan_ids"]
            result["archive_review"] = {"catalog_plan_ids": ids, "inspected_plan_ids": ids, "gaps": []}
            result["plans"][0]["closest_plan_ids"] = ids
            result["plans"][0]["novelty"] = "Compared the updated executable mechanisms: " + repr(ids)
        return result
    gateway.invoke = reviewed
    publish = store.publish_plans
    publication = []
    def concurrently_published(node, owner, token, plans, priors, archive, zero_reason=None, **kwargs):
        if node == "root":
            publication.append(archive["catalog_plan_ids"])
        return publish(node, owner, token, plans, priors, archive, zero_reason, **kwargs)
    monkeypatch.setattr(store, "publish_plans", concurrently_published)
    executor._expand("root", "expansion-worker")
    assert publication == [[], ["sibling-plan"]]
    proposer = [request for request in requests if request.role == "proposer"]
    assert len(proposer) == 2 and all(request.execution_mode == "plan" for request in proposer)
    assert all(request.timeout_seconds == 7200 for request in requests)
    assert proposer[1].context["archive_recheck"]["previous_proposal"]["plans"]
    assert proposer[1].context["plan_archive"]["plan_ids"] == ["sibling-plan"]
    assert store.capacity("root")["closed"]
    assert store.db.execute("SELECT count(*) FROM plans WHERE node='root'").fetchone()[0] == 3
    assert gateway.calls.count("policy") == 2
    assert store.budget()["spent"] == 4 and store.budget()["reserved"] == 0
    assert evaluator.calls == []
    store.close()


def test_repeated_catalog_changes_stop_at_reserved_recheck_limit(engine_factory, monkeypatch):
    from alpha_agents.tree_search.core.mcts.controller import AssessmentPending
    from alpha_agents.tree_search.core.mcts.storage import CatalogChanged

    store, executor, gateway, _ = engine_factory()
    def changed(*args, **kwargs):
        raise CatalogChanged("Concurrent archive publication")
    monkeypatch.setattr(store, "publish_plans", changed)
    with pytest.raises(AssessmentPending, match="recheck allowance exhausted"):
        executor._expand("root", "expansion-worker")
    assert gateway.calls.count("proposer") == gateway.calls.count("policy") == 2
    assert store.budget()["spent"] == 4 and store.budget()["reserved"] == 0
    assert not store.capacity("root")["closed"]
    assert store.db.execute("SELECT count(*) FROM plans").fetchone()[0] == 0
    store.close()


def test_archive_attempt_evidence_is_complete_readable_and_versioned(engine_factory):
    import hashlib
    from dataclasses import asdict

    from alpha_agents.tree_search.core.mcts.controller import StageEvidence
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.permissions import (
        role_permissions,
    )
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.prompts import (
        RoleRequest,
    )

    store, executor, gateway, evaluator = engine_factory()
    evaluate = evaluator.evaluate
    def with_trajectory(artifact, workspace, stage, tasks):
        result = evaluate(artifact, workspace, stage, tasks)
        path = store.directory / "tasks" / artifact / stage / "evidence" / "trajectory.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(result.outcomes)))
        return StageEvidence(artifact, stage, result.outcomes, ("trace", str(path)))
    evaluator.evaluate = with_trajectory
    try:
        executor.run(store.reserve_trial())
        first = executor._catalog()
        attempt = first["plans"][0]["attempts"][0]
        path = Path(attempt["evidence_bundle_path"])
        content = path.read_bytes()
        bundle = json.loads(content)
        assert hashlib.sha256(content).hexdigest() == attempt["evidence_bundle_hash"]
        assert not path.stat().st_mode & 0o222
        assert bundle["attempt"]["artifact_id"] == attempt["artifact_id"]
        assert bundle["parent_artifact_id"] == store.score("root").artifact_id
        assert {row["stage"] for row in bundle["evaluations"]} == {"screen", "s0"}
        assert bundle["availability"] == dict.fromkeys(
            ["score", "evaluations", "judges", "canonical_s0", "canonical_judge"], "available")
        assert bundle["judges"][0]["payload"]["dimensions"]["robustness"]["rationale"]
        req = RoleRequest(str(store.directory), "archive-inspection", "proposer", store.score("root").artifact_id,
                          "configured-model", "high", {"plan_archive": first})
        profile = role_permissions(req, store.directory / "own-workspace")
        assert profile["filesystem"][str(path)] == "read"
        for reference in attempt["canonical_evidence"]:
            if reference.startswith("/"):
                assert profile["filesystem"][reference] == "read"
        assert str(store.directory / "evidence_archive") not in profile["filesystem"]
        assert str(store.directory / "tree.sqlite3") not in profile["filesystem"]
        # New development evidence produces a new immutable bundle. The request
        # already supplied to a role remains exactly recoverable.
        selected = StageEvidence(attempt["artifact_id"], "selection", {"additional-task": False}, ("trace",))
        store.save_evaluation(selected.identity, selected.artifact_id, selected.stage, asdict(selected))
        second = executor._catalog()
        later = second["plans"][0]["attempts"][0]
        assert later["evidence_bundle_path"] != str(path)
        assert path.read_bytes() == content
        path.unlink()
        executor._restore_archive_evidence({"plan_archive": first}, store)
        assert path.read_bytes() == content
        assert {row["stage"] for row in json.loads(Path(later["evidence_bundle_path"]).read_text())["evaluations"]} == {"screen", "s0", "selection"}
        calls = list(gateway.calls)
        later_path = Path(later["evidence_bundle_path"])
        later_path.unlink()
        assert executor._catalog() == second
        assert gateway.calls == calls
        later_path.chmod(0o644)
        later_path.write_text("corrupt")
        with pytest.raises(ValueError, match="corrupt"):
            executor._catalog()
    finally:
        store.close()


def test_attempt_evidence_archive_rejects_symlink_before_export(tmp_path):
    from alpha_agents.tree_search.core.mcts.plan_archive import freeze_attempt_evidence

    run, outside = tmp_path / "run", tmp_path / "outside"
    run.mkdir()
    outside.mkdir()
    (run / "evidence_archive").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        freeze_attempt_evidence(run, {"attempt": "one"})
    assert list(outside.iterdir()) == []


def test_catalog_snapshot_write_allows_peer_commit_after_read(engine_factory, monkeypatch):
    store, executor, _, _ = engine_factory()
    peer = TreeStore(store.directory, store.config, store.objective)
    try:
        expand(store, drafts=[draft("plan-a", "observations"), draft("plan-b", "recovery")])
        trial = store.reserve_trial()
        selected = store.select_and_reserve(trial)
        child_transition(store, trial, selected)
        save = store.save_archive_evidence
        committed = []
        def after_peer_commit(payload):
            peer.db.execute("UPDATE nodes SET n=n+1 WHERE id='root'")
            committed.append(payload["attempt"]["id"])
            return save(payload)
        monkeypatch.setattr(store, "save_archive_evidence", after_peer_commit)
        catalog = executor._catalog()
        assert catalog["plan_ids"] == ["plan-a", "plan-b"]
        assert committed == [selected["attempt"]]
        assert Path(catalog["plans"][0]["attempts"][0]["evidence_bundle_path"]).is_file()
    finally:
        peer.close()
        store.close()


def test_review_timing_captures_code_overlap_wait_and_cached_score_reuse(engine_factory, monkeypatch):
    import threading

    from alpha_agents.tree_search.core.mcts.assessment_timing import AssessmentTiming

    store, executor, gateway, evaluator = engine_factory()
    code_started, android_finished = threading.Event(), threading.Event()
    invoke, evaluate, mark = gateway.invoke, evaluator.evaluate, AssessmentTiming.mark
    def phased_invoke(request, workspace):
        if request.role == "judge_code":
            code_started.set()
            assert android_finished.wait(5)
        return invoke(request, workspace)
    def phased_evaluate(artifact, workspace, stage, tasks):
        assert code_started.wait(5)
        return evaluate(artifact, workspace, stage, tasks)
    def observed_mark(self, name):
        mark(self, name)
        if name == "android_finish":
            android_finished.set()
    gateway.invoke, evaluator.evaluate = phased_invoke, phased_evaluate
    monkeypatch.setattr(AssessmentTiming, "mark", observed_mark)
    try:
        result = executor.run(store.reserve_trial())
        rows = store.status()["reviewer_timings"]["assessments"]
        assert len(rows) == 1
        row = rows[0]
        assert row["state"] == "completed"
        events = row["events"]
        assert events["code_start"] < events["s0_ready"] <= events["android_finish"] < events["code_finish"]
        assert events["code_finish"] <= events["execution_start"] <= events["execution_finish"] <= events["score_ready"]
        assert row["code_android_overlap_seconds"] > 0
        assert row["code_wait_after_s0_seconds"] > 0
        assert row["added_continuation_seconds"] >= row["code_wait_after_s0_seconds"] + row["execution_phase_seconds"]
        calls = list(gateway.calls)
        child = store._row("nodes", result["path"][0]["child"])
        executor.assess("reuse", child["artifact"])
        assert gateway.calls == calls
        assert store.status()["reviewer_timings"]["assessments"] == rows
    finally:
        store.close()


def test_judge_outage_timing_does_not_claim_continuation_completed(engine_factory):
    from alpha_agents.tree_search.core.mcts.controller import AssessmentPending

    store, executor, _, _ = engine_factory(fail_judge=True)
    try:
        with pytest.raises(AssessmentPending):
            executor.run(store.reserve_trial())
        row = store.status()["reviewer_timings"]["assessments"][0]
        assert row["state"] == "judge_unavailable"
        assert row["execution_phase_seconds"] is not None
        assert row["added_continuation_seconds"] is None
        assert "score_ready" not in row["events"]
    finally:
        store.close()


def test_policy_response_reuse_keeps_original_invocation_provenance(engine_factory):
    store, executor, gateway, _ = engine_factory()
    try:
        operation = store.claim_operation("root", "expansion", "worker", "policy-evidence")
        artifact = store.score("root").artifact_id
        context = {"artifact": artifact, "evidence_snapshot_id": "policy-evidence", "plans": [draft()]}
        first = executor._readonly_role(operation["request"], "policy", "policy", artifact, context)
        before = len(gateway.calls)
        reused = executor._readonly_role(operation["request"], "policy", "policy", artifact, context)
        assert len(gateway.calls) == before
        assert reused["controller_provenance"] == first["controller_provenance"]
        assert store.status()["role_response_reuses"] == 1
        source = first["controller_provenance"]["request_id"]
        assert store.db.execute("SELECT COUNT(*) FROM invocations WHERE purpose='policy'").fetchone()[0] == 1
        assert store.db.execute("SELECT source FROM role_reuses").fetchone()[0] == source
    finally:
        store.close()


@pytest.mark.parametrize("prepared", [False, True])
def test_value_protocol_describes_implementation_tools_without_granting_them(engine_factory, tmp_path, prepared):
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.codex import (
        CodexGateway,
        CodexRuntime,
    )
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.permissions import (
        role_permissions,
    )

    store, executor, gateway, _ = engine_factory()
    environment = tmp_path / "verification-venv"
    python = environment / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(os.sys.executable)
    runtime = CodexRuntime("/sdk/python", "/codex", "openai", "https://example.test/v1", "codex_login",
                           verification_python=str(python) if prepared else None)
    gateway.enrich_context = CodexGateway(runtime).enrich_context
    requests = []
    invoke = gateway.invoke
    def capture(request, workspace):
        if request.role == "value":
            requests.append(request)
        return invoke(request, workspace)
    gateway.invoke = capture
    try:
        executor.run(store.reserve_trial())
        assert requests
        request = requests[0]
        protocol = request.context["continuation_role_protocol"]
        assert protocol["model"] == executor.settings.model
        assert protocol["effort"] == executor.settings.effort
        assert protocol["proposer_mode"] == "native_plan"
        assert protocol["implementer_mode"] == "native_goal"
        verification = protocol["implementation_verification"]
        assert verification["prepared_environment_configured"] is prepared
        if prepared:
            assert verification["trusted_verification_tools"] == runtime.implementation_tools_context()["trusted_verification_tools"]
        record = json.loads((store.directory / "role_requests" / request.request_id / "request.json").read_text())
        assert record["context"]["continuation_role_protocol"] == protocol
        assert record["context"]["evidence_reference_contract"] == request.context["evidence_reference_contract"]
        assert request.context["evidence_reference_contract"]["version"] == "role-exact-evidence-references-v2"
        assert "context:capacity_snapshot_id" in request.context["evidence_reference_contract"]["available_snapshot_aliases"]
        assert record["context"]["trajectory_interpretation_contract"]["version"] == "role-trajectory-temporal-grounding-v1"
        assert request.context["utility_context"]["s0_task_weights"] == executor.s0_weights
        assert request.context["utility_context"]["judge_weight"] == store.objective.judge_weight
        profile = role_permissions(request, Path(record["workspace"]))
        assert str(environment) not in profile["filesystem"]
        assert str(python) not in profile["filesystem"]
    finally:
        store.close()


def test_value_bundle_preserves_reachable_code_plans_and_snapshot_time_evidence(engine_factory):
    from dataclasses import asdict

    from alpha_agents.tree_search.core.mcts.controller import StageEvidence
    from alpha_agents.tree_search.core.mcts.value_context import (
        continuation_context,
        restore_continuation,
        verify_code_views,
    )
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.permissions import (
        role_permissions,
    )
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.prompts import (
        RoleRequest,
    )

    store, executor, _, _ = engine_factory()
    try:
        completed = executor.run(store.reserve_trial())
        child = completed["path"][0]["child"]
        artifact = store.score(child).artifact_id
        before = StageEvidence(artifact, "selection", {"extra-task": False}, ("selection-before",))
        store.save_evaluation(before.identity, artifact, before.stage, asdict(before))
        pending = store.reserve_trial()
        frozen = store.snapshot_frontier(pending)
        # The same artifact's later development evidence is not part of this
        # prediction, even if it arrives before the model invocation resumes.
        after = StageEvidence(artifact, "targeted", {"diagnostic": True}, ("targeted-after",))
        store.save_evaluation(after.identity, artifact, after.stage, asdict(after))
        context = continuation_context(store, executor.artifacts, frozen)
        path = Path(context["continuation_evidence"]["evidence_bundle_path"])
        bundle = json.loads(path.read_text())
        assert bundle == frozen
        evidence = bundle["reachable_evidence"]
        assert evidence["root"]["plans"][0]["document"]
        assert evidence["root"]["prior"]["assessment"]["effective"]
        policy = evidence["root"]["prior"]["assessment"]["policy_response"]
        assert policy["comparisons"] == "Compare permitted bounded mechanisms"
        assert policy["uncertainty"] == "Deterministic fixture"
        assert policy["evidence"] == ["controller.py"]
        provenance = policy["controller_provenance"]
        assert provenance["model"] == executor.settings.model
        assert provenance["effort"] == executor.settings.effort
        request = json.loads((store.directory / "role_requests" / provenance["request_id"] / "request.json").read_text())
        assert provenance["prompt_hash"] == request["prompt_hash"]
        assert provenance["library_hash"] == request["library_hash"]
        assert evidence[child]["canonical_evidence"]["judge"]["dimensions"]
        assert before.identity in {row["snapshot_id"] for row in evidence[child]["evaluations"]}
        assert after.identity not in {row["snapshot_id"] for row in evidence[child]["evaluations"]}
        assert evidence[child]["previous_values"][0]["value"] == 0
        assert "capacity" not in evidence[child]["previous_values"][0]
        assert "selection-before" in context["continuation_evidence"]["canonical_evidence"]
        assert "targeted-after" not in context["continuation_evidence"]["canonical_evidence"]
        views = {view["node_id"]: view for view in context["reachable_code_views"]}
        assert (Path(views["root"]["code_path"]) / "controller.py").read_text() == "value = 0\n"
        assert (Path(views[child]["code_path"]) / "controller.py").read_text() == "value = 1\n"
        req = RoleRequest(str(store.directory), "value-inspection", "value", store.score("root").artifact_id,
                          "configured-model", "high", context)
        profile = role_permissions(req, store.directory / "own-workspace")
        assert profile["filesystem"][str(path)] == "read"
        assert all(profile["filesystem"][view["code_path"]] == "read" for view in views.values())
        assert str(store.directory / "value_code_views") not in profile["filesystem"]
        path.unlink()
        restore_continuation(store, context)
        assert json.loads(path.read_text()) == frozen
        (Path(views[child]["code_path"]) / "controller.py").write_text("tampered\n")
        with pytest.raises(ValueError, match="differs"):
            verify_code_views(store, executor.artifacts, context["reachable_code_views"])
    finally:
        store.close()


def test_native_goal_gateway_services_targeted_request_during_engineering(engine_factory):
    import time

    from alpha_agents.tree_search.core.mcts.controller import StageEvidence
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.codex import (
        CodexGateway,
    )

    store, executor, gateway, evaluator = engine_factory()
    evaluate = evaluator.evaluate
    def diagnostic_evaluate(artifact, workspace, stage, tasks):
        evidence = evaluate(artifact, workspace, stage, tasks)
        return StageEvidence(artifact, stage, evidence.outcomes, ("targeted-trace",)) if stage == "targeted" else evidence
    evaluator.evaluate = diagnostic_evaluate
    requests = []
    class GoalGateway(CodexGateway):
        def invoke(self, request, workspace):
            requests.append(request)
            result = gateway.invoke(request, workspace)
            if request.role == "implementer":
                channel = request.context["device_check_channel"]
                Path(channel["request_path"]).write_text(json.dumps({"check_id": "mechanism", "attempt_id": request.context["attempt_id"],
                    "tasks": ["s0-task"], "mechanism": "bounded recovery", "expected_observation": "Recover after verified failure"}))
                latest = Path(channel["result_directory"]) / "latest.json"
                limit = time.monotonic() + 5
                while time.monotonic() < limit:
                    if latest.exists():
                        observed = json.loads(latest.read_text())
                        if observed["status"] != "pending":
                            break
                    time.sleep(.01)
                else:
                    raise AssertionError("Targeted check did not produce its terminal receipt")
                assert observed["status"] == "completed"
                result["check_requests"] = [{key: observed.get(key) for key in ("check_id", "status", "artifact_id", "snapshot_id", "outcomes")}]
                result["verification"].append({"ran": True, "status": "completed", "evidence": [str(latest)]})
            return result
    evaluator.supports_checks = True
    executor.gateway = GoalGateway(None)
    try:
        executor.run(store.reserve_trial())
        engineering = next(request for request in requests if request.role == "implementer")
        assert engineering.execution_mode == "goal" and engineering.timeout_seconds == 7200
        judge = next(request for request in requests if request.role == "judge_execution")
        assert "targeted-trace" not in judge.context["canonical_evidence"]["evidence"]
        assert evaluator.calls == ["targeted", "screen", "s0"]
        assert store.db.execute("SELECT count(*) FROM targeted_check_requests WHERE state='completed'").fetchone()[0] == 1
        assert store.db.execute("SELECT count(*) FROM evaluation_snapshots WHERE stage='targeted'").fetchone()[0] == 1
        assert store.status()["backups"] == 1
    finally:
        store.close()


def test_manual_judge_narrative_review_preserves_scores_and_changes_evidence_context(tmp_path):
    s = TreeStore(tmp_path / 'review-run', FixedPUCTConfig(screen_gate='execution_valid'), OBJECTIVE)
    try:
        dimensions = {name: {'score': .5, 'rationale': 'Original rationale', 'confidence': 'medium',
                            'evidence': ['canonical-trace']}
                      for name in ('robustness', 'decisions', 'generalizable')}
        original = {'artifact_id': 'artifact', 'canonical_evidence_id': 'evidence',
                    'phase': 'execution', 'status': 'completed', 'rubric_version': OBJECTIVE.rubric_version,
                    'dimensions': dimensions, 'findings': 'Incorrect four-click account'}
        jid = s.save_judge('artifact', 'evidence', original)
        s.add_root('root', CombinedScore('artifact', 'evidence', jid, OBJECTIVE, .6, .5))
        before_score = s.score('root')
        before_context = s.development_evidence('root')
        s.reserve_calls('manual-review', {'judge_execution': 1})
        invocation = s.consume_call('manual-review', 'judge_execution')
        directory = s.directory / 'role_requests' / invocation
        directory.mkdir(parents=True)
        (directory / 'request.json').write_text(json.dumps({'role': 'judge_execution', 'artifact_id': 'artifact'}))
        reviewed = copy.deepcopy(original)
        reviewed['findings'] = 'Five recorded dispatches; physical effect not assumed'
        (directory / 'response.json').write_text(json.dumps(reviewed))
        budget = s.budget()
        manual_file = directory / 'manual_review.json'
        manual_file.write_text('Reviewed locally; this file grants no model access')
        provenance = {'reviewer': 'operator', 'manual_review_path': str(manual_file)}
        review_id = s.review_judge_narrative(jid, invocation, 'Verified action count', provenance)
        assert s.review_judge_narrative(jid, invocation, 'Verified action count', provenance) == review_id
        assert s.score('root') == before_score
        assert s.budget() == budget
        assert json.loads(s._row('judge_snapshots', jid)['payload']) == original
        evidence = s.node_evidence('root')
        assert evidence['judge']['findings'] == reviewed['findings']
        assert evidence['judge_snapshot_id'] == jid
        assert evidence['judge_narrative_review']['original_score_snapshot_id'] == jid
        assert str(manual_file) not in json.dumps(evidence)
        assert json.loads(s.db.execute('SELECT provenance FROM judge_narrative_reviews').fetchone()[0]) == provenance
        from alpha_agents.tree_search.mutators.coding_agents.codex.roles.permissions import (
            role_permissions,
        )
        from alpha_agents.tree_search.mutators.coding_agents.codex.roles.prompts import (
            RoleRequest,
        )
        workspace = s.directory / 'workspaces' / 'proposer'
        workspace.mkdir(parents=True)
        permissions = role_permissions(RoleRequest(str(s.directory), 'call-proposer', 'proposer', 'artifact',
                                                   'gpt-6-luna', 'low', {'canonical_evidence': evidence}), workspace)
        assert str(manual_file) not in json.dumps(permissions)
        assert s.development_evidence('root') != before_context
        assert s.development_evidence('root')['judge_narrative_review_id'] == review_id
        assert s.db.execute('SELECT count(*) FROM trials').fetchone()[0] == 0
        assert s.db.execute('SELECT count(*) FROM backups').fetchone()[0] == 0
        with pytest.raises(ValueError, match='immutable'):
            s.review_judge_narrative(jid, invocation, 'Different review reason', {'reviewer': 'operator'})
        s.close()
        s = TreeStore.readonly(tmp_path / 'review-run')
        assert s.node_evidence('root')['judge_narrative_review']['review_id'] == review_id
    finally:
        s.close()


@pytest.mark.parametrize('change', ['score', 'unknown', 'artifact', 'canonical', 'rubric', 'partial', 'missing_dimension', 'boolean'])
def test_manual_judge_narrative_review_rejects_recalibration_or_wrong_identity(tmp_path, change):
    s = TreeStore(tmp_path / 'invalid-review', FixedPUCTConfig(screen_gate='execution_valid'), OBJECTIVE)
    try:
        original = {'artifact_id': 'artifact', 'canonical_evidence_id': 'evidence', 'phase': 'execution',
                    'status': 'completed', 'rubric_version': OBJECTIVE.rubric_version,
                    'dimensions': {name: {'score': .5, 'rationale': 'Mixed evidence', 'confidence': 'medium',
                                         'evidence': ['trace']} for name in ('robustness', 'decisions', 'generalizable')}}
        jid = s.save_judge('artifact', 'evidence', original)
        s.reserve_calls('review', {'judge_execution': 1})
        call = s.consume_call('review', 'judge_execution')
        path = s.directory / 'role_requests' / call
        path.mkdir(parents=True)
        (path / 'request.json').write_text(json.dumps({'role': 'judge_execution', 'artifact_id': 'artifact'}))
        reviewed = copy.deepcopy(original)
        if change == 'score':
            reviewed['dimensions']['robustness']['score'] = .75
        elif change == 'unknown':
            reviewed['dimensions']['robustness'].update(score=None, unknown_reason='Unseen')
        elif change == 'artifact':
            reviewed['artifact_id'] = 'other'
        elif change == 'canonical':
            reviewed['canonical_evidence_id'] = 'other'
        elif change == 'rubric':
            reviewed['rubric_version'] = 'other'
        elif change == 'partial':
            reviewed['status'] = 'partial'
        elif change == 'missing_dimension':
            reviewed['dimensions'].pop('decisions')
        elif change == 'boolean':
            reviewed['dimensions']['robustness']['score'] = True
        (path / 'response.json').write_text(json.dumps(reviewed))
        with pytest.raises(ValueError):
            s.review_judge_narrative(jid, call, 'Manual check', {'reviewer': 'operator'})
        assert s.db.execute('SELECT count(*) FROM judge_narrative_reviews').fetchone()[0] == 0
        assert json.loads(s._row('judge_snapshots', jid)['payload']) == original
    finally:
        s.close()


def test_canonical_task_reference_index_names_only_existing_owned_s0_bundles(tmp_path):
    from alpha_agents.tree_search.core.mcts.contracts import fingerprint
    from alpha_agents.tree_search.core.mcts.responses import _evidence
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.prompts import (
        RoleRequest,
    )

    s = TreeStore(tmp_path / 'reference-index', FixedPUCTConfig(screen_gate='execution_valid'), OBJECTIVE)
    try:
        task_id = 'a' * 64
        evidence = s.directory / 'tasks' / task_id / 'execution' / 'evidence'
        evidence.mkdir(parents=True)
        foreign = tmp_path / 'foreign' / 'tasks' / ('b' * 64) / 'execution' / 'evidence'
        foreign.mkdir(parents=True)
        missing = s.directory / 'tasks' / ('c' * 64) / 'execution' / 'evidence'
        payload = {'artifact_id': 'artifact', 'stage': 's0', 'outcomes': {'task': True},
                   'evidence': [str(evidence), str(foreign), str(missing)]}
        sid = fingerprint(payload)
        s.save_evaluation(sid, 'artifact', 's0', payload)
        s.add_root('root', CombinedScore('artifact', sid, 'unused-judge', OBJECTIVE, 1, .5))
        context = s.node_evidence('root')
        assert context['canonical_task_references']['tasks'] == [{'task_id': task_id, 'evidence_path': str(evidence)}]
        workspace = s.directory / 'workspaces' / 'proposer'
        workspace.mkdir(parents=True)
        req = RoleRequest(str(s.directory), 'call-test', 'proposer', 'artifact', 'gpt-6-luna', 'low',
                          {'canonical_evidence': context})
        _evidence([task_id], req, workspace)
        summary = evidence / 'summary.json'
        summary.write_text('{}')
        _evidence([str(summary)], req, workspace)
        sibling = evidence.parent / 'outside.json'
        sibling.write_text('{}')
        escaping = evidence / 'escaping.json'
        escaping.symlink_to(sibling)
        for forbidden in [sibling, escaping]:
            with pytest.raises(ValueError, match='out-of-scope'):
                _evidence([str(forbidden)], req, workspace)
        for unknown in ['b' * 64, 'c' * 64, 'd' * 64, 'Invocation envelope evidence: ' + 'x' * 300]:
            with pytest.raises(ValueError, match='out-of-scope'):
                _evidence([unknown], req, workspace)
        assert json.loads(s.db.execute('SELECT payload FROM evaluation_snapshots WHERE id=?', (sid,)).fetchone()[0]) == payload
    finally:
        s.close()


@pytest.mark.parametrize('count', [0, 1, 2])
def test_required_three_plan_publication_rejects_incomplete_set_atomically(store, count):
    operation = store.claim_operation('root', 'expansion', 'worker', 'evidence')
    before = store.budget()
    drafts = [draft(f'candidate-{i}', mechanism=f'mechanism-{i}') for i in range(count)]
    archive = {'catalog_plan_ids': [], 'inspected_plan_ids': [], 'gaps': []}
    with pytest.raises(ValueError, match='exactly three'):
        store.publish_plans('root', 'worker', operation['token'], drafts,
                            {d['id']: 1 for d in drafts}, archive,
                            required_plan_count=3)
    assert store.budget() == before
    assert store.db.execute('SELECT count(*) FROM plans').fetchone()[0] == 0
    assert not store.capacity('root')['closed']
    assert not store.capacity('root')['terminal']
    drafts = [draft(f'candidate-{i}', mechanism=f'mechanism-{i}') for i in range(3)]
    store.publish_plans('root', 'worker', operation['token'], drafts,
                        {d['id']: 1 for d in drafts}, archive, required_plan_count=3)
    capacity = store.capacity('root')
    assert capacity['closed'] and len(capacity['plans']) == 3
    assert all(plan['remaining'] == 3 for plan in capacity['plans'])


@pytest.mark.parametrize('count', [0, 1, 2])
def test_incomplete_three_plan_proposal_keeps_expansion_open(engine_factory, count):
    from alpha_agents.tree_search.core.mcts.controller import AssessmentPending
    store, executor, gateway, evaluator = engine_factory()
    invoke = gateway.invoke
    def incomplete(request, workspace):
        result = invoke(request, workspace)
        if request.role == 'proposer':
            assert request.context['required_plan_count'] == 3
            assert request.context['implementation_slots_per_plan'] == 3
            result['plans'] = result['plans'][:count]
        return result
    gateway.invoke = incomplete
    with pytest.raises(AssessmentPending, match='Proposal failed twice'):
        executor._expand('root', 'worker')
    assert gateway.calls.count('proposer') == 2
    assert 'policy' not in gateway.calls
    assert store.db.execute('SELECT count(*) FROM plans').fetchone()[0] == 0
    assert not store.capacity('root')['closed']
    assert not store.capacity('root')['terminal']
    assert store.budget()['reserved'] == 0
    store.close()


def test_parked_attempts_do_not_cause_indefinite_selection_wait_or_slot_reuse(store):
    expand(store)
    attempts = []
    for _ in range(3):
        trial = store.reserve_trial()
        selected = store.select_and_reserve(trial)
        assert selected["kind"] == "implement"
        attempts.append(selected["attempt"])
        store.register_child(selected["attempt"], f"pending-{trial}", f"artifact-{trial}")
        store.awaiting(trial, "evaluation")
    funding = store.budget()["reserved"]
    peer = store.reserve_trial()
    selected = store.select_and_reserve(peer)
    assert selected["kind"] == "cutoff" and selected["reason"] == "pending_recovery"
    assert not selected["capacity"]["exhausted"]  # an outage is not a terminal value of zero
    assert selected["capacity"]["pending"]
    assert store.db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 3
    assert store.budget()["reserved"] == funding + 2
    assert all(store.db.execute("SELECT 1 FROM reservations WHERE owner=? AND remaining>0", (aid,)).fetchone()
               for aid in attempts)
    assert store.db.execute("SELECT count(*) FROM backups").fetchone()[0] == 0


def test_value_storage_failure_is_fatal_instead_of_local_model_outage(engine_factory, monkeypatch):
    store, executor, _, _ = engine_factory()
    def corrupted(*args, **kwargs):
        raise sqlite3.DatabaseError("Shared state is corrupt")
    monkeypatch.setattr(store, "persist_value", corrupted)
    try:
        trial = store.reserve_trial()
        with pytest.raises(sqlite3.DatabaseError, match="Shared state"):
            executor.run(trial)
        assert store.status()["backups"] == 0
        assert store._row("trials", trial)["state"] == "active"
    finally:
        store.close()


def test_local_evaluation_recovery_reuses_screen_and_implementation(engine_factory):
    from alpha_agents.tree_search.core.mcts.controller import EvaluationPending

    store, executor, gateway, evaluator = engine_factory()
    evaluate = evaluator.evaluate
    def unavailable(artifact, workspace, stage, tasks):
        if stage == "s0":
            raise TimeoutError("S0 infrastructure unavailable")
        return evaluate(artifact, workspace, stage, tasks)
    evaluator.evaluate = unavailable
    try:
        trial = store.reserve_trial()
        with pytest.raises(EvaluationPending):
            executor.run(trial)
        assert store._row("trials", trial)["state"] == "awaiting_evaluation"
        assert not executor.stop_admission.is_set()
        assert store.status()["backups"] == 0
        evaluator.evaluate = evaluate
        executor.recover(trial)
        assert gateway.calls.count("implementer") == 1
        assert evaluator.calls == ["screen", "s0"]
        assert store.status()["backups"] == 1
        assert executor.run(trial) == store.backup(trial)
        assert store.status()["backups"] == 1
        assert store.budget()["reserved"] == 0
    finally:
        store.close()
