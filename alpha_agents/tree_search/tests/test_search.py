import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from alpha_agents.tree_search import (
    Candidate,
    DGMController,
    Evaluation,
    MutationContext,
    SearchConfig,
)
from alpha_agents.tree_search.bridges.command import CommandBridge
from alpha_agents.tree_search.core.contracts import MutationOutcome
from alpha_agents.tree_search.core.storage import read_json, write_json
from alpha_agents.tree_search.core.workspace import materialize
from alpha_agents.tree_search.mutators.command import CommandMutator
from helpers import Harness, Increment


@pytest.mark.parametrize("scheduling", ["synchronous", "asynchronous"])
def test_search_lineage_and_resume(source, tmp_path, scheduling):
    config = SearchConfig(
        max_children=4, workers=2, batch_size=2, selection="best", scheduling=scheduling
    )
    output = tmp_path / "run"
    bridge = Harness(source)
    state = DGMController(bridge, Increment(), output, config).run()
    assert state["completed_children"] == 4
    assert len(state["archive"]) == 5
    assert not state["pending"] and bridge.leases == 0
    assert max(r["evaluation"]["score"] for r in state["records"].values()) >= 0.2
    state = DGMController(
        bridge, Increment(), output, replace(config, max_children=6)
    ).run(resume=True)
    assert state["completed_children"] == 6
    assert len(state["records"]) == 7
    assert (source / "value.txt").read_text() == "0"


def test_partial_mutation_is_evaluated(source, tmp_path):
    state = DGMController(
        Harness(source),
        Increment(fail=True),
        tmp_path / "run",
        SearchConfig(max_children=1),
    ).run()
    child = next(r for r in state["records"].values() if r["parent_id"])
    assert child["status"] == "completed"
    assert child["evaluation"]["score"] == 0.1
    assert child["id"] in state["diagnostic"]


@pytest.mark.parametrize("scheduling", ["synchronous", "asynchronous"])
def test_stop_after_one_evaluated_child_then_resume_same_archive(
    source, tmp_path, scheduling
):
    output = tmp_path / "run"
    config = SearchConfig(
        max_children=4, workers=4, batch_size=4, scheduling=scheduling
    )
    bridge = Harness(source)
    state = DGMController(bridge, Increment(), output, config).run(
        stop_after_children=1
    )
    assert state["completed_children"] == 1
    assert state["budget"] == 4
    assert state["stop_reason"] == "invocation_limit"
    assert not state["pending"] and bridge.leases == 0
    assert state["records"]["child_000001"]["evaluation"]["status"] == "completed"
    first_child = read_json(output / "child_000001/candidate.json")
    baseline = read_json(output / "initial/candidate.json")
    state = DGMController(bridge, Increment(), output, config).run(
        resume=True, stop_after_children=1
    )
    assert state["completed_children"] == 2
    assert state["records"]["initial"] == baseline
    assert state["records"]["child_000001"] == first_child
    assert not state["pending"] and bridge.leases == 0
    state = DGMController(bridge, Increment(), output, config).run(resume=True)
    assert state["completed_children"] == 4
    assert state["stop_reason"] == "budget_complete"
    assert not state["pending"] and bridge.leases == 0


def test_no_patch_does_not_get_scored(source, tmp_path):
    state = DGMController(
        Harness(source),
        Increment(no_patch=True),
        tmp_path / "run",
        SearchConfig(max_children=1),
    ).run()
    child = next(r for r in state["records"].values() if r["parent_id"])
    assert child["status"] == "mutation_failed"
    assert child["evaluation"]["score"] is None
    assert child["id"] not in state["archive"]


def test_runtime_failure_remains_diagnostic(source, tmp_path):
    state = DGMController(
        Harness(source, fail=True),
        Increment(),
        tmp_path / "run",
        SearchConfig(max_children=2),
    ).run()
    assert not state["archive"]
    assert len(state["diagnostic"]) == 3
    assert all(r["evaluation"]["score"] is None for r in state["records"].values())


