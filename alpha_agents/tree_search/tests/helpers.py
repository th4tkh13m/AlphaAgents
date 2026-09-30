from contextlib import contextmanager

from alpha_agents.tree_search import Evaluation, MutationContext, MutationOutcome


class Harness:
    def __init__(self, source, fail=False):
        self.source, self.fail = source, fail
        self.leases = 0

    def identity(self):
        return {"type": "test", "source": str(self.source), "fail": self.fail}

    @contextmanager
    def lease(self):
        self.leases += 1
        try:
            yield None
        finally:
            self.leases -= 1

    def prepare(self, candidate, parent, resource):
        return MutationContext("Improve", "Preserve evaluator")

    def evaluate(self, candidate, resource):
        if self.fail:
            return Evaluation("failed", error="Evaluator unavailable")
        value = int((candidate.workspace / "value.txt").read_text())
        return Evaluation("completed", min(value / 10, 1))


class Increment:
    def __init__(self, fail=False, no_patch=False):
        self.fail, self.no_patch = fail, no_patch

    def identity(self):
        return {"type": "increment", "fail": self.fail, "no_patch": self.no_patch}

    def mutate(self, candidate, context):
        if not self.no_patch:
            path = candidate.workspace / "value.txt"
            path.write_text(str(int(path.read_text()) + 1))
        return MutationOutcome("failed" if self.fail else "completed")
