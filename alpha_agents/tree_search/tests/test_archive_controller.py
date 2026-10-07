"""The existing controller must actually expose and assess archive use."""

from contextlib import contextmanager

import pytest
from test_archive_transfer import build_run

from alpha_agents.tree_search.archive.catalog import ArchiveProvider
from alpha_agents.tree_search.archive.cli import run
from alpha_agents.tree_search.bridges.androidworld.archive import reuse_root
from alpha_agents.tree_search.core.contracts import (
    Evaluation,
    MutationContext,
    MutationOutcome,
    RequiredEvaluationError,
)
from alpha_agents.tree_search.core.controller import DGMController, SearchConfig
from alpha_agents.tree_search.core.storage import read_json, write_json


def test_existing_controller_exposes_archive_and_assesses_agent_selected_task(tmp_path):
    origin, source, config, adapter, records, parent, donor = build_run(tmp_path)
    evaluations = []

    class Bridge:
        def __init__(self):
            self.source = source

        def identity(self):
            return read_json(origin / "state.json")["contract"]["bridge"]

        @contextmanager
        def lease(self):
            yield config

        def prepare(self, candidate, parent, resource):
            return MutationContext("Improve", "Agent chooses a task")

        def evaluate(self, candidate, resource):
            evaluations.append(candidate.id)
            scores = [
                1,
                int("output = True" in (candidate.workspace / "agent.py").read_text()),
            ]
            template = read_json(
                parent.directory / "stages/selection/worker_00/manifest.json"
            )
            for row, score in zip(template["episodes"], scores):
                row.update(success=score, androidworld_reward=score)
            output = candidate.directory / "stages/selection/worker_00"
            write_json(output / "manifest.json", template)
            write_json(
                output / "runner/process.json", {"returncode": 0, "timed_out": False}
            )
            temporary_record = {
                "id": candidate.id,
                "parent_id": candidate.parent_id,
                "evaluation": {"status": "completed", "score": sum(scores) / 2},
            }
            profile = adapter.profile(candidate.directory.parent, temporary_record)
            return Evaluation(
                "completed", sum(scores) / 2, {"task_results": list(profile.values())}
            )

    class Mutator:
        def identity(self):
            return {"type": "archive-test"}

        def mutate(self, candidate, context):
            catalog = context.evidence["archive_access"]["catalog"]
            matches = run(catalog, "query", task="Answer")
            assert len(matches["donors"]) == 2
            ref = "import_00/child_000002"
            run(catalog, "export", node=ref, task="Answer")
            (candidate.workspace / "agent.py").write_text(
                "clipboard = True\noutput = True\n"
            )
            write_json(
                candidate.directory / "transfer_report.json",
                {
                    "schema": 1,
                    "target_task": "Answer",
                    "donors_used": [ref],
                    "mechanism": "preserve structured answer",
                    "development_checks": ["component test"],
                },
            )
            return MutationOutcome("completed")

    provider = ArchiveProvider({"import_runs": [str(origin)]}, adapter)
    output = tmp_path / "experiment"
    controller = DGMController(
        Bridge(),
        Mutator(),
        output,
        SearchConfig(max_children=1),
        archive_provider=provider,
    )
    state = controller.run()
    assert evaluations == ["initial", "child_000001"]
    assert state["records"]["child_000001"]["parent_id"] == "initial"
    assert state["records"]["child_000001"]["evaluation"]["score"] == 1
    assert (
        read_json(output / "child_000001/transfer_assessment.json")["status"]
        == "demonstrated"
    )
    assert state["archive"] == ["initial", "child_000001"]
    assert "archive_access" in state["contract"]
    # A clean resume consumes no additional mutation or evaluation.
    controller.run(resume=True)
    assert evaluations == ["initial", "child_000001"]


def test_root_reuse_rejects_different_source_before_any_evaluation(tmp_path):
    root, source, config, adapter, records, parent, donor = build_run(tmp_path)
    from alpha_agents.tree_search.core.workspace import materialize

    output = tmp_path / "new"
    output.mkdir()
    import shutil

    shutil.copytree(source, output / "source")
    (output / "source/agent.py").write_text("different implementation")
    candidate = materialize(output / "source", output, "initial", None)
    with pytest.raises(RequiredEvaluationError, match="source differs"):
        reuse_root(candidate, {**config, "reuse_root_from": str(root)})
