from __future__ import annotations

import subprocess
import sys

from core.continuity import MemoryCandidate, MemoryKind, MemoryResolver, TurnEvidence


def test_semantic_module_is_lazy_and_does_not_import_rag_stack_by_default() -> None:
    code = (
        "import sys, tempfile; from pathlib import Path; "
        "from core.continuity.store import ContinuityStore; "
        "from core.continuity.embedding_runtime import MemorySemanticIndex; "
        "s=ContinuityStore(Path(tempfile.mkdtemp())/'c.sqlite3'); "
        "MemorySemanticIndex(s, model_id='intfloat/multilingual-e5-small'); "
        "assert not {'faiss','sentence_transformers','torch'} & sys.modules.keys(); s.close()"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_embedding_rows_are_derived_and_removed_when_memory_becomes_inactive(continuity_store) -> None:
    resolver = MemoryResolver()
    first = TurnEvidence("s", "t1", "我的生日是1月2日", "ack", observed_at=100.0)
    candidate = MemoryCandidate(
        memory_key="user.fact.birth_date",
        kind=MemoryKind.USER_FACT,
        summary="我的生日是1月2日",
        predicate="birth_date",
        object_text="1月2日",
    )
    continuity_store.apply_memory_candidates(first, (candidate,), resolver=resolver, complete_turn=True)
    record = continuity_store.get_active_memory("user.fact.birth_date", scope="s")
    assert record is not None
    continuity_store.upsert_memory_embedding(
        record.id,
        model_id="fake-model",
        dimension=2,
        vector_blob=b"12345678",
        content_hash=continuity_store.memory_embedding_content_hash(record),
        updated_at=100.0,
    )
    assert continuity_store.get_memory_embedding(record.id, model_id="fake-model") is not None

    second = TurnEvidence("s", "t2", "我的生日是2月3日", "ack", observed_at=200.0)
    replacement = MemoryCandidate(
        memory_key="user.fact.birth_date",
        kind=MemoryKind.USER_FACT,
        summary="我的生日是2月3日",
        predicate="birth_date",
        object_text="2月3日",
    )
    continuity_store.apply_memory_candidates(second, (replacement,), resolver=resolver, complete_turn=True)
    assert continuity_store.get_memory_embedding(record.id, model_id="fake-model") is None


def test_semantic_cosine_score_is_not_neutrally_inflated() -> None:
    from core.continuity.embedding_runtime import _semantic_score_from_cosine

    assert _semantic_score_from_cosine(-0.4) == 0.0
    assert _semantic_score_from_cosine(0.0) == 0.0
    assert _semantic_score_from_cosine(0.72) == 0.72
    assert _semantic_score_from_cosine(1.2) == 1.0
