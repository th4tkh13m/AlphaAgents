"""Argument-list template expansion shared by external commands."""

from collections.abc import Mapping


def expand_command(command: list[str], values: Mapping[str, str]) -> list[str]:
    try:
        return [argument.format_map(values) for argument in command]
    except KeyError as error:
        raise ValueError(f"Unknown command placeholder: {error.args[0]}") from error
