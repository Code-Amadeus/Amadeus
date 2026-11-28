import asyncio
from dataclasses import replace
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core import session_manager as sm
from core import local_embeddings
from server.reference_deadline import deadline_now
from server.cooperative_chat_ingress import _shared_role_history
from server.memory_service import (HostMemoryService, MemoryRequestError,
    MEMORY_CANDIDATE_LIMIT, MEMORY_CANDIDATE_CHAR_BUDGET)
from server.protocol import Method
from test_cooperative_character_rag import loop_for
from test_memory_service import host as host, remember, forget


class SyntheticMultilingualEmbedder:
    version = "synthetic-multilingual-v1"
    dimensions = 2
    def split_passage(self, text):
        return [text]
    def embed_passages(self, texts):
        return [[1., 0.] if "抹茶" in text else [0., 1.] for text in texts]
    def embed_query(self, query):
        return [1., 0.] if "绿色饮品" in query else [0., 1.]


async def semantic_service(host, embedder_factory=SyntheticMultilingualEmbedder, *, threshold=.84):
    await host.service.close()
    service = HostMemoryService(replace(host.config, semantic_enabled=True, semantic_threshold=threshold),
        project_for_session=lambda sid:host.project["id"], embedder_factory=embedder_factory)
    service.start()
    await service._startup
    await service._index_task
    sm.configure_model_history_projection(service.project_history)
    host.service = service
    host.handler.service = service
    return service


async def test_semantic_cross_language_plumbing_frame_separation_and_privacy(host, caplog):
    service = await semantic_service(host)
    await remember(host, "私は抹茶ラテが好きです。")
    if service._index_task is not None:
        await service._index_task
    reference = await service.recall_for_session("A", "我偏爱哪种绿色饮品？")
    assert reference.result.candidates and reference.result.candidates[0].text == "私は抹茶ラテが好きです。"
    assert reference.result.coverage.semantic == "ready"
    captured = []
    async def query(messages):
        captured.append(messages)
        return '{"action":null,"say":"synthetic response"}'
    loop = loop_for(query, None)
    loop.memory_reference = lambda text, turn_id, **kwargs:service.recall_for_session("A", text, turn_id, **kwargs)
    loop.memory_validate = service.validate_reference
    loop.history_source = lambda turn:_shared_role_history("A", turn)
    try:
        with caplog.at_level(logging.INFO):
            await loop.submit("我偏爱哪种绿色饮品？", input_id="input", turn_id="current")
        frame = json.loads(captured[0][1]["content"])
        assert "私は抹茶ラテが好きです。" not in captured[0][0]["content"]
        assert frame["memory_reference"]["authority"] == "user_reference_only"
        assert frame["memory_reference"]["entries"][0]["text"] == "私は抹茶ラテが好きです。"
        assert "budget" not in frame["memory_reference"]
        assert "私は抹茶ラテが好きです。" not in caplog.text
    finally:
        await loop.close()


async def test_final_revalidation_filters_forgotten_and_stale_scope_without_upgrading_coverage(host):
    await semantic_service(host)
    saved = await remember(host, "私は抹茶が好きです。")
    await host.service._index_task
    reference = await host.service.recall_for_session("A", "绿色饮品")
    coverage = replace(reference.result.coverage, semantic="busy", scan_limited=True, scanned_vectors=17)
    reference = replace(reference, result=replace(reference.result, coverage=coverage))
    rendered = await host.service.validate_reference(reference)
    assert rendered["entries"]
    assert rendered["coverage"]["semantic"] == "busy" and rendered["coverage"]["scan_limited"]
    await forget(host, saved["memory_id"])
    rendered = await host.service.validate_reference(reference)
    assert rendered["entries"] == [] and rendered["coverage"]["semantic"] == "busy"
    host.project["id"] = "changed"
    with pytest.raises(MemoryRequestError, match="context_changed"):
        await host.service.validate_reference(reference)


