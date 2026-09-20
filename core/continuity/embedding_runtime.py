"""Optional local semantic recall for C3 Continuity memory.

The semantic layer is a derived accelerator.  It never owns memory truth and it
is disabled by default.  When enabled, all heavyweight imports are lazy and
model loading is local-files-only so a chat turn never downloads a model.
"""

from __future__ import annotations

import logging
from threading import Lock
from typing import Sequence

from core.continuity.models import MemoryRecord
from core.continuity.store import ContinuityStore

logger = logging.getLogger(__name__)

def _semantic_score_from_cosine(score: float) -> float:
    """Clamp normalized cosine/IP without inflating neutral similarity."""

    return max(0.0, min(1.0, float(score)))


class MemorySemanticIndex:
    """Lazy E5 + FAISS search over active memory records."""

    def __init__(self, store: ContinuityStore, *, model_id: str) -> None:
        self.store = store
        self.model_id = str(model_id)
        self._embedder = None
        self._unavailable = False
        self._lock = Lock()

    @property
    def available(self) -> bool:
        return not self._unavailable

    def _load_embedder(self):
        if self._unavailable:
            return None
        if self._embedder is not None:
            return self._embedder
        with self._lock:
            if self._embedder is not None:
                return self._embedder
            if self._unavailable:
                return None
            try:
                from config.local_model_loading import enforce_local_model_loading

                enforce_local_model_loading()
                from sentence_transformers import SentenceTransformer

                self._embedder = SentenceTransformer(
                    self.model_id,
                    device="cpu",
                    local_files_only=True,
                    trust_remote_code=False,
                )
            except Exception as exc:
                self._unavailable = True
                logger.warning(
                    "[Continuity] semantic recall unavailable (%s); falling back to structured/FTS",
                    type(exc).__name__,
                )
                return None
        return self._embedder

    def _vectors_for_records(self, records: Sequence[MemoryRecord]):
        import numpy as np

        embedder = self._load_embedder()
        if embedder is None or not records:
            return None

        vectors: list[object | None] = [None] * len(records)
        missing_indices: list[int] = []
        missing_texts: list[str] = []
        for index, record in enumerate(records):
            expected_hash = self.store.memory_embedding_content_hash(record)
            cached = self.store.get_memory_embedding(record.id, model_id=self.model_id)
            if cached is None or cached[2] != expected_hash:
                missing_indices.append(index)
                missing_texts.append("passage: " + record.summary)
                continue
            dimension, blob, _ = cached
            vector = np.frombuffer(blob, dtype=np.float32)
            if vector.size != dimension:
                missing_indices.append(index)
                missing_texts.append("passage: " + record.summary)
                continue
            vectors[index] = vector.copy()

        if missing_texts:
            encoded = embedder.encode(
                missing_texts,
                normalize_embeddings=True,
                convert_to_numpy=True,
            ).astype("float32")
            for local_index, record_index in enumerate(missing_indices):
                vector = encoded[local_index]
                record = records[record_index]
                self.store.upsert_memory_embedding(
                    record.id,
                    model_id=self.model_id,
                    dimension=int(vector.shape[0]),
                    vector_blob=vector.tobytes(),
                    content_hash=self.store.memory_embedding_content_hash(record),
                )
                vectors[record_index] = vector

        if not vectors or any(vector is None for vector in vectors):
            return None
        matrix = np.vstack(vectors).astype("float32")
        return matrix

    def search(
        self,
        query: str,
        records: Sequence[MemoryRecord],
        *,
        top_k: int,
    ) -> dict[str, float]:
        """Return memory_id -> normalized cosine/IP score.

        Any optional dependency/model failure degrades to an empty result.
        """

        if not str(query or "").strip() or not records or self._unavailable:
            return {}
        try:
            import faiss
            import numpy as np

            matrix = self._vectors_for_records(records)
            embedder = self._load_embedder()
            if matrix is None or embedder is None:
                return {}
            query_vector = embedder.encode(
                ["query: " + str(query)],
                normalize_embeddings=True,
                convert_to_numpy=True,
            ).astype("float32")
            dimension = int(matrix.shape[1])
            base = faiss.IndexFlatIP(dimension)
            index = faiss.IndexIDMap2(base)
            numeric_ids = np.arange(1, len(records) + 1, dtype=np.int64)
            index.add_with_ids(matrix, numeric_ids)
            scores, ids = index.search(query_vector, min(max(1, int(top_k)), len(records)))
            result: dict[str, float] = {}
            for score, numeric_id in zip(scores[0], ids[0]):
                if int(numeric_id) <= 0:
                    continue
                record = records[int(numeric_id) - 1]
                # Normalized E5 vectors already produce cosine/IP in [-1, 1].
                # Do not remap 0 -> 0.5: that would give unrelated memories a
                # material semantic score and cause false-positive recall.
                result[record.id] = _semantic_score_from_cosine(float(score))
            return result
        except Exception as exc:
            logger.warning(
                "[Continuity] semantic search failed (%s); using structured/FTS only",
                type(exc).__name__,
            )
            return {}
