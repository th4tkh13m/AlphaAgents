"""Archive integration tests exercise actual manifests, reconstruction and CLI."""

import shutil
import sqlite3
from dataclasses import asdict
from pathlib import Path

import pytest

from alpha_agents.tree_search.archive.catalog import (
    ArchiveProvider,
    digest,
    reconstruct,
)
from alpha_agents.tree_search.archive.cli import run
from alpha_agents.tree_search.bridges.androidworld.archive import (
    AndroidWorldArchive,
    instance_seed,
)
from alpha_agents.tree_search.core.contracts import (
    Evaluation,
    MutationContext,
)
from alpha_agents.tree_search.core.storage import read_json, write_json
from alpha_agents.tree_search.core.workspace import (
    materialize,
    recover_patch,
    source_digest,
)


def build_run(tmp_path):
    root = tmp_path / "run"
    source = root / "source"
    source.mkdir(parents=True)
    (source / "agent.py").write_text("clipboard = False\noutput = False\n")
    tasks = tmp_path / "tasks.json"
    write_json(tasks, {"screen": ["Gate"], "selection": ["Copy", "Answer"]})
    config = {
        "task_file": str(tasks),
        "seed": 42,
        "model": "test",
        "base_url": "local",
        "evaluation_runner": "artemis_local",
        "max_steps": 50,
    }
    adapter = AndroidWorldArchive(config)
    records = {}

    def candidate(node, parent, scores, code):
        value = materialize(source, root, node, parent)
        if parent:
            (value.workspace / "agent.py").write_text(code)
            recover_patch(value)
        results = []
        worker = value.directory / "stages/selection/worker_00"
        for task, score in zip(adapter.tasks, scores):
            results.append(
                {
                    "template": task,
                    "index": 0,
                    "seed": instance_seed(42, task),
                    "goal": "Do " + task,
                    "success": score,
                    "androidworld_reward": score,
                    "artemis_status": "completed",
                    "exception": None,
                }
            )
        write_json(
            worker / "manifest.json",
            {
                "status": "completed",
                "model": "test",
                "model_base_url": "local",
                "seed": 42,
                "tasks": adapter.tasks,
                "combinations": 1,
                "episodes": results,
            },
        )
        write_json(
            worker / "runner/process.json", {"timed_out": False, "returncode": 0}
        )
        score = sum(s > 0.5 for s in scores) / 2
        row = {
            "id": node,
            "parent_id": parent.id if parent else None,
            "base_commit": value.base_commit,
            "status": "completed",
            "evaluation": asdict(Evaluation("completed", score)),
        }
        write_json(value.directory / "candidate.json", row)
        records[node] = row
        row["evaluation"]["metrics"]["task_results"] = [
            {
                **r,
                "task_name": r["template"],
                "score": r["success"],
                "success": r["success"] > 0.5,
                "outcome_status": "completed",
            }
            for r in results
        ]
        write_json(value.directory / "candidate.json", row)
        return value

    initial = candidate("initial", None, [1, 0], "")
    first = candidate(
        "child_000001", initial, [0, 1], "clipboard = False\noutput = True\n"
    )
    donor = candidate(
        "child_000002", first, [1, 1], "clipboard = True\noutput = True\n"
    )
    write_json(
        root / "state.json",
        {
            "source_digest": source_digest(source),
            "records": records,
            "contract": {
                "bridge": {
                    "type": "androidworld",
                    "config": config,
                    "task_sets": read_json(tasks),
                }
            },
        },
    )
    return root, source, config, adapter, records, initial, donor


