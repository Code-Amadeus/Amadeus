"""Jina's offline vector boundary; synthetic inputs require no model download."""
import hashlib
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest

from config.environment import EnvironmentReader
from config.memory import MemoryConfig
from config.memory_jina import JINA_CONTRACT, JINA_MEMORY_VERSION, JINA_MODEL_ID, MemoryModelError
from core import jina_embeddings as jina, local_embeddings
from tools import prepare_memory_model as preparation
from test_memory_service import host as host


class CharacterTokenizer:
    def __init__(self):
        self.padding = False
    def encode(self, value):
        return SimpleNamespace(ids=[1, *range(10, len(value) + 10), 2],
            offsets=[(0, 0), *[(i, i + 1) for i in range(len(value))], (0, 0)])
    def encode_batch(self, values):
        encoded = [self.encode(value) for value in values]
        maximum = max(len(value.ids) for value in encoded)
        return [SimpleNamespace(ids=value.ids + [0] * (maximum - len(value.ids)),
            attention_mask=[1] * len(value.ids) + [0] * (maximum - len(value.ids))) for value in encoded]
    def enable_padding(self, **_kwargs):
        self.padding = True
    def no_padding(self):
        self.padding = False


@pytest.fixture
def adapter():
    model = jina._JinaModel.__new__(jina._JinaModel)
    model._lock, model.np = RLock(), np
    model.tokenizer = CharacterTokenizer()
    model.query_tokenizer = jina._QueryTokenizer(model.tokenizer)
    model.padding = {}
    def inference(outputs, values):
        assert outputs == ["sentence_embedding"]
        assert values["input_ids"].dtype == np.int64
        vector = np.zeros((values["input_ids"].shape[0], 768), dtype=np.float32)
        vector[:, :2] = [3, 4]
        return [vector]
    model.session = SimpleNamespace(run=Mock(side_effect=inference))
    value = jina.MemoryJinaEmbedder.__new__(jina.MemoryJinaEmbedder)
    value._model = model
    return value


def test_default_selects_jina_and_explicit_e5_keeps_live_rag_owner(monkeypatch):
    config = MemoryConfig.from_environment(EnvironmentReader({}))
    assert config.model == JINA_MODEL_ID and config.model_backend == ""
    constructor = Mock(return_value="jina")
    monkeypatch.setattr(jina, "MemoryJinaEmbedder", constructor)
    assert local_embeddings.create_memory_embedder(config.model) == "jina"
    constructor.assert_called_once_with(None, version="")
    legacy = Mock(return_value="e5")
    monkeypatch.setattr(local_embeddings, "MemoryE5Embedder", legacy)
    assert local_embeddings.create_memory_embedder("legacy-snapshot", backend="e5", version="old") == "e5"
    legacy.assert_called_once_with("legacy-snapshot", version="old")
    for old_model in ("intfloat/multilingual-e5-small", "arbitrary-local-directory"):
        old_config = MemoryConfig.from_environment(EnvironmentReader({
            "AMADEUS_MEMORY_MODEL":old_model, "AMADEUS_MEMORY_MODEL_VERSION":"old"}))
        assert local_embeddings.create_memory_embedder(old_config.model,
            backend=old_config.model_backend, version=old_config.model_version) == "e5"
        legacy.assert_called_with(old_model, version="old")
    constructor.reset_mock()
    assert local_embeddings.create_memory_embedder("custom-jina", backend="jina-onnx-q4") == "jina"
    constructor.assert_called_once_with("custom-jina", version="")
    legacy.reset_mock()
    constructor.side_effect = MemoryModelError("model_missing")
    with pytest.raises(MemoryModelError, match="model_missing"):
        local_embeddings.create_memory_embedder(JINA_MODEL_ID)
    legacy.assert_not_called()
    with pytest.raises(ValueError, match="invalid_memory_configuration"):
        MemoryConfig.from_environment(EnvironmentReader({"AMADEUS_MEMORY_MODEL_BACKEND":"automatic"}))
    with pytest.raises(MemoryModelError, match="model_unsupported"):
        local_embeddings.create_memory_embedder("path", backend="automatic")


