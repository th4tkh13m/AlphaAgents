"""Trusted Selection normalization and task-scoped evidence for archive lookup."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

from ...archive.catalog import digest
from ...core.contracts import Evaluation, RequiredEvaluationError
from ...core.storage import read_json, write_json
from ...core.workspace import source_digest
from . import artemis
from .task_sets import stage_tasks

SETTINGS = (
    "evaluation_runner",
    "model",
    "model_revision",
    "base_url",
    "seed",
    "max_steps",
    "task_timeout",
    "llm_timeout",
    "openai_memory_compatibility",
    "fixture_preflight",
    "fixture_identity",
    "perform_emulator_setup",
)


def instance_seed(base, task, index=0):
    return (
        int(hashlib.sha256(f"{base}_{task}_{index}".encode()).hexdigest(), 16) % 2**32
    )


class AndroidWorldArchive:
    def __init__(self, config):
        self.config = config
        self.tasks = stage_tasks(config["task_file"], "selection")

    def identity(self):
        return {
            "type": "androidworld_selection",
            "tasks": self.tasks,
            "settings": {k: self.config.get(k) for k in SETTINGS},
        }

    def metadata(self):
        path = self.config.get("task_metadata_file")
        if not path:
            return {}
        return {
            row["task"]: {
                key: row.get(key)
                for key in ("difficulty", "optimal_human_steps", "app_domain", "family")
            }
            for row in read_json(Path(path))["rows"]
            if row["task"] in self.tasks
        }

    def compatible(self, bridge):
        if bridge["type"] != "androidworld":
            raise ValueError("Archive benchmark differs")
        config = bridge["config"]
        if bridge["task_sets"].get("selection") != self.tasks:
            raise ValueError("Archive Selection tasks differ")
        # max_steps default was historically implicit.
        for key in SETTINGS:
            default = 50 if key == "max_steps" else None
            if config.get(key, default) != self.config.get(key, default):
                raise ValueError(f"Archive evaluation setting differs: {key}")

    def profile(self, run, record):
        if record["evaluation"]["status"] != "completed":
            return None
        stage = Path(run) / record["id"] / "stages/selection"
        workers = sorted(stage.glob("worker_*"))
        if not workers and (stage / "manifest.json").exists():
            workers = [stage]
        outcomes = {}
        for worker in workers:
            manifest_path = worker / "manifest.json"
            process_path = worker / "runner/process.json"
            manifest = read_json(manifest_path)
            tasks = manifest.get("tasks", [])
            normalized = artemis.normalize_manifest(
                manifest, tasks, self.config, read_json(process_path)
            )
            if normalized["status"] != "completed":
                raise ValueError(f"Invalid settled archive manifest: {worker}")
            for row in normalized["task_results"]:
                task = row["task_name"]
                if task in outcomes or task not in self.tasks:
                    raise ValueError("Duplicate or unexpected archive task")
                if row["seed"] != instance_seed(self.config["seed"], task):
                    raise ValueError("Archive instance seed differs")
                outcomes[task] = {
                    **row,
                    "worker": str(worker),
                    "manifest_sha256": digest(manifest_path),
                    "process_sha256": digest(process_path),
                }
        if set(outcomes) != set(self.tasks):
            return None
        score = sum(row["score"] > 0.5 for row in outcomes.values()) / len(self.tasks)
        if score != record["evaluation"]["score"]:
            raise ValueError(
                "Archive recorded score differs from authoritative manifests"
            )
        patch = Path(run) / record["id"] / "model_patch.diff"
        if record["parent_id"] is not None:
            # Historical independent transition audits, when present.
            audit = Path(run).parent / (
                Path(run).name + "_" + record["id"] + "_selection_verification.json"
            )
            if audit.exists():
                expected = read_json(audit).get("patch_sha256")
                if expected and digest(patch) != expected:
                    raise ValueError("Evaluated donor patch identity differs")
        return outcomes

    @staticmethod
    def export_task(outcome, output):
        output = Path(output)
        output.mkdir(parents=True)
        worker = Path(outcome["worker"])
        if digest(worker / "manifest.json") != outcome["manifest_sha256"]:
            raise ValueError("Settled donor manifest changed")
        if digest(worker / "runner/process.json") != outcome["process_sha256"]:
            raise ValueError("Settled donor process evidence changed")
        write_json(
            output / "outcome.json", {k: v for k, v in outcome.items() if k != "worker"}
        )
        database = worker / "runtime/data_engine.db"
        warnings, tables, images = [], {}, set()
        association = None
        if database.exists():
            connection = sqlite3.connect(
                database.resolve().as_uri() + "?mode=ro", uri=True
            )
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("BEGIN")
                sessions = connection.execute(
                    "SELECT * FROM sessions WHERE initial_goal=? ORDER BY start_time",
                    (outcome["goal"],),
                ).fetchall()
                association = "exact_goal"
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(sessions)")
                }
                if "video_filepath" in columns:
                    # Goals can change formatting during task.initialize_task().
                    # The runner's named recording is an explicit task-instance
                    # link, not a fuzzy text or chronological association.
                    prefix = f"androidworld-{outcome['task_name']}-{outcome.get('index', 0)}_"
                    linked = []
                    for row in connection.execute("SELECT * FROM sessions"):
                        if not row["video_filepath"]:
                            continue
                        recording = Path(row["video_filepath"]).resolve()
                        if (
                            recording.parent.parent == (worker / "traces").resolve()
                            and recording.parent.name.startswith(prefix)
                            and recording.name == "recording.mp4"
                            and recording.parent.is_dir()
                        ):
                            linked.append(row)
                    if linked:
                        sessions = linked
                        association = "task_named_recording"
                if len(sessions) != 1:
                    warnings.append(
                        f"Session association ambiguous: {len(sessions)} exact goal matches"
                    )
                    tables["matching_sessions"] = [dict(r) for r in sessions]
                else:
                    session = sessions[0]["session_id"]
                    tables["sessions"] = [dict(sessions[0])]
                    available = {
                        r[0]
                        for r in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    for table in (
                        "steps",
                        "traces",
                        "failed_outputs",
                        "background_tasks",
                        "history_chunks",
                    ):
                        if table in available:
                            rows = connection.execute(
                                f'SELECT * FROM "{table}" WHERE session_id=? ORDER BY rowid',
                                (session,),
                            ).fetchall()
                            tables[table] = [dict(r) for r in rows]
                    for step in tables.get("steps", []):
                        images.update(
                            step[k]
                            for k in ("pre_image_name", "post_image_name")
                            if step.get(k)
                        )
                    tables["images"] = []
                    for name in sorted(images):
                        if Path(name).name != name:
                            raise ValueError("Invalid donor image reference")
                        row = connection.execute(
                            "SELECT * FROM images WHERE image_name=?", (name,)
                        ).fetchone()
                        if row:
                            tables["images"].append(dict(row))
                        source = worker / "runtime/images" / (name + ".jpg")
                        if source.is_file() and source.stat().st_size:
                            (output / "images").mkdir(exist_ok=True)
                            shutil.copy2(source, output / "images" / source.name)
                        else:
                            warnings.append(f"Missing or empty screenshot: {name}")
            finally:
                connection.close()
        else:
            warnings.append("Missing trajectory database")
        write_json(output / "trajectory.json", tables)
        write_json(
            output / "evidence_manifest.json",
            {
                "schema": 2,
                "association": association,
                "warnings": warnings,
                "images": len(list((output / "images").glob("*.jpg"))),
                "referenced_images": len(images),
            },
        )


def reuse_root(candidate, resource):
    """Explicit fail-closed reuse; never silently rerun an already evaluated root."""
    origin = Path(resource["reuse_root_from"]).resolve()
    state = read_json(origin / "state.json")
    adapter = AndroidWorldArchive(resource)
    adapter.compatible(state["contract"]["bridge"])
    if source_digest(candidate.directory.parent / "source") != state["source_digest"]:
        raise RequiredEvaluationError(
            "Root reuse source differs from evaluated baseline"
        )
    record = read_json(origin / "initial/candidate.json")
    outcomes = adapter.profile(origin, record)
    if outcomes is None:
        raise RequiredEvaluationError("Root reuse has no complete verified Selection")
    # Validate evaluator, dependency and agent fingerprints against the existing
    # full audit identity. Bridge orchestration hashes intentionally differ.
    report = read_json(origin / "root_full_benchmark.json")
    cache = (
        Path(state["contract"]["bridge"]["config"]["benchmark_cache_dir"])
        / report["cache_key"]
    )
    from .benchmark import cache_identity

    old_identity = read_json(cache / "identity.json")
    current_identity = cache_identity(candidate.workspace, resource)
    for key in ("agent_sha256", "evaluator_sha256", "packages", "task_sets"):
        if old_identity[key] != current_identity[key]:
            raise RequiredEvaluationError(f"Root reuse fingerprint differs: {key}")
    output = candidate.directory / "stages/selection"
    shutil.copytree(origin / "initial/stages/selection", output)
    write_json(
        candidate.directory.parent / "root_full_benchmark.json",
        {**report, "reused_from": str(origin)},
    )
    write_json(
        candidate.directory / "root_reuse.json",
        {
            "status": "verified_reuse",
            "source_digest": state["source_digest"],
            "original": str(origin),
            "score": record["evaluation"]["score"],
            "checked_fingerprints": [
                "agent_sha256",
                "evaluator_sha256",
                "packages",
                "task_sets",
            ],
            "bridge_code_changed": old_identity["bridge_sha256"]
            != current_identity["bridge_sha256"],
        },
    )
    return Evaluation(**record["evaluation"])
