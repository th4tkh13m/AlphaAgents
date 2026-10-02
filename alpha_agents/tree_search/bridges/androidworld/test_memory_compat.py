import copy
import json

from alpha_agents.tree_search.bridges.androidworld.memory_compat import (
    configure_openai_memory,
)


def test_google_only_services_are_disabled_without_changing_transcript(tmp_path):
    settings = {
        "flash": {"step_summarizer": {"enabled": True, "model": "gemini"}},
        "memory": {
            "chunking": {"enabled": True, "max_steps": 12},
            "transcript": {"enabled": True, "image_scrub_depth": 3},
            "runtime": {"retry_limit": 3},
        },
        "planner_validation": {"enabled": True},
    }
    before = copy.deepcopy(settings)
    path = configure_openai_memory(settings, tmp_path)
    actual = json.loads(path.read_text())["agent"]
    assert not actual["flash"]["step_summarizer"]["enabled"]
    assert not actual["memory"]["chunking"]["enabled"]
    assert actual["memory"]["transcript"] == before["memory"]["transcript"]
    assert actual["planner_validation"] == before["planner_validation"]
    assert actual["memory"]["runtime"] == before["memory"]["runtime"]
    assert settings == before
