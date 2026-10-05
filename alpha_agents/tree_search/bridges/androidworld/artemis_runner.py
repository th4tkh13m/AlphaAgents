#!/usr/bin/env python3
"""Run Artemis on AndroidWorld using AndroidWorld's own task and reward APIs.

Run from the repository root with a Python 3.12 Artemis dependency environment:
  python artemis_runner.py --help

The full benchmark is long-running. Start with --tasks ClockStopWatchRunning,
then remove --tasks to evaluate every AndroidWorld task template.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ARTEMIS_ROOT = Path(os.environ["DGM_ARTEMIS_WORKTREE"]).resolve()
ANDROID_WORLD_ROOT = Path(os.environ["DGM_ANDROIDWORLD_ROOT"]).resolve()
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ARTEMIS_ROOT))
sys.path.insert(0, str(ANDROID_WORLD_ROOT))

from answer_handoff import AgentAnswer, submit_answer
from task_outcomes import bounded_agent_failure


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tasks",
        help="Comma-separated task template names; default is all AndroidWorld tasks",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--combinations", type=int, default=1)
    parser.add_argument(
        "--serial", default=os.environ.get("ANDROID_SERIAL", "emulator-5560")
    )
    parser.add_argument(
        "--console-port",
        type=int,
        default=int(os.environ.get("ANDROID_CONSOLE_PORT", "5558")),
    )
    parser.add_argument(
        "--grpc-port",
        type=int,
        default=int(os.environ.get("ANDROID_GRPC_PORT", "8558")),
    )
    parser.add_argument("--adb", default=os.environ.get("ANDROID_ADB", "adb"))
    parser.add_argument(
        "--perform-emulator-setup",
        action="store_true",
        help="Install/configure AndroidWorld apps once before evaluation",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path.cwd() / "artemis_results"
    )
    parser.add_argument(
        "--max-steps-multiplier",
        type=int,
        default=10,
        help="AndroidWorld task complexity multiplier, default 10",
    )
    parser.add_argument(
        "--task-timeout-seconds",
        type=int,
        default=900,
        help="Wall clock limit for each Artemis task",
    )
    parser.add_argument(
        "--llm-hard-timeout-seconds",
        type=int,
        default=int(os.environ.get("ARTEMIS_LLM_HARD_TIMEOUT_SECONDS", "180")),
        help="Agent model-call hard timeout and stream chunk timeout in seconds",
    )
    parser.add_argument(
        "--artemis-steps-multiplier",
        type=int,
        default=100,
        help="Artemis graph recursion budget per AndroidWorld complexity point",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip task instances already present in the result manifest",
    )
    parser.add_argument(
        "--artemis-config", type=Path, help="Optional Artemis JSONC config override"
    )
    return parser.parse_args()


async def main_async(args: argparse.Namespace) -> int:
    from adbutils import AdbClient
    from android_world import checkpointer, registry
    from android_world.env import adb_utils, env_launcher
    from android_world.task_evals.information_retrieval.information_retrieval import (
        InformationRetrieval,
    )
    from artemis import Agent, Builders, ConcurrencyMode
    from artemis.config import load_agent_config, load_llm_config_override
    from artemis.context import DevicePlatform
    from artemis.sdk.types.task import AgentProfile
    from artemis.services.llm import invoke_llm_with_timeout_message
    from runtime_limits import configure_llm_timeout

    invocation_timeout = configure_llm_timeout(
        invoke_llm_with_timeout_message, args.llm_hard_timeout_seconds
    )
    print(f"Agent LLM invocation timeout: {invocation_timeout} seconds", flush=True)

    if args.artemis_config:
        config_path = args.artemis_config.resolve()
        if not config_path.is_file():
            raise SystemExit(f"Artemis config file does not exist: {config_path}")
        os.environ["ARTEMIS_ARTEMIS_JSONC"] = str(config_path)
    memory_config = load_agent_config()

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if os.getenv("DGM_ARTEMIS_OPENAI_MEMORY_COMPAT", "1") == "1":
        from memory_compat import configure_openai_memory

        runtime_config = configure_openai_memory(
            memory_config.model_dump(mode="json"), output
        )
        os.environ["ARTEMIS_ARTEMIS_JSONC"] = str(runtime_config)
        memory_config = load_agent_config(runtime_config)
        print(
            "OpenAI memory compatibility: Google-only visual summarization and "
            "chunk compression disabled; transcript configuration retained",
            flush=True,
        )
    traces = output / "traces"
    traces.mkdir(exist_ok=True)

    adb = AdbClient(
        host=os.getenv("ADB_HOST", "127.0.0.1"), port=int(os.getenv("ADB_PORT", "5037"))
    )
    devices = [d.serial for d in adb.device_list()]
    if args.serial not in devices:
        raise SystemExit(
            f"Android device {args.serial!r} is not online in ADB; connected devices: {devices}"
        )

    model_url = os.getenv("OPENAI_BASE_URL", "http://localhost:8001/v1")
    os.environ["OPENAI_BASE_URL"] = model_url
    from model_health import wait_for_model

    model_name = os.getenv("ARTEMIS_MODEL", "Qwen/Qwen3.8-27B-FP8")
    wait_for_model(model_url, model_name)

    task_registry = registry.TaskRegistry().get_registry(
        registry.TaskRegistry.ANDROID_WORLD_FAMILY
    )
    selected = [
        name.strip()
        for name in (args.tasks.split(",") if args.tasks else task_registry)
    ]
    unknown = sorted(set(selected) - task_registry.keys())
    if unknown:
        raise SystemExit(f"Unknown AndroidWorld task templates: {unknown}")
    if not selected:
        raise SystemExit("No AndroidWorld task templates selected")

    manifest_path = output / "manifest.json"
    aw_revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ANDROID_WORLD_ROOT, text=True
    ).strip()
    prior_episodes = []
    if args.resume and manifest_path.exists():
        previous = json.loads(manifest_path.read_text())
        if (
            previous.get("model"),
            previous.get("model_base_url"),
            previous.get("seed"),
            previous.get("combinations"),
            previous.get("tasks"),
            previous.get("androidworld_revision"),
            previous.get("llm_invocation_timeout_seconds", 180),
        ) != (
            model_name,
            model_url,
            args.seed,
            args.combinations,
            selected,
            aw_revision,
            invocation_timeout,
        ):
            raise SystemExit(
                "Refusing to resume with a changed model, endpoint, task list, seed, combination count, AndroidWorld revision, or LLM invocation timeout"
            )
        prior_episodes = previous.get("episodes", [])
    prior_keys = {
        (episode["template"], int(episode["index"])) for episode in prior_episodes
    }
    metadata = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "agent": "Artemis",
        "artemis_source": str(ARTEMIS_ROOT),
        "artemis_import": str(sys.modules["artemis"].__file__),
        "androidworld_source": str(ANDROID_WORLD_ROOT),
        "model": model_name,
        "model_base_url": model_url,
        "androidworld_revision": aw_revision,
        "tasks": selected,
        "seed": args.seed,
        "combinations": args.combinations,
        "adb_serial": args.serial,
        "perform_emulator_setup": args.perform_emulator_setup,
        "androidworld_version": "checkout",
        "configured_llm_stream_timeout_seconds": args.llm_hard_timeout_seconds,
        "llm_invocation_timeout_seconds": invocation_timeout,
        "llm_stream_chunk_timeout_seconds": os.getenv(
            "LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S",
            str(args.llm_hard_timeout_seconds),
        ),
        "video_recording_enabled": os.getenv(
            "ARTEMIS_VIDEO_RECORDING_ENABLED", "auto"
        ).lower()
        not in {"false", "0", "no", "off"}
        and os.getenv("ARTEMIS_WITH_VIDEO_RECORDING_TOOLS", "auto").lower()
        not in {"false", "0", "no", "off"},
        "artemis_config_override": os.getenv("ARTEMIS_ARTEMIS_JSONC"),
        "memory_settings": {
            "openai_compatibility_enabled": os.getenv(
                "DGM_ARTEMIS_OPENAI_MEMORY_COMPAT", "1"
            )
            == "1",
            "step_summarizer_enabled": memory_config.flash.step_summarizer.enabled,
            "transcript_enabled": memory_config.memory.transcript.enabled,
            "image_scrub_depth": memory_config.memory.transcript.image_scrub_depth,
            "image_scrub_depth_relaxed": memory_config.memory.transcript.image_scrub_depth_relaxed,
            "pending_grace_steps": memory_config.memory.transcript.pending_grace_steps,
            "chunking_enabled": memory_config.memory.chunking.enabled,
        },
        "status": "running",
        "episodes": prior_episodes,
    }
    manifest_path.write_text(json.dumps(metadata, indent=2) + "\n")

    env = env_launcher.load_and_setup_env(
        console_port=args.console_port,
        grpc_port=args.grpc_port,
        adb_path=args.adb,
        emulator_setup=args.perform_emulator_setup,
        freeze_datetime=True,
    )
    # create_suite instantiates every registry task before applying `tasks`.
    # Build only the selected entries with the same AndroidWorld seed scheme.
    import hashlib
    import random

    suite = {}
    for name in selected:
        task_type = task_registry[name]
        instances = []
        for index in range(args.combinations):
            instance_seed = int(
                hashlib.sha256(f"{args.seed}_{name}_{index}".encode()).hexdigest(), 16
            ) % (2**32)
            random.seed(instance_seed)
            params = task_type.generate_random_params()
            params["seed"] = instance_seed
            task_type.set_device_time(env)
            instances.append(task_type(params))
        suite[name] = instances
    suite = dict(sorted(suite.items()))

    from device_preflight import check_task_apps

    # Information-retrieval templates populate app_names on their instances.
    selected_instances = {
        f"{name}[{index}]": task
        for name, instances in suite.items()
        for index, task in enumerate(instances)
    }
    metadata["device_preflight"] = check_task_apps(
        selected_instances,
        list(selected_instances),
        adb.device(args.serial),
        adb_utils.get_adb_activity,
        adb_utils.extract_package_name,
    )
    manifest_path.write_text(json.dumps(metadata, indent=2) + "\n")
    print("Device preflight ready: all selected task apps are installed", flush=True)

    if os.getenv("DGM_ANDROIDWORLD_FIXTURE_PREFLIGHT", "1") == "1":
        from fixture_preflight import check_task_fixtures

        def record_fixture(record):
            metadata.setdefault("fixture_preflight_progress", []).append(record)
            manifest_path.write_text(json.dumps(metadata, indent=2) + "\n")
            print(f"Fixture preflight: {record['task']} {record['status']}", flush=True)

        metadata["fixture_preflight"] = check_task_fixtures(
            selected_instances, env, record_fixture
        )
        if metadata["fixture_preflight"]["status"] != "ready":
            metadata["status"] = "failed"
            metadata["infrastructure_error"] = "Benchmark fixture initialization failed"
            manifest_path.write_text(json.dumps(metadata, indent=2) + "\n")
            env.close()
            raise RuntimeError(metadata["infrastructure_error"])
        manifest_path.write_text(json.dumps(metadata, indent=2) + "\n")

    profile = AgentProfile(
        name="androidworld-qwen",
        llm_config=load_llm_config_override(Path(os.environ["DGM_ARTEMIS_LLM_CONFIG"])),
    )
    config = (
        Builders.AgentConfig.with_default_profile(profile=profile)
        .for_device(DevicePlatform.ANDROID, args.serial)
        .build(validate_profiles=False)
    )
    checkpointer_dir = output / "checkpoints"
    checkpoint = checkpointer.IncrementalCheckpointer(str(checkpointer_dir))

    successes = sum(float(ep.get("success", 0)) > 0.5 for ep in prior_episodes)
    failures = len(prior_episodes) - successes
    try:
        for task_name, instances in suite.items():
            for index, task in enumerate(instances):
                if (task_name, index) in prior_keys:
                    continue
                # A service outage between episodes is infrastructure failure,
                # not evidence that the next benchmark task is difficult.
                try:
                    wait_for_model(model_url, model_name)
                except RuntimeError as exc:
                    metadata["status"] = "failed"
                    metadata["infrastructure_error"] = str(exc)
                    raise
                started = time.time()
                print(
                    f"Task started: {task_name}[{index}] device={args.serial}",
                    flush=True,
                )
                agent = Agent(
                    config=config, concurrency_mode=ConcurrencyMode.PER_DEVICE
                )
                task_record = {
                    "template": task_name,
                    "index": index,
                    "seed": task.params.get("seed"),
                    "goal": task.goal,
                }
                failure_phase = "setup"
                try:
                    await agent.init(device_serial=args.serial)
                    task.initialize_task(env)
                    env.reset(go_home=task.start_on_home_screen)
                    request = agent.new_task(task.goal)
                    requires_answer = isinstance(task, InformationRetrieval)
                    if requires_answer:
                        request.with_output_format(AgentAnswer)
                    request.using_profile("androidworld-qwen")
                    request.with_name(f"androidworld-{task_name}-{index}")
                    # Artemis counts internal graph nodes as well as UI actions.
                    request.with_max_steps(
                        max(100, int(args.artemis_steps_multiplier * task.complexity))
                    )
                    request.with_trace_recording(enabled=True, path=str(traces))
                    failure_phase = "agent_execution"
                    agent_output = await asyncio.wait_for(
                        agent.run_task(request=request.build()),
                        timeout=args.task_timeout_seconds,
                    )
                    failure_phase = "answer_submission"
                    artemis_run = agent._tasks[-1]
                    artemis_completed = artemis_run.status == "completed"
                    if requires_answer and artemis_completed:
                        task_record["agent_answer"] = submit_answer(env, agent_output)
                        task_record["answer_submitted"] = True
                    failure_phase = "grading"
                    androidworld_reward = float(task.is_successful(env))
                    score = androidworld_reward if artemis_completed else 0.0
                    failure_phase = "teardown"
                    task.tear_down(env)
                    task_record["androidworld_reward"] = androidworld_reward
                    task_record["artemis_status"] = artemis_run.status
                    task_record["artemis_error"] = (
                        artemis_run.result.error if artemis_run.result else None
                    )
                    task_record.update(
                        success=score,
                        exception=None,
                        seconds=round(time.time() - started, 2),
                    )
                    successes += int(score > 0.5)
                    failures += int(score <= 0.5)
                except Exception as exc:
                    task_record.update(
                        success=0.0,
                        exception=f"{type(exc).__name__}: {exc}",
                        failure_phase=failure_phase,
                        seconds=round(time.time() - started, 2),
                    )
                    task_record.update(bounded_agent_failure(exc, failure_phase))
                    failures += 1
                finally:
                    if task.initialized:
                        try:
                            task.tear_down(env)
                        except Exception:
                            pass
                    await agent.clean()
                metadata["episodes"].append(task_record)
                checkpoint.save_episodes([task_record], f"{task_name}_{index}")
                metadata["successful"] = successes
                metadata["failed"] = failures
                metadata["success_rate"] = successes / (successes + failures)
                manifest_path.write_text(json.dumps(metadata, indent=2) + "\n")
                print(
                    f"[{successes + failures}/{len(selected) * args.combinations}] {task_name}[{index}] score={task_record['success']} rate={metadata['success_rate']:.3f}",
                    flush=True,
                )
        metadata["status"] = "completed"
    finally:
        metadata["finished_at"] = datetime.now(timezone.utc).isoformat()
        metadata["successful"] = successes
        metadata["failed"] = failures
        metadata["success_rate"] = (
            successes / (successes + failures) if successes + failures else None
        )
        manifest_path.write_text(json.dumps(metadata, indent=2) + "\n")
    env.close()
    return 0


def main() -> int:
    args = parse_args()
    os.environ.setdefault(
        "ARTEMIS_TRACES_DIR", str(args.output_dir.resolve() / "runtime")
    )
    os.environ.setdefault("OPENAI_API_KEY", "EMPTY")
    os.environ.setdefault("OPENAI_BASE_URL", "http://localhost:8001/v1")
    os.environ.setdefault(
        "LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S",
        str(args.llm_hard_timeout_seconds),
    )
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
