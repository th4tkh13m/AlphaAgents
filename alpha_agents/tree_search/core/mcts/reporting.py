"""Read-only, source-backed score breakdowns for every retained tree node."""

from __future__ import annotations

import json
import math

from alpha_agents.tree_search.core.mcts.judge_review import (
    CRITERIA,
    criterion_aggregate,
    review_score,
)


def reviewer_timings(store) -> dict:
    present = store.db.execute("SELECT 1 FROM sqlite_master WHERE name='assessment_timings'").fetchone()
    rows = list(store.db.execute("SELECT * FROM assessment_timings ORDER BY started,id")) if present else []
    assessments = []
    for row in rows:
        events = json.loads(row["events"])
        def duration(start, end, allow_negative=False):
            if start not in events or end not in events:
                return None
            elapsed = events[end] - events[start]
            if elapsed < 0 and not allow_negative:
                raise ValueError("Assessment phase endpoints are out of order")
            return max(0, elapsed)
        overlap = None
        if all(name in events for name in ("code_start", "code_finish", "android_start", "android_finish")):
            overlap = max(0, min(events["code_finish"], events["android_finish"])
                          - max(events["code_start"], events["android_start"]))
        assessments.append({"assessment_id": row["id"], "owner": row["owner"], "artifact_id": row["artifact"],
            "state": row["state"], "started": row["started"], "finished": row["finished"], "events": events,
            "code_phase_seconds": duration("code_start", "code_finish"),
            "android_phase_seconds": duration("android_start", "android_finish"),
            "code_android_overlap_seconds": overlap,
            "code_wait_after_s0_seconds": duration("s0_ready", "code_finish", allow_negative=True),
            "execution_phase_seconds": duration("execution_start", "execution_finish"),
            "review_phase_seconds": duration("review_start", "review_finish"),
            "added_continuation_seconds": duration("s0_ready", "score_ready")})
    return {"source": "assessment_timings", "scope": "Observed phase elapsed time within each assessment; "
            "includes workspace/checkpoint/validation work and any compatible response reuse. "
            "Actual Codex invocation time is reported separately. Missing or interrupted intervals remain unknown.",
            "assessments": assessments}


