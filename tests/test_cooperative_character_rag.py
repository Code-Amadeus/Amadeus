import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agent_host.provider_runtime import ProviderRuntime
from config import settings
from core import character_rag as rag
from server.cooperative_provider_loop import CooperativeProviderLoop


def loop_for(query, reference, *, professional=False):
    return CooperativeProviderLoop(ProviderRuntime(), query,
        Mock(side_effect=AssertionError("unexpected execution")), provider="unavailable",
        context_requirements={}, publish=lambda _event: True,
        role_reference=reference, work_proposals_only=professional)


@pytest.mark.parametrize("professional", [True, False])
@pytest.mark.parametrize("work", [True, False])
async def test_one_turn_reference_is_not_a_host_frame_history_or_effect_field(professional, work):
    reference = rag.render_reference([{"id": 1, "text": "PRIVATE_CORPUS_FACT"}])
    captured, retrieved = [], []

    async def retrieve(text):
        assert not loop._foreground.locked()
        retrieved.append(text)
        return reference if len(retrieved) == 1 else ""

    async def query(messages):
        captured.append(messages)
        if json.loads(messages[1]["content"])["source_kind"] != "user":
            return "Receipt."
        return json.dumps({"say": "Acknowledged.",
            "action": {"op": "work", "intent": "execute"} if work else None})

    loop = loop_for(query, retrieve, professional=professional)
    try:
        result = await loop.submit("Exact user wording", input_id="one")
        assert retrieved == ["Exact user wording"]
        assert reference in captured[0][0]["content"]
        frame = json.loads(captured[0][1]["content"])
        assert frame["current"]["text"] == "Exact user wording"
        assert "PRIVATE_CORPUS_FACT" not in json.dumps(frame)
        assert "PRIVATE_CORPUS_FACT" not in json.dumps(result, default=repr)
        if work:
            assert result["state"] == ("work_plan_required" if professional else "work_required")
            assert result["text"] == "Exact user wording"
        await loop._express_and_deliver({"source": "host_receipt", "state": "done"}, cause="receipt")
        await loop._decide({"source": "provider", "text": "provider evidence"})
        assert retrieved == ["Exact user wording"]
        assert all("PRIVATE_CORPUS_FACT" not in json.dumps(messages) for messages in captured[1:])
        await loop.submit("Different question", input_id="two")
        assert retrieved == ["Exact user wording", "Different question"]
        assert "PRIVATE_CORPUS_FACT" not in json.dumps(captured[-1])
        assert "PRIVATE_CORPUS_FACT" not in json.dumps(loop.history)
        assert "PRIVATE_CORPUS_FACT" not in json.dumps(loop.trace, default=repr)
    finally:
        await loop.close()


async def test_disabled_reference_does_not_start_a_thread_or_load(monkeypatch):
    monkeypatch.setattr(settings, "RAG_ENABLED", False)
    thread = AsyncMock(side_effect=AssertionError("disabled retrieval started"))
    monkeypatch.setattr(rag.asyncio, "to_thread", thread)
    assert await rag.role_character_reference("question") == ""
    thread.assert_not_called()


async def test_cancelled_load_is_shared_but_its_result_does_not_reach_another_turn(monkeypatch):
    monkeypatch.setattr(settings, "RAG_ENABLED", True)
    service = rag.CharacterRAG()
    monkeypatch.setattr(rag, "_shared_rag", service)
    assert rag.get_character_rag() is rag.get_character_rag() is service
    started, release = threading.Event(), threading.Event()

    def load(_directory):
        started.set()
        if not release.wait(5):
            raise TimeoutError("test did not release loader")
        return SimpleNamespace(texts=["fact"], model_name="synthetic",
            search=lambda query, **kwargs: [{"id": 1, "distance": 0.0, "text": query + "_FACT"}])

    loader = Mock(side_effect=load)
    monkeypatch.setattr(rag, "CharacterKnowledgeIndex", loader)
    query = AsyncMock(return_value='{"action":null,"say":"ok"}')
    first = loop_for(query, rag.role_character_reference)
    second = loop_for(query, rag.role_character_reference)
    task = asyncio.create_task(first.submit("STALE", input_id="one"))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 3), 4)
        # Ingress cancellation targets the owned input task, not submit's shield.
        first._inputs["one"][1].cancel()
        await asyncio.wait_for(first.close(), 2)
        with pytest.raises(asyncio.CancelledError):
            await task
        query.assert_not_called()
        release.set()
        await asyncio.wait_for(second.submit("CURRENT", input_id="two"), 4)
        loader.assert_called_once()
        query.assert_awaited_once()
        messages = query.call_args.args[0]
        assert "CURRENT_FACT" in messages[0]["content"]
        assert "STALE_FACT" not in json.dumps(messages)
        assert service.status()["state"] == "ready"
    finally:
        release.set()
        await first.close()
        await second.close()
