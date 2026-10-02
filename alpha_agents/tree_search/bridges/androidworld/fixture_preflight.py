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
            task = type(original)(copy.deepcopy(original.params))
            record = {"task": name, "status": "ready"}
            try:
                task.initialize_task(env)
            except Exception as exc:
                record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            finally:
                try:
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
