"""Runtime source attestation verifies code identity without reading results."""

import subprocess

import pytest

from alpha_agents.tree_search.bridges.androidworld.runtime_attestation import (
    observe_androidworld,
    verify_androidworld,
    verify_imports,
)
from alpha_agents.tree_search.composition import (
    evaluation_model_identity,
    resolve_config,
)


def test_unknown_model_revision_is_stable_on_resume_and_isolated_between_runs(tmp_path):
    harness = {"model": "Qwen/Qwen3.8-27B-FP8", "base_url": "http://localhost:8001/v1"}
    first = evaluation_model_identity({}, harness, tmp_path / "first")
    assert first.startswith("unverified:Qwen/Qwen3.8-27B-FP8:run:")
    assert evaluation_model_identity({"model_revision": None}, harness, tmp_path / "first") == first
    assert evaluation_model_identity({}, harness, tmp_path / "second") != first
    assert evaluation_model_identity({}, dict(harness, base_url="http://localhost:8002/v1"), tmp_path / "first") != first
    assert evaluation_model_identity({"model_revision": "recorded-revision"}, harness, tmp_path / "first") == "recorded-revision"


def test_config_keeps_virtualenv_interpreter_symlinks(tmp_path):
    import json
    import sys
    from pathlib import Path

    interpreter = tmp_path / "venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    config = json.loads((Path(__file__).parents[1] / "examples" / "fixed_puct.json").read_text())
    config["codex"]["runtime"]["python"] = "venv/bin/python"
    config["codex"]["runtime"]["verification_python"] = "venv/bin/python"
    config["harness"]["config"]["python"] = "venv/bin/python"
    resolved = resolve_config(config, tmp_path)
    assert resolved["codex"]["runtime"]["python"] == str(interpreter)
    assert resolved["codex"]["runtime"]["verification_python"] == str(interpreter)
    assert resolved["harness"]["config"]["python"] == str(interpreter)
    assert config["codex"]["runtime"]["python"] == "venv/bin/python"


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "androidworld"
    package = root / "android_world"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("# Runtime package\n")
    (package / "controller.py").write_text("value = 0\n")
    (root / "run.py").write_text("# Entry point\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Runtime test", "-c", "user.email=runtime@example.invalid",
                    "commit", "-qm", "runtime fixture"], check=True)
    revision = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    return root, revision


def test_observed_runtime_includes_dirty_and_generated_code_but_excludes_measurements(checkout):
    root, revision = checkout
    initial = observe_androidworld(root, revision)
    assert initial["git_commit"] == revision
    assert verify_androidworld(root, initial) == initial
    results = root / "test_runs"
    results.mkdir()
    (results / "protected_results.json").write_text("Do not use these outcomes\n")
    assert observe_androidworld(root, revision) == initial
    (root / "android_world" / "controller.py").write_text("value = 1\n")
    dirty = observe_androidworld(root, revision)
    assert dirty["git_commit"] == initial["git_commit"] and dirty["source_hash"] != initial["source_hash"]
    with pytest.raises(ValueError, match="changed"):
        verify_androidworld(root, initial)
    (root / "android_world" / "generated_pb2.py").write_text("# Generated protocol definitions\n")
    generated = observe_androidworld(root, revision)
    assert generated["source_hash"] != dirty["source_hash"]
    assert generated["package_records"] == dirty["package_records"] + 1


def test_wrong_or_ambiguous_revision_is_rejected_before_model_or_device_work(checkout):
    root, revision = checkout
    with pytest.raises(ValueError, match="full expected Git commit"):
        observe_androidworld(root, revision[:12])
    with pytest.raises(ValueError, match="differs"):
        observe_androidworld(root, "0" * len(revision))
    with pytest.raises(ValueError, match="own Git checkout"):
        observe_androidworld(root / "android_world", revision)


def test_imported_packages_must_match_configured_runtime_and_assigned_harness(checkout, tmp_path):
    root, _ = checkout
    source = tmp_path / "harness"
    package = source / "artemis"
    package.mkdir(parents=True)
    harness = package / "__init__.py"
    harness.write_text("# Harness package\n")
    dependencies = {"android_world": str(root / "android_world" / "__init__.py"), "artemis": str(harness)}
    verify_imports(root, source, dependencies)
    with pytest.raises(ValueError, match="outside the configured"):
        verify_imports(root, source, {**dependencies, "android_world": str(tmp_path / "installed" / "__init__.py")})
    with pytest.raises(ValueError, match="outside the assigned"):
        verify_imports(root, source, {**dependencies, "artemis": str(tmp_path / "other" / "__init__.py")})