async def test_query_keeps_bounded_current_and_eligible_recent_user_text(host, monkeypatch):
    await semantic_service(host)
    assert sm.append_session_message("A", role="user", content="older eligible user", turn_id="old", message_id="old-user")
    assert sm.append_session_message("A", role="assistant", content="ASSISTANT_NOT_QUERY", turn_id="old", message_id="old-role")
    assert sm.append_session_message("A", role="user", content="CURRENT_DUPLICATE", turn_id="current", message_id="current-user")
    captured = []
    real = host.service._store.recall
    def recall(access, query, **kwargs):
        captured.append((query, kwargs.get("recent_context")))
        return real(access, query, **kwargs)
    monkeypatch.setattr(host.service._store, "recall", recall)
    monkeypatch.setattr(sm, "require_session_character", Mock(side_effect=AssertionError("hot read of entire Session")))
    await host.service.recall_for_session("A", "当" * 3000, "current")
    assert captured[0][0] == "当" * 3000
    assert captured[0][1] == ("older eligible user",)


async def test_memory_retrieval_error_timeout_and_cancellation_leave_query_authority_intact(host, monkeypatch):
    query = AsyncMock(return_value='{"action":null,"say":"ordinary"}')
    loop = loop_for(query, None)
    loop.memory_reference = AsyncMock(side_effect=MemoryRequestError("busy"))
    loop.memory_validate = host.service.validate_reference
    try:
        assert (await loop.submit("ordinary", input_id="one"))["state"] == "no_action"
        query.assert_awaited_once()
        loop.memory_reference = AsyncMock(side_effect=asyncio.CancelledError)
        with pytest.raises(asyncio.CancelledError):
            await loop.submit("cancelled", input_id="two")
        assert query.await_count == 1
    finally:
        await loop.close()


def test_exact_local_snapshot_has_one_resource_for_memory_and_character_rag(tmp_path, monkeypatch):
    import sys
    import numpy as np
    model_path = tmp_path / ("a" * 40)
    model_path.mkdir()
    model = Mock()
    model.get_embedding_dimension.return_value = 2
    model.tokenizer.encode = lambda text, **kwargs:list(text)
    model.max_seq_length = 512
    model.encode.return_value = np.array([[1., 0.]], dtype="float32")
    loader = Mock(return_value=model)
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=loader))
    monkeypatch.setattr(local_embeddings, "_owners", {})
    first = local_embeddings.get_local_embedding_model(str(model_path))
    memory = local_embeddings.MemoryE5Embedder(str(model_path))
    assert memory._model is first
    memory.embed_passages(["synthetic"])
    first.encode(["query: synthetic"], normalize_embeddings=True, convert_to_numpy=True)
    loader.assert_called_once_with(str(model_path), device="cpu", local_files_only=True, trust_remote_code=False)
    assert model.encode.call_count == 2


@pytest.mark.parametrize("mode,reason", [("change", "superseded"), ("error", "corrected")])
async def test_panel_correction_retires_known_old_source_before_next_actual_frame(host, mode, reason):
    from test_memory_service import source_for
    origin, canonical = source_for("Unrelated COFFEE_OLD more text", role="user")
    source = {**origin, "start_cp":10, "end_cp":20}
    saved = await remember(host, "coffee preference", source=source)
    result = await host.request(Method.MEMORY_CORRECT, memory_id=saved["memory_id"], expected_version=1,
        operation_id="correction", item_id="tea-item", text="tea preference", mode=mode)
    assert result["ok"]
    captured = []
    async def query(messages):
        captured.append(messages)
        return '{"action":null,"say":"ordinary"}'
    loop = loop_for(query, None)
    loop.history_source = lambda turn:_shared_role_history("A", turn)
    loop.memory_reference = lambda text, turn, **kwargs:host.service.recall_for_session("A", text, turn, **kwargs)
    loop.memory_validate = host.service.validate_reference
    try:
        await loop.submit("tea", input_id="next", turn_id="next-turn")
        frame = json.loads(captured[0][1]["content"])
        assert "COFFEE_OLD" not in json.dumps(frame)
        assert frame["history"][0]["text"] == f"Unrelated [earlier assertion {reason}] more text"
        assert "memory_reference" not in frame  # Lexical candidates alone are not automatically admitted.
        # Inspection and a new explicit save can still use a corrected source;
        # retirement is neither physical forget nor source tombstoning.
        assert sm.read_session_source("A", "source", revision=1)["text"] == canonical
        await remember(host, "new manual assertion", operation="new-explicit", item="new-explicit-item", source=source)
    finally:
        await loop.close()


