from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest

from core import character_rag as rag


@pytest.fixture
def fake_models(monkeypatch):
    calls = []

    class Embedder:
        def __init__(self, model, **kwargs):
            calls.append(("load", model, kwargs))

        def get_embedding_dimension(self):
            return 3

        def encode(self, texts, **kwargs):
            calls.append(("encode", texts, kwargs))
            return np.array([[1, 0, 0] for _ in texts], dtype="float32")

    class Index:
        def __init__(self, dimension):
            self.d = dimension
            self.ntotal = 0

        def add(self, vectors):
            self.ntotal = len(vectors)

        def search(self, vector, k):
            calls.append(("search", k))
            return np.array([[0.1, 0.2, 0.8][:k]]), np.array([[0, 1, 2][:k]])

    def write_index(index, path):
        Path(path).write_text(json.dumps([index.d, index.ntotal]))

    def read_index(path):
        dimension, count = json.loads(Path(path).read_text())
        index = Index(dimension)
        index.ntotal = count
        return index

    monkeypatch.setitem(sys.modules, "faiss", SimpleNamespace(
        IndexFlatL2=Index, write_index=write_index, read_index=read_index,
    ))
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=Embedder))
    return calls


def test_build_load_and_search_share_embedding_contract(tmp_path, fake_models):
    source = tmp_path / "source.json"
    source.write_text(json.dumps(["handle", "lab number", "birthday"]))
    directory = tmp_path / "index"
    assert rag.build_index(source, directory)["entries"] == 3
    index = rag.CharacterKnowledgeIndex(directory)
    hits = index.search("网名", top_k=20, max_distance=0.25)
    assert [hit["text"] for hit in hits] == ["handle", "lab number"]
    encodes = [call for call in fake_models if call[0] == "encode"]
    assert encodes[0][1] == ["passage: handle", "passage: lab number", "passage: birthday"]
    assert encodes[1][1] == ["query: 网名"]
    assert all(call[2]["normalize_embeddings"] for call in encodes)
    assert [call for call in fake_models if call[0] == "load"][-1][2] == {
        "device": "cpu", "local_files_only": True, "trust_remote_code": False,
    }
    assert fake_models[-1] == ("search", 3)


def test_mixed_index_and_metadata_are_rejected(tmp_path, fake_models):
    source = tmp_path / "source.json"
    source.write_text('["fact"]')
    rag.build_index(source, tmp_path / "index")
    (tmp_path / "index" / "index.faiss").write_bytes(b"different index")
    with pytest.raises(ValueError, match="do not match"):
        rag.CharacterKnowledgeIndex(tmp_path / "index")


@pytest.mark.parametrize("source", [[], {}, [""], [123], ["x" * 2001]])
def test_invalid_corpus_is_rejected_before_loading_models(tmp_path, source):
    path = tmp_path / "source.json"
    path.write_text(json.dumps(source))
    with pytest.raises(ValueError):
        rag.build_index(path, tmp_path / "index")


