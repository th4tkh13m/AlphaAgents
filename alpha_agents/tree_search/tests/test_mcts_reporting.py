"""Forecasts use canonical observed leases, without introducing budget gates."""

import json

import pytest

from alpha_agents.tree_search.core.mcts.contracts import (
    FixedPUCTConfig,
    Objective,
    canonical_json,
)
from alpha_agents.tree_search.core.mcts.reporting import (
    reviewer_timings,
    runtime_forecasts,
    value_calibration,
)
from alpha_agents.tree_search.core.mcts.storage import TreeStore
from alpha_agents.tree_search.core.mcts.task_cache import TaskCache, TaskIdentity


def frozen_store(tmp_path, s0, selection, subset=None):
    store = TreeStore(tmp_path / "run", FixedPUCTConfig(screen_gate="execution_valid"), Objective("subset", "rubric", "judge"))
    record = {"root_artifact_id": "root", "selection_tasks": selection,
              "s0_weights": dict.fromkeys(s0, 1 / len(s0)), "subset": subset or {}}
    store.db.execute("INSERT INTO meta VALUES('run_identity',?)", (canonical_json(record),))
    return store


def observe_task(store, task, duration, *, retry_seconds=None, classification="observed", artifact="root", kind="canonical"):
    cache = TaskCache(store.directory, store.config, store.objective)
    identity = TaskIdentity(artifact, task, 42, "model", "evaluator", "fixture", {"runner": "test"}, {"task": 1800}, sample_kind=kind)
    cache.register("baseline", artifact, "s0" if kind == "canonical" else "targeted", (identity,))
    if retry_seconds is not None:
        run = cache.begin(identity, "device", store.directory / "tasks" / identity.key / "failed")
        cache.finish(run, None, "Reset failed", retry_seconds, "s0")
    output = store.directory / "tasks" / identity.key / "evidence"
    output.mkdir(parents=True, exist_ok=True)
    (output / "outcome.json").write_text(json.dumps({"task": task}))
    run = cache.begin(identity, "device", output)
    cache.finish(run, {"task_name": task, "seed": identity.contract["generated_seed"], "success": True,
                       "evidence_root": str(output)}, None, duration, "s0", cost_classification=classification)
    return identity


def test_final_selection_bounds_and_root_margin_preserve_invocation_balance(tmp_path):
    tasks = [f"task-{i}" for i in range(20)]
    store = frozen_store(tmp_path, tasks[:10], tasks)
    try:
        initial = runtime_forecasts(store)
        assert initial["root_s0_timing"]["measured_tasks"] == 0
        assert all(row["expected_device_hours_from_root"] is None for row in initial["scenarios"])
        assert [row["timeout_bound_one_infrastructure_retry_device_hours"] for row in initial["scenarios"][1:]] == pytest.approx([23.3333333333, 35, 46.6666666667])
        assert initial["scenarios"][-1]["fresh_full_selection_timeout_bound_one_retry_device_hours"] == pytest.approx(93.3333333333)
        for i, task in enumerate(tasks[:10]):
            identity = observe_task(store, task, 90 if i == 0 else 100, retry_seconds=10 if i == 0 else None)
            # Production preparation records these beside actual lease costs.
            store.record_cost("upper:" + identity.key, identity.key, "s0", "device", 4200, "upper_bound")
            store.record_cost("estimate:" + identity.key, identity.key, "s0", "device", 1800, "estimate")
        # Neither another artifact nor a targeted diagnostic is a baseline sample.
        observe_task(store, tasks[0], 5000, artifact="child")
        observe_task(store, tasks[1], 5000, kind="targeted")
        budget = store.budget()
        forecast = store.status()["runtime_forecasts"]
        assert forecast["device_hour_cap"] is None
        assert forecast["confirmation_status"] == "not_run"
        assert forecast["root_s0_timing"]["complete"]
        assert forecast["root_s0_timing"]["mean_seconds_per_completed_task_including_observed_retries"] == 100
        assert forecast["root_s0_timing"]["samples"][0]["attempts"] == 2
        scenario = forecast["scenarios"][-1]
        assert scenario["expected_device_hours_from_root"] == pytest.approx(4000 / 3600)
        assert scenario["expected_device_hours_with_25_percent_margin"] == pytest.approx(5000 / 3600)
        assert forecast["reviewer_timeout_bounds"]["total_role_seconds"] == 14400
        assert store.budget() == budget
    finally:
        store.close()


