"""Opt-in real E5/FAISS check; normal model-less suites never download a model."""

import json
import os
from pathlib import Path

import pytest

from core.character_rag import CharacterKnowledgeIndex, resolve_directory


@pytest.mark.skipif(
    os.environ.get("AMADEUS_TEST_CHARACTER_RAG") != "1",
    reason="requires explicitly built starter index and cached E5 model",
)
def test_starter_corpus_positive_and_unrelated_queries():
    index = CharacterKnowledgeIndex(resolve_directory(".amadeus/character-rag"))
    cases = json.loads((Path(__file__).parent / "fixtures/character_rag_retrieval.json").read_text(encoding="utf-8"))
    report = []
    for case in cases:
        candidates = index.search(case["query"], top_k=3)
        hits = [hit for hit in candidates if hit["distance"] <= 0.33]
        expected = case["expected_any"]
        passed = (
            any(fragment in hit["text"] for hit in hits for fragment in expected)
            if expected else not hits
        )
        report.append({**case, "passed": passed, "hits": hits, "candidates": candidates})
    output = resolve_directory(".amadeus/character-rag/retrieval-check.json")
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    assert all(row["passed"] for row in report), json.dumps(
        [row for row in report if not row["passed"]], ensure_ascii=False, indent=2
    )