async def test_loaded_model_attachment_waits_for_foreground_db_without_reloading(host):
    import threading
    await host.service.close()
    loading, allow_model, db_started, allow_db = (threading.Event() for _ in range(4))
    calls = []
    def load():
        calls.append(True)
        loading.set()
        assert allow_model.wait(3)
        return SyntheticMultilingualEmbedder()
    service = HostMemoryService(replace(host.config, semantic_enabled=True), embedder_factory=load)
    host.service = service
    service.start()
    try:
        await service._startup
        assert await asyncio.to_thread(loading.wait, 1)
        def foreground():
            db_started.set()
            assert allow_db.wait(3)
        blocked = asyncio.create_task(service._db.run(foreground))
        assert await asyncio.to_thread(db_started.wait, 1)
        allow_model.set()
        await asyncio.sleep(.04)
        assert service._semantic_state == "warming" and service._semantic_reason != "model_missing"
        allow_db.set()
        await blocked
        await service._index_task
        assert service._semantic_state == "ready" and calls == [True]
    finally:
        allow_model.set()
        allow_db.set()
        await service.close()


async def test_last_empty_index_batch_coalesces_a_new_save(host, monkeypatch):
    import threading
    service = await semantic_service(host)
    empty, release = threading.Event(), threading.Event()
    real = service._store.run_index_batch
    blocked = False
    def batch(**kwargs):
        nonlocal blocked
        result = real(**kwargs)
        if result.attempted == 0 and not blocked:
            blocked = True
            empty.set()
            assert release.wait(3)
        return result
    monkeypatch.setattr(service._store, "run_index_batch", batch)
    service._schedule_index()
    try:
        assert await asyncio.to_thread(empty.wait, 1)
        saved = await remember(host, "私は抹茶が好き", operation="last-batch", item="last-item")
        release.set()
        await service._index_task
        detail = await host.request(Method.MEMORY_GET, memory_id=saved["memory_id"])
        assert detail["memory"]["index_state"] == "ready"
    finally:
        release.set()


async def test_failed_index_status_does_not_claim_pending_automatic_work(host):
    class BrokenEmbedder(SyntheticMultilingualEmbedder):
        def embed_passages(self, texts):
            raise ValueError("PRIVATE_MODEL_FAILURE")
    service = await semantic_service(host, BrokenEmbedder)
    await remember(host, "synthetic index failure")
    await service._index_task
    status = await service.status(service.context("A"))
    assert status["failed_memories"] == 1 and status["pending_memories"] == 0
    assert status["semantic_state"] == "partial" and status["semantic_reason"] == "embedding_failed"


async def test_automatic_admission_can_reject_semantic_and_shared_word_candidates(host):
    service = await semantic_service(host)
    await remember(host, "私は抹茶ラテが好きです。 unrelated shared word")
    await service._index_task
    for query in ("What is today's weather?", "unrelated shared word"):
        reference = await service.recall_for_session("A", query)
        admitted = await service.validate_reference(reference)
        assert admitted["entries"] == []
    # The optional automatic gate does not alter explicit core candidate search.
    candidates = service._store.recall(service._access(service.context("A")),
        "unrelated shared word", now_ms=1)
    assert candidates.candidates and candidates.candidates[0].lexical_match
    related = await service.validate_reference(await service.recall_for_session("A", "绿色饮品"))
    assert related["entries"][0]["text"].startswith("私は抹茶")


