"""Durable phase intervals measured within one assessment's monotonic clock."""

from __future__ import annotations

import json
import time
import uuid

from alpha_agents.tree_search.core.mcts.contracts import canonical_json
from alpha_agents.tree_search.core.mcts.storage import TreeStore


class AssessmentTiming:
    def __init__(self, store: TreeStore, owner: str, artifact: str):
        self.directory, self.config, self.objective = store.directory, store.config, store.objective
        self.id = "assessment-" + uuid.uuid4().hex
        self.origin = time.monotonic()
        with store.transaction():
            store.db.execute("INSERT INTO assessment_timings VALUES(?,?,?,'running',?,NULL,'{}')",
                             (self.id, owner, artifact, time.time()))

    def mark(self, name: str):
        offset = time.monotonic() - self.origin
        store = TreeStore(self.directory, self.config, self.objective)
        try:
            with store.transaction():
                row = store.db.execute("SELECT events FROM assessment_timings WHERE id=? AND state='running'", (self.id,)).fetchone()
                if row is None:
                    raise ValueError("Assessment timing is no longer active")
                events = json.loads(row[0])
                if name in events:
                    raise ValueError("Assessment timing event cannot be overwritten")
                events[name] = offset
                store.db.execute("UPDATE assessment_timings SET events=? WHERE id=?", (canonical_json(events), self.id))
        finally:
            store.close()

    def finish(self, state: str):
        if state not in {"completed", "failed", "screen_rejected", "judge_unavailable", "evaluation_unavailable"}:
            raise ValueError("Unknown terminal assessment timing state")
        store = TreeStore(self.directory, self.config, self.objective)
        try:
            with store.transaction():
                updated = store.db.execute("UPDATE assessment_timings SET state=?,finished=? WHERE id=? AND state='running'",
                                           (state, time.time(), self.id))
                if updated.rowcount != 1:
                    raise ValueError("Assessment timing cannot be finalized twice")
        finally:
            store.close()