def test_agent_lookup_full_ancestry_and_evidence_scope(tmp_path):
    root, source, config, adapter, records, parent, donor = build_run(tmp_path)
    provider = ArchiveProvider({"enabled": True}, adapter)
    descriptor = provider.snapshot(root, "child_000003", parent.id, records)
    child = materialize(source, root, "child_000003", parent)
    context = provider.augment(
        child, MutationContext("Repair", "Choose task"), descriptor
    )
    catalog = Path(context.evidence["archive_access"]["catalog"])
    matches = run(catalog, "query", task="Answer")
    assert {r["ref"] for r in matches["donors"]} == {
        "current/child_000001",
        "current/child_000002",
    }
    with pytest.raises(ValueError, match="outside"):
        run(catalog, "query", task="HeldOut")
    result = run(catalog, "export", node="current/child_000002", task="Answer")
    assert (
        Path(result["source"]) / "agent.py"
    ).read_text() == "clipboard = True\noutput = True\n"
    previous = Path(result["evidence"]) / "evidence_manifest.json"
    write_json(previous, {"warnings": ["old export"], "images": 0})
    old_bytes = previous.read_bytes()
    refreshed = run(catalog, "export", node="current/child_000002", task="Answer")
    assert Path(refreshed["evidence"]).name == "schema_2"
    assert previous.read_bytes() == old_bytes
    assert (
        read_json(Path(refreshed["evidence"]) / "evidence_manifest.json")["schema"] == 2
    )
    diff = run(catalog, "compare", node="current/child_000002")
    assert "+output = True" in Path(diff["diff"]).read_text()
    assert "-output = False\n" in Path(diff["diff"]).read_text()
    # Comparing an identical donor must not invent a missing final newline.
    run(catalog, "export", node="current/initial", task="Answer")
    unchanged = run(catalog, "compare", node="current/initial")
    assert Path(unchanged["diff"]).read_text() == ""
    # Donor worktree mutations must never become inherited code.
    (donor.workspace / "agent.py").write_text("hidden_answer = 42")
    exported = tmp_path / "reconstructed"
    reconstruct(read_json(catalog)["entries"]["current/child_000002"], exported)
    assert "hidden_answer" not in (exported / "agent.py").read_text()
    (child.workspace / "agent.py").write_text("clipboard = True\noutput = True\n")
    recover_patch(child)
    assert ".dgm_archive" not in child.patch.read_text()


def test_assessment_keeps_score_and_detects_regressions(tmp_path):
    root, source, config, adapter, records, parent, donor = build_run(tmp_path)
    provider = ArchiveProvider({}, adapter)
    snapshot = provider.snapshot(root, "child_000003", parent.id, records)
    child = materialize(source, root, "child_000003", parent)
    context = provider.augment(child, MutationContext("Repair", ""), snapshot)
    run(
        context.evidence["archive_access"]["catalog"],
        "export",
        node="current/child_000002",
        task="Answer",
    )
    write_json(
        child.directory / "transfer_report.json",
        {
            "target_task": "Answer",
            "donors_used": ["current/child_000002"],
            "mechanism": "preserve structured output",
            "development_checks": [{"result": "pass"}],
        },
    )
    child_record = {"evaluation": records[donor.id]["evaluation"], "validation": []}
    provider.assess(child, parent.id, child_record)
    assert (
        read_json(child.directory / "transfer_assessment.json")["status"]
        == "demonstrated"
    )
    child_record = {
        "evaluation": records["child_000001"]["evaluation"],
        "validation": [],
    }
    provider.assess(child, parent.id, child_record)
    assessment = read_json(child.directory / "transfer_assessment.json")
    assert assessment["regressions"] == ["Copy"]
    assert assessment["status"] == "not_demonstrated"
    assert child_record["evaluation"]["score"] == 0.5
    write_json(child.directory / "transfer_report.json", [])
    provider.assess(child, parent.id, child_record)
    assert (
        "invalid_report_object"
        in read_json(child.directory / "transfer_assessment.json")["reasons"]
    )
    write_json(child.workspace / ".dgm_archive/requests/invalid.json", [])
    provider.assess(child, parent.id, child_record)
    assert (
        "invalid_helper_request"
        in read_json(child.directory / "transfer_assessment.json")["reasons"]
    )


def test_changed_seed_and_source_fail_closed(tmp_path):
    root, source, config, adapter, records, parent, donor = build_run(tmp_path)
    manifest = donor.directory / "stages/selection/worker_00/manifest.json"
    value = read_json(manifest)
    value["episodes"][0]["seed"] = 1
    write_json(manifest, value)
    with pytest.raises(ValueError, match="seed"):
        adapter.profile(root, records[donor.id])
    value["episodes"][0]["seed"] = instance_seed(42, "Copy")
    write_json(manifest, value)
    provider = ArchiveProvider({}, adapter)
    snapshot = provider.snapshot(root, "child_000003", parent.id, records)
    entry = read_json(Path(snapshot["path"]))["entries"]["current/child_000002"]
    (Path(entry["source"]) / "agent.py").write_text("changed")
    with pytest.raises(ValueError, match="snapshot changed"):
        reconstruct(entry, tmp_path / "bad")