async def test_automatic_gate_precedes_rank_and_context_quotas(host):
    service = await semantic_service(host)
    for index in range(6):
        await remember(host, f"绿色饮品 lexical distractor {index}", operation=f"distractor-{index}", item=f"distractor-item-{index}")
    await remember(host, "私は抹茶が好きです。", operation="semantic-only", item="semantic-only-item")
    await service._index_task
    reference = await service.recall_for_session("A", "绿色饮品")
    admitted = await service.validate_reference(reference)
    assert [entry["text"] for entry in admitted["entries"]] == ["私は抹茶が好きです。"]
    assert reference.result.candidates[0].lexical_match is False


async def test_model_threshold_cannot_grant_automatic_recall_before_query_work(host, monkeypatch):
    embedder = SyntheticMultilingualEmbedder()
    embedder.automatic_threshold = .84  # Legacy E5-style policy hint.
    embed_query = Mock(wraps=embedder.embed_query)
    monkeypatch.setattr(embedder, "embed_query", embed_query)
    service = await semantic_service(host, lambda:embedder, threshold=None)
    saved = await remember(host, "私は抹茶が好きです。")
    await service._index_task
    detail = await host.request(Method.MEMORY_GET, memory_id=saved["memory_id"])
    assert detail["memory"]["index_state"] == "ready"
    recall = Mock(wraps=service._store.recall)
    with monkeypatch.context() as local:
        local.setattr(service._store, "recall", recall)
        local.setattr(service._query, "run", AsyncMock(side_effect=AssertionError("unused query lane")))
        assert await service.recall_for_session("A", "绿色饮品") is None
        recall.assert_not_called()
        embed_query.assert_not_called()
        service._query.run.assert_not_awaited()
    status = await service.status(service.context("A"))
    assert status["semantic_state"] == "ready" and status["automatic_admission"] == "gate_required"
    # The same indexed memory is still available to explicit core search.
    result = service._store.recall(service._access(service.context("A")), "绿色饮品", now_ms=1)
    assert result.candidates and result.candidates[0].text == "私は抹茶が好きです。"
    embed_query.assert_called_once()


async def test_automatic_reference_has_four_item_and_named_token_estimate_bounds(host):
    service = await semantic_service(host)
    for index in range(6):
        await remember(host, f"抹茶 preference {index}", operation=f"budget-{index}", item=f"budget-item-{index}")
    await service._index_task
    admitted = await service.validate_reference(await service.recall_for_session("A", "绿色饮品"))
    payload = {key:value for key, value in admitted.items() if key != "coverage"}
    assert 0 < len(payload["entries"]) <= 4
    from server.memory_context import serialize_reference, estimate_reply_tokens
    budget = admitted["coverage"]["context_budget"]
    size = len(serialize_reference(payload).encode("utf-8"))
    assert size <= service.config.context_bytes
    assert budget["used"] == estimate_reply_tokens(serialize_reference(payload)) <= 512


async def test_whole_165_cjk_entry_survives_large_candidate_pool_and_final_token_packing(host):
    service = await semantic_service(host)
    for index in range(5):
        await remember(host,"抹茶"+"长"*2000,operation=f"large-{index}",item=f"large-item-{index}")
    text = "抹茶" + "春"*162 + "。"
    await remember(host,text,operation="cjk165",item="cjk165-item")
    await service._index_task
    reference = await service.recall_for_session("A","绿色饮品")
    assert len(reference.result.candidates) == 6  # Internal pool is separate from the final four slots.
    admitted = await service.validate_reference(reference)
    assert [entry["text"] for entry in admitted["entries"]] == [text]
    assert len(text) == 165 and admitted["coverage"]["budget_omitted"] == 5
    assert admitted["coverage"]["context_budget"]["used"] <= 512


