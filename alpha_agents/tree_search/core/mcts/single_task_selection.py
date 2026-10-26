"""Select an observed harness after qualified success or complete single-task rollouts."""

import json
import time
from dataclasses import asdict

from ..shared.storage import write_json
from .contracts import canonical_json
from .ports import StageEvidence
from .success_stop import qualifying_success, stop_receipt


def finalize_single_task(finalizer):
    store = finalizer.store
    task = store.config.evaluation_task
    if finalizer.tasks != (task,):
        raise ValueError("Winner selection must use the frozen single task")
    prior = store.db.execute("SELECT state,payload FROM finalizations WHERE id='final'").fetchone()
    if prior and prior["state"] != "completed":
        raise ValueError("Single-task finalization has an unexpected persisted state")
    if prior and prior["state"] == "completed":
        payload = json.loads(prior["payload"])
    else:
        with store.transaction():
            completed = store.db.execute("SELECT count(*) FROM trials WHERE state='completed'").fetchone()[0]
            backups = store.db.execute("SELECT count(*) FROM backups").fetchone()[0]
            pending = store.db.execute("SELECT 1 FROM trials WHERE state!='completed'").fetchone()
            success = stop_receipt(store)
            if success and qualifying_success(store, success["node"]) != success:
                raise ValueError("Persisted success stop no longer matches its frozen evidence")
            if (pending or (completed != store.config.max_trials and not success)
                    or backups != completed or store.budget()["reserved"]):
                raise ValueError("Single-task selection requires all configured rollouts and backups to finish")
            evaluated = []
            for node in store.db.execute("SELECT * FROM nodes WHERE eligible=1 ORDER BY depth,id"):
                score = store.score(node["id"])
                row = store.db.execute("SELECT payload FROM evaluation_snapshots WHERE id=?", (score.u_snapshot_id,)).fetchone()
                if row is None:
                    raise ValueError("Single-task score requires its canonical observation")
                evidence = json.loads(row["payload"])
                stage = StageEvidence(evidence["artifact_id"], evidence["stage"], evidence["outcomes"],
                                      tuple(evidence["evidence"]), evidence.get("samples"))
                stage.validate_tasks((task,))
                stage.validate_repetitions(store.config.evaluation_repetitions)
                if stage.artifact_id != score.artifact_id or stage.rates[task] != score.u:
                    raise ValueError("Single-task score must match its empirical pass fraction")
                evaluated.append({"node": node["id"], "artifact_id": score.artifact_id,
                                  "depth": node["depth"], "u": score.u, "utility": score.u,
                                  "j": score.j, "f": score.f, "score": asdict(score),
                                  "snapshot_id": score.u_snapshot_id, "outcomes": evidence["outcomes"],
                                  "evidence": evidence["evidence"], "samples": evidence.get("samples"),
                                  "passes": sum(stage.samples[task]) if stage.samples else int(stage.outcomes[task]),
                                  "evaluations": len(stage.samples[task]) if stage.samples else 1})
            candidates = [item for item in evaluated if qualifying_success(store, item["node"])] if success else evaluated
            winner = min(candidates, key=lambda item: (-item["f"], item["depth"], item["artifact_id"], item["node"]))
            actions, node = [], store._row("nodes", winner["node"])
            while node["parent"]:
                attempt = store.db.execute("SELECT id,slot FROM attempts WHERE child=?", (node["id"],)).fetchone()
                actions.append({"parent": node["parent"], "child": node["id"], "plan": node["plan"],
                                "attempt": attempt["id"], "implementation_slot": attempt["slot"]})
                node = store._row("nodes", node["parent"])
            payload = {"selection_status": "completed", "selection_mode": "qualified-single-task-success" if success else "observed-single-task-combined-score",
                       "task": task, "selection_tasks": [task], "objective": asdict(store.objective),
                       "shortlist": evaluated, "evaluated": evaluated, "failures": [], "winner": winner,
                       "selected_actions": list(reversed(actions)), "completed_rollouts": completed,
                       "configured_rollouts": store.config.max_trials, "stopped_early": bool(success and completed < store.config.max_trials),
                       "success_stop": success,
                       "confirmed": False, "confirmation_status": "not_run", "development_evidence": True,
                       "independent_generalization_estimate": False}
    # Idempotent after a crash between freezing the choice and exporting it.
    for candidate in payload["evaluated"]:
        finalizer.artifacts.verify(candidate["artifact_id"])
    winner = payload["winner"]
    if payload.get("success_stop") and not qualifying_success(store, winner["node"]):
        raise ValueError("Frozen early-stop winner no longer satisfies the success rule")
    evidence = StageEvidence(winner["artifact_id"], "s0", winner["outcomes"], tuple(winner["evidence"]), winner.get("samples"))
    evidence.validate_tasks((task,))
    finalizer.evaluator.verify_stage(evidence)
    if not prior:
        with store.transaction():
            store.db.execute("INSERT INTO finalizations VALUES('final','completed',?,?,?)",
                             (canonical_json(payload["evaluated"]), canonical_json(payload), time.time()))
    finalizer.export(payload["winner"]["artifact_id"], store.directory / "winner", provisional=False)
    write_json(store.directory / "final_selection.json", payload)
    write_json(store.directory / "status.json", store.status())
    lines = ["# Single-task MCTS result", "", f"Task: `{task}`. Completed rollouts: {payload['completed_rollouts']}.", "",
             ("Stop after all required fresh repetitions pass and child J > original root J, excluding confirmed manipulation; select among qualifying harnesses."
              if payload.get("success_stop") else "Select the highest observed F = (1 − judge weight) × task success + judge weight × J,"),
             "including intermediate nodes and the root. Equal F prefers lower depth, then artifact ID.",
             "PUCT returns guide allocation; they are not the harness selection score.", "",
             f"Winner: `{payload['winner']['artifact_id']}`.", "",
             "No separate Screen, full-Selection, or Confirmation evaluation was run.",
             "This measures one development task, not generalization or retention of earlier tasks.",
             "Node repetitions run sequentially and stop at the first measured failure. U uses only actual executions; unrun repetitions are not votes.",
             "The adaptively stopped pass fraction is not an unbiased reliability estimate.", "",
             "| Node | Depth | Passes / evaluations | Empirical U | Judge J | Combined F |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
    lines += [f"| `{c['node']}` | {c['depth']} | {c['passes']}/{c['evaluations']} | {c['u']:.3f} | {c['j']:.4f} | {c['f']:.4f} |" for c in payload["evaluated"]]
    (store.directory / "FINAL_REPORT.md").write_text("\n".join(lines) + "\n")
    return payload
