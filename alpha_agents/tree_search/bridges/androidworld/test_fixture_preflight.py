import random

from .fixture_preflight import check_task_fixtures


class Fixture:
    def __init__(self, params):
        self.params = params
        self.initialized = False

    def initialize_task(self, env):
        env.append(("initialize", self.params["name"]))
        self.initialized = True
        random.random()
        if self.params.get("fail"):
            raise RuntimeError("missing app database table")

    def tear_down(self, env):
        env.append(("cleanup", self.params["name"]))
        self.initialized = False
        if self.params.get("cleanup_fail"):
            raise RuntimeError("snapshot restoration failed")


def test_partial_fixture_failure_is_reported_and_cleaned_without_touching_originals():
    originals = {
        "good": Fixture({"name": "good"}),
        "bad": Fixture({"name": "bad", "fail": True}),
    }
    events = []
    random_state = random.getstate()
    result = check_task_fixtures(originals, events)
    assert result["status"] == "failed"
    assert result["tasks"][1]["error"] == "RuntimeError: missing app database table"
    assert events == [
        ("initialize", "good"), ("cleanup", "good"),
        ("initialize", "bad"), ("cleanup", "bad"),
    ]
    assert all(not task.initialized for task in originals.values())
    assert random.getstate() == random_state


def test_cleanup_failure_prevents_ready_result():
    result = check_task_fixtures(
        {"task": Fixture({"name": "task", "cleanup_fail": True})}, []
    )
    assert result["status"] == "failed"
    assert "snapshot restoration failed" in result["tasks"][0]["cleanup_error"]