def runtime_forecasts(store) -> dict:
    """Informational workload only; never an admission or affordability gate.

    Root S0 task costs include all observed infrastructure attempts and cleanup.
    Unknown/interrupted bounds cannot be converted into observed timings.
    """
    frozen = store.db.execute("SELECT value FROM meta WHERE key='run_identity'").fetchone()
    if frozen is None:
        return {"status": "unavailable", "reason": "Frozen task/runtime identity is unavailable",
                "device_hour_cap": None, "confirmation_status": "not_run"}
    identity = json.loads(frozen[0])
    selection, s0 = set(identity["selection_tasks"]), set(identity["s0_weights"])
    if not s0 <= selection:
        raise ValueError("Runtime forecast requires S0 to belong to frozen Selection")
    root = identity["root_artifact_id"]
    root_reviews = [row for row in reviewer_timings(store)["assessments"]
                    if row["artifact_id"] == root and row["added_continuation_seconds"] is not None]
    costs = {}
    # Planning estimates and total retry bounds share a job owner with leases,
    # but are not additional execution attempts. Keep recovered lease bounds
    # here so an unknown actual duration still prevents an observed forecast.
    for row in store.db.execute("SELECT * FROM costs WHERE resource='device' AND id LIKE 'lease:%'"):
        costs.setdefault(row["owner"], []).append(dict(row))
    jobs = {}
    for row in store.db.execute("SELECT * FROM task_jobs"):
        contract = json.loads(row["contract"])
        if (contract["artifact_id"] == root and contract.get("sample_kind", "canonical") == "canonical"
                and contract["template"] in s0):
            jobs.setdefault(contract["template"], []).append(dict(row))
    samples, unknown = [], []
    for task in sorted(s0):
        candidates = jobs.get(task, [])
        if len(candidates) != 1 or candidates[0]["state"] != "completed":
            unknown.append({"task": task, "reason": "Canonical completed root job is missing or ambiguous"})
            continue
        job = candidates[0]
        leases = costs.get(job["id"], [])
        attempts = store.db.execute("SELECT count(*) FROM task_runs WHERE job=?", (job["id"],)).fetchone()[0]
        if not leases or len(leases) != attempts or any(lease["classification"] != "observed" for lease in leases):
            unknown.append({"task": task, "reason": "Complete observed lease timings are unavailable"})
            continue
        samples.append({"task": task, "job_id": job["id"], "lease_cost_ids": [lease["id"] for lease in leases],
                        "attempts": attempts, "observed_seconds": math.fsum(lease["seconds"] for lease in leases)})
    complete = bool(s0) and not unknown
    mean = math.fsum(sample["observed_seconds"] for sample in samples) / len(samples) if complete else None
    subset = identity.get("subset", {})
    task_metadata = {row["task"]: row for row in subset.get("tasks", [])}
    stratum_timings = []
    total_observed = math.fsum(sample["observed_seconds"] for sample in samples)
    for stratum in subset.get("strata", []):
        selected = [sample for sample in samples if
            (task_metadata.get(sample["task"], {}).get("family"), task_metadata.get(sample["task"], {}).get("difficulty"))
            == (stratum["family"], stratum["difficulty"])]
        duration = math.fsum(sample["observed_seconds"] for sample in selected)
        measured = len(selected)
        stratum_timings.append({**stratum, "measured_tasks": measured, "observed_seconds": duration,
            "share_of_measured_root_s0_runtime": duration / total_observed if total_observed else None,
            "mean_seconds": duration / measured if measured == stratum["selected"] and measured else None})
    if stratum_timings:
        stratum_complete = complete and all(row["mean_seconds"] is not None for row in stratum_timings)
        complement_estimate = math.fsum((row["full"] - row["selected"]) * row["mean_seconds"]
            for row in stratum_timings) if stratum_complete else None
        estimate_method = "Root S0 stratum means multiplied by frozen Selection complement counts"
    else:
        complement_estimate = len(selection - s0) * mean if mean is not None else None
        estimate_method = "Pooled root S0 mean; no frozen stratum metadata is available"
    scenarios = []
    for nominees in range(0, store.config.shortlist_limit + 1):
        artifacts = nominees + 1
        missing = artifacts * len(selection - s0)
        fresh = artifacts * len(selection)
        scenarios.append({"non_root_nominees": nominees, "artifacts_including_baseline": artifacts,
            "paired_complement_tasks": missing, "fresh_full_selection_tasks": fresh,
            "timeout_bound_no_retry_device_hours": missing * 2100 / 3600,
            "timeout_bound_one_infrastructure_retry_device_hours": missing * 2 * 2100 / 3600,
            "fresh_full_selection_timeout_bound_one_retry_device_hours": fresh * 2 * 2100 / 3600,
            "expected_device_hours_from_root": artifacts * complement_estimate / 3600 if complement_estimate is not None else None,
            "expected_device_hours_with_25_percent_margin": artifacts * complement_estimate * 1.25 / 3600 if complement_estimate is not None else None})
    return {"status": "available", "device_hour_cap": None, "confirmation_status": "not_run",
            "scope": "Final Selection only; excludes search, targeted checks, promotions, and Codex time",
            "cache_assumption": "Paired canonical S0 is reusable; additional verified cache reuse may reduce actual work",
            "forecasting_margin": .25, "task_execution_timeout_seconds": 2100, "infrastructure_retry_limit": 1,
            "root_s0_timing": {"artifact_id": root, "required_tasks": len(s0), "measured_tasks": len(samples),
                "complete": complete, "mean_seconds_per_completed_task_including_observed_retries": mean,
                "samples": samples, "unknown": unknown, "strata": stratum_timings,
                "estimate_method": estimate_method,
                "expected_complement_seconds_per_artifact": complement_estimate,
                "assumption": "Root task timing extrapolated to unseen complements and changed harnesses; no runtime guarantee"},
            "reviewer_timeout_bounds": {"code_phase_seconds": 7200, "execution_phase_seconds": 7200,
                "total_role_seconds": 14400, "possible_added_continuation_seconds": 14400,
                "scope": "Per harness assessment; code phase overlaps Android evaluation; model time is not device time"},
            "root_observed_review_timing": root_reviews,
            "scenarios": scenarios}


