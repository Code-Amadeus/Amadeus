from __future__ import annotations

import asyncio
import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

# The analysis/CI-lite environment may omit project runtime providers.  These
# stubs only make core.chat_runtime importable; the tests never call them.
if "openai" not in sys.modules:
    try:
        import openai  # noqa: F401
    except Exception:
        sys.modules["openai"] = SimpleNamespace(OpenAI=object)
try:
    from google import genai as _unused_genai  # noqa: F401
except Exception:
    google = sys.modules.get("google") or types.ModuleType("google")
    genai = types.ModuleType("google.genai")
    google.genai = genai
    sys.modules["google"] = google
    sys.modules["google.genai"] = genai

from core import chat_runtime as chat


def _patch_turn_runtime(monkeypatch, runtime):
    runtime._ensure_clients = Mock()
    runtime._start_auip_decision = Mock(return_value=False)
    runtime._repair_missing_delegate = AsyncMock()
    monkeypatch.setattr(chat, "reset_all_expressions", Mock())
    monkeypatch.setattr(chat, "_get_expr_ctrl", lambda: Mock())
    monkeypatch.setattr("server.task_lookup.pre_turn_resolve", AsyncMock())


def test_continuity_grounding_is_per_turn_and_reaches_provider_state(monkeypatch) -> None:
    async def run():
        runtime = chat.ChatRuntime()
        provider = Mock(return_value=SimpleNamespace(text="CONTINUITY-CONTEXT"))
        runtime.configure(
            pending_sentence_items=asyncio.Queue(),
            playback_manager=None,
            provider="deepseek",
            continuity_grounding_provider=provider,
        )
        _patch_turn_runtime(monkeypatch, runtime)
        runtime.character_rag.reference = Mock(return_value="CHARACTER-REFERENCE")
        monkeypatch.setattr(chat, "RAG_ENABLED", True)
        captured = []

        async def capture(st, question, *args):
            captured.append((st.character_reference, st.continuity_context, chat._turn_role_grounding(st)))

        runtime._run_deepseek_openai = capture
        result = await runtime.stream_llm_query(
            "你还记得我的生日吗？",
            enable_conversation=False,
            turn_id="turn-c3",
        )
        assert result == ""
        assert captured[0][0] == "CHARACTER-REFERENCE"
        assert captured[0][1] == "CONTINUITY-CONTEXT"
        assert "CHARACTER-REFERENCE" in captured[0][2]
        assert "CONTINUITY-CONTEXT" in captured[0][2]
        provider.assert_called_once()
        assert provider.call_args.kwargs["turn_id"] == "turn-c3"

    asyncio.run(run())


def test_character_and_continuity_retrieval_start_in_parallel(monkeypatch) -> None:
    async def run():
        runtime = chat.ChatRuntime()
        barrier = threading.Barrier(2)

        def character_reference(_question):
            barrier.wait(timeout=2.0)
            return "CHAR"

        def continuity_reference(_question, **_kwargs):
            barrier.wait(timeout=2.0)
            return SimpleNamespace(text="CONT")

        runtime.configure(
            pending_sentence_items=asyncio.Queue(),
            playback_manager=None,
            provider="deepseek",
            continuity_grounding_provider=continuity_reference,
        )
        _patch_turn_runtime(monkeypatch, runtime)
        runtime.character_rag.reference = character_reference
        runtime._run_deepseek_openai = AsyncMock()
        monkeypatch.setattr(chat, "RAG_ENABLED", True)
        await runtime.stream_llm_query("parallel cue", enable_conversation=False, turn_id="parallel")
        runtime._run_deepseek_openai.assert_awaited_once()

    asyncio.run(run())


def test_host_generated_or_prompt_variant_turns_do_not_recall_continuity(monkeypatch) -> None:
    async def run(preserve, variant):
        runtime = chat.ChatRuntime()
        provider = Mock(return_value=SimpleNamespace(text="SHOULD-NOT-APPEAR"))
        runtime.configure(
            pending_sentence_items=asyncio.Queue(),
            playback_manager=None,
            provider="deepseek",
            continuity_grounding_provider=provider,
        )
        _patch_turn_runtime(monkeypatch, runtime)
        runtime._run_deepseek_openai = AsyncMock()
        monkeypatch.setattr(chat, "RAG_ENABLED", False)
        await runtime.stream_llm_query(
            "host generated",
            preserve_emotion=preserve,
            prompt_variant=variant,
            enable_conversation=False,
            turn_id="host-turn",
        )
        provider.assert_not_called()

    asyncio.run(run(True, ""))
    asyncio.run(run(False, "base"))