def test_reject_resume_contract_change(source, tmp_path):
    output = tmp_path / "run"
    DGMController(
        Harness(source), Increment(), output, SearchConfig(max_children=0)
    ).run()
    with pytest.raises(ValueError, match="contract"):
        DGMController(
            Harness(source), Increment(), output, SearchConfig(max_children=1, seed=7)
        ).run(resume=True)


def test_resume_recovers_pending_patch(source, tmp_path):
    output = tmp_path / "run"
    config = SearchConfig(max_children=0)
    controller = DGMController(Harness(source), Increment(), output, config)
    controller.run()
    candidate = materialize(
        source, output, "interrupted", controller.store.candidate("initial")
    )
    (candidate.workspace / "value.txt").write_text("3")
    state = read_json(output / "state.json")
    state["pending"] = [{"id": candidate.id, "parent_id": "initial"}]
    write_json(output / "state.json", state)
    state = DGMController(
        Harness(source),
        Increment(),
        output,
        replace(config, max_children=2, selection=config.selection),
    ).run(resume=True)
    assert state["completed_children"] == 2
    assert "interrupted" in state["diagnostic"]
    assert (output / "interrupted" / "model_patch.diff").stat().st_size


def test_evaluator_workspace_changes_do_not_leak_to_descendants(source, tmp_path):
    class DirtyHarness(Harness):
        def evaluate(self, candidate, resource):
            result = super().evaluate(candidate, resource)
            (candidate.workspace / "value.txt").write_text("9")
            return result

    state = DGMController(
        DirtyHarness(source),
        Increment(),
        tmp_path / "run",
        SearchConfig(max_children=2, batch_size=1, selection="best"),
    ).run()
    assert sorted(r["evaluation"]["score"] for r in state["records"].values()) == [
        0,
        0.1,
        0.2,
    ]


@pytest.mark.parametrize("score", [None, -1, 2, float("nan"), float("inf")])
def test_invalid_scores_are_rejected(score):
    with pytest.raises(ValueError):
        Evaluation("completed", score)
    with pytest.raises(ValueError):
        Evaluation("failed", 0.5)


def test_command_harness_end_to_end(tmp_path):
    examples = Path(__file__).resolve().parents[1] / "examples"
    bridge = CommandBridge(
        examples / "toy_harness",
        [sys.executable, str(examples / "evaluate_toy.py"), "{workspace}"],
    )
    mutator = CommandMutator(
        [sys.executable, str(examples / "mutate_toy.py"), "{workspace}"]
    )
    state = DGMController(
        bridge, mutator, tmp_path / "run", SearchConfig(max_children=1)
    ).run()
    assert state["records"]["initial"]["evaluation"]["score"] == 0
    child = next(r for r in state["records"].values() if r["parent_id"])
    assert child["evaluation"]["score"] == 1
    assert (tmp_path / "run" / child["id"] / "evaluation" / "process.json").exists()


def test_exclusive_resources_and_release_after_error(source):
    bridge = CommandBridge(source, [sys.executable], resources=("worker",))
    entered = threading.Event()
    with pytest.raises(RuntimeError):
        with bridge.lease() as resource:
            assert resource == "worker"
            raise RuntimeError("deliberate")
    with bridge.lease():

        def acquire():
            with bridge.lease():
                entered.set()

        thread = threading.Thread(target=acquire)
        thread.start()
        assert not entered.wait(0.05)
    thread.join(timeout=2)
    assert entered.is_set()


