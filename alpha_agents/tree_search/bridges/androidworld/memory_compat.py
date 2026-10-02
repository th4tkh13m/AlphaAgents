"""Public runtime configuration for memory paths bound to the Google client."""

import copy
import json


def configure_openai_memory(agent_settings, output):
    """Keep transcript history while disabling Google-only compression calls.

    The preserved harness directly constructs Google clients for these two
    services, independently of its LLM role configuration. Use public feature
    flags rather than editing or monkeypatching the harness.
    """
    settings = copy.deepcopy(agent_settings)
    settings.setdefault("flash", {}).setdefault("step_summarizer", {})["enabled"] = (
        False
    )
    settings.setdefault("memory", {}).setdefault("chunking", {})["enabled"] = False
    path = output / "runtime_agent_config.json"
    path.write_text(json.dumps({"agent": settings}, indent=2) + "\n", encoding="utf-8")
    return path
