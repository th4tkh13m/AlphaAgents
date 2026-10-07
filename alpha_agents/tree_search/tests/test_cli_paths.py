"""Executable paths must preserve the Python virtual-environment prefix."""

import sys

import pytest

from alpha_agents.tree_search.cli import resolve_config


@pytest.mark.parametrize("absolute", [False, True])
def test_validator_python_preserves_virtual_environment_symlink(tmp_path, absolute):
    interpreter = tmp_path / "venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    config = {
        "harness": {"source": str(tmp_path / "source")},
        "mutation": {},
        "validation": [
            {
                "type": "changed_pytest",
                "python": str(interpreter) if absolute else "venv/bin/python",
            }
        ],
    }
    resolved = resolve_config(config, tmp_path)
    assert resolved["validation"][0]["python"] == str(interpreter)
    assert resolved["validation"][0]["python"] != str(interpreter.resolve())