def test_exact_prefix_special_token_boundary_and_selected_normalized_output(adapter):
    # The complete prefixed input includes both special tokens at the boundary.
    limit = JINA_CONTRACT["max_passage_tokens"]
    text = "x" * (limit - 12)
    assert len(adapter._model.tokenizer.encode("Document: " + text).ids) == limit
    vectors = adapter.embed_passages([text, "short"])
    assert len(vectors) == 2 and len(vectors[0]) == 768
    assert np.allclose(vectors[0][:2], [.6, .8])
    assert np.isclose(np.linalg.norm(vectors[0]), 1)
    with pytest.raises(ValueError, match="passage_too_long"):
        adapter.embed_passages([text + "x"])
    assert len(adapter.embed_query("x" * 247)) == 768  # Query prefix is 7.
    with pytest.raises(ValueError, match="query_too_long"):
        adapter.embed_query("x" * 248)
    assert not adapter._model.tokenizer.padding


def test_lossless_chunks_and_bounded_current_input_priority(adapter):
    for text in ("abc " * 4000, "中国語と日本語🙂\n" * 400):
        chunks = adapter.split_passage(text)
        assert len(chunks) > 1 and "".join(chunks) == text
        assert all(adapter._model.fits("Document: " + chunk, max_tokens=JINA_CONTRACT["max_passage_tokens"]) for chunk in chunks)
        prepared = adapter.prepare_query(text, context=("OPTIONAL_HISTORY",))
        assert text.startswith(prepared) and "OPTIONAL_HISTORY" not in prepared
        assert adapter._model.fits("Query: " + prepared)
    query = adapter.prepare_query("CURRENT_INPUT", context=("RECENT", "OLD" * 500))
    assert query.startswith("CURRENT_INPUT\nRECENT\n")
    assert adapter._model.fits("Query: " + query)


def test_invalid_output_fails_and_releases_tokenizer_padding(adapter):
    for output in (np.zeros((1, 768)), np.full((1, 768), np.nan), np.ones((1, 3))):
        adapter._model.session.run.side_effect = None
        adapter._model.session.run.return_value = [output]
        with pytest.raises(ValueError, match="invalid_embedding|zero_embedding"):
            adapter.embed_query("input")
        assert not adapter._model.tokenizer.padding


def test_same_verified_snapshot_reuses_one_resource(tmp_path, monkeypatch):
    constructor = Mock(return_value=object())
    monkeypatch.setattr(jina, "_owners", {})
    monkeypatch.setattr(jina, "verify_jina_snapshot", lambda path: Path(path).resolve())
    monkeypatch.setattr(jina, "_JinaModel", constructor)
    assert jina._get_model(tmp_path) is jina._get_model(tmp_path / ".")
    constructor.assert_called_once()


def test_unqualified_runtime_and_version_rejected_without_loader(monkeypatch):
    monkeypatch.setattr(jina, "package_version", lambda name:"unexpected")
    with pytest.raises(MemoryModelError, match="model_unsupported"):
        jina._JinaModel(Path("unused"))
    with pytest.raises(MemoryModelError, match="model_unsupported"):
        jina.MemoryJinaEmbedder("unused", version="e5-version")
    monkeypatch.setattr(jina, "package_version", Mock(side_effect=jina.PackageNotFoundError("onnxruntime")))
    with pytest.raises(ImportError, match="memory_runtime_missing"):
        jina._JinaModel(Path("unused"))


def _synthetic_manifest(tmp_path, monkeypatch):
    import config.memory_jina as manifest
    files = {"tokenizer.json": b"tokenizer", "onnx/model_q4.onnx": b"graph", "README.md": b"license"}
    hashes = {name: (len(data), hashlib.sha256(data).hexdigest()) for name, data in files.items()}
    monkeypatch.setattr(manifest, "JINA_FILES", hashes)
    monkeypatch.setattr(preparation, "JINA_FILES", hashes)
    source = tmp_path / "source"
    for name, data in files.items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return source


