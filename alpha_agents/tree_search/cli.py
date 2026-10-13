"""Configuration composition lives here, outside the search controller."""

import argparse
import json
import logging
from pathlib import Path

from .core.dgm.controller import DGMController, SearchConfig


def resolve_config(config: dict, base: Path) -> dict:
    """Resolve local paths relative to the config file, independent of cwd."""
    config = json.loads(json.dumps(config))
    if config.get("controller", {}).get("type") == "fixed_puct":
        from alpha_agents.tree_search.composition import resolve_config as resolve_fixed
        return resolve_fixed(config, base)

    def path(value):
        result = Path(value).expanduser()
        return str(
            (base / result).resolve() if not result.is_absolute() else result.resolve()
        )

    harness = config["harness"]
    harness["source"] = path(harness["source"])
    for key in ("evaluator_cwd",):
        if harness.get(key):
            harness[key] = path(harness[key])
    for key in ("task_file", "venv_source"):
        if harness.get("config", {}).get(key):
            harness["config"][key] = path(harness["config"][key])
    mutation = config["mutation"]
    if mutation.get("prompt_template"):
        mutation["prompt_template"] = path(mutation["prompt_template"])
    for validator in config.get("validation", []):
        interpreter = validator.get("python")
        if interpreter and (base / interpreter).is_file():
            validator["python"] = path(interpreter)
    for section, key in ((harness, "evaluate_command"), (mutation, "command")):
        if key in section:
            section[key] = [
                path(arg) if "{" not in arg and (base / arg).is_file() else arg
                for arg in section[key]
            ]
    return config


def build(config: dict, output: Path):
    if config.get("controller", {}).get("type") == "fixed_puct":
        from alpha_agents.tree_search.composition import build as build_fixed
        return build_fixed(config, output)
    if config.get("controller", {}).get("type", "dgm") != "dgm":
        raise ValueError("Unknown controller type")
    harness = config["harness"]
    if harness["type"] == "command":
        from alpha_agents.tree_search.bridges.command import CommandBridge

        bridge = CommandBridge(
            Path(harness["source"]),
            harness["evaluate_command"],
            instructions=harness.get("instructions", ""),
            resources=tuple(harness.get("resources", ["local"])),
            timeout=harness.get("timeout", 300),
            evaluator_cwd=Path(harness["evaluator_cwd"])
            if harness.get("evaluator_cwd")
            else None,
            protected_paths=tuple(harness.get("protected_paths", [])),
            evaluator_version=harness.get("evaluator_version", ""),
        )
    elif harness["type"] == "androidworld":
        from alpha_agents.tree_search.bridges.androidworld.bridge import (
            AndroidWorldBridge,
        )

        bridge = AndroidWorldBridge(Path(harness["source"]), harness.get("config", {}))
    else:
        raise ValueError("Unknown harness type")
    mutation = config["mutation"]
    if mutation["type"] == "command":
        from alpha_agents.tree_search.mutators.command import CommandMutator

        mutator = CommandMutator(mutation["command"], mutation.get("timeout", 300))
    elif mutation["type"] == "codex":
        from alpha_agents.tree_search.mutators.coding_agents.codex.backend import (
            CodexMutator,
        )

        mutator = CodexMutator(
            model=mutation.get("model"),
            effort=mutation.get("effort"),
            timeout=mutation.get("timeout"),
            prompt_template=Path(mutation["prompt_template"])
            if mutation.get("prompt_template")
            else None,
        )
    else:
        raise ValueError("Unknown mutation type")
    validators = []
    for settings in config.get("validation", []):
        if settings["type"] == "changed_pytest":
            import sys

            from alpha_agents.tree_search.validators.changed_pytest import (
                ChangedPytestValidator,
            )

            validators.append(
                ChangedPytestValidator(
                    settings.get("python", sys.executable), settings.get("timeout", 300)
                )
            )
        else:
            raise ValueError("Unknown validation type")
    return DGMController(
        bridge,
        mutator,
        output,
        SearchConfig(**config.get("search", {})),
        tuple(validators),
    )


