import io
import json

import pytest

from alpha_agents.tree_search.bridges.androidworld import model_health


def test_connection_reset_is_retried_before_model_selection(monkeypatch):
    calls = []

    def request(url, *, timeout):
        calls.append((url, timeout))
        if len(calls) == 1:
            raise ConnectionResetError("server restarted")
        return io.BytesIO(json.dumps({"data": [{"id": "expected"}]}).encode())

    monkeypatch.setattr(model_health, "urlopen", request)
    assert model_health.wait_for_model(
        "http://model/v1/", "expected", retry_delay=0
    ) == ["expected"]
    assert calls == [("http://model/v1/models", 10)] * 2


def test_unavailable_server_has_a_bounded_failure(monkeypatch):
    calls = []

    def request(*args, **kwargs):
        calls.append(1)
        raise ConnectionResetError("offline")

    monkeypatch.setattr(model_health, "urlopen", request)
    with pytest.raises(RuntimeError, match="unavailable after 3 attempts"):
        model_health.wait_for_model("http://model/v1", "expected", retry_delay=0)
    assert len(calls) == 3


def test_wrong_model_is_rejected_without_substitution(monkeypatch):
    monkeypatch.setattr(
        model_health,
        "urlopen",
        lambda *a, **k: io.BytesIO(b'{"data": [{"id": "other"}]}'),
    )
    with pytest.raises(RuntimeError, match="not advertised"):
        model_health.wait_for_model("http://model/v1", "expected", retry_delay=0)