def value_calibration(store) -> dict:
    """Compare frozen forecasts with later traversals; never invent a tail."""
    backups = {row["trial"]: json.loads(row["payload"]) for row in store.db.execute("SELECT * FROM backups")}
    assessments = []
    for row in store.db.execute("SELECT * FROM values_assessed ORDER BY rowid"):
        cap = json.loads(row["capacity"])
        item = {"value_id": row["id"], "frontier": row["frontier"], "prediction": row["value"],
                "discount_factor": cap.get("discount_factor"),
                "continuation_policy_id": cap.get("continuation_policy_id"), "observations": []}
        provenance = json.loads(row["provenance"])
        if provenance.get("forecast_version") == "value-v2":
            item["forecast"] = provenance
        cutoff = cap.get("observed_path_rowid")
        if cutoff is None:
            item["unavailable_reason"] = "Historical snapshot has no traversal cutoff; timing cannot be established."
            assessments.append(item)
            continue
        gamma = cap["discount_factor"]
        for start in store.db.execute("SELECT trial,position FROM path WHERE parent=? AND rowid>? ORDER BY rowid",
                                      (row["frontier"], cutoff)):
            backup = backups.get(start["trial"])
            if (not backup or backup.get("discount_factor") != gamma or
                    backup.get("continuation_policy_id") != cap["continuation_policy_id"]):
                continue
            suffix = list(store.db.execute("SELECT * FROM path WHERE trial=? AND position>=? ORDER BY position",
                                           (start["trial"], start["position"])))
            observed = math.fsum(gamma**i * (json.loads(edge["reward"])["androidworld"] +
                                           json.loads(edge["reward"])["judge"]) for i, edge in enumerate(suffix))
            endpoint = store.score(suffix[-1]["child"])
            complete = bool(backup["capacity"]["exhausted"])
            weight = gamma**len(suffix)
            tail = [0.0, 0.0] if complete else [-weight*endpoint.f, weight*(1-endpoint.f)]
            interval = [observed+tail[0], observed+tail[1]]
            item["observations"].append({"trial": start["trial"], "transitions": len(suffix),
                "observed_prefix_return": observed, "genuinely_terminated": complete,
                "tail_bounds": tail, "compatible_return_interval": interval,
                "prediction_minus_observed_return": row["value"]-observed if complete else None,
                "outside_possible_tail": row["value"] < interval[0]-1e-12 or row["value"] > interval[1]+1e-12,
                "reused_transition_count": sum(store._row("attempts", edge["attempt"])["trial"] != start["trial"]
                                               for edge in suffix)})
        assessments.append(item)
    return {"scope": "Descriptive comparison with later completed trial suffixes under matching discount/policy. "
                     "Reused evidence and adaptive selection are correlated; these are not independent calibration samples. "
                     "Truncated prefixes retain bounds on the unobserved discounted tail, not a substitute zero or final-minus-initial score.",
            "assessments": assessments}


def retained_performance(store) -> dict:
    """Measured retained-artifact quality, independent of discounted allocation."""
    candidates = []
    for node in store.db.execute("SELECT id,artifact,depth FROM nodes WHERE eligible=1 AND score IS NOT NULL ORDER BY id"):
        score = store.score(node["id"])
        candidates.append({"node_id": node["id"], "artifact_id": node["artifact"], "depth": node["depth"],
                           "u": score.u, "j": score.j, "f": score.f, "score_snapshot_id": score.identity})
    return {"scope": "Measured canonical S0 and combined scores of eligible retained artifacts; not discounted returns or held-out results.",
            "best_s0": min(candidates, key=lambda c: (-c["u"], c["depth"], c["artifact_id"])) if candidates else None,
            "best_combined": min(candidates, key=lambda c: (-c["f"], c["depth"], c["artifact_id"])) if candidates else None}


