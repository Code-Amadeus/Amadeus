"""The loaded model, not a default setting, determines message capacity."""

import copy
from types import SimpleNamespace

import pytest
import requests

from llm import client
from llm.local_backends import require_llama_message_capacity


@pytest.mark.parametrize("streaming", [True, False])
@pytest.mark.parametrize("context,accepted", [(4096, False), (6099, False), (6100, True), (16384, True)])
def test_complete_template_and_output_reserve_are_checked_before_generation(
    monkeypatch, streaming, context, accepted,
):
    messages = [{"role": "system", "content": "role contract"},
                {"role": "user", "content": "complete history"}]
    original = copy.deepcopy(messages)
    seen, delivered = [], []
    closed = []
    monkeypatch.setattr(client, "LOCAL_LLM_TYPE", "llama_server")
    monkeypatch.setattr(client, "LOCAL_LLM_URL", "http://local.test/prefix/v1")

    def get(url, **kwargs):
        assert url == "http://local.test/prefix/props"
        assert kwargs["params"] == {"model": "selected"}
        return response({"default_generation_settings": {"n_ctx": context}, "total_slots": 4})

    def response(data):
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: data)

    def post(url, **kwargs):
        seen.append(url)
        data = kwargs["json"]
        assert data["model"] == "selected"
        if url.endswith("/apply-template"):
            assert data["messages"] == original
            assert data["max_tokens"] == 100
            assert data["response_format"]["json_schema"]["schema"] == {"type": "object"}
            return response({"prompt": "exact rendered template"})
        if url.endswith("/tokenize"):
            assert data["content"] == "exact rendered template"
            assert data["parse_special"] is data["add_special"] is True
            return response({"tokens": [1] * 6000})
        assert url.endswith("/v1/chat/completions")
        assert data["messages"] == original and data["max_tokens"] == 100
        result = response({"choices": [{"message": {"content": "ok"}}]})
        result.iter_lines = lambda: iter([
            b'data: {"choices":[{"delta":{"content":"ok"}}]}', b'data: [DONE]'])
        result.close = lambda: closed.append(True)
        return result

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)
    kwargs = dict(temperature=0.0, max_tokens=100, timeout=5, model="selected",
                  on_text=delivered.append if streaming else None)
    if accepted:
        assert client._local_messages_query(messages, **kwargs) == "ok"
        assert len(seen) == 3
        assert delivered == (["ok"] if streaming else [])
        assert bool(closed) is streaming
    else:
        with pytest.raises(RuntimeError, match=f"6000 input.*100 output.*{context}"):
            client._local_messages_query(messages, **kwargs)
        assert len(seen) == 2 and not delivered
    assert messages == original


@pytest.mark.parametrize("fault", ["offline", "missing", "invalid_context", "template", "tokens"])
def test_unverifiable_capacity_never_substitutes_a_configured_default(monkeypatch, fault):
    calls = []

    def get(*args, **kwargs):
        if fault == "offline":
            raise requests.ConnectionError("unavailable")
        data = {} if fault == "missing" else {"default_generation_settings": {
            "n_ctx": True if fault == "invalid_context" else 16384}}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: data)

    def post(url, **kwargs):
        calls.append(url)
        data = ({"prompt": None if fault == "template" else "template"}
                if url.endswith("/apply-template") else {"tokens": "not token IDs"})
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: data)

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(RuntimeError, match="Cannot verify llama-server context capacity"):
        require_llama_message_capacity("http://local.test/v1",
            {"messages": [], "model": "selected", "max_tokens": 100}, timeout=5)
    assert not any(url.endswith("/chat/completions") for url in calls)
