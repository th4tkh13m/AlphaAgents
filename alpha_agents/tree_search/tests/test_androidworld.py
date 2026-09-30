import sys

import pytest

if sys.platform == "win32":
    pytest.skip("AndroidWorld runtime requires Linux/WSL", allow_module_level=True)

import pytest
from alpha_agents.tree_search.bridges.androidworld.bridge import AndroidWorldBridge
from alpha_agents.tree_search.core.contracts import Candidate


@pytest.fixture
def bridge(tmp_path):
    source = tmp_path / "agent"
    source.mkdir()
    return AndroidWorldBridge(
        source,
        {
            "androidworld_api_url": "http://test",
            "base_url": "http://model",
            "model": "test",
            "androidworld_devices": ["device-0"],
        },
    )


def test_mobile_config_is_private_to_each_lease(bridge):
    with bridge.lease() as config:
        assert config["androidworld_assigned_device"] == "device-0"
        config["model"] = "changed"
    with bridge.lease() as config:
        assert config["model"] == "test"
    assert "androidworld_assigned_device" not in bridge.config


def test_androidworld_stages_and_failure_mapping(bridge, tmp_path, monkeypatch):
    from alpha_agents.tree_search.bridges.androidworld import runtime

    calls = []

    def evaluate(workspace, artifacts, stage, config, lock_root):
        calls.append(stage)
        return {
            "status": "completed",
            "planned_denominator": 20,
            "success_rate_pct": 60,
        }

    monkeypatch.setattr(runtime, "run_stage", evaluate)
    candidate = Candidate("initial", None, tmp_path, "base")
    with bridge.lease() as resource:
        result = bridge.evaluate(candidate, resource)
    assert (
        result.status == "completed" and result.score == 0.6 and calls == ["evaluation"]
    )
    monkeypatch.setattr(
        runtime, "run_stage", lambda *a, **k: {"status": "invalid_runtime"}
    )
    with bridge.lease() as resource:
        result = bridge.evaluate(candidate, resource)
    assert result.status == "failed" and result.score is None and result.diagnostic


def test_mobile_evaluation_requires_no_mutator_live_evidence(
    bridge, tmp_path, monkeypatch
):
    from alpha_agents.tree_search.bridges.androidworld import runtime

    monkeypatch.setattr(
        runtime,
        "run_stage",
        lambda *a, **k: {
            "status": "completed",
            "planned_denominator": 2,
            "success_rate_pct": 50,
        },
    )
    with bridge.lease() as resource:
        result = bridge.evaluate(
            Candidate("child", "initial", tmp_path, "base"), resource
        )
    assert result.status == "completed" and not result.diagnostic
