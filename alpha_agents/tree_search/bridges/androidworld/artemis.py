"""Trusted local Artemis execution and AndroidWorld result translation."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import os
import shlex
import shutil
import tempfile
from pathlib import Path

from ...infrastructure.process import execute
from .evaluation import atomic_json
from .runtime import evaluation_lock, read_json
from .task_outcomes import is_bounded_agent_failure

ROLES = (
    "planner",
    "summarizer",
    "operator",
    "validator_pixel_safety_net",
    "operator_summarizer",
    "log_reader_sub_agent",
    "log_analyzer",
    "diagnoser",
    "checker",
    "planner_avatar",
    "history_analyzer_expert",
    "diagnoser_expert",
    "explorer",
)
UTILS = ("outputter", "hopper", "video_analyzer", "object_detector")


def preserve_source(source):
    """Keep required output* modules through the core's artifact exclusion.

    The immutable original harness is never edited. Preserved files are tracked
    under safe names in the bridge snapshot and exposed as ignored symlinks in
    each candidate. Mutations to the backing files are ordinary candidate patches.
    """
    source = Path(source).resolve()
    excluded = {".git", "__pycache__", ".pytest_cache", ".venv", "venv", "runs"}
    files = [
        path
        for path in sorted(source.rglob("*"))
        if path.is_file() and not excluded.intersection(path.relative_to(source).parts)
    ]
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(source)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    snapshot = Path(tempfile.gettempdir()) / (
        "alpha-artemis-source-" + digest.hexdigest()
    )
    if not snapshot.exists():
        temporary = Path(tempfile.mkdtemp(prefix="alpha-artemis-building-"))
        shutil.copytree(
            source,
            temporary,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(*excluded),
        )
        assets = temporary / ".dgm_preserved_modules"
        assets.mkdir()
        mapping = {}
        for path in files:
            relative = path.relative_to(source)
            if any(fnmatch.fnmatchcase(part, "output*") for part in relative.parts):
                name = f"{len(mapping):06d}{path.suffix}"
                shutil.copy2(path, assets / name)
                mapping[str(relative)] = name
        (assets / "mapping.json").write_text(json.dumps(mapping, indent=2))
        try:
            temporary.rename(snapshot)
        except FileExistsError:
            shutil.rmtree(temporary)
    return snapshot


def expose_preserved_modules(candidate):
    assets = candidate.workspace / ".dgm_preserved_modules"
    mapping = read_json(assets / "mapping.json")
    excludes = []
    for relative, name in mapping.items():
        target = candidate.workspace / relative
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(os.path.relpath(assets / name, target.parent))
            excludes.append("/" + relative)
    if excludes:
        with (candidate.directory / "gitdir/info/exclude").open("a") as handle:
            handle.write("\n" + "\n".join(excludes) + "\n")
    return mapping


def json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


def runner_command(worktree, output, tasks, config):
    return [
        config["python"],
        str(Path(__file__).with_name("artemis_runner.py")),
        "--tasks",
        ",".join(tasks),
        "--seed",
        str(config["seed"]),
        "--serial",
        config["androidworld_assigned_device"],
        "--console-port",
        str(config["console_port"]),
        "--grpc-port",
        str(config["grpc_port"]),
        "--adb",
        config["adb_path"],
        "--output-dir",
        str(output),
        "--task-timeout-seconds",
        str(config["task_timeout"]),
        "--llm-hard-timeout-seconds",
        str(config.get("llm_timeout", 180)),
    ]


def execution_environment(worktree, output, config):
    output = Path(output).resolve()
    endpoint = {"provider": "openai", "model": config["model"]}
    role = {
        **endpoint,
        "timeout": config.get("llm_timeout", 180),
        "fallback": dict(endpoint),
    }
    llm = {name: dict(role) for name in ROLES}
    llm["utils"] = {name: dict(role) for name in UTILS}
    llm_path = output / "llm_config.json"
    atomic_json(llm_path, llm)
    env = os.environ.copy()
    env.update(
        DGM_ARTEMIS_WORKTREE=str(Path(worktree).resolve()),
        DGM_ANDROIDWORLD_ROOT=config["androidworld_root"],
        DGM_ARTEMIS_LLM_CONFIG=str(llm_path),
        ARTEMIS_TRACES_DIR=str(output / "runtime"),
        ARTEMIS_MODEL=config["model"],
        OPENAI_BASE_URL=config["base_url"],
        OPENAI_API_KEY=env.get("OPENAI_API_KEY", "EMPTY"),
        LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S=str(config.get("llm_timeout", 180)),
        PYTHONDONTWRITEBYTECODE="1",
    )
    return env


def normalize_manifest(manifest, tasks, config, process):
    """Reject incomplete, duplicate, or mismatched evidence instead of scoring it."""
    errors = []
    if process["timed_out"] or process["returncode"]:
        errors.append("Artemis evaluator timed out or exited unsuccessfully")
    if (
        manifest.get("status"),
        manifest.get("model"),
        manifest.get("model_base_url"),
        manifest.get("seed"),
        manifest.get("tasks"),
        manifest.get("combinations"),
    ) != ("completed", config["model"], config["base_url"], config["seed"], tasks, 1):
        errors.append(
            "Manifest is incomplete or does not match the evaluation contract"
        )
    episodes = manifest.get("episodes", [])
    keys = [(episode.get("template"), episode.get("index")) for episode in episodes]
    if len(keys) != len(tasks) or set(keys) != {(task, 0) for task in tasks}:
        errors.append("Missing, duplicate, or unexpected task instances")
    results = []
    for episode in episodes:
        score = episode.get("success")
        reward = episode.get("androidworld_reward")
        agent_failed = is_bounded_agent_failure(episode)
        valid = agent_failed or (
            isinstance(score, (float, int))
            and not isinstance(score, bool)
            and math.isfinite(score)
            and 0 <= score <= 1
            and isinstance(reward, (float, int))
            and not isinstance(reward, bool)
            and math.isfinite(reward)
            and 0 <= reward <= 1
            and not episode.get("exception")
            and episode.get("artemis_status") in {"completed", "failed"}
            and score == (reward if episode.get("artemis_status") == "completed" else 0)
        )
        if not valid:
            errors.append(f"Invalid task outcome: {episode.get('template')}")
        results.append(
            {
                **json_safe(episode),
                "task_name": episode.get("template"),
                "score": score if valid else None,
                "success": bool(valid and score > 0.5),
                "outcome_status": (
                    "agent_failed" if agent_failed else "completed" if valid else "invalid_runtime"
                ),
                "duration_sec": json_safe(episode.get("seconds", 0)),
            }
        )
    successes = sum(result["success"] for result in results)
    return {
        "runtime": "artemis_local",
        "status": "invalid_runtime" if errors else "completed",
        "model": config["model"],
        "base_url": config["base_url"],
        "seed": config["seed"],
        "planned_tasks": tasks,
        "planned_denominator": len(tasks),
        "successful_tasks": successes,
        "success_rate_pct": 100 * successes / len(tasks) if tasks else 0,
        "task_results": results,
        "errors": errors,
        "manifest": json_safe(manifest),
    }


def run_stage(worktree, output, stage, config, lock_root):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    tasks = read_json(config["task_file"])[stage]
    if (
        not tasks
        or len(tasks) != len(set(tasks))
        or any(not isinstance(t, str) for t in tasks)
    ):
        raise ValueError("Artemis task split must contain unique nonempty task names")
    command = runner_command(worktree, output, tasks, config)
    atomic_json(
        output / "evaluation_request.json",
        {
            "worktree": str(worktree),
            "stage": stage,
            "tasks": tasks,
            "command": command,
            "config": config,
        },
    )
    with evaluation_lock(lock_root, config["androidworld_assigned_device"]):
        env = execution_environment(worktree, output, config)
        process, _ = execute(
            command, Path(worktree), output / "runner", config["stage_timeout"], env
        )
    try:
        manifest = read_json(output / "manifest.json")
    except (OSError, ValueError):
        manifest = {}
    summary = normalize_manifest(manifest, tasks, config, process)
    atomic_json(output / "summary.json", summary)
    return summary


def mutation_context(candidate, parent, resource):
    from ...core.contracts import MutationContext

    tasks = read_json(resource["task_file"])[resource["score_stage"]]
    output = candidate.directory / "mutation_experiment"
    command = runner_command(candidate.workspace, output, tasks[:1], resource)
    env = execution_environment(candidate.workspace, output, resource)
    keys = (
        "DGM_ARTEMIS_WORKTREE",
        "DGM_ANDROIDWORLD_ROOT",
        "DGM_ARTEMIS_LLM_CONFIG",
        "ARTEMIS_TRACES_DIR",
        "ARTEMIS_MODEL",
        "OPENAI_BASE_URL",
        "LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S",
        "PYTHONDONTWRITEBYTECODE",
    )
    experiment = "env " + " ".join(shlex.quote(f"{key}={env[key]}") for key in keys)
    experiment += " OPENAI_API_KEY=EMPTY " + shlex.join(command)
    parent_record = read_json(parent.directory / "candidate.json") if parent else None
    return MutationContext(
        "Improve Artemis general reliability using the parent evaluation evidence.",
        "Edit only this disposable candidate workspace. Original harness directories, "
        "the trusted evaluator, benchmark tasks, rewards, and parent evidence are immutable. "
        "Do not add task-specific rules or fixed coordinates.\n"
        "Required modules named output* are exposed through symlinks because the core "
        "excludes output artifacts. If changing one, edit its tracked backing file listed "
        "in .dgm_preserved_modules/mapping.json; do not replace the ignored symlink.\n"
        "Use ./venv/bin/python for candidate tests. The assigned ADB device is "
        f"{resource['androidworld_assigned_device']} (console {resource['console_port']}, "
        f"gRPC {resource['grpc_port']}). No HTTP AndroidWorld service is required.\n"
        "Inspect .dgm_parent_evidence/candidate.json and .dgm_parent_evidence/stages/ "
        "for task outcomes, exceptions, stdout/stderr, traces, and manifests. "
        "A process exit of zero does not establish benchmark success.\n"
        "Representative run command, using this candidate and the trusted bridge evaluator:\n"
        + experiment
        + "\n"
        f"Read {output}/manifest.json and {output}/runner/ if present; this direct command "
        "prints to your terminal. Independent scoring will run after mutation.",
        {
            "parent_id": parent.id if parent else None,
            "parent_evaluation": parent_record.get("evaluation")
            if parent_record
            else None,
            "parent_evidence": ".dgm_parent_evidence",
            "resource": resource["androidworld_assigned_device"],
            "model": resource["model"],
            "base_url": resource["base_url"],
            "experiment_command": command,
            "experiment_environment": {key: env[key] for key in keys},
            "evaluation_tasks": tasks,
        },
        tuple(resource.get("protected_paths", [])),
    )
