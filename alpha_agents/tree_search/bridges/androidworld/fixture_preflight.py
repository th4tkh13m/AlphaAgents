"""Validate trusted fixture initialization before any agent evaluation."""

import copy
import random
import time


def check_task_fixtures(instances, env, on_result=None):
    results = []
    random_state = random.getstate()
    try:
        for name, original in instances.items():
            started = time.monotonic()
            record = {"task": name, "status": "ready"}
            task = None
            try:
                # Some benchmark constructors consume params (e.g. pop("img"))
                # and retain the value as an attribute. Copy the already-built
                # instance so preflight neither reconstructs incomplete params
                # nor mutates the instance used for agent evaluation.
                task = copy.deepcopy(original)
                task.initialize_task(env)
            except Exception as exc:
                record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            finally:
                try:
                    if task is not None:
                        task.tear_down(env)
                except Exception as exc:
                    record.update(
                        status="failed", cleanup_error=f"{type(exc).__name__}: {exc}"
                    )
            record["seconds"] = round(time.monotonic() - started, 2)
            results.append(record)
            if on_result:
                on_result(record)
    finally:
        random.setstate(random_state)
    return {
        "status": "ready" if all(r["status"] == "ready" for r in results) else "failed",
        "tasks": results,
    }
