"""Exercise inference failure handling with real SDK response models."""

import json
import sys
from functools import partial
from pathlib import Path
from unittest.mock import Mock

import httpx2 as httpx
import pytest
from openai import OpenAI
from openai.types.chat import ChatCompletion

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'harness/mobile-gui'))
from mobile_agent.infer_ma3 import GUIOwlWrapper, LlmResponseError
from mobile_agent.mobile_agent_v3_agent import ActionReflector

VALID = '### Outcome ###\nC\n### Error Description ###\nNo change.'


def completion(content=VALID, finish='stop', choices=True):
    return ChatCompletion.model_validate({
        'id': 'test', 'object': 'chat.completion', 'created': 0, 'model': 'qwen',
        'choices': [{'index': 0, 'finish_reason': finish, 'message': {
            'role': 'assistant', 'content': content, 'reasoning_content': VALID,
        }}] if choices else [],
    })


def client(monkeypatch, tmp_path, responses):
    wrapper = GUIOwlWrapper('EMPTY', 'http://localhost:8001/v1', 'qwen', max_retry=2)
    wrapper.response_log_path = str(tmp_path / 'responses.jsonl')
    wrapper.bot.chat.completions.create = Mock(side_effect=responses)
    monkeypatch.setattr('mobile_agent.infer_ma3.time.sleep', Mock())
    return wrapper


@pytest.mark.parametrize('bad', [
    completion(''), completion(None), completion('  '),
    completion(finish='length'), completion(finish='tool_calls'),
    completion(choices=False), completion('No change.'),
    completion('### Outcome ###\nD\n### Error Description ###\nNone'),
    completion('### Outcome ###\nA'), TimeoutError('request timed out'),
])
def test_bad_response_retries_before_parser(monkeypatch, tmp_path, bad):
    wrapper = client(monkeypatch, tmp_path, [bad, completion()])
    content, messages, raw = wrapper.predict_mm(
        'Original prompt', [], response_validator=ActionReflector().validate_response,
    )
    assert content == VALID
    calls = wrapper.bot.chat.completions.create.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs['messages'] == calls[1].kwargs['messages'] == messages
    assert raw.choices[0].finish_reason == 'stop'
    records = [json.loads(line) for line in Path(wrapper.response_log_path).read_text().splitlines()]
    assert records[0]['error'] and records[1]['error'] is None


def test_empty_content_exhaustion_does_not_promote_reasoning(monkeypatch, tmp_path):
    wrapper = client(monkeypatch, tmp_path, [completion(''), completion('')])
    with pytest.raises(LlmResponseError, match='after 2 attempts.*empty message.content'):
        wrapper.predict_mm('prompt', [], response_validator=ActionReflector().validate_response)
    from mobile_agent.infer_ma3 import time
    time.sleep.assert_called_once()  # No delay after the final failed attempt.


def test_empty_http_200_retries_with_runner_token_limit(monkeypatch, tmp_path):
    requests = []
    responses = [completion('').model_dump(mode='json'), completion().model_dump(mode='json')]

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses.pop(0))

    wrapper = client(monkeypatch, tmp_path, [])
    with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
        wrapper.bot = OpenAI(api_key='EMPTY', base_url='http://localhost:8001/v1', http_client=transport)
        wrapper.bot.chat.completions.create = partial(wrapper.bot.chat.completions.create, max_tokens=200000)
        assert wrapper.predict_mm('prompt', [])[0] == VALID
    assert len(requests) == 2
    assert all(request['max_tokens'] == 200000 for request in requests)


def test_success_preserves_existing_messages(monkeypatch, tmp_path):
    wrapper = client(monkeypatch, tmp_path, [completion()])
    messages = [{'role': 'user', 'content': 'Unchanged conversation'}]
    assert wrapper.predict_mm('', [], messages=messages)[1] is messages
    assert wrapper.bot.chat.completions.create.call_count == 1