def test_interrupted_lease_bound_does_not_become_expected_runtime(tmp_path):
    store = frozen_store(tmp_path, ["a", "b"], ["a", "b", "c", "d"])
    try:
        observe_task(store, "a", 100)
        observe_task(store, "b", 2100, classification="upper_bound")
        forecast = runtime_forecasts(store)
        timing = forecast["root_s0_timing"]
        assert not timing["complete"] and timing["measured_tasks"] == 1
        assert timing["mean_seconds_per_completed_task_including_observed_retries"] is None
        assert timing["unknown"][0]["task"] == "b"
        assert forecast["scenarios"][-1]["expected_device_hours_with_25_percent_margin"] is None
        assert forecast["scenarios"][-1]["timeout_bound_one_infrastructure_retry_device_hours"] > 0
    finally:
        store.close()


def test_complement_forecast_uses_strata_and_reports_measured_runtime_shares(tmp_path):
    subset = {"tasks": [{"task": "a", "family": "direct", "difficulty": "easy"},
                        {"task": "b", "family": "direct", "difficulty": "hard"}],
              "strata": [{"family": "direct", "difficulty": "easy", "full": 8, "selected": 1},
                         {"family": "direct", "difficulty": "hard", "full": 12, "selected": 1}]}
    store = frozen_store(tmp_path, ["a", "b"], ["a", "b"] + [f"task-{i}" for i in range(18)], subset)
    try:
        observe_task(store, "a", 10)
        observe_task(store, "b", 100)
        forecast = runtime_forecasts(store)
        timing = forecast["root_s0_timing"]
        assert timing["expected_complement_seconds_per_artifact"] == 1170
        assert [row["share_of_measured_root_s0_runtime"] for row in timing["strata"]] == pytest.approx([1 / 11, 10 / 11])
        assert forecast["scenarios"][-1]["expected_device_hours_from_root"] == pytest.approx(4 * 1170 / 3600)
    finally:
        store.close()


def test_reviewer_intervals_preserve_known_partial_work_and_unknown_endpoints(tmp_path):
    store = frozen_store(tmp_path, ["a"], ["a", "b"])
    try:
        completed = {"code_start": 0, "code_finish": 12, "android_start": 1, "android_finish": 10,
                     "s0_ready": 10, "execution_start": 12, "execution_finish": 15, "score_ready": 16}
        interrupted = {"code_start": 0, "android_start": 1, "android_finish": 10,
                       "s0_ready": 10, "execution_start": 13}
        for id, state, events in (("complete", "completed", completed), ("partial", "interrupted", interrupted)):
            store.db.execute("INSERT INTO assessment_timings VALUES(?,?,'root',?,100,120,?)",
                             (id, "baseline", state, canonical_json(events)))
        rows = reviewer_timings(store)["assessments"]
        good, partial = rows
        assert good["code_android_overlap_seconds"] == 9
        assert good["code_wait_after_s0_seconds"] == 2
        assert good["execution_phase_seconds"] == 3
        assert good["added_continuation_seconds"] == 6
        assert partial["android_phase_seconds"] == 9
        assert partial["code_android_overlap_seconds"] is None
        assert partial["code_phase_seconds"] is None
        assert partial["execution_phase_seconds"] is None
        assert partial["added_continuation_seconds"] is None
        assert runtime_forecasts(store)["root_observed_review_timing"] == [good]
        store.db.execute("UPDATE assessment_timings SET events=? WHERE id='complete'",
                         (canonical_json({"code_start": 2, "code_finish": 1}),))
        with pytest.raises(ValueError, match="out of order"):
            reviewer_timings(store)
    finally:
        store.close()


