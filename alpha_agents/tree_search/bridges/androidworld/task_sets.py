"""Load the frozen manifest without using its historical outcomes as scores."""

import json
from pathlib import Path

STAGES = ("screen", "selection", "confirmation", "evaluation")


def load_sets(path):
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if "rows" in manifest:
        sets = {stage: [] for stage in STAGES if stage != "evaluation"}
        for row in manifest["rows"]:
            stage = row["stage"].lower()
            if stage not in sets:
                raise ValueError(f"Unknown manifest stage: {row['stage']}")
            sets[stage].append(row["task"])
    else:
        sets = {stage: manifest[stage] for stage in STAGES if stage in manifest}
    for stage, tasks in sets.items():
        if not isinstance(tasks, list) or any(
            not isinstance(task, str) or not task.strip() for task in tasks
        ):
            raise ValueError(f"Invalid task list for {stage}")
        if len(tasks) != len(set(tasks)):
            raise ValueError(f"Duplicate task in {stage}")
    if "confirmation" in sets:
        groups = [
            set(sets.get(stage, []))
            for stage in ("screen", "selection", "confirmation")
        ]
        if any(groups[i] & groups[j] for i in range(3) for j in range(i + 1, 3)):
            raise ValueError("Screen, Selection, and Confirmation must be disjoint")
    return sets


def stage_tasks(path, stage):
    sets = load_sets(path)
    # Keep existing configurations usable; their scoring set is now Selection.
    if stage == "evaluation" and stage not in sets:
        stage = "selection"
    if stage not in sets or not sets[stage]:
        raise ValueError(f"No tasks defined for {stage}")
    return list(sets[stage])
