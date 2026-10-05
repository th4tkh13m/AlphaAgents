"""Runtime limits must reach existing imports without overwriting explicit limits."""

import asyncio

import pytest

from .runtime_limits import configure_llm_timeout


def test_existing_alias_uses_configured_limit_and_preserves_explicit_override():
    async def invoke(call, timeout_seconds=10, hard_timeout=180):
        return timeout_seconds, hard_timeout

    imported_alias = invoke
    assert configure_llm_timeout(invoke, 300) == 300
    assert asyncio.run(imported_alias(None)) == (10, 300)
    assert asyncio.run(imported_alias(None, hard_timeout=17)) == (10, 17)


@pytest.mark.parametrize("seconds", [0, -1, True, 3.5, "300"])
def test_invalid_limit_is_rejected(seconds):
    def invoke(call, timeout_seconds=10, hard_timeout=180):
        pass

    with pytest.raises(ValueError):
        configure_llm_timeout(invoke, seconds)
    assert invoke.__defaults__ == (10, 180)


def test_changed_upstream_signature_fails_before_evaluation():
    def invoke(call, timeout_seconds=10):
        pass

    with pytest.raises(ValueError, match="hard_timeout"):
        configure_llm_timeout(invoke, 300)