@pytest.mark.parametrize("large_count,large_size", [(8,2000),(4,25000)])
async def test_full_candidate_pool_keeps_short_entries_after_oversized_ranked_entries(host, monkeypatch,
                                                                                   large_count, large_size):
    class OrderedPoolEmbedder(SyntheticMultilingualEmbedder):
        def embed_passages(self,texts):
            # Controlled scores put all oversized entries before all shorts.
            return [[1.,0.] if "oversized" in text else [.99,(1-.99**2)**.5] for text in texts]
    service = await semantic_service(host,OrderedPoolEmbedder)
    for index in range(large_count):
        prefix = f"抹茶 oversized {index} "
        await remember(host,prefix+"长"*(large_size-len(prefix)),
            operation=f"large-{index}",item=f"large-item-{index}")
    short_texts = {f"抹茶 short {index}" for index in range(MEMORY_CANDIDATE_LIMIT-large_count)}
    for index,text in enumerate(sorted(short_texts)):
        await remember(host,text,operation=f"short-{index}",item=f"short-item-{index}")
    await service._index_task
    reference = await service.recall_for_session("A","绿色饮品")
    assert len(reference.result.candidates) == MEMORY_CANDIDATE_LIMIT
    assert [len(item.text) for item in reference.result.candidates[:large_count]] == [large_size]*large_count
    assert {item.text for item in reference.result.candidates[large_count:]} == short_texts
    observed = []
    real = service._store.revalidate
    def revalidate(*args,**kwargs):
        result = real(*args,**kwargs)
        observed.append((result,kwargs))
        return result
    monkeypatch.setattr(service._store,"revalidate",revalidate)
    admitted = await service.validate_reference(reference)
    current,options = observed[0]
    assert options["limit"] == MEMORY_CANDIDATE_LIMIT
    assert options["max_chars"] == MEMORY_CANDIDATE_CHAR_BUDGET
    assert len(current.candidates) == MEMORY_CANDIDATE_LIMIT
    assert {item.text for item in current.candidates if "short" in item.text} == short_texts
    assert len(admitted["entries"]) == 4
    assert {entry["text"] for entry in admitted["entries"]} <= short_texts
    assert admitted["coverage"]["budget_omitted"] == MEMORY_CANDIDATE_LIMIT-4
    budget = admitted["coverage"]["context_budget"]
    assert budget["used"] <= 512 and budget["used_utf8_bytes"] <= service.config.context_bytes


async def test_final_validity_read_waits_boundedly_for_panel_and_prevents_overtaking(host):
    import threading
    service = await semantic_service(host)
    await remember(host, "私は抹茶が好きです。")
    await service._index_task
    reference = await service.recall_for_session("A", "绿色饮品")
    started, release = threading.Event(), threading.Event()
    def panel_read():
        started.set()
        assert release.wait(3)
    panel = asyncio.create_task(service._db.run(panel_read))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        validation = asyncio.create_task(service.validate_reference(reference))
        await asyncio.sleep(.01)
        with pytest.raises(MemoryRequestError, match="busy"):
            await service._db.run(lambda:None)
        release.set()
        await panel
        assert (await validation)["entries"]
    finally:
        release.set()


async def test_independent_references_overlap_and_empty_memory_preserves_system_bytes():
    started = set()
    cancelled = []
    release = asyncio.Event()
    captured = []
    async def retrieve(kind):
        started.add(kind)
        if len(started) == 2:
            release.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.append(kind)
            raise
        return "" if kind == "role" else object()
    async def query(messages):
        captured.append(messages)
        return '{"action":null,"say":"ordinary"}'
    loop = loop_for(query, lambda text:retrieve("role"))
    loop.memory_reference = lambda text, turn, **kwargs:retrieve("memory")
    loop.memory_validate = AsyncMock(return_value={"entries":[]})
    original = loop.system
    try:
        await loop.submit("ordinary", input_id="overlap")
        assert started == {"role", "memory"}
        assert cancelled == []
        assert captured[0][0]["content"] == original
        assert "memory_reference" not in json.loads(captured[0][1]["content"])
    finally:
        await loop.close()