def test_invalid_command_result_and_timeout(source, tmp_path):
    candidate = Candidate("test", None, tmp_path, "")
    (tmp_path / "worktree").mkdir()
    bridge = CommandBridge(source, [sys.executable, "-c", "print('invalid')"])
    assert bridge.evaluate(candidate, "local").status == "failed"
    bridge = CommandBridge(
        source, [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.05
    )
    assert "timed out" in bridge.evaluate(candidate, "local").error


def test_single_controller_lock(source, tmp_path):
    controller = DGMController(
        Harness(source), Increment(), tmp_path / "run", SearchConfig(max_children=0)
    )
    with controller.store.lock():
        with pytest.raises(RuntimeError, match="owns"):
            controller.run()


def test_output_must_not_be_inside_source(source):
    with pytest.raises(ValueError, match="outside"):
        DGMController(Harness(source), Increment(), source / "run", SearchConfig())


def test_protected_source_is_not_admitted(source, tmp_path):
    class ProtectedHarness(Harness):
        def prepare(self, candidate, parent, resource):
            return MutationContext("Improve", "", protected_paths=("value.txt",))

    state = DGMController(
        ProtectedHarness(source),
        Increment(),
        tmp_path / "run",
        SearchConfig(max_children=1),
    ).run()
    child = state["records"]["child_000001"]
    assert child["status"] == "mutation_failed"
    assert "protected" in child["evaluation"]["error"]
    assert state["archive"] == ["initial"]


def test_archive_policy_retains_valid_lower_score(source, tmp_path):
    (source / "value.txt").write_text("5")

    class Decrement(Increment):
        def mutate(self, candidate, context):
            (candidate.workspace / "value.txt").write_text("1")
            return MutationOutcome("completed")

    state = DGMController(
        Harness(source),
        Decrement(),
        tmp_path / "run",
        SearchConfig(max_children=1, archive_policy="keep_better"),
    ).run()
    assert state["archive"] == ["initial"]
    assert state["retained"] == ["child_000001"]


def test_resume_retries_interrupted_baseline(source, tmp_path):
    class Interrupted(Harness):
        interrupt = True

        def evaluate(self, candidate, resource):
            if self.interrupt:
                raise KeyboardInterrupt()
            return super().evaluate(candidate, resource)

    bridge = Interrupted(source)
    output = tmp_path / "run"
    config = SearchConfig(max_children=1)
    with pytest.raises(KeyboardInterrupt):
        DGMController(bridge, Increment(), output, config).run()
    assert not read_json(output / "state.json")["initialized"]
    bridge.interrupt = False
    state = DGMController(bridge, Increment(), output, config).run(resume=True)
    assert state["initialized"] and state["completed_children"] == 1
    assert list(output.glob(".interrupted-initial-*"))


def test_resume_rejects_changed_snapshot(source, tmp_path):
    output = tmp_path / "run"
    config = SearchConfig(max_children=0)
    DGMController(Harness(source), Increment(), output, config).run()
    (output / "source" / "value.txt").write_text("7")
    with pytest.raises(ValueError, match="snapshot"):
        DGMController(Harness(source), Increment(), output, config).run(resume=True)


def test_resume_budget_cannot_shrink(source, tmp_path):
    output = tmp_path / "run"
    DGMController(
        Harness(source), Increment(), output, SearchConfig(max_children=1)
    ).run()
    with pytest.raises(ValueError, match="budget"):
        DGMController(
            Harness(source), Increment(), output, SearchConfig(max_children=0)
        ).run(resume=True)


def test_new_files_are_inherited_as_patches(source, tmp_path):
    class NewFile(Increment):
        def mutate(self, candidate, context):
            super().mutate(candidate, context)
            path = candidate.workspace / "new.txt"
            path.write_text(path.read_text() + "x" if path.exists() else "x")
            return MutationOutcome("completed")

    state = DGMController(
        Harness(source),
        NewFile(),
        tmp_path / "run",
        SearchConfig(max_children=2, batch_size=1, selection="best"),
    ).run()
    assert (
        tmp_path / "run" / "child_000002" / "worktree" / "new.txt"
    ).read_text() == "xx"
    assert state["records"]["child_000002"]["parent_id"] == "child_000001"
