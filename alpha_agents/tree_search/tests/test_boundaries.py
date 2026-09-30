import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from alpha_agents.tree_search import DGMController, SearchConfig, ValidationReport
from alpha_agents.tree_search.cli import build
from alpha_agents.tree_search.core.storage import write_json
from alpha_agents.tree_search.mutators.coding_agents.codex import backend, worker
from alpha_agents.tree_search.mutators.coding_agents.codex.backend import CodexMutator
from alpha_agents.tree_search.validators.changed_pytest import ChangedPytestValidator
from helpers import Harness, Increment


def test_generic_imports_do_not_load_optional_implementations():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import alpha_agents.tree_search; "
            "assert not any(name.startswith('openai_codex') or "
            "name.endswith('androidworld.runtime') for name in sys.modules)",
        ],
        check=True,
    )


class FailingValidator:
    def identity(self):
        return {"type": "failure-test"}

    def validate(self, candidate, context):
        return ValidationReport("failure-test", "failed", error="Regression failure")


def test_validation_failure_does_not_replace_benchmark_score(source, tmp_path):
    state = DGMController(
        Harness(source),
        Increment(),
        tmp_path / "run",
        SearchConfig(max_children=1),
        (FailingValidator(),),
    ).run()
    child = state["records"]["child_000001"]
    assert child["status"] == "completed" and child["evaluation"]["score"] == 0.1
    assert child["evaluation"]["diagnostic"]
    assert child["validation"][0]["status"] == "failed"


def test_context_is_written_once_and_consumed_by_command_backend(
    source, tmp_path, monkeypatch
):
    from alpha_agents.tree_search.core import controller

    examples = Path(__file__).resolve().parents[1] / "examples"
    calls = []
    original = controller.write_json

    def write(path, value):
        if path.name == "mutation_context.json":
            calls.append(path)
        return original(path, value)

    monkeypatch.setattr(controller, "write_json", write)
    cfg = {
        "harness": {
            "type": "command",
            "source": str(examples / "toy_harness"),
            "evaluate_command": [
                sys.executable,
                str(examples / "evaluate_toy.py"),
                "{workspace}",
            ],
        },
        "mutation": {
            "type": "command",
            "command": [sys.executable, str(examples / "mutate_toy.py"), "{workspace}"],
        },
        "search": {"max_children": 1},
    }
    state = build(cfg, tmp_path / "run").run()
    assert (
        len(calls) == 1 and state["records"]["child_000001"]["evaluation"]["score"] == 1
    )


def test_codex_backend_consumes_canonical_context_without_writing_patch(
    source, tmp_path, monkeypatch
):
    from alpha_agents.tree_search.core.contracts import Candidate, MutationContext

    candidate = Candidate("child", "initial", tmp_path, "base")
    context = MutationContext("Improve", "Use evidence", {"parent": "initial"})
    write_json(tmp_path / "mutation_context.json", asdict(context))
    before = (tmp_path / "mutation_context.json").read_bytes()
    calls = []
    monkeypatch.setattr(
        backend,
        "execute",
        lambda command, *args: (
            calls.append(command) or {"timed_out": False, "returncode": 0},
            "",
        ),
    )
    assert CodexMutator().mutate(candidate, context).status == "completed"
    assert calls[0][calls[0].index("--context") + 1] == str(
        tmp_path / "mutation_context.json"
    )
    assert (tmp_path / "mutation_context.json").read_bytes() == before
    assert not candidate.patch.exists()
    assert "--base_commit" not in calls[0]


def test_codex_worker_main_only_executes_engineering_goal(tmp_path, monkeypatch):
    context = tmp_path / "context.json"
    write_json(
        context,
        {
            "objective": "Improve",
            "instructions": "Check",
            "evidence": {"parent": "initial"},
            "protected_paths": ["tasks/*"],
        },
    )
    calls = []
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "worker",
            "--context",
            str(context),
            "--git_dir",
            str(tmp_path),
            "--chat_history_file",
            str(tmp_path / "history.md"),
            "--outdir",
            str(tmp_path),
        ],
    )
    monkeypatch.setattr(
        worker, "run_codex", lambda **kwargs: calls.append(kwargs) or "done"
    )
    worker.main()
    assert calls[0]["problem_statement"] == "Improve"
    assert "tasks/*" in calls[0]["test_description"]
    assert not (tmp_path / "model_patch.diff").exists()


def test_changed_tests_validator_and_controller_capture_new_files(source, tmp_path):
    from alpha_agents.tree_search.core.contracts import MutationOutcome

    class AddTest(Increment):
        def mutate(self, candidate, context):
            super().mutate(candidate, context)
            (candidate.workspace / "test_regression.py").write_text(
                "def test_regression():\n    assert 1 == 2\n"
            )
            return MutationOutcome("completed")

    state = DGMController(
        Harness(source),
        AddTest(),
        tmp_path / "run",
        SearchConfig(max_children=1),
        (ChangedPytestValidator(),),
    ).run()
    child = state["records"]["child_000001"]
    assert child["validation"][0]["status"] == "failed"
    assert child["evaluation"]["score"] == 0.1
    assert (
        "test_regression.py"
        in (tmp_path / "run/child_000001/model_patch.diff").read_text()
    )
