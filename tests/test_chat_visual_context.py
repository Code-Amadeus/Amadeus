"""Image capability must agree across direct, cooperative, and hybrid Chat."""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest

from llm.visual_context import visual_notice_text


@pytest.mark.parametrize("provider,model,supported", [
    ("deepseek", "deepseek-flash", True),
    ("deepseek", "deepseek-v4-flash", True),
    ("deepseek", "deepseek-v4-flash-vision-exp", True),
    ("deepseek", "deepseek-v4-pro", False),
    ("openai", "unused", True),
    ("hybrid2", "deepseek-flash", True),
    ("hybrid2", "deepseek-v4-pro", False),
    ("hybrid3", "unused", True),
])
@pytest.mark.parametrize("capture_error", [False, True])
def test_chat_sends_images_only_to_supported_remote_models(
    monkeypatch, provider, model, supported, capture_error,
):
    from core import chat_runtime as chat
    from core.session_manager import ConversationHistory
    from llm import hybrid_stream

    calls = []
    runtime = chat.ChatRuntime()
    runtime.llm_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kwargs: (calls.append(kwargs) or iter(())),
    )))

    async def stream(local, remote, **kwargs):
        calls.append({"local": local, "messages": remote, **kwargs})
        if False:
            yield "", ""

    monkeypatch.setattr(hybrid_stream, "hybrid_llm_stream", stream)
    monkeypatch.setattr(chat, "DEEPSEEK_MODEL_NAME", model)
    monkeypatch.setattr(chat, "_turn_system_prompt", lambda *args: "Persona")
    monkeypatch.setattr(chat, "_wrap_user_message_for_language_lock", lambda text: text)
    history = ConversationHistory()
    history.add_user("Earlier question")
    history.add_assistant("Earlier answer")
    before = copy.deepcopy(history.dialog)
    state = chat._TurnState(
        gui_callback=None, question="Describe the image", history_snapshot=history.snapshot(),
    )
    visual = {
        "reason": "attachment", "scope": "user_image",
        "frame": {"dataUrl": "data:image/png;base64,AA=="},
        **({"error": "capture failed"} if capture_error else {}),
    }
    if provider.startswith("hybrid"):
        asyncio.run(runtime._run_hybrid(
            state, state.question, visual_notice_text(state.question, visual, supported=False),
            visual, True, provider,
        ))
        assert all(isinstance(message["content"], str) for message in calls[0]["local"])
        if supported:
            assert "[LOCAL_FIRST_SENTENCE_VISUAL_HINT]" in calls[0]["local"][-1]["content"]
        assert calls[0]["tail_provider"] == ("deepseek" if provider == "hybrid2" else "openai")
    else:
        asyncio.run(runtime._run_deepseek_openai(
            state, state.question, visual, True, provider,
        ))
    assert len(calls) == 1
    assert history.dialog == before
    messages = calls[0]["messages"]
    assert all(isinstance(message["content"], str) for message in messages[:-1])
    content = messages[-1]["content"]
    if capture_error:
        assert "[VISUAL_CONTEXT_ERROR]" in content
    elif supported:
        assert messages[-1]["role"] == "user"
        assert content[1] == {
            "type": "image_url",
            "image_url": {"url": visual["frame"]["dataUrl"], "detail": "auto"},
        }
        assert "[VISUAL_CONTEXT_UNAVAILABLE]" not in content[0]["text"]
    else:
        assert "[VISUAL_CONTEXT_UNAVAILABLE]" in content