def score_breakdowns(store, subset: dict) -> list[dict]:
    weights = {task["task"]: task["weight"]["display"] for task in subset.get("tasks", [])}
    result = []
    for node in store.db.execute("SELECT * FROM nodes ORDER BY depth,id"):
        item = {"node_id": node["id"], "artifact_id": node["artifact"], "parent": node["parent"],
                "depth": node["depth"], "eligible": bool(node["eligible"]),
                "lambda": store.objective.judge_weight, "u": None, "j": None, "f": None,
                "weighted_tasks": [], "judge_dimensions": None, "unknown_dimensions": None,
                "parent_reward": None, "missing_sources": []}
        score = store.score(node["id"]) if node["score"] else None
        if score:
            item.update(u=score.u, j=score.j, f=score.f, score_snapshot_id=score.identity,
                        u_snapshot_id=score.u_snapshot_id, j_snapshot_id=score.j_snapshot_id)
            observed = store.db.execute("SELECT payload FROM evaluation_snapshots WHERE id=?", (score.u_snapshot_id,)).fetchone()
            judged = store.db.execute("SELECT payload FROM judge_snapshots WHERE id=?", (score.j_snapshot_id,)).fetchone()
        else:
            observed = store.db.execute("SELECT payload FROM evaluation_snapshots WHERE artifact=? AND stage='s0' ORDER BY rowid LIMIT 1", (node["artifact"],)).fetchone()
            judged = None
        observation = json.loads(observed[0]) if observed else {}
        outcomes = observation.get("outcomes", {})
        samples = observation.get("samples") or {}
        if not observed:
            item["missing_sources"].append("canonical_s0_outcomes")
        for task, weight in sorted(weights.items()):
            success = outcomes.get(task)
            rate = sum(samples[task]) / len(samples[task]) if task in samples else success
            contribution = {"task": task, "weight": weight, "success": success,
                            "contribution": weight * rate if isinstance(rate, (bool, int, float)) else None}
            if task in samples:
                contribution.update(samples=samples[task], pass_rate=rate,
                                    passes=sum(samples[task]), evaluations=len(samples[task]))
            item["weighted_tasks"].append(contribution)
        item["s0_coverage"] = {"completed": sum(isinstance(outcomes.get(task), bool) for task in weights),
                                "required": len(weights)}
        if score and weights and item["s0_coverage"]["completed"] == len(weights):
            measured = math.fsum(task["contribution"] for task in item["weighted_tasks"])
            if not math.isclose(measured, score.u, abs_tol=1e-12, rel_tol=0):
                raise ValueError("Report task contributions disagree with frozen evaluation utility")
        if judged:
            evidence = store.judge_evidence(score.j_snapshot_id)
            dimensions = evidence["payload"]["dimensions"]
            if "narrative_review" in evidence:
                item["judge_narrative_review"] = evidence["narrative_review"]
            aggregate, unknown = review_score(evidence["payload"])
            if not math.isclose(aggregate, score.j, abs_tol=1e-12, rel_tol=0):
                raise ValueError("Report judge dimensions disagree with frozen judge utility")
            if set(dimensions) == set(CRITERIA):
                raw, _ = criterion_aggregate(dimensions)
                rated = sum(c["assessment"] == "rated" for group in dimensions.values() for c in group.values())
                item["judge_raw_score"] = raw
                item["judge_integrity"] = evidence["payload"]["integrity"]
                item["judge_rated_criteria"] = rated
                item["unknown_criteria"] = list(unknown)
                item["not_applicable_criteria"] = [id for group in dimensions.values() for id, c in group.items() if c["assessment"] == "not_applicable"]
                item["judge_dimensions"] = {}
                for name, group in dimensions.items():
                    values = [c["score"] / 4 for c in group.values() if c["assessment"] == "rated"]
                    item["judge_dimensions"][name] = {"criteria": group, "rated_count": len(values),
                        "normalized_score": math.fsum(values) / len(values) if values else None,
                        "aggregate_contribution": math.fsum(values) / rated if aggregate == raw else 0.0}
            else:
                item["judge_dimensions"] = {name: {**dimension, "effective_rating": .5 if dimension["score"] is None else dimension["score"],
                    "aggregate_contribution": (.5 if dimension["score"] is None else dimension["score"]) / 3}
                    for name, dimension in sorted(dimensions.items())}
                item["unknown_dimensions"] = list(unknown)
        else:
            item["missing_sources"].append("canonical_judge_dimensions")
        if score and node["parent"]:
            parent = store.score(node["parent"])
            reward = parent.reward_to(score)
            item["parent_reward"] = {"delta_u": score.u - parent.u, "delta_j": score.j - parent.j,
                                     "weighted_android": reward.evaluation, "weighted_judge": reward.judge,
                                     "total": reward.total}
        result.append(item)
    return result


def dispatch_metrics(store) -> dict:
    """Assigned-slot occupancy, distinct from Android lease cost or forecasts.

    Each session's denominator is configured pool size times its first queued
    to last finished dispatch window. Restart downtime is not combined into a
    session. Unfinished/interrupted callbacks leave utilization unavailable.
    """
    present = store.db.execute("SELECT 1 FROM sqlite_master WHERE name='task_dispatches'").fetchone()
    rows = list(store.db.execute("SELECT * FROM task_dispatches ORDER BY queued,id")) if present else []
    grouped, states = {}, {}
    for row in rows:
        grouped.setdefault(row["session"], []).append(row)
        states[row["state"]] = states.get(row["state"], 0) + 1
    sessions = []
    for session, dispatches in grouped.items():
        pools = {row["pool_size"] for row in dispatches}
        settled = all(row["state"] in {"completed", "failed", "not_started"} and row["finished"] is not None for row in dispatches)
        timed = all(row["callback_seconds"] is not None for row in dispatches if row["started"] is not None)
        window = max(row["finished"] for row in dispatches) - min(row["queued"] for row in dispatches) if settled else None
        busy = math.fsum(row["callback_seconds"] for row in dispatches if row["callback_seconds"] is not None)
        pool = next(iter(pools)) if len(pools) == 1 else None
        denominator = window * pool if window is not None and window > 0 and pool else None
        sessions.append({"session_id": session, "pool_size": pool, "dispatches": len(dispatches),
                         "observed_callback_seconds": busy, "dispatch_window_seconds": window,
                         "assigned_slot_utilization": busy / denominator if denominator and timed else None})
    waits = [row["queue_delay"] for row in rows if row["queue_delay"] is not None]
    return {"source": "task_dispatches", "scope": "assigned callback work over each session's configured dispatch window",
            "states": states, "sessions": sessions, "queue_delay_samples": len(waits),
            "mean_queue_delay_seconds": math.fsum(waits) / len(waits) if waits else None,
            "max_queue_delay_seconds": max(waits) if waits else None,
            "unknown_callback_durations": sum(row["started"] is not None and row["callback_seconds"] is None for row in rows)}
