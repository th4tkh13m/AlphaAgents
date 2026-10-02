"""DGM archive search. Harness details enter only through HarnessBridge."""

from __future__ import annotations

import fnmatch
import json
import logging
import math
import random
import shutil
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from pathlib import Path

from .contracts import (
    Candidate,
    CandidateValidator,
    Evaluation,
    HarnessBridge,
    MutationBackend,
    ValidationReport,
)
from .storage import RunStore, read_json, write_json
from .workspace import git, materialize, recover_patch, source_digest


@dataclass(frozen=True)
class SearchConfig:
    max_children: int = 20
    workers: int = 1
    scheduling: str = "synchronous"
    batch_size: int = 2
    selection: str = "score_child_prop"
    archive_policy: str = "keep_all"
    score_tolerance: float = 0.1
    seed: int = 42

    def __post_init__(self):
        if self.max_children < 0 or self.workers < 1 or self.batch_size < 1:
            raise ValueError("Invalid search limits")
        if self.scheduling not in {"synchronous", "asynchronous"}:
            raise ValueError("Unknown scheduling mode")
        if self.selection not in {"random", "best", "score_prop", "score_child_prop"}:
            raise ValueError("Unknown parent selection")
        if self.archive_policy not in {"keep_all", "keep_better"}:
            raise ValueError("Unknown archive policy")
        if not math.isfinite(self.score_tolerance) or self.score_tolerance < 0:
            raise ValueError("Invalid score tolerance")


logger = logging.getLogger(__name__)


