"""Configuration composition lives here, outside the search controller."""

import argparse
import json
import logging
from pathlib import Path

from .core.controller import DGMController, SearchConfig


def resolve_config(config: dict, base: Path) -> dict:
    """Resolve local paths relative to the config file, independent of cwd."""
    config = json.loads(json.dumps(config))

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
    for key in ("task_file", "venv_source", "task_metadata_file"):
        if harness.get("config", {}).get(key):
            harness["config"][key] = path(harness["config"][key])
    mutation = config["mutation"]
    if config.get("archive_access", {}).get("enabled"):
        config["archive_access"]["import_runs"] = [
            path(value) for value in config["archive_access"].get("import_runs", [])
        ]
    if harness.get("config", {}).get("reuse_root_from"):
        harness["config"]["reuse_root_from"] = path(harness["config"]["reuse_root_from"])
    if mutation.get("prompt_template"):
        mutation["prompt_template"] = path(mutation["prompt_template"])
    for validator in config.get("validation", []):
        interpreter = validator.get("python")
        if interpreter and (base / interpreter).is_file():
            # A virtual environment is identified by the invoked executable's
            # path. Resolving its symlink selects the base Python instead.
            validator["python"] = str((base / Path(interpreter).expanduser()).absolute())
    for section, key in ((harness, "evaluate_command"), (mutation, "command")):
        if key in section:
            section[key] = [
                path(arg) if "{" not in arg and (base / arg).is_file() else arg
                for arg in section[key]
            ]
    return config


def build(config: dict, output: Path):
    harness = config["harness"]
    if harness["type"] == "command":
        from .bridges.command import CommandBridge

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
        from .bridges.androidworld.bridge import AndroidWorldBridge

        bridge = AndroidWorldBridge(Path(harness["source"]), harness.get("config", {}))
    else:
        raise ValueError("Unknown harness type")
    mutation = config["mutation"]
    if mutation["type"] == "command":
        from .mutators.command import CommandMutator

        mutator = CommandMutator(mutation["command"], mutation.get("timeout", 300))
    elif mutation["type"] == "codex":
        from .mutators.coding_agents.codex.backend import CodexMutator

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

            from .validators.changed_pytest import ChangedPytestValidator

            validators.append(
                ChangedPytestValidator(
                    settings.get("python", sys.executable), settings.get("timeout", 300)
                )
            )
        else:
            raise ValueError("Unknown validation type")
    archive_provider = None
    if config.get("archive_access", {}).get("enabled"):
        from .archive.catalog import ArchiveProvider
        from .bridges.androidworld.archive import AndroidWorldArchive

        if harness["type"] != "androidworld" or bridge.config["evaluation_runner"] != "artemis_local":
            raise ValueError("Archive access currently requires local Artemis/AndroidWorld")
        archive_provider = ArchiveProvider(
            config["archive_access"], AndroidWorldArchive(bridge.config),
            config.get("transfer_assessment", {}),
        )
    return DGMController(
        bridge,
        mutator,
        output,
        SearchConfig(**config.get("search", {})),
        tuple(validators),
        archive_provider=archive_provider,
    )


def main():
    parser = argparse.ArgumentParser(description="Harness-independent DGM search")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--stop-after-children",
        type=int,
        help="Stop after this many additional child lifecycles, keeping the total search budget for resume",
    )
    parser.add_argument(
        "--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO"
    )
    args = parser.parse_args()
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
    state = build(config, args.output).run(
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