def test_task_export_only_copies_matching_session(tmp_path):
    root, source, config, adapter, records, parent, donor = build_run(tmp_path)
    profile = adapter.profile(root, records[donor.id])
    worker = Path(profile["Answer"]["worker"])
    database = worker / "runtime/data_engine.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as c:
        c.executescript(
            "CREATE TABLE sessions(session_id TEXT,initial_goal TEXT,start_time REAL);"
            "CREATE TABLE steps(session_id TEXT,pre_image_name TEXT,post_image_name TEXT);"
            "CREATE TABLE traces(session_id TEXT,payload TEXT);"
            "CREATE TABLE images(image_name TEXT,ocr_result TEXT);"
        )
        c.executemany(
            "INSERT INTO sessions VALUES(?,?,?)",
            [("wanted", "Do Answer", 1), ("other", "Do Copy", 2)],
        )
        c.executemany(
            "INSERT INTO traces VALUES(?,?)",
            [("wanted", "retrieved result"), ("other", "unrelated secret")],
        )
    destination = tmp_path / "evidence"
    adapter.export_task(profile["Answer"], destination)
    text = (destination / "trajectory.json").read_text()
    assert "retrieved result" in text
    assert "unrelated secret" not in text


def test_imported_archive_snapshot_is_frozen(tmp_path):
    root, source, config, adapter, records, parent, donor = build_run(tmp_path)
    other = tmp_path / "new"
    shutil.copytree(source, other / "source")
    new_parent = materialize(other / "source", other, "initial", None)
    write_json(new_parent.directory / "candidate.json", records["initial"])
    shutil.copytree(parent.directory / "stages", new_parent.directory / "stages")
    state = read_json(root / "state.json")
    state["records"] = {"initial": records["initial"]}
    write_json(other / "state.json", state)
    provider = ArchiveProvider({"import_runs": [str(root)]}, adapter)
    first = provider.snapshot(other, "child_000001", "initial", state["records"])
    catalog = read_json(Path(first["path"]))
    assert "import_00/child_000002" in catalog["entries"]
    assert len(catalog["entries"]["import_00/child_000002"]["patches"]) == 2
    assert digest(first["path"]) == first["sha256"]


def test_task_named_recording_resolves_changed_goal_without_fuzzy_matching(tmp_path):
    root, source, config, adapter, records, parent, donor = build_run(tmp_path)
    outcome = adapter.profile(root, records[donor.id])["Answer"]
    worker = Path(outcome["worker"])
    trace = worker / "traces/androidworld-Answer-0_PASS_date"
    trace.mkdir(parents=True)
    database = worker / "runtime/data_engine.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE sessions(session_id TEXT,initial_goal TEXT,start_time REAL,video_filepath TEXT);"
            "CREATE TABLE traces(session_id TEXT,payload TEXT);"
        )
        connection.executemany(
            "INSERT INTO sessions VALUES(?,?,?,?)",
            [
                (
                    "wanted",
                    "Answer with a dynamically rendered date",
                    1,
                    str(trace / "recording.mp4"),
                ),
                (
                    "other",
                    "Answer with a different date",
                    2,
                    str(
                        tmp_path
                        / "foreign/androidworld-Answer-0_PASS_date/recording.mp4"
                    ),
                ),
            ],
        )
        connection.executemany(
            "INSERT INTO traces VALUES(?,?)",
            [
                ("wanted", "correct evidence"),
                ("other", "unrelated evidence"),
            ],
        )
    output = tmp_path / "export"
    adapter.export_task(outcome, output)
    assert (
        read_json(output / "evidence_manifest.json")["association"]
        == "task_named_recording"
    )
    assert "correct evidence" in (output / "trajectory.json").read_text()
    assert "unrelated evidence" not in (output / "trajectory.json").read_text()
    # Multiple explicit task links are ambiguous, not permission to guess.
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO sessions VALUES(?,?,?,?)",
            ("duplicate", "Another date", 3, str(trace / "recording.mp4")),
        )
    output = tmp_path / "ambiguous"
    adapter.export_task(outcome, output)
    assert read_json(output / "evidence_manifest.json")["warnings"]
    assert "correct evidence" not in (output / "trajectory.json").read_text()