class DGMController:
    def __init__(
        self,
        bridge: HarnessBridge,
        mutator: MutationBackend,
        output: Path,
        config: SearchConfig,
        validators: tuple[CandidateValidator, ...] = (),
    ):
        self.bridge, self.mutator, self.config = bridge, mutator, config
        self.validators = tuple(validators)
        if output.resolve().is_relative_to(bridge.source.resolve()):
            raise ValueError(
                "Search output must be outside the editable harness source"
            )
        self.store = RunStore(output)
        self.rng = random.Random(config.seed)

    def _checkpoint(self):
        self.state["random_state"] = self.rng.getstate()
        self.store.checkpoint(self.state)

    def _admit(self, record):
        id = record["id"]
        self.state["records"][id] = record
        evaluation = record["evaluation"]
        if evaluation["status"] == "completed":
            baseline = (
                self.state["records"]
                .get("initial", {})
                .get("evaluation", {})
                .get("score")
            )
            accepted = (
                self.config.archive_policy == "keep_all"
                or baseline is None
                or evaluation["score"] >= baseline - self.config.score_tolerance
            )
            self.state["archive" if accepted else "retained"].append(id)
        if evaluation["diagnostic"] or record["status"] == "evaluation_failed":
            self.state["diagnostic"].append(id)

    def _parent(self):
        ids = list(
            dict.fromkeys(
                self.state["diagnostic"]
                + self.state["archive"]
                + self.state["retained"]
            )
        )
        if not ids:
            raise RuntimeError("No eligible parents remain")
        records = self.state["records"]

        def score(id):
            return records[id]["evaluation"]["score"] or 0.0

        if self.config.selection == "best":
            return max(ids, key=score)
        if self.config.selection == "random":
            return self.rng.choice(ids)
        weights = [1 / (1 + math.exp(-10 * (score(id) - 0.5))) for id in ids]
        if self.config.selection == "score_child_prop":
            counts = {
                id: sum(record["parent_id"] == id for record in records.values())
                for id in ids
            }
            weights = [weight / (1 + counts[id]) for id, weight in zip(ids, weights)]
        return self.rng.choices(ids, weights=weights, k=1)[0]

    def _child(self, id: str, parent_id: str):
        directory = self.store.root / id
        candidate = Candidate(id, parent_id, directory, "")
        mutation = None
        validation = []
        has_patch = False
        try:
            parent = self.store.candidate(parent_id)
            candidate = materialize(self.bridge.source, self.store.root, id, parent)
            with self.bridge.lease() as resource:
                logger.info("Candidate=%s parent=%s phase=mutation", id, parent_id)
                context = self.bridge.prepare(candidate, parent, resource)
                write_json(candidate.mutation_context, asdict(context))
                try:
                    mutation = asdict(self.mutator.mutate(candidate, context))
                except Exception as error:
                    mutation = {
                        "status": "failed",
                        "error": f"{type(error).__name__}: {error}",
                    }
                has_patch = recover_patch(candidate)
                logger.info(
                    "Candidate=%s mutation=%s has_patch=%s",
                    id,
                    mutation["status"],
                    has_patch,
                )
                protected = self._protected_changes(candidate, context.protected_paths)
                if protected:
                    evaluation = Evaluation(
                        "failed",
                        error="Mutation changed protected paths: "
                        + ", ".join(protected),
                    )
                    status = "mutation_failed"
                elif not has_patch:
                    evaluation = Evaluation(
                        "failed", error="Mutation produced no patch"
                    )
                    status = "mutation_failed"
                else:
                    # A partial patch still gets independent evaluation after
                    # a timeout or mutation error. Never infer score from a test.
                    for validator in self.validators:
                        try:
                            report = validator.validate(candidate, context)
                        except Exception as error:
                            report = ValidationReport(
                                type(validator).__name__,
                                "failed",
                                error=f"{type(error).__name__}: {error}",
                            )
                        validation.append(asdict(report))
                    write_json(candidate.directory / "validation.json", validation)
                    logger.info("Candidate=%s phase=evaluation", id)
                    evaluation = self.bridge.evaluate(candidate, resource)
                    if mutation["status"] != "completed" or any(
                        report["status"] == "failed" for report in validation
                    ):
                        evaluation = Evaluation(
                            **{**asdict(evaluation), "diagnostic": True}
                        )
                    status = (
                        "completed"
                        if evaluation.status == "completed"
                        else "evaluation_failed"
                    )
        except Exception as error:
            logger.exception("Candidate=%s failed", id)
            evaluation = Evaluation(
                "failed", error=f"{type(error).__name__}: {error}", diagnostic=has_patch
            )
            status = "evaluation_failed" if has_patch else "mutation_failed"
        return self.store.save_candidate(
            candidate, evaluation, status, mutation, validation
        )

    def _protected_changes(self, candidate: Candidate, patterns):
        changed = git(
            candidate.workspace,
            "diff",
            "--name-only",
            "--no-renames",
            candidate.base_commit,
        ).splitlines()
        return [
            path
            for path in changed
            if any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)
        ]

    def _bootstrap(self):
        source = self.store.root / "source"
        if not self.state.get("source_ready"):
            temporary = self.store.root / (".source-" + uuid.uuid4().hex)
            shutil.copytree(
                self.bridge.source,
                temporary,
                ignore=shutil.ignore_patterns(
                    ".git",
                    "__pycache__",
                    ".pytest_cache",
                    ".venv",
                    "venv",
                    "runs",
                    "output*",
                ),
            )
            # An interrupted snapshot is preserved separately, and never used.
            if source.exists():
                source.rename(
                    self.store.root / (".interrupted-source-" + uuid.uuid4().hex)
                )
            temporary.rename(source)
            self.state["source_ready"] = True
            self.state["source_digest"] = source_digest(source)
            self._checkpoint()
        directory = self.store.root / "initial"
        record = directory / "candidate.json"
        if record.exists():
            self._admit(read_json(record))
        else:
            # Preserve incomplete baseline work/evidence before a clean retry.
            if directory.exists():
                directory.rename(
                    self.store.root / (".interrupted-initial-" + uuid.uuid4().hex)
                )
            candidate = materialize(source, self.store.root, "initial", None)
            try:
                with self.bridge.lease() as resource:
                    self.bridge.prepare(candidate, None, resource)
                    evaluation = self.bridge.evaluate(candidate, resource)
            except Exception as error:
                evaluation = Evaluation(
                    "failed", error=f"{type(error).__name__}: {error}", diagnostic=True
                )
            status = (
                "completed" if evaluation.status == "completed" else "evaluation_failed"
            )
            self._admit(self.store.save_candidate(candidate, evaluation, status))
            logger.info("Baseline: status=%s score=%s", status, evaluation.score)
        self.state["initialized"] = True
        self._checkpoint()

    def _recover_pending(self):
        # A terminated controller cannot leave active search slots forever.
        # Preserve partial patches/evidence as diagnostic parents.
        for pending in self.state["pending"]:
            directory = self.store.root / pending["id"]
            record_path = directory / "candidate.json"
            if record_path.exists():
                record = read_json(record_path)
            else:
                base = ""
                if (directory / "workspace.json").exists():
                    base = read_json(directory / "workspace.json")["base_commit"]
                candidate = Candidate(
                    pending["id"], pending["parent_id"], directory, base
                )
                # A recovered workspace may contain uncommitted mutations.
                protected = []
                if base:
                    recover_patch(candidate)
                    context_path = directory / "mutation_context.json"
                    if context_path.exists():
                        protected = self._protected_changes(
                            candidate,
                            read_json(context_path).get("protected_paths", []),
                        )
                record = self.store.save_candidate(
                    candidate,
                    Evaluation(
                        "failed",
                        error="Interrupted mutation changed protected paths"
                        if protected
                        else "Controller interrupted",
                        diagnostic=not bool(protected),
                    ),
                    "mutation_failed" if protected else "evaluation_failed",
                )
            self._admit(record)
            self.state["completed_children"] += 1
        self.state["pending"] = []

    def _initialize(self, resume: bool):
        contract = {
            "bridge": self.bridge.identity(),
            "mutator": self.mutator.identity(),
            "search": asdict(self.config),
            "validators": [validator.identity() for validator in self.validators],
        }
        contract = json.loads(json.dumps(contract))
        # Only the total budget may increase on resume. All other settings,
        # source snapshots, and evaluator identities must match.
        comparable = {
            **contract,
            "search": {
                key: value
                for key, value in contract["search"].items()
                if key != "max_children"
            },
        }
        snapshot = self.store.root / "state.json"
        if resume:
            self.state = read_json(snapshot)
            if self.state.get("schema_version") != 1:
                raise ValueError("Unsupported search snapshot version")
            recorded_contract = dict(self.state["contract"])
            recorded_contract.setdefault("validators", [])
            if recorded_contract != comparable:
                raise ValueError(
                    "Resume configuration differs from the recorded run contract"
                )
            if self.config.max_children < self.state["budget"]:
                raise ValueError("Child budget may only increase on resume")
            if (
                self.state.get("source_ready")
                and source_digest(self.store.root / "source")
                != self.state["source_digest"]
            ):
                raise ValueError("Immutable source snapshot has changed")
            self.state["budget"] = self.config.max_children

            def tuples(value):
                return (
                    tuple(tuples(item) for item in value)
                    if isinstance(value, list)
                    else value
                )

            self.rng.setstate(tuples(self.state["random_state"]))
            if not self.state.get("initialized"):
                self._bootstrap()
            self._recover_pending()
            self._checkpoint()
            return
        if any(path.name != ".search.lock" for path in self.store.root.iterdir()):
            raise ValueError(
                "Fresh search requires an empty output directory; use resume for an existing run"
            )
        self.state = {
            "schema_version": 1,
            "contract": comparable,
            "records": {},
            "archive": [],
            "diagnostic": [],
            "retained": [],
            "pending": [],
            "completed_children": 0,
            "budget": self.config.max_children,
            "source_ready": False,
            "initialized": False,
        }
        self._checkpoint()
        self._bootstrap()

    def run(
        self, *, resume: bool = False, stop_after_children: int | None = None
    ) -> dict:
        if stop_after_children is not None and stop_after_children < 1:
            raise ValueError("stop_after_children must be positive")
        with self.store.lock():
            return self._run(resume, stop_after_children)

    def _run(self, resume: bool, stop_after_children: int | None = None) -> dict:
        self._initialize(resume)
        self.state.pop("stop_reason", None)
        self._checkpoint()
        invocation_limit = self.config.max_children
        if stop_after_children is not None:
            invocation_limit = min(
                invocation_limit,
                self.state["completed_children"] + stop_after_children,
            )
        with ThreadPoolExecutor(max_workers=self.config.workers) as executor:
            futures = {}
            while (
                self.state["completed_children"] < invocation_limit or futures
            ):
                remaining = (
                    invocation_limit
                    - self.state["completed_children"]
                    - len(futures)
                )
                if self.config.scheduling == "synchronous":
                    slots = min(self.config.batch_size, remaining) if not futures else 0
                else:
                    slots = min(self.config.workers - len(futures), remaining)
                for _ in range(max(0, slots)):
                    id = f"child_{self.state['completed_children'] + len(futures) + 1:06d}"
                    parent = self._parent()
                    self.state["pending"].append({"id": id, "parent_id": parent})
                    self._checkpoint()
                    futures[executor.submit(self._child, id, parent)] = id
                    logger.info("Started candidate=%s parent=%s", id, parent)
                if not futures:
                    break
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in sorted(done, key=lambda item: futures[item]):
                    id = futures.pop(future)
                    record = future.result()
                    logger.info(
                        "Finished candidate=%s status=%s score=%s",
                        id,
                        record["status"],
                        record["evaluation"]["score"],
                    )
                    self._admit(record)
                    self.state["pending"] = [
                        item for item in self.state["pending"] if item["id"] != id
                    ]
                    self.state["completed_children"] += 1
                    self._checkpoint()
        self.state["stop_reason"] = (
            "invocation_limit"
            if self.state["completed_children"] < self.config.max_children
            else "budget_complete"
        )
        self._checkpoint()
        logger.info(
            "Search stopped reason=%s children=%s budget=%s; resume with --resume",
            self.state["stop_reason"],
            self.state["completed_children"],
            self.config.max_children,
        )
        return self.state