@pytest.mark.parametrize("terminated", [False, True])
@pytest.mark.parametrize("prediction", [.1, .34])
@pytest.mark.parametrize("steps", [1, 2])
def test_value_report_uses_future_discounted_prefix_and_bounds_unknown_tail(tmp_path, terminated, prediction, steps):
    from alpha_agents.tree_search.tests.test_mcts import child_transition, draft, expand, score

    store = TreeStore(tmp_path, FixedPUCTConfig(screen_gate="execution_valid"),
                      Objective("subset", "rubric", "judge", judge_weight=0))
    try:
        store.add_root("root", score("root-artifact", .6, objective=store.objective))
        expand(store)
        trial = store.reserve_trial()
        child_transition(store, trial, store.select_and_reserve(trial), u=.65)
        cap = store.snapshot_frontier(trial)
        forecast = store.persist_value(trial, prediction, cap, {"test": True})
        store.backup(trial, forecast)
        assert value_calibration(store)["assessments"][0]["observations"] == []
        # Use the other root slots so a later trial traverses its stored child.
        for _ in range(2):
            failed = store.reserve_trial()
            selected = store.select_and_reserve(failed)
            store.fail_attempt(selected["attempt"], "Invalid fixture")
            store.backup(failed)
        later = store.reserve_trial()
        selected = store.select_and_reserve(later)
        assert selected["kind"] == "stored"
        store.append_transition(later, selected["position"])
        expand(store, "child", [draft("child-plan", "child mechanism")])
        selected = store.select_and_reserve(later)
        child_transition(store, later, selected, id="grandchild", u=.7)
        endpoint, utility = "grandchild", .7
        if steps == 2:
            expand(store, endpoint, [draft("grandchild-plan", "grandchild mechanism")])
            selected = store.select_and_reserve(later)
            child_transition(store, later, selected, id="great-grandchild", u=.4)
            endpoint, utility = "great-grandchild", .4
        observed = .05 if steps == 1 else .05-.9*.3
        if terminated:
            expand(store, endpoint, drafts=[])
            store.backup(later)
        else:
            current = store.snapshot_frontier(later)
            store.backup(later, store.persist_value(later, .2, current, {"test": True}))
        report = value_calibration(store)
        observation = report["assessments"][0]["observations"][0]
        assert observation["trial"] == later
        assert observation["transitions"] == steps
        assert observation["observed_prefix_return"] == pytest.approx(observed)
        assert observation["genuinely_terminated"] is terminated
        if terminated:
            assert observation["tail_bounds"] == [0, 0]
            assert observation["prediction_minus_observed_return"] == pytest.approx(prediction-observed)
        else:
            assert observation["tail_bounds"] == pytest.approx([-.9**steps*utility, .9**steps*(1-utility)])
            assert observation["compatible_return_interval"] == pytest.approx([observed-.9**steps*utility, observed+.9**steps*(1-utility)])
            assert observation["prediction_minus_observed_return"] is None
            assert observation["outside_possible_tail"] is (prediction == .34)
            # The later model's .2 forecast was NOT used as a realized tail.
            assert observation["observed_prefix_return"] != pytest.approx(observed+.9**steps*.2)
        saved = json.loads(store.db.execute("SELECT capacity FROM values_assessed WHERE id=?", (forecast,)).fetchone()[0])
        saved.pop("observed_path_rowid")
        store.db.execute("UPDATE values_assessed SET capacity=? WHERE id=?", (canonical_json(saved), forecast))
        historical = value_calibration(store)["assessments"][0]
        assert historical["observations"] == []
        assert "traversal cutoff" in historical["unavailable_reason"]
    finally:
        store.close()
