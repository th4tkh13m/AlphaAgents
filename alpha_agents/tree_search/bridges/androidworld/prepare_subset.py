"""Deterministic S0 preparation from benchmark metadata, never outcomes."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import re
from collections import Counter
from fractions import Fraction
from pathlib import Path

from alpha_agents.tree_search.core.mcts.contracts import fingerprint

ALGORITHM = "quota-app-tag-sha256-v1"
QUOTAS = {("Direct", "easy"): 3, ("Direct", "hard"): 3, ("Direct", "medium"): 2,
          ("Information retrieval", "easy"): 1, ("Information retrieval", "medium"): 1}


def prepare(manifest: dict, seed: int = 42) -> dict:
    rows = []
    partitions = {"screen": [], "selection": [], "confirmation": []}
    seen = set()
    for source in manifest["rows"]:
        stage = source["stage"].lower()
        task = source["task"]
        if stage not in partitions or not isinstance(task, str) or not task.strip() or task in seen:
            raise ValueError("Manifest tasks require unique nonempty IDs and known partitions")
        seen.add(task)
        partitions[stage].append(task)
        tags = source.get("tags", "")
        tags = re.split(r"[\s,]+", tags.strip()) if isinstance(tags, str) else tags
        # Only Selection metadata is admitted as development evidence. Confirmation
        # membership is retained to reject submission, not its outcomes/trajectories.
        if stage == "selection":
            rows.append({"task": task, "stage": stage, "family": source["family"],
                         "difficulty": source["difficulty"], "app_domain": source["app_domain"],
                         "tags": sorted(set(tag for tag in tags if tag))})
    rows.sort(key=lambda r: r["task"])
    groups = {key: [r for r in rows if (r["family"], r["difficulty"]) == key] for key in sorted(QUOTAS)}
    if sum(map(len, groups.values())) != len(rows):
        raise ValueError("Selection contains an unconfigured family/difficulty stratum")
    if any(len(groups[key]) < QUOTAS[key] for key in groups):
        raise ValueError("Selection manifest cannot satisfy the frozen quotas")
    choices = [itertools.combinations(groups[key], QUOTAS[key]) for key in groups]
    priorities = {row["task"]: int(hashlib.sha256(f"{seed}:{row['task']}".encode()).hexdigest(), 16) for row in rows}
    best = None
    best_key = None
    for product in itertools.product(*choices):
        selected = sorted(itertools.chain.from_iterable(product), key=lambda row: row["task"])
        apps = {r["app_domain"] for r in selected}
        tags = {t for r in selected for t in r["tags"]}
        key = (-len(apps), -len(tags), sum(priorities[r["task"]] for r in selected), tuple(r["task"] for r in selected))
        if best_key is None or key < best_key:
            best, best_key = selected, key
    weighted = []
    for row in best:
        group = (row["family"], row["difficulty"])
        weight = Fraction(len(groups[group]), len(rows) * QUOTAS[group])
        weighted.append({**row, "weight": {"numerator": weight.numerator, "denominator": weight.denominator,
                                          "display": float(weight)}})
    weights = [Fraction(r["weight"]["numerator"], r["weight"]["denominator"]) for r in weighted]
    if sum(weights) != 1:
        raise ValueError("Subset weights do not preserve full-Selection stratum proportions")
    apps = {r["app_domain"] for r in rows}
    selected_apps = {r["app_domain"] for r in best}
    tags = {t for r in rows for t in r["tags"]}
    selected_tags = {t for r in best for t in r["tags"]}
    counts = Counter(r["app_domain"] for r in rows)
    selected_counts = Counter(r["app_domain"] for r in best)
    selected_ids = {r["task"] for r in best}
    result = {
        "algorithm": ALGORITHM, "seed": seed,
        # Canonical permitted fields only: invariant to input ordering and to
        # changes in historical success, runtime, or held-out evidence fields.
        "source_manifest_hash": fingerprint({"selection": rows, "partitions": {k: sorted(v) for k, v in partitions.items()}}),
        "tasks": weighted, "selection": sorted(partitions["selection"]),
        "screen": sorted(partitions["screen"]), "protected_confirmation": sorted(partitions["confirmation"]),
        "complement": sorted(set(partitions["selection"]) - selected_ids),
        "strata": [{"family": key[0], "difficulty": key[1], "full": len(groups[key]), "selected": QUOTAS[key]} for key in groups],
        "coverage": {"app_labels": {"selected": len(selected_apps), "full": len(apps), "omitted": sorted(apps - selected_apps)},
                     "apps": [{"label": name, "full": counts[name], "selected": selected_counts[name]} for name in sorted(apps)],
                     "tags": {"selected": len(selected_tags), "full": len(tags), "omitted": sorted(tags - selected_tags)},
                     "failure_modes": "unknown", "runtime_representativeness": "unmeasured",
                     "effective_weighted_tasks": float(1 / sum(w * w for w in weights))},
    }
    result["manifest_id"] = fingerprint(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = prepare(json.loads(args.manifest.read_text()), args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