def test_atomic_offline_preparation_verifies_and_includes_license(tmp_path, monkeypatch):
    source = _synthetic_manifest(tmp_path, monkeypatch)
    destination = tmp_path / "prepared"
    assert preparation.prepare(destination=destination, source=source) == destination
    assert "CC-BY-NC-4.0" in (destination / "NOTICE.md").read_text()
    assert JINA_MEMORY_VERSION in (destination / "prepared.json").read_text()
    assert preparation.prepare(destination=destination, source=source) == destination
    (destination / "tokenizer.json").write_bytes(b"corrupt")
    with pytest.raises(MemoryModelError, match="model_corrupt"):
        preparation.prepare(destination=destination, source=source)


def test_corrupt_copy_never_publishes_and_missing_assets_are_truthful(tmp_path, monkeypatch):
    from config.memory_jina import verify_jina_snapshot
    source = _synthetic_manifest(tmp_path, monkeypatch)
    (source / "tokenizer.json").write_bytes(b"wrongdata")
    destination = tmp_path / "unpublished"
    with pytest.raises(MemoryModelError, match="model_corrupt"):
        preparation.prepare(destination=destination, source=source)
    assert not destination.exists() and not list(tmp_path.glob(".jina-prepare-*"))
    with pytest.raises(MemoryModelError, match="model_missing"):
        verify_jina_snapshot(destination)


@pytest.mark.parametrize("reason", ["model_missing", "model_corrupt", "model_unsupported"])
async def test_service_surfaces_model_reason_and_keeps_store_available(tmp_path, reason):
    from server.memory_service import HostMemoryService
    def unavailable():
        raise MemoryModelError(reason)
    service = HostMemoryService(MemoryConfig(db_path=tmp_path / "memory.sqlite3"), embedder_factory=unavailable)
    service.start()
    try:
        await service._startup
        await service._index_task
        assert service._state == "ready" and service._semantic_reason == reason
        assert service._store is not None
    finally:
        await service.close()


def test_jina_version_migration_interruption_and_forget_cannot_restore_stale_vectors(tmp_path):
    import concurrent.futures
    import sqlite3
    import threading
    import eidema
    access = eidema.AccessScope("synthetic", "character")
    scope = eidema.MemoryScope("character", "synthetic", "character")
    class Legacy:
        version, dimensions = "amadeus-e5-query-passage-v1:old", 2
        def split_passage(self, text):
            return [text]
        def embed_passages(self, texts):
            return [[1., 0.] for _ in texts]
        def embed_query(self, query):
            return [1., 0.]
    class Migrated(Legacy):
        version, dimensions = JINA_MEMORY_VERSION, 768
        def embed_passages(self, texts):
            return [[1., *([0.] * 767)] for _ in texts]
        def embed_query(self, query):
            return [1., *([0.] * 767)]
    path = tmp_path / "migration.sqlite3"
    store = eidema.MemoryStore(path)
    store.configure_embedder(Legacy())
    saved = store.remember(access, scope, operation_id="save", item_id="memory",
        text="synthetic preference", now_ms=1)
    store.run_index_batch()
    assert store.recall(access, "unmatched", now_ms=2).candidates
    store.configure_embedder(Migrated())
    assert store.detail(access, saved.memory_id).memory.index_state == "pending"
    assert not store.recall(access, "unmatched", now_ms=3).candidates
    store.close()  # Interrupted after invalidation, before any Jina index batch.
    store = eidema.MemoryStore(path)
    store.configure_embedder(Migrated())
    assert not store.recall(access, "unmatched", now_ms=4).candidates
    store.run_index_batch()
    assert store.recall(access, "unmatched", now_ms=5).candidates
    class PreviousJina(Migrated):
        pass
    for previous in (
        "amadeus-jina-onnx-q4-v1:cb63e3b21e8ec7775f7a423a06bc6ae7319d36957e933e097e015916a0610950",
        "amadeus-jina-onnx-q4-v1:9367705812ae66e74db91d037cdcc3f915d2f92fd9fc30e41bff5b5f34ceeea6",
        "amadeus-jina-onnx-q4-v1:60aca62813f3accc5971e17fbfa9a503f18599f04665de3ab6c5852127e32fe8",
    ):
        PreviousJina.version = previous
        store.configure_embedder(PreviousJina())
        store.run_index_batch()
        store.configure_embedder(Migrated())
        assert store.detail(access, saved.memory_id).memory.index_state == "pending"
        assert not store.recall(access, "unmatched", now_ms=5).candidates
        store.run_index_batch()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT DISTINCT embedding_version FROM embeddings").fetchall() == [(JINA_MEMORY_VERSION,)]
    started, release = threading.Event(), threading.Event()
    class Blocked(Migrated):
        version = JINA_MEMORY_VERSION + ":changed"
        def embed_passages(self, texts):
            started.set()
            assert release.wait(3)
            return super().embed_passages(texts)
    store.configure_embedder(Blocked())
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(store.run_index_batch)
            assert started.wait(1)
            preview = store.preview_forget(access, saved.memory_id, 1)
            store.forget(access, saved.memory_id, 1, operation_id="forget", item_id="memory",
                now_ms=6, confirmed_targets=preview.targets)
            release.set()
            pending.result(timeout=3)
        assert not store.recall(access, "synthetic preference", now_ms=7).candidates
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT count(*) FROM embeddings").fetchone()[0] == 0
    finally:
        release.set()
        store.close()


