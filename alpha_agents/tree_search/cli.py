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
    return DGMController(
        bridge,
        mutator,
        output,
        SearchConfig(**config.get("search", {})),
        tuple(validators),
    )


def main():
    parser = argparse.ArgumentParser(description="Harness-independent DGM search")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO"
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(message)s"
    )
    config = resolve_config(
        json.loads(args.config.read_text(encoding="utf-8")),
        args.config.resolve().parent,
    )
    state = build(config, args.output).run(resume=args.resume)
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