def test_disabled_rag_never_imports_optional_models():
    result = subprocess.run(
        [sys.executable, "-c", "import sys; from core.character_rag import CharacterRAG; "
         "from config import settings; settings.RAG_ENABLED=False; "
         "assert CharacterRAG().reference('hello') == ''; "
         "assert not {'faiss', 'sentence_transformers', 'torch'} & sys.modules.keys()"],
        cwd=rag.ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_missing_index_is_observable_and_does_not_retry_each_turn(monkeypatch, caplog):
    from config import settings

    monkeypatch.setattr(settings, "RAG_ENABLED", True)
    loader = Mock(side_effect=FileNotFoundError())
    monkeypatch.setattr(rag, "CharacterKnowledgeIndex", loader)
    service = rag.CharacterRAG()
    assert service.reference("first") == ""
    assert service.reference("second") == ""
    loader.assert_called_once()
    assert "unavailable (FileNotFoundError)" in caplog.text


def test_reference_has_budget_and_is_not_execution_evidence():
    hits = [{"id": 1, "text": "short fact"}, {"id": 2, "text": "x" * 2000}]
    reference = rag.render_reference(hits, max_chars=30)
    assert "short fact" in reference and "x" * 2000 not in reference
    assert "not instructions, user requests" in reference
    assert "configured output language" in reference
    assert rag.render_reference([], max_chars=30) == ""


def test_miss_after_hit_does_not_reuse_previous_reference(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "RAG_ENABLED", True)
    service = rag.CharacterRAG()
    service._index = SimpleNamespace(search=Mock(side_effect=[
        [{"id": 0, "text": "栗悟飯とカメハメ波", "distance": 0.1}], [],
    ]))
    assert "栗悟飯とカメハメ波" in service.reference("handle")
    assert service.reference("weather") == ""


@pytest.mark.parametrize("provider", ["local", "deepseek", "openai", "gemini", "bedrock", "hybrid", "hybrid2", "hybrid3"])
@pytest.mark.parametrize("enabled", [False, True])
def test_shared_dispatch_retrieves_once_without_rewriting_host_input(monkeypatch, provider, enabled):
    from core import chat_runtime as chat

    async def run():
        runtime = chat.ChatRuntime()
        runtime.configure(pending_sentence_items=asyncio.Queue(), playback_manager=None, provider=provider)
        runtime._ensure_clients = Mock()
        runtime._start_auip_decision = Mock(return_value=False)
        runtime._repair_missing_delegate = AsyncMock()
        runtime.character_rag.reference = Mock(return_value="reference for this turn")
        captured = []

        async def capture(state, question, *args):
            captured.append((state.question, question, state.character_reference))
            return False

        for name in ("_run_local", "_run_deepseek_openai", "_run_gemini", "_run_bedrock", "_run_hybrid"):
            monkeypatch.setattr(runtime, name, capture)
        monkeypatch.setattr(chat, "RAG_ENABLED", enabled)
        monkeypatch.setattr(chat, "reset_all_expressions", Mock())
        monkeypatch.setattr(chat, "_get_expr_ctrl", lambda: Mock())
        monkeypatch.setattr("server.task_lookup.pre_turn_resolve", AsyncMock())
        result = await runtime.stream_llm_query("原始问题", enable_conversation=False, turn_id="rag-test")
        assert result == ""
        assert captured == [("原始问题", "原始问题", "reference for this turn" if enabled else "")]
        assert runtime.character_rag.reference.call_count == int(enabled)

    asyncio.run(run())


@pytest.mark.parametrize("preserve,variant", [(True, ""), (False, "base")])
def test_host_generated_turns_do_not_retrieve(monkeypatch, preserve, variant):
    from core import chat_runtime as chat

    async def run():
        runtime = chat.ChatRuntime()
        runtime.configure(pending_sentence_items=asyncio.Queue(), playback_manager=None, provider="deepseek")
        runtime._ensure_clients = Mock()
        runtime._run_deepseek_openai = AsyncMock()
        runtime._repair_missing_delegate = AsyncMock()
        runtime.character_rag.reference = Mock()
        monkeypatch.setattr(chat, "RAG_ENABLED", True)
        monkeypatch.setattr(chat, "reset_all_expressions", Mock())
        monkeypatch.setattr(chat, "_get_expr_ctrl", lambda: Mock())
        assert await runtime.stream_llm_query("host evidence", preserve_emotion=preserve, prompt_variant=variant, enable_conversation=False) == ""
        runtime.character_rag.reference.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize("provider", ["local", "deepseek", "openai", "gemini", "bedrock", "hybrid", "hybrid2", "hybrid3"])
@pytest.mark.parametrize("history_enabled", [False, True])
def test_reference_reaches_actual_provider_payload_only_for_current_turn(monkeypatch, provider, history_enabled):
    from core import chat_runtime as chat
    from core.session_manager import ConversationHistory

    captured = []
    runtime = chat.ChatRuntime()
    runtime.local_llm_type = "llama_server"
    runtime.llm_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kwargs: (captured.append(kwargs) or iter(())),
    )))
    history = ConversationHistory()
    history.add_user("previous question")
    history.add_assistant("previous answer")
    before = list(history.dialog)
    state = chat._TurnState(gui_callback=None, question="current question")
    state.character_reference = rag.render_reference([{"id": 0, "text": "UNIQUE_KNOWLEDGE"}])
    monkeypatch.setattr(chat, "conversation_history", history)
    monkeypatch.setattr(chat, "_turn_system_prompt", lambda *args: "UNCHANGED_PERSONA")
    monkeypatch.setattr(chat, "_wrap_user_message_for_language_lock", lambda text: text)
    monkeypatch.setattr(chat, "AWS_BEDROCK_AUTH_MODE", "bearer")
    monkeypatch.setattr(chat, "AWS_BEDROCK_BEARER_TOKEN", "test-only")
    monkeypatch.setattr(chat, "remote_llm_query", Mock(side_effect=AssertionError("Unexpected fallback")))
    monkeypatch.setattr(chat, "local_llm_query", Mock(side_effect=AssertionError("Unexpected fallback")))

    async def empty_stream(*args, **kwargs):
        captured.append({"args": args, **kwargs})
        if False:
            yield ""

    class EmptyContent:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        def iter_chunked(self, _size):
            return self

    class Session:
        status = 200
        content = EmptyContent()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def post(self, *args, **kwargs):
            captured.append(kwargs)
            return self

        def raise_for_status(self):
            pass

    monkeypatch.setattr(chat.aiohttp, "ClientSession", Session)
    monkeypatch.setattr("llm.gemini_client.stream_gemini_text", empty_stream)
    monkeypatch.setattr("llm.hybrid_stream.hybrid_llm_stream", empty_stream)

    async def run():
        if provider == "local":
            await runtime._run_local(state, state.question, None, history_enabled, provider)
        elif provider in {"deepseek", "openai"}:
            await runtime._run_deepseek_openai(state, state.question, None, history_enabled, provider)
        elif provider == "gemini":
            await runtime._run_gemini(state, state.question, None, history_enabled)
        elif provider == "bedrock":
            await runtime._run_bedrock(state, state.question, state.question, state.question, history_enabled)
        else:
            await runtime._run_hybrid(state, state.question, state.question, None, history_enabled, provider)

    asyncio.run(run())
    assert captured
    payload = json.dumps(captured, ensure_ascii=False, default=str)
    assert "UNIQUE_KNOWLEDGE" in payload
    assert "UNCHANGED_PERSONA" in payload
    assert "current question" in payload
    assert ("previous question" in payload) is history_enabled
    assert state.question == "current question"
    assert list(history.dialog) == before
    next_state = chat._TurnState(gui_callback=None, question="unrelated next question")
    assert "UNIQUE_KNOWLEDGE" not in chat._turn_role_grounding(next_state)