def test_explicit_download_uses_the_same_pinned_verification(tmp_path, monkeypatch):
    import io
    from config.memory_jina import JINA_REVISION
    source = _synthetic_manifest(tmp_path, monkeypatch)
    requested = []
    def response(url, *, timeout):
        requested.append(url)
        relative = url.split("/" + JINA_REVISION + "/", 1)[1]
        return io.BytesIO((source / relative).read_bytes())
    monkeypatch.setattr(preparation.urllib.request, "urlopen", response)
    destination = tmp_path / "downloaded"
    preparation.prepare(destination=destination, download=True)
    assert len(requested) == 3 and all(JINA_REVISION in url for url in requested)
    assert (destination / "NOTICE.md").is_file()
    (source / "tokenizer.json").write_bytes(b"not pinned")
    with pytest.raises(ValueError, match="model_corrupt"):
        preparation.prepare(destination=tmp_path / "bad-download", download=True)
    assert not (tmp_path / "bad-download").exists()


@pytest.mark.parametrize("passage_cap", [64, 96])
def test_maximum_text_and_condition_index_with_final_lossless_chunker(adapter, tmp_path, monkeypatch, passage_cap):
    monkeypatch.setitem(JINA_CONTRACT, "max_passage_tokens", passage_cap)
    import eidema
    from server.memory_service import (MAX_MEMORY_TEXT_CHARS, MAX_MEMORY_CONDITION_CHARS,
        MEMORY_INDEX_CHUNK_LIMIT)
    store = eidema.MemoryStore(tmp_path / "capacity.sqlite3", max_text_chars=MAX_MEMORY_TEXT_CHARS)
    access = eidema.AccessScope("synthetic", "character")
    scope = eidema.MemoryScope("character", "synthetic", "character")
    text = "界" * MAX_MEMORY_TEXT_CHARS
    condition = "条件" * (MAX_MEMORY_CONDITION_CHARS // 2)
    combined = text + "\n" + condition
    chunks = adapter.split_passage(combined)
    assert 128 < len(chunks) <= MEMORY_INDEX_CHUNK_LIMIT
    assert "".join(chunks) == combined
    try:
        store.configure_embedder(adapter)
        saved = store.remember(access, scope, operation_id="save", item_id="boundary",
            text=text, condition=condition, now_ms=1)
        # The former count-only ceiling rejects the same legal, lossless input.
        assert store.run_index_batch(limit=1, max_chunks=128).failed == 1
        assert adapter._model.session.run.call_count == 0
        outcome = store.run_index_batch(limit=1, max_chunks=MEMORY_INDEX_CHUNK_LIMIT, retry_failed=True)
        assert outcome.indexed == 1 and outcome.failed == 0
        memory = store.detail(access, saved.memory_id).memory
        assert memory.index_state == "ready" and memory.text == text and memory.condition == condition
        assert adapter._model.session.run.call_count == len(chunks)
    finally:
        store.close()


async def test_host_text_bound_stays_closed_above_maximum(host, monkeypatch):
    from server.memory_service import MAX_MEMORY_TEXT_CHARS
    from server.protocol import Method
    mutation = Mock(side_effect=AssertionError("oversized input reached store"))
    monkeypatch.setattr(host.service._store, "remember", mutation)
    result = await host.request(Method.MEMORY_REMEMBER, operation_id="too-large", item_id="input",
        scope="character", text="界" * (MAX_MEMORY_TEXT_CHARS + 1))
    assert not result["ok"] and result["error"] == "invalid_request"
    mutation.assert_not_called()


def test_pathological_fragmentation_above_cap_fails_truthfully(adapter, tmp_path):
    import eidema
    from server.memory_service import MEMORY_INDEX_CHUNK_LIMIT
    access = eidema.AccessScope("synthetic", "character")
    scope = eidema.MemoryScope("character", "synthetic", "character")
    # A valid lossless but deliberately pathological fragmenter still cannot
    # create unbounded inference work from one otherwise legal text entry.
    adapter.split_passage = list
    store = eidema.MemoryStore(tmp_path / "fragmentation.sqlite3")
    try:
        store.configure_embedder(adapter)
        saved = store.remember(access, scope, operation_id="save", item_id="fragmented",
            text="x" * (MEMORY_INDEX_CHUNK_LIMIT + 1), now_ms=1)
        outcome = store.run_index_batch(limit=1, max_chunks=MEMORY_INDEX_CHUNK_LIMIT)
        assert outcome.failed == 1 and outcome.indexed == 0
        assert store.detail(access, saved.memory_id).memory.index_state == "failed"
        adapter._model.session.run.assert_not_called()
    finally:
        store.close()


@pytest.mark.parametrize("override", [None, .84])
async def test_default_jina_gate_and_explicit_override_are_truthful(tmp_path, monkeypatch, override):
    from server.memory_service import HostMemoryService
    # Exercise the default factory without loading weights. Only the Host
    # configuration grants automatic admission.
    monkeypatch.setattr(jina, "_get_model", lambda path: object())
    service = HostMemoryService(MemoryConfig(db_path=tmp_path / "gate.sqlite3", semantic_threshold=override))
    service.start()
    try:
        await service._startup
        await service._index_task
        status = await service.status({"character_id":"synthetic", "project_id":None})
        assert status["semantic_state"] == "ready"
        assert status["automatic_threshold"] == override
        assert status["automatic_admission"] == ("gate_required" if override is None else "ready")
        assert not status["threshold_calibrated"]
        if override is None:
            query = AsyncMock(side_effect=AssertionError("Jina query without automatic admission"))
            recall = Mock(side_effect=AssertionError("store recall without automatic admission"))
            monkeypatch.setattr(service._query, "run", query)
            monkeypatch.setattr(service._store, "recall", recall)
            assert await service.recall_for_session("", "synthetic") is None
            query.assert_not_awaited()
            recall.assert_not_called()
    finally:
        await service.close()


@pytest.mark.parametrize("history_cap", [64, 96])
def test_optional_history_budget_preserves_both_turns_head_tail_and_current(adapter, monkeypatch, history_cap):
    monkeypatch.setitem(JINA_CONTRACT, "max_history_tokens", history_cap)
    current = "CURRENT_QUESTION"
    recent = ("HEAD_A " + "a" * 200 + " TAIL_A", "HEAD_B " + "b" * 200 + " TAIL_B")
    prepared = adapter.prepare_query(current, context=recent)
    assert prepared.startswith(current + "\n")
    for anchor in ("HEAD_A", "TAIL_A", "HEAD_B", "TAIL_B"):
        assert anchor in prepared
    assert prepared.count("\n[…]\n") == 2
    count = lambda text:len(adapter._model.tokenizer.encode("Query: " + text).ids)
    assert count(prepared) - count(current) <= history_cap
    assert count(prepared) <= JINA_CONTRACT["max_tokens"]


@pytest.mark.parametrize("history_cap", [64, 96])
def test_short_history_stays_verbatim_and_unused_share_goes_to_other_turn(adapter, monkeypatch, history_cap):
    monkeypatch.setitem(JINA_CONTRACT, "max_history_tokens", history_cap)
    recent = ("Recent complete statement.", "Older complete statement.")
    assert adapter.prepare_query("CURRENT", context=recent) == "CURRENT\n" + "\n".join(recent)
    prepared = adapter.prepare_query("CURRENT", context=("short", "HEAD " + "x" * 200 + " TAIL"))
    assert prepared.startswith("CURRENT\nshort\nHEAD") and prepared.endswith("TAIL")
    assert len(prepared.removeprefix("CURRENT\nshort\n")) > history_cap // 2


@pytest.mark.parametrize("current", ["short query", "C:/src/module_123.py:" * 100,
    "中国語と日本語🙂\n" * 100, "x" * 231 + " FINAL_CONSTRAINT"])
def test_history_never_changes_current_only_256token_projection(adapter, monkeypatch, current):
    from eidema import prepare_embedding_query
    monkeypatch.setitem(JINA_CONTRACT, "max_history_tokens", 64)
    expected = prepare_embedding_query(adapter._model.query_tokenizer, current,
        max_tokens=256, prefix="Query: ")
    assert adapter.prepare_query(current) == expected
    with_history = adapter.prepare_query(current, context=("long previous topic " * 100, "older context " * 100))
    assert with_history.startswith(expected)
    if expected != current or len(adapter._model.tokenizer.encode("Query: " + expected).ids) >= 255:
        assert with_history == expected
    assert len(adapter._model.tokenizer.encode("Query: " + with_history).ids) <= 256


@pytest.mark.parametrize("passage_cap", [64, 96])
def test_offset_splitter_tokenizes_linearly_and_preserves_unicode_code_and_whitespace(adapter, monkeypatch, passage_cap):
    monkeypatch.setitem(JINA_CONTRACT, "max_passage_tokens", passage_cap)
    tokenizer = adapter._model.tokenizer
    encode = Mock(wraps=tokenizer.encode)
    monkeypatch.setattr(tokenizer, "encode", encode)
    text = ("字🙂 C:/src/file_12.py  \n\t" * 100) + "   \n"
    chunks = adapter.split_passage(text)
    assert "".join(chunks) == text
    assert encode.call_count <= len(chunks) + 3
    assert all(adapter._model.fits("Document: " + chunk, max_tokens=passage_cap) for chunk in chunks)


@pytest.mark.parametrize("passage_cap", [64, 96])
def test_duplicate_byte_offsets_and_prefix_retokenization_stay_lossless_and_bounded(adapter, monkeypatch, passage_cap):
    monkeypatch.setitem(JINA_CONTRACT, "max_passage_tokens", passage_cap)
    class ByteTokenizer(CharacterTokenizer):
        def encode(self, value):
            ids, offsets = [1], [(0, 0)]
            for index, char in enumerate(value):
                size = len(char.encode("utf-8"))
                ids.extend([10] * size)
                offsets.extend([(index, index + 1)] * size)
            if value.startswith("Document: #"):
                ids.extend([11] * 3)  # Exact prefix boundary can differ from source tokenization.
                offsets.extend([(10, 11)] * 3)
            return SimpleNamespace(ids=[*ids, 2], offsets=[*offsets, (0, 0)])
    adapter._model.tokenizer = ByteTokenizer()
    for text in ("#🙂中a  \n" * 30, chr(0x10ffff) * 36864 + "\n"):
        chunks = adapter.split_passage(text)
        assert "".join(chunks) == text
        assert all(adapter._model.fits("Document: " + chunk, max_tokens=passage_cap) for chunk in chunks)
        from server.memory_service import MEMORY_INDEX_CHUNK_LIMIT
        assert len(chunks) <= MEMORY_INDEX_CHUNK_LIMIT <= 4096


def test_processing_contract_change_invalidates_previous_192token_profile():
    import json
    baseline = {**JINA_CONTRACT, "max_passage_tokens":192,
        "truncation":"reject-overflow;query-current-first-offsets-v1;passage-lossless-v1"}
    baseline.pop("max_history_tokens", None)
    previous = "amadeus-jina-onnx-q4-v1:" + hashlib.sha256(
        json.dumps(baseline, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert JINA_MEMORY_VERSION != previous



def test_long_current_final_constraint_survives_history_budget(adapter, monkeypatch):
    monkeypatch.setitem(JINA_CONTRACT, "max_history_tokens", 64)
    current = "x" * 220 + " KEEP_FINAL_CONSTRAINT"
    assert len(adapter._model.tokenizer.encode("Query: " + current).ids) <= 256
    assert adapter.prepare_query(current, context=("previous unrelated topic " * 50,)) == current


@pytest.mark.parametrize("passage_cap", [64, 96])
def test_short_passage_units_allow_foreground_query_then_finish_index(adapter, tmp_path, monkeypatch, passage_cap):
    import concurrent.futures
    import threading
    import time
    import eidema
    monkeypatch.setitem(JINA_CONTRACT, "max_passage_tokens", passage_cap)
    first, release_first, second, release_second, queried = (threading.Event() for _ in range(5))
    original_passages, original_query = adapter.embed_passages, adapter.embed_query
    consumed = []
    def passages(texts):
        consumed.extend(texts)
        if len(consumed) == 1:
            first.set()
            assert release_first.wait(3)
        elif len(consumed) == 2:
            second.set()
            assert release_second.wait(3)
        return original_passages(texts)
    def query(text):
        vector = original_query(text)
        queried.set()
        return vector
    monkeypatch.setattr(adapter, "embed_passages", passages)
    monkeypatch.setattr(adapter, "embed_query", query)
    store = eidema.MemoryStore(tmp_path / "priority.sqlite3")
    access, scope = eidema.AccessScope("synthetic", "character"), eidema.MemoryScope("character", "synthetic", "character")
    store.configure_embedder(adapter)
    text = "source including a qualification and no lost characters. " * 20
    saved = store.remember(access, scope, operation_id="save", item_id="long", text=text, now_ms=1)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            indexing = executor.submit(store.run_index_batch, limit=1, max_chunks=4096)
            assert first.wait(1)
            foreground = executor.submit(store.recall, access, "ordinary question", now_ms=2)
            # Observe the real gate's queued reader before releasing its native unit.
            deadline = time.monotonic() + 1
            while True:
                with store._embedding_lock._condition:
                    if store._embedding_lock._queries:
                        break
                assert time.monotonic() < deadline
                time.sleep(.001)
            release_first.set()
            foreground.result(timeout=1)
            assert queried.is_set() and second.wait(1) and not indexing.done()
            release_second.set()
            assert indexing.result(timeout=3).indexed == 1
        assert "".join(consumed) == text
        assert store.detail(access, saved.memory_id).memory.index_state == "ready"
    finally:
        release_first.set();release_second.set();store.close()


def test_correction_during_short_unit_index_cannot_publish_old_vectors(adapter, tmp_path, monkeypatch):
    import concurrent.futures
    import sqlite3
    import threading
    import eidema
    monkeypatch.setitem(JINA_CONTRACT, "max_passage_tokens", 64)
    started, release = threading.Event(), threading.Event()
    original = adapter.embed_passages
    def passages(texts):
        started.set()
        assert release.wait(3)
        return original(texts)
    monkeypatch.setattr(adapter, "embed_passages", passages)
    store = eidema.MemoryStore(tmp_path / "correction.sqlite3")
    access, scope = eidema.AccessScope("synthetic", "character"), eidema.MemoryScope("character", "synthetic", "character")
    store.configure_embedder(adapter)
    old = store.remember(access, scope, operation_id="save", item_id="old", text="old preference " * 20, now_ms=1)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            indexing = executor.submit(store.run_index_batch, limit=1, max_chunks=4096)
            assert started.wait(1)
            changed = store.correct(access, old.memory_id, 1, operation_id="correct", item_id="new",
                text="new preference", mode="change", now_ms=2)
            release.set()
            assert indexing.result(timeout=3).stale == 1
        with sqlite3.connect(tmp_path / "correction.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM embeddings WHERE memory_id=?", (old.memory_id,)).fetchone()[0] == 0
        store.run_index_batch(limit=1, max_chunks=4096)
        assert store.detail(access, changed.memory_id).memory.index_state == "ready"
        result = store.recall(access, "new preference", now_ms=3)
        assert result.candidates and all(item.memory_id != old.memory_id for item in result.candidates)
    finally:
        release.set();store.close()