def test_host_embedder_uses_actual_prefixed_token_budget_and_prioritizes_current_input():
    import threading
    class Tokenizer:
        def encode(self, value, **_kwargs):
            return list(range(len(value) + 2))  # Explicit start/end special tokens.
        def __call__(self, value, **_kwargs):
            return {"offset_mapping":[(0, 0), *[(index, index + 1) for index in range(len(value))], (0, 0)]}
    embedder = local_embeddings.MemoryE5Embedder.__new__(local_embeddings.MemoryE5Embedder)
    tokenizer = Tokenizer()
    embedder._model = SimpleNamespace(tokenizer=tokenizer, _lock=threading.RLock())
    embedder._max_tokens = 512
    for query in ("C:/src/project/module_123.py:" * 100, "中国語と日本語" * 500):
        prepared = embedder.prepare_query(query, context=("OLD_CONTEXT",))
        assert query.startswith(prepared) and "OLD_CONTEXT" not in prepared
        assert len(tokenizer.encode("query: " + prepared)) <= 512
    prepared = embedder.prepare_query("CURRENT_INPUT", context=("OPTIONAL_HISTORY" * 100,))
    assert prepared.startswith("CURRENT_INPUT\n")
    assert len(tokenizer.encode("query: " + prepared)) <= 512


@pytest.mark.parametrize("change", ["forget", "correct", "scope"])
async def test_final_validation_rejects_memory_changed_during_independent_role_wait(host, change):
    service = await semantic_service(host)
    saved = await remember(host, "私は抹茶が好きです。")
    await service._index_task
    recalled = asyncio.Event()
    async def memory_reference(text, turn, *, deadline):
        reference = await service.recall_for_session("A", text, turn, deadline=deadline)
        assert reference.result.candidates
        recalled.set()
        return reference
    async def role_reference(_text):
        await recalled.wait()
        if change == "forget":
            await forget(host, saved["memory_id"])
        elif change == "correct":
            result = await host.request(Method.MEMORY_CORRECT, memory_id=saved["memory_id"],
                expected_version=1, operation_id="during-rag", item_id="changed-item",
                text="私は紅茶が好きです。", mode="change")
            assert result["ok"]
        else:
            host.project["id"] = "different-project"
        return "ROLE_REFERENCE"
    query = AsyncMock(return_value='{"action":null,"say":"ordinary"}')
    loop = loop_for(query, role_reference)
    loop.memory_reference = memory_reference
    loop.memory_validate = AsyncMock(wraps=service.validate_reference)
    try:
        await loop.submit("绿色饮品", input_id="changed-during-rag")
        loop.memory_validate.assert_awaited_once()
        messages = query.call_args.args[0]
        assert "ROLE_REFERENCE" in messages[0]["content"]
        assert "memory_reference" not in json.loads(messages[1]["content"])
        assert "私は抹茶が好きです。" not in json.dumps(messages, ensure_ascii=False)
    finally:
        await loop.close()


async def test_gate_required_chat_keeps_forget_filtering_independent_of_automatic_recall(host, monkeypatch):
    from test_memory_service import source_for
    service = await semantic_service(host, threshold=None)
    origin, text = source_for("keep PRIVATE_FORGOTTEN keep", role="user")
    saved = await remember(host, "私は抹茶が好きです。", source={**origin, "start_cp":5, "end_cp":22})
    await service._index_task
    await forget(host, saved["memory_id"])
    monkeypatch.setattr(service._query, "run", AsyncMock(side_effect=AssertionError("unused query lane")))
    query = AsyncMock(return_value='{"action":null,"say":"ordinary"}')
    loop = loop_for(query, None)
    loop.history_source = lambda turn:_shared_role_history("A", turn)
    loop.memory_reference = lambda text, turn, **kwargs:service.recall_for_session("A", text, turn, **kwargs)
    loop.memory_validate = AsyncMock(wraps=service.validate_reference)
    try:
        await loop.submit("ordinary", input_id="gate-required", turn_id="next")
        frame = json.loads(query.call_args.args[0][1]["content"])
        assert "PRIVATE_FORGOTTEN" not in json.dumps(frame)
        assert "memory_reference" not in frame
        assert frame["history"][0]["text"] != text
        service._query.run.assert_not_awaited()
        loop.memory_validate.assert_not_awaited()
        assert (await service.status(service.context("A")))["automatic_admission"] == "gate_required"
        assert (await host.request(Method.MEMORY_LIST))["items"] == []
    finally:
        await loop.close()


