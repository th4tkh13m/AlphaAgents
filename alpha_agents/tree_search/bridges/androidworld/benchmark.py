"""Private full-benchmark audits and isolated parallel Artemis task batches."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import logging
import math
from pathlib import Path
import subprocess
import time

from ...core.contracts import RequiredEvaluationError
from ...core.storage import read_json, write_json
from ...core.workspace import git
from . import artemis
from .runtime import evaluation_lock
from .task_sets import load_sets, stage_tasks

logger = logging.getLogger(__name__)


def digest_files(root, names):
    digest = sha256()
    for name in sorted(names):
        path = root / name
        if path.is_file():
            digest.update(name.encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()


def cache_identity(workspace, config):
    """Scheduling and device addresses do not change paired task instances."""
    tracked = git(workspace, "ls-files", "-z").split("\0")
    evaluator = Path(config["androidworld_root"])
    evaluator_names = subprocess.run(
        ["git", "-C", str(evaluator), "ls-files", "-z"],
        check=True, capture_output=True, text=True,
    ).stdout.split("\0")
    bridge = Path(__file__).parent
    packages = subprocess.run(
        [config["python"], "-c", "import importlib.metadata,json; print(json.dumps(sorted((d.metadata['Name'],d.version) for d in importlib.metadata.distributions())))"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    keys = (
        "evaluation_runner", "model", "model_revision", "base_url", "seed",
        "confirmation_seed", "task_timeout", "llm_timeout", "max_steps",
        "openai_memory_compatibility", "fixture_preflight", "fixture_identity",
        "perform_emulator_setup",
    )
    return {
        "schema": 1,
        "agent_sha256": digest_files(workspace, tracked),
        "evaluator_sha256": digest_files(evaluator, evaluator_names),
        "bridge_sha256": digest_files(bridge, [p.name for p in bridge.glob("*.py")]),
        "packages": json.loads(packages),
        "task_sets": load_sets(config["task_file"]),
        "settings": {key: config.get(key) for key in keys},
    }


def merge_summaries(summaries, tasks, config):
    results = [item for summary in summaries for item in summary.get("task_results", [])]
    errors = [error for summary in summaries for error in summary.get("errors", [])]
    names = [item.get("task_name") for item in results]
    if len(names) != len(tasks) or set(names) != set(tasks):
        errors.append("Missing, duplicate, or unexpected parallel task results")
    if any(summary.get("status") != "completed" for summary in summaries):
        errors.append("At least one evaluator batch did not complete normally")
    for item in results:
        score = item.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
            errors.append(f"Invalid score for {item.get('task_name')}")
    successes = sum(item.get("success") is True for item in results)
    by_name = {item.get("task_name"): item for item in results}
    return {
        "runtime": "artemis_local", "status": "invalid_runtime" if errors else "completed",
        "model": config["model"], "base_url": config["base_url"], "seed": config["seed"],
        "planned_tasks": tasks, "planned_denominator": len(tasks),
        "successful_tasks": successes,
        "success_rate_pct": 100 * successes / len(tasks),
        "task_results": [by_name[task] for task in tasks if task in by_name],
        "errors": errors,
    }


def run_parallel_stage(workspace, output, stage, configs, lock_root):
    """One serial runner per leased device, with disjoint task assignments."""
    output = Path(output)
    tasks = stage_tasks(configs[0]["task_file"], stage)
    configs = configs[:len(tasks)]
    jobs = [{"worker": i, "device": config["androidworld_assigned_device"], "tasks": tasks[i::len(configs)]} for i, config in enumerate(configs)]
    write_json(output / "progress.json", {"status": "running", "stage": stage, "jobs": jobs})

    def run(job):
        i = job["worker"]
        return artemis.run_stage(
            workspace, output / f"worker_{i:02d}", stage, configs[i], lock_root,
            tasks=job["tasks"],
        )

    with ThreadPoolExecutor(max_workers=len(configs)) as executor:
        summaries = list(executor.map(run, jobs))
    summary = merge_summaries(summaries, tasks, configs[0])
    write_json(output / "summary.json", summary)
    write_json(output / "progress.json", {"status": summary["status"], "stage": stage, "jobs": jobs})
    return summary


def _cached_stage(output, stage, configs):
    """Re-normalize actual process/manifest evidence rather than trusting a score."""
    try:
        jobs = read_json(output / "progress.json")["jobs"]
        summaries = []
        for job in jobs:
            directory = output / f"worker_{job['worker']:02d}"
            summaries.append(artemis.normalize_manifest(
                read_json(directory / "manifest.json"), job["tasks"], configs[0],
                read_json(directory / "runner/process.json"),
            ))
        summary = merge_summaries(summaries, stage_tasks(configs[0]["task_file"], stage), configs[0])
        return summary if summary["status"] == "completed" else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def full_benchmark(candidate, configs, cache_root, identity, *, report_path=None):
    """All 116 outcomes stay outside candidate directories and mutation evidence."""
    cache_root = Path(cache_root).resolve()
    if cache_root.is_relative_to(candidate.directory.resolve()):
        raise ValueError("Benchmark cache must be outside candidate evidence")
    key = sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    output = cache_root / key
    output.mkdir(parents=True, exist_ok=True)
    config = configs[0]
    summaries = {}
    if report_path is not None:
        write_json(report_path, {"status": "running", "candidate": candidate.id, "cache_key": key, "report": str(output / "audit.json")})
    with evaluation_lock(cache_root, "cache-" + key):
        write_json(output / "identity.json", identity)
        write_json(output / "audit.json", {"status": "running", "candidate": candidate.id, "cache_key": key})
        for stage in ("screen", "selection", "confirmation"):
            write_json(output / "audit.json", {"status": "running", "candidate": candidate.id, "cache_key": key, "current_stage": stage, "completed_stages": list(summaries)})
            stage_configs = [dict(value, seed=config["confirmation_seed"] if stage == "confirmation" else config["seed"]) for value in configs]
            directory = output / stage
            summary = _cached_stage(directory, stage, stage_configs)
            reused = summary is not None
            if summary is None:
                if directory.exists():
                    directory.rename(output / f"{stage}.incomplete.{time.time_ns()}")
                summary = run_parallel_stage(candidate.workspace, directory, stage, stage_configs, cache_root)
            summaries[stage] = summary
            write_json(directory / "summary.json", summary)
            logger.info("Full benchmark candidate=%s stage=%s reused=%s status=%s", candidate.id, stage, reused, summary["status"])
            write_json(output / "audit.json", {"status": "running", "candidate": candidate.id, "cache_key": key, "completed_stages": list(summaries)})
            if summary["status"] != "completed":
                write_json(output / "audit.json", {"status": "failed", "candidate": candidate.id, "stage": stage, "errors": summary["errors"]})
                raise RequiredEvaluationError(f"Required full benchmark failed in {stage}: {directory}")
        tasks = [task for stage in summaries for task in summaries[stage]["planned_tasks"]]
        combined = merge_summaries(list(summaries.values()), tasks, config)
        combined.update(candidate=candidate.id, cache_key=key, stages={stage: {"tasks": value["planned_denominator"], "success_rate_pct": value["success_rate_pct"]} for stage, value in summaries.items()})
        combined["mean_reward"] = sum(item["score"] for item in combined["task_results"]) / len(tasks)
        write_json(output / "audit.json", combined)
    report = {"status": combined["status"], "candidate": candidate.id, "tasks": len(tasks), "success_rate_pct": combined["success_rate_pct"], "mean_reward": combined["mean_reward"], "cache_key": key, "report": str(output / "audit.json")}
    if report_path is not None:
        write_json(report_path, report)
    return summaries["selection"], report, output