def main():
    parser = argparse.ArgumentParser(description="Harness search: DGM or fixed-PUCT controller")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--command", choices=["run", "status", "finalize", "export", "grant-invocations", "grant-task-recovery"], default="run")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--stop-after-trials", type=int, help="Drain after N additional admitted fixed-PUCT trials")
    parser.add_argument("--no-finalize", action="store_true")
    parser.add_argument("--artifact")
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--invocation-cap", type=int)
    parser.add_argument("--recovery-reason")
    parser.add_argument("--task-job")
    parser.add_argument("--recovery-id", help="Stable unique ID for one explicitly requested task recovery round")
    parser.add_argument(
        "--stop-after-children",
        type=int,
        help="Stop after this many additional child lifecycles, keeping the total search budget for resume",
    )
    parser.add_argument(
        "--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO"
    )
    args = parser.parse_args()
    if args.command in {"status", "export", "grant-invocations", "grant-task-recovery"}:
        from alpha_agents.tree_search.core.mcts.storage import TreeStore
        inspected = TreeStore.readonly(args.output)
        try:
            if args.command == "status":
                print(json.dumps(inspected.status(), indent=2))
                return
            if args.command == "export":
                if not args.artifact or not args.destination:
                    parser.error("export requires --artifact and --destination")
                from alpha_agents.tree_search.core.mcts.artifacts import ArtifactStore
                from alpha_agents.tree_search.core.mcts.finalization import Finalizer
                artifact_root = args.output / "artifacts"
                if not artifact_root.is_dir():
                    raise ValueError("Run has no frozen artifact store")
                identity = inspected.db.execute("SELECT value FROM meta WHERE key='run_identity'").fetchone()
                runtime = json.loads(identity[0])["runtime_identity"] if identity else {}
                final = inspected.db.execute("SELECT state,payload FROM finalizations WHERE id='final'").fetchone()
                provisional = not (final and final["state"] == "completed" and
                    json.loads(final["payload"])["winner"]["artifact_id"] == args.artifact)
                finalizer = Finalizer(inspected, ArtifactStore(artifact_root), None, ("export-only",), {}, runtime)
                finalizer.export(args.artifact, args.destination, provisional=provisional)
                print(json.dumps({"artifact_id": args.artifact, "destination": str(args.destination), "confirmed": False}))
                return
            if args.command == "grant-task-recovery":
                if not args.task_job or not args.recovery_reason or not args.recovery_id:
                    parser.error("grant-task-recovery requires --task-job, --recovery-reason and --recovery-id")
            elif not args.invocation_cap or not args.recovery_reason:
                parser.error("grant-invocations requires --invocation-cap and --recovery-reason")
            with inspected.lock():
                writable = TreeStore(args.output, inspected.config, inspected.objective)
                try:
                    if args.command == "grant-task-recovery":
                        writable.grant_task_recovery(args.task_job, args.recovery_reason, args.recovery_id)
                    else:
                        writable.grant_invocations(args.invocation_cap, args.recovery_reason)
                    print(json.dumps(writable.status(), indent=2))
                finally:
                    writable.close()
            return
        finally:
            inspected.close()
    if args.config is None:
        parser.error("run/finalize requires --config")
    args.output.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(args.output / "search.log", delay=True),
        ],
    )
    config = resolve_config(
        json.loads(args.config.read_text(encoding="utf-8")),
        args.config.resolve().parent,
    )
    controller = build(config, args.output)
    if config.get("controller", {}).get("type") == "fixed_puct":
        import signal
        previous = {}
        def request_stop(signum, frame):
            if controller.stop.is_set():
                raise KeyboardInterrupt
            controller.request_stop()
        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.signal(signum, request_stop)
            if args.preflight_only:
                state = controller.preflight()
            elif args.command == "finalize":
                state = controller.finalize()
            else:
                state = controller.run(resume=args.resume, stop_after_trials=args.stop_after_trials,
                                       finalize=False if args.no_finalize else None)
            print(json.dumps(state, indent=2))
        finally:
            try:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
            finally:
                controller.close()
        return
    if args.command != "run" or args.preflight_only or args.stop_after_trials is not None:
        parser.error("Selected operator command requires controller.type=fixed_puct")
    state = controller.run(
        resume=args.resume, stop_after_children=args.stop_after_children
    )
    print(
        json.dumps(
            {
                "children": state["completed_children"],
                "archive": state["archive"],
                "diagnostic": state["diagnostic"],
                "retained": state["retained"],
            },
            indent=2,
        )
    )
