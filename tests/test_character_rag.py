from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

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


def test_local_messages_query_sends_the_given_context(monkeypatch):
    from llm import client

    post = Mock(return_value=SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"message": {"content": "reply"}},
    ))
    monkeypatch.setattr(client, "LLM_PROVIDER", "local")
    monkeypatch.setattr(client, "LOCAL_LLM_TYPE", "ollama")
    monkeypatch.setattr(client.requests, "post", post)
    messages = [{"role": "system", "content": "persona plus reference"},
                {"role": "user", "content": "question"}]
    assert client.remote_llm_messages_query(messages, json_output=False) == "reply"
    assert post.call_args.kwargs["json"]["messages"] == [
        {"role": "system", "content": "persona plus reference"},
        {"role": "user", "content": "question"},
    ]
