"""Completion diagnostics distinguish truncation without exposing prompt text."""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from vn_player.llm_client import VNLLMClient
from vn_player.schemas import VNProfile


@pytest.mark.parametrize("finish_reason, content, expected", [
    ("length", '{"reaction_plan":[{"spoiler_safe_hint":"incomplete', None),
    ("stop", '{"reaction_plan":[]}', {"reaction_plan": []}),
])
def test_completion_metadata_preserves_parse_result_without_logging_content(
    finish_reason, content, expected, monkeypatch, caplog,
):
    from config import settings
    import openai

    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "test-only-secret")
    response = SimpleNamespace(
        model="test-model", usage=SimpleNamespace(completion_tokens=123),
        choices=[SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content=content))],
    )
    create = Mock(return_value=response)
    monkeypatch.setattr(openai, "OpenAI", Mock(return_value=SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )))
    client = VNLLMClient(VNProfile(session_id="diagnostics", provider="deepseek"))
    with caplog.at_level(logging.INFO, logger="vn_player.llm_client"):
        parsed, raw = asyncio.run(client.complete_json(
            [{"role": "user", "content": "private-story-text"}], lane="lookahead",
        ))
    assert (parsed, raw) == (expected, content)
    assert f"finish_reason={finish_reason}" in caplog.text
    assert "completion_tokens=123" in caplog.text
    assert "lookahead" in caplog.text and "model=test-model" in caplog.text
    assert "test-only-secret" not in caplog.text
    assert "private-story-text" not in caplog.text and content not in caplog.text
    assert create.call_count == 1, "diagnostics must not add a model retry"