async def test_final_validity_busy_lane_is_capped_and_recall_provenance_does_not_expire_candidates(host, monkeypatch):
    import threading
    from config.memory import MEMORY_FINAL_VALIDITY_TIMEOUT_S
    service = await semantic_service(host)
    await remember(host, "私は抹茶が好きです。")
    await service._index_task
    reference = await service.recall_for_session("A", "绿色饮品")
    reference = replace(reference, deadline=deadline_now() - 1)
    started, release = threading.Event(), threading.Event()
    def panel_read():
        started.set()
        assert release.wait(3)
    original_run = service._db.run
    panel = asyncio.create_task(original_run(panel_read))
    timeouts = []
    async def observed_run(function, **kwargs):
        timeouts.append(kwargs["timeout"])
        assert kwargs["priority"] is True
        return await original_run(function, **kwargs)
    try:
        assert await asyncio.to_thread(started.wait, 1)
        monkeypatch.setattr(service._db, "run", observed_run)
        with pytest.raises(MemoryRequestError, match="busy"):
            await service.validate_reference(reference, deadline=deadline_now() + 10)
        assert len(timeouts) == 1 and 0 < timeouts[0] <= MEMORY_FINAL_VALIDITY_TIMEOUT_S + 1e-9
        release.set()
        await panel
        assert (await service.validate_reference(reference))["entries"]
    finally:
        release.set()
        await panel
        monkeypatch.setattr(service._db, "run", original_run)


async def test_final_packing_cannot_publish_after_its_validity_window(host, monkeypatch):
    import time
    from config.memory import MEMORY_FINAL_VALIDITY_TIMEOUT_S
    service = await semantic_service(host)
    await remember(host, "私は抹茶が好きです。")
    await service._index_task
    reference = await service.recall_for_session("A", "绿色饮品")
    def slow_token_count(_text):
        time.sleep(MEMORY_FINAL_VALIDITY_TIMEOUT_S * 2)
        return 1
    monkeypatch.setattr(service, "_reply_token_count", slow_token_count)
    monkeypatch.setattr(service, "_reply_token_count_method", "synthetic_slow_counter")
    with pytest.raises(MemoryRequestError, match="busy"):
        await service.validate_reference(reference)


async def test_default_role_rag_timeout_keeps_ready_host_memory_after_its_recall_window(host):
    service = await semantic_service(host)
    await remember(host, "私は抹茶が好きです。")
    await service._index_task
    async def role_reference(_text):
        await asyncio.Event().wait()
    query = AsyncMock(return_value='{"action":null,"say":"ordinary"}')
    loop = loop_for(query, role_reference)
    loop.memory_timeout_s = service.config.recall_timeout_s
    loop.memory_reference = lambda text, turn, **kwargs:service.recall_for_session("A", text, turn, **kwargs)
    loop.memory_validate = AsyncMock(wraps=service.validate_reference)
    try:
        assert loop.memory_timeout_s == .15 and loop.role_reference_timeout_s == .5
        await loop.submit("绿色饮品", input_id="default-rag-timeout")
        loop.memory_validate.assert_awaited_once()
        reference = loop.memory_validate.call_args.args[0]
        final_deadline = loop.memory_validate.call_args.kwargs["deadline"]
        assert reference.deadline < final_deadline
        frame = json.loads(query.call_args.args[0][1]["content"])
        assert [entry["text"] for entry in frame["memory_reference"]["entries"]] == ["私は抹茶が好きです。"]
    finally:
        await loop.close()
