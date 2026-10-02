from types import SimpleNamespace

import pytest

from alpha_agents.tree_search.bridges.androidworld.device_preflight import (
    check_task_apps,
)


def test_missing_app_in_later_task_prevents_evaluation():
    registry = {
        "First": SimpleNamespace(app_names=("recorder",)),
        "Second": SimpleNamespace(app_names=("notes",)),
    }
    device = SimpleNamespace(list_packages=lambda: {"pkg.recorder": "installed"})
    with pytest.raises(RuntimeError, match="pkg.notes"):
        check_task_apps(
            registry,
            ["First", "Second"],
            device,
            lambda name: "pkg." + name,
            lambda value: value,
        )


def test_preflight_uses_only_the_assigned_task_set():
    registry = {
        "First": SimpleNamespace(app_names=("recorder",)),
        "HeldOut": SimpleNamespace(app_names=("uninstalled",)),
    }
    device = SimpleNamespace(list_packages=lambda: {"pkg.recorder": "installed"})
    result = check_task_apps(
        registry, ["First"], device, lambda name: "pkg." + name, lambda value: value
    )
    assert result == {
        "status": "ready",
        "required_packages": {"recorder": "pkg.recorder"},
    }