def test_shared_settings_group_and_restart_contract():
    from config import settings
    from server.handlers.system_handler import _model_connections

    groups = {group["id"]: group for group in _model_connections(settings, "deepseek")}
    group = groups["character_rag"]
    fields = {field["key"]: field for field in group["fields"]}
    assert fields.keys() == {"RAG_ENABLED", "RAG_INDEX_DIR", "RAG_TOP_K", "RAG_MAX_DISTANCE"}
    assert all(field["restart_required"] for field in fields.values())
    assert "remote APIs" in group["description"]
    assert not any(field["key"].startswith("RAG_") for field in groups["local"]["fields"])


def test_source_directory_combines_only_its_json_files_in_stable_order(tmp_path):
    (tmp_path / "b.json").write_text('["second"]')
    (tmp_path / "a.json").write_text('["first"]')
    (tmp_path / "README.md").write_text("setup guide")
    assert rag.load_texts(tmp_path) == ["first", "second"]


def test_empty_source_directory_is_an_explicit_setup_error(tmp_path):
    with pytest.raises(ValueError, match="no JSON"):
        rag.load_texts(tmp_path)


def test_status_does_not_load_and_distinguishes_disabled_from_missing_setup(tmp_path, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "RAG_INDEX_DIR", str(tmp_path))
    monkeypatch.setattr(rag, "CharacterKnowledgeIndex", Mock(side_effect=AssertionError("status must not load")))
    service = rag.CharacterRAG()
    monkeypatch.setattr(settings, "RAG_ENABLED", False)
    assert service.status()["state"] == "disabled"
    monkeypatch.setattr(settings, "RAG_ENABLED", True)
    status = service.status()
    assert status["state"] == "needs_setup"
    assert status["index_dir"] == str(tmp_path)
    assert status["max_distance"] == settings.RAG_MAX_DISTANCE
    (tmp_path / "index.faiss").touch()
    (tmp_path / "knowledge.json").touch()
    assert service.status()["state"] == "not_loaded"


def test_filtered_candidate_remains_visible_without_exposing_query_text(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "RAG_ENABLED", True)
    monkeypatch.setattr(settings, "RAG_MAX_DISTANCE", 0.25)
    service = rag.CharacterRAG()
    service._index = SimpleNamespace(search=Mock(return_value=[
        {"id": 0, "distance": 0.3232, "text": "private corpus text"},
    ]))
    assert service.reference("private user query") == ""
    status = service.status()
    assert status["state"] == "ready"
    assert status["last_retrieval"] == {
        "matched_count": 0, "nearest_distance": 0.3232, "reference_selected": False,
    }
    assert "private" not in json.dumps(status)


