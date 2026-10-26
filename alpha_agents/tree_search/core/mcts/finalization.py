"""Frozen shortlist, full-Selection winner choice, and exact artifact export."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from alpha_agents.tree_search.core.mcts.contracts import canonical_json
from alpha_agents.tree_search.core.mcts.ports import StageEvidence
from alpha_agents.tree_search.core.mcts.reporting import score_breakdowns
from alpha_agents.tree_search.core.mcts.storage import TreeStore
from alpha_agents.tree_search.core.shared.storage import write_json


def shortlist(store: TreeStore):
    root_id = store.db.execute("SELECT value FROM meta WHERE key='root'").fetchone()[0]
    root = dict(store._row("nodes", root_id))
    distinct = {}
    for row in store.db.execute("SELECT * FROM nodes WHERE eligible=1 AND parent IS NOT NULL ORDER BY depth,id"):
        if row["artifact"] == root["artifact"]:
            continue
        utility = store.score(row["id"]).u
        if row["artifact"] in distinct:
            if distinct[row["artifact"]]["u"] != utility:
                raise ValueError("One immutable artifact has conflicting measured S0 utilities")
            continue
        distinct[row["artifact"]] = {"node": row["id"], "artifact_id": row["artifact"], "u": utility,
                                      "depth": row["depth"], "lineage": [row["parent"], row["plan"]]}
    candidates = list(distinct.values())
    chosen, used_lineages = [], set()
    while candidates and len(chosen) < store.config.shortlist_limit:
        best = max(candidate["u"] for candidate in candidates)
        tied = [candidate for candidate in candidates if candidate["u"] == best]
        candidate = min(tied, key=lambda c: (tuple(c["lineage"]) in used_lineages, c["artifact_id"]))
        chosen.append(candidate)
        used_lineages.add(tuple(candidate["lineage"]))
        candidates.remove(candidate)
    baseline = {"node": root_id, "artifact_id": root["artifact"], "u": store.score(root_id).u,
                "depth": 0, "lineage": None}
    return [baseline, *chosen]


class Finalizer:
    def __init__(self, store, artifacts, evaluator, selection_tasks, subset, runtime_config=None):
        self.store, self.artifacts, self.evaluator = store, artifacts, evaluator
        self.tasks, self.subset = tuple(selection_tasks), subset
        self.runtime_config = runtime_config or {}
        if not self.tasks or len(set(self.tasks)) != len(self.tasks):
            raise ValueError("Freeze the complete unique Selection task manifest")

    def freeze_shortlist(self):
        with self.store.transaction():
            existing = self.store.db.execute("SELECT shortlist FROM finalizations WHERE id='final'").fetchone()
            if existing:
                return json.loads(existing[0])
            if self.store.db.execute("SELECT 1 FROM trials WHERE state!='completed'").fetchone():
                raise ValueError("Final Selection must wait for trial recovery and drain")
            if self.store.budget()["reserved"]:
                raise ValueError("Final Selection requires settled model-call reservations")
            frozen = shortlist(self.store)
            self.store.db.execute("INSERT INTO finalizations VALUES('final','pending',?,NULL,?)", (canonical_json(frozen), time.time()))
            return frozen

    def _stage(self, candidate):
        artifact = candidate["artifact_id"]
        prior = self.store.db.execute("SELECT payload FROM evaluation_snapshots WHERE artifact=? AND stage='selection' ORDER BY rowid LIMIT 1", (artifact,)).fetchone()
        if prior:
            data = json.loads(prior[0])
            evidence = StageEvidence(artifact, "selection", data["outcomes"], tuple(data["evidence"]), data.get("samples"))
            self.evaluator.verify_stage(evidence)
        else:
            workspace = self.store.directory / "workspaces" / ("final-" + artifact)
            if workspace.exists():
                # A disposable workspace is only reusable after exact identity
                # verification, never by its path or filename alone.
                from alpha_agents.tree_search.core.mcts.artifacts import manifest
                if manifest(workspace) != self.artifacts.verify(artifact):
                    raise ValueError("Final execution workspace no longer matches its artifact")
            else:
                self.artifacts.materialize(artifact, workspace)
            evidence = self.evaluator.evaluate(artifact, workspace, "selection", self.tasks)
        evidence.validate_tasks(self.tasks)
        if evidence.artifact_id != artifact or evidence.stage != "selection":
            raise ValueError("Final evaluation identifies another artifact or stage")
        self.store.save_evaluation(evidence.identity, artifact, "selection", asdict(evidence))
        return {**candidate, "snapshot_id": evidence.identity, "outcomes": dict(evidence.outcomes),
                "evidence": list(evidence.evidence), "utility": sum(evidence.outcomes.values()) / len(self.tasks)}

    def run(self):
        if self.store.config.evaluation_task:
            from .single_task_selection import finalize_single_task
            return finalize_single_task(self)
        frozen = self.freeze_shortlist()
        prior = self.store.db.execute("SELECT state,payload FROM finalizations WHERE id='final'").fetchone()
        if prior["state"] == "completed":
            payload = json.loads(prior["payload"])
            for candidate in payload["evaluated"]:
                self._stage(candidate)
            self._write_report(payload)
            self.export(payload["winner"]["artifact_id"], self.store.directory / "winner", provisional=False)
            return payload
        preparation_failures = {}
        for candidate in frozen:
            try:
                self.evaluator.prepare(candidate["artifact_id"], {"selection": self.tasks})
            except Exception as error:
                preparation_failures[candidate["artifact_id"]] = str(error)
        evaluated, failures = [], []
        for candidate in frozen:
            try:
                if candidate["artifact_id"] in preparation_failures:
                    raise RuntimeError(preparation_failures[candidate["artifact_id"]])
                evaluated.append(self._stage(candidate))
            except Exception as error:
                failures.append({"artifact_id": candidate["artifact_id"], "error": str(error)})
        payload = {"selection_status": "final_evaluation_incomplete" if failures else "completed",
                   "confirmation_status": "not_run", "confirmed": False,
                   "shortlist": frozen, "configured_shortlist_limit": self.store.config.shortlist_limit,
                   "evaluated": evaluated, "failures": failures, "winner": None,
                   "selection_tasks": list(self.tasks), "objective": asdict(self.store.objective),
                   "development_evidence": True, "independent_generalization_estimate": False}
        if not failures:
            payload["winner"] = min(evaluated, key=lambda c: (-c["utility"], c["depth"] != 0, c["depth"], c["artifact_id"]))
        with self.store.transaction():
            self.store.db.execute("UPDATE finalizations SET state=?,payload=? WHERE id='final'",
                                  ("incomplete" if failures else "completed", canonical_json(payload)))
        self._write_report(payload)
        if payload["winner"]:
            self.export(payload["winner"]["artifact_id"], self.store.directory / "winner", provisional=False)
        return payload

    def export(self, artifact, destination, *, provisional=True):
        destination = Path(destination).resolve()
        self.artifacts.verify(artifact)
        if destination.exists():
            from alpha_agents.tree_search.core.mcts.artifacts import manifest
            if manifest(destination) != self.artifacts.verify(artifact):
                raise ValueError("Export destination already contains a different artifact")
        else:
            self.artifacts.materialize(artifact, destination)
        # Keep export metadata outside the exact harness content.
        write_json(destination.with_name(destination.name + ".export.json"), {
            "artifact_id": artifact, "provisional": provisional, "confirmed": False,
            "confirmation_status": "not_run", "runtime_configuration": self.runtime_config,
            "objective": asdict(self.store.objective)})
        return destination

    def _write_report(self, payload):
        status = self.store.status()
        write_json(self.store.directory / "runtime_forecasts.json", status["runtime_forecasts"])
        scores = score_breakdowns(self.store, self.subset)
        write_json(self.store.directory / "score_breakdowns.json", {
            "objective": asdict(self.store.objective), "nodes": scores,
            "runtime_configuration": self.runtime_config, "confirmed": False})
        write_json(self.store.directory / "status.json", status)
        write_json(self.store.directory / "final_selection.json", payload)
        lines = ["# AlphaAgents final Selection report", "",
                 f"Selection status: **{payload['selection_status']}**. Confirmation: **not run**. Winner is **unconfirmed**.", "",
                 "S0 and full Selection are development evidence. The selected score is not an independent estimate of generalization.", "",
                 f"Screen gate: `{self.store.config.screen_gate}`. Judge weight: `{self.store.objective.judge_weight}`. "
                 f"Configured non-root shortlist limit: `{self.store.config.shortlist_limit}`.", "",
                 f"Allocation return contract: `{canonical_json(status['return_contract'])}`. "
                 "Discounted continuation guides search allocation; final Selection uses measured task utility.", "",
                 "Best retained artifact measurements (separate from allocation returns):", "",
                 "```json", json.dumps(status["retained_performance"], indent=2), "```", "",
                 f"Trials: `{canonical_json(status['trials'])}`. Attempts: `{canonical_json(status['attempts'])}`. "
                 f"Exactly-once backups: `{status['backups']}`.", "",
                 f"Role orchestration invocations and completion reserves: `{canonical_json(status['role_invocations'])}`. "
                 "Internal adapter turns and model usage are separate from this invocation count.", "",
                 "No device-hour cap is applied. Observed device durations, forecasts, and timeout bounds remain separate.", "",
                 "| Artifact | Depth | S0 utility | Full Selection utility |", "| --- | ---: | ---: | ---: |"]
        by_artifact = {candidate["artifact_id"]: candidate for candidate in payload["evaluated"]}
        for candidate in payload["shortlist"]:
            full = by_artifact.get(candidate["artifact_id"])
            lines.append(f"| `{candidate['artifact_id']}` | {candidate['depth']} | {candidate['u']:.6f} | "
                         + (f"{full['utility']:.6f}" if full else "incomplete") + " |")
        winner = payload["winner"]
        if winner:
            baseline = by_artifact[payload["shortlist"][0]["artifact_id"]]
            regressions = [task for task in self.tasks if baseline["outcomes"][task] and not winner["outcomes"][task]]
            gains = [task for task in self.tasks if not baseline["outcomes"][task] and winner["outcomes"][task]]
            lines += ["", f"Selected exact artifact: `{winner['artifact_id']}`.", "",
                      f"Full Selection change from baseline: `{winner['utility'] - baseline['utility']:+.6f}`.", "",
                      f"Per-task gains: `{canonical_json(gains)}`. Regressions: `{canonical_json(regressions)}`."]
        lines += ["", "## Task weights and subset coverage", "", "| S0 task | Weight |", "| --- | ---: |"]
        for task in self.subset.get("tasks", []):
            lines.append(f"| {task['task']} | {task['weight']['display']:.10f} |")
        lines += ["", "```json", json.dumps(self.subset.get("coverage", {}), indent=2), "```", "",
                  "## Observed costs and forecasts", "", "```json", json.dumps(status["costs"], indent=2), "```", "",
                  "Forecasts are informational. The 25% margin adjusts the root-based estimate; "
                  "it is not a device allocation. Timeout bounds and model-review time are separate from expected device hours.", "",
                  "```json", json.dumps(status["runtime_forecasts"], indent=2), "```", "",
                  "## Observed harness-review timing", "",
                  "The unified engineering review starts after canonical S0 evidence is available. Added continuation latency runs from canonical S0 "
                  "availability until a valid combined score is ready. Interrupted intervals remain unknown.", "",
                  "```json", json.dumps(status["reviewer_timings"], indent=2), "```", "",
                  "## Targeted engineering checks", "",
                  "Diagnostics retain their intermediate artifact identities and actual outcomes. They do not replace "
                  "canonical benchmark samples or contribute separately to judge rewards.", "",
                  "```json", json.dumps(status["targeted_checks"], indent=2), "```", "",
                  "## Device queue and assigned-slot utilization", "",
                  "Occupancy measures assigned callback work over each session's configured dispatch window; "
                  "Device lease hours remain in the separate observed-cost ledger. Interrupted durations are unknown.", "",
                  "```json", json.dumps(status["scheduler_metrics"], indent=2), "```", "",
                  "## Value forecasts and observed discounted continuation", "",
                  "Truncated observations keep an unknown discounted tail. Reused transitions are correlated evidence, "
                  "not fresh independent samples. This report does not establish forecast calibration.", "",
                  "```json", json.dumps(status["value_calibration"], indent=2), "```", "",
                  "## Persistent reasoning and statistics", "",
                  "Every backed-up edge retains its measured evaluation and judge reward components and predicted frontier contribution. "
                  "The authoritative SQLite store retains selection-time prior versions, capacity snapshots, role requests, failures, task attempts, and cache reuse.", ""]
        for row in self.store.db.execute("SELECT payload FROM backups ORDER BY rowid"):
            lines += ["```json", json.dumps(json.loads(row[0]), indent=2), "```", ""]
        lines += ["## Per-node measured score breakdowns", "",
                  "U is weighted canonical S0 success; J is the fixed harness rubric aggregate; "
                  "F = (1-lambda) U + lambda J. Parent rewards below are observed changes. "
                  "Predicted continuation contributions remain separate in the backup records above.", ""]
        for node in scores:
            lines += [f"### Node `{node['node_id']}`", "",
                      f"Artifact: `{node['artifact_id']}`. Eligible: `{node['eligible']}`. "
                      f"U/J/F: `{node['u']}` / `{node['j']}` / `{node['f']}`. Lambda: `{node['lambda']}`.", "",
                      "| S0 task | Weight | Canonical success | Weighted contribution |",
                      "| --- | ---: | --- | ---: |"]
            for task in node["weighted_tasks"]:
                lines.append(f"| {task['task']} | {task['weight']:.10f} | {task['success']} | {task['contribution']} |")
            lines += ["", f"S0 coverage: `{canonical_json(node['s0_coverage'])}`. "
                      f"Missing source evidence: `{canonical_json(node['missing_sources'])}`.", ""]
            if node["judge_dimensions"] and "judge_raw_score" in node:
                lines += [f"Raw criterion score: `{node['judge_raw_score']}`. Rated criteria: `{node['judge_rated_criteria']}`.", "",
                          "| Judge dimension | Normalized rating | Rated criteria | Contribution to effective J |",
                          "| --- | ---: | ---: | ---: |"]
                for name, dimension in node["judge_dimensions"].items():
                    lines.append(f"| {name} | {dimension['normalized_score']} | {dimension['rated_count']} | {dimension['aggregate_contribution']:.10f} |")
                lines += ["", "| Criterion | Assessment | Score (0–4) |", "| --- | --- | ---: |"]
                for dimension in node["judge_dimensions"].values():
                    for criterion, rating in dimension["criteria"].items():
                        lines.append(f"| {criterion} | {rating['assessment']} | {rating['score']} |")
                lines += ["", f"Unknown criteria: `{canonical_json(node['unknown_criteria'])}`. "
                          f"Non-applicable criteria: `{canonical_json(node['not_applicable_criteria'])}`. "
                          "Both are excluded from the rated-criterion denominator; no ratings means unavailable.", "",
                          "Integrity conclusion (confirmed manipulation forces effective J to zero):", "", "```json",
                          json.dumps(node["judge_integrity"], indent=2), "```", ""]
            elif node["judge_dimensions"]:
                lines += ["| Judge dimension | Rating | Effective rating | Contribution to J | Unknown reason |",
                          "| --- | ---: | ---: | ---: | --- |"]
                for name, dimension in node["judge_dimensions"].items():
                    reason = str(dimension.get("unknown_reason", "")).replace("|", r"\|").replace("\n", " ")
                    lines.append(f"| {name} | {dimension['score']} | {dimension['effective_rating']} | "
                                 f"{dimension['aggregate_contribution']:.10f} | {reason} |")
                lines += ["", f"Unknown dimensions: `{canonical_json(node['unknown_dimensions'])}`. "
                          "An unknown dimension contributes the declared neutral rating; a missing final review is unavailable.", ""]
            if node["parent_reward"]:
                lines += ["Observed parent reward (raw changes and weighted components):", "", "```json",
                          json.dumps(node["parent_reward"], indent=2), "```", ""]
        lines += ["## Reproducible runtime configuration", "", "```json",
                  json.dumps(self.runtime_config, indent=2), "```", ""]
        if payload["failures"]:
            lines += ["## Incomplete final evaluation", "", "```json", json.dumps(payload["failures"], indent=2), "```", ""]
        (self.store.directory / "FINAL_REPORT.md").write_text("\n".join(lines))