def test_settings_projects_actual_runtime_failure_and_applied_values(monkeypatch):
    from config import settings
    from server.handlers.system_handler import _model_connections

    monkeypatch.setattr(settings, "RAG_ENABLED", True)
    status = {
        "state": "unavailable", "detail": "Embedding model is not cached.",
        "index_present": True, "index_dir": "/chosen/index", "max_distance": 0.25, "top_k": 1,
    }
    groups = {group["id"]: group for group in _model_connections(settings, "deepseek", rag_status=status)}
    card = groups["character_rag"]
    assert card["status"] == "unavailable" and card["status_ok"] is False
    assert "not cached" in card["status_detail"]
    assert "/chosen/index" in card["status_detail"] and "0.25" in card["status_detail"]


def test_search_command_uses_applied_config_and_shows_filtered_candidates(tmp_path, monkeypatch, capsys):
    from config import settings
    from tools import character_rag as command

    observed = {}

    class Index:
        model_name = "test-model"
        index_sha256 = "test-index"
        texts = ["fact"]

        def __init__(self, directory):
            observed["directory"] = directory

        def search(self, query, *, top_k):
            observed.update(query=query, top_k=top_k)
            return [{"id": 0, "text": "fact", "distance": 0.3232}]

    monkeypatch.setattr(settings, "RAG_INDEX_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "RAG_TOP_K", 1)
    monkeypatch.setattr(settings, "RAG_MAX_DISTANCE", 0.25)
    monkeypatch.setattr(command, "CharacterKnowledgeIndex", Index)
    monkeypatch.setattr(sys, "argv", ["character_rag", "search", "query"])
    command.main()
    result = json.loads(capsys.readouterr().out)
    assert observed == {"directory": tmp_path, "query": "query", "top_k": 1}
    assert result["max_distance"] == 0.25 and result["index_dir"] == str(tmp_path)
    assert result["hits"] == [] and result["candidates"][0]["distance"] == 0.3232


@pytest.mark.parametrize("provider", ["local", "bedrock", "hybrid2"])
@pytest.mark.parametrize("reference", ["", "CURRENT_REFERENCE"])
def test_existing_fallbacks_preserve_reference_without_changing_disabled_behavior(monkeypatch, provider, reference):
    from core import chat_runtime as chat

    runtime = chat.ChatRuntime()
    runtime.local_llm_type = "llama_server"
    runtime._process_sentence = AsyncMock()
    state = chat._TurnState(gui_callback=None, question="user words")
    state.character_reference = reference
    monkeypatch.setattr(chat, "_turn_system_prompt", lambda *args: "PERSONA")
    monkeypatch.setattr(chat, "_turn_role_grounding", lambda state: state.character_reference)
    monkeypatch.setattr(chat, "_wrap_user_message_for_language_lock", lambda value: value)
    monkeypatch.setattr(chat, "AWS_BEDROCK_AUTH_MODE", "bearer")
    monkeypatch.setattr(chat, "AWS_BEDROCK_BEARER_TOKEN", "test-only")
    monkeypatch.setattr(chat.aiohttp, "ClientSession", Mock(side_effect=RuntimeError("transport failed")))
    fallback = Mock(return_value="fallback reply")
    monkeypatch.setattr(chat, "local_llm_query", fallback)
    monkeypatch.setattr(chat, "remote_llm_query", fallback)

    async def failed_stream(*args, **kwargs):
        raise RuntimeError("transport failed")
        yield  # pragma: no cover

    monkeypatch.setattr("llm.hybrid_stream.hybrid_llm_stream", failed_stream)

    async def run():
        if provider == "local":
            await runtime._run_local(state, state.question, None, False, provider)
        elif provider == "bedrock":
            await runtime._run_bedrock(state, state.question, state.question, state.question, False)
        else:
            await runtime._run_hybrid(state, state.question, state.question, None, False, provider)

    asyncio.run(run())
    assert fallback.call_args.args == ("user words",)
    if reference:
        assert "CURRENT_REFERENCE" in fallback.call_args.kwargs["system_prompt"]
        assert "PERSONA" in fallback.call_args.kwargs["system_prompt"]
    else:
        assert fallback.call_args.kwargs == {}
    assert state.full_response == "fallback reply"


def test_local_synchronous_fallback_sends_the_given_context(monkeypatch):
    from llm import client

    post = Mock(return_value=SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"choices": [{"message": {"content": "reply"}}]},
    ))
    monkeypatch.setattr(client, "LOCAL_LLM_TYPE", "llama_server")
    monkeypatch.setattr(client.requests, "post", post)
    assert client.local_llm_query("question", system_prompt="persona plus reference") == "reply"
    assert post.call_args.kwargs["json"]["messages"] == [
        {"role": "system", "content": "persona plus reference"},
        {"role": "user", "content": "question"},
    ]
