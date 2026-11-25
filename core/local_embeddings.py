"""Legacy E5/RAG owner and explicit memory backend selection; offline only."""
from pathlib import Path
from threading import RLock
import re

from config.local_model_loading import enforce_local_model_loading

_owners = {}
_owners_lock = RLock()


class LocalEmbeddingModel:
    def __init__(self, path: Path):
        from sentence_transformers import SentenceTransformer
        self._lock = RLock()
        self._model = SentenceTransformer(str(path), device="cpu", local_files_only=True,
            trust_remote_code=False)
        self.path = path

    def encode(self, *args, **kwargs):
        with self._lock:
            return self._model.encode(*args, **kwargs)

    def get_embedding_dimension(self):
        return self._model.get_embedding_dimension()

    def token_count(self, value):
        with self._lock:
            return len(self._model.tokenizer.encode(value, truncation=False))

    @property
    def tokenizer(self):
        return self._model.tokenizer

    @property
    def max_seq_length(self):
        return self._model.max_seq_length


def get_local_embedding_model(model: str) -> LocalEmbeddingModel:
    """Blocking owner: call off-loop. Resolve cache only, never download."""
    enforce_local_model_loading()
    path = Path(model).expanduser()
    if not path.is_dir():
        from huggingface_hub import snapshot_download
        path = Path(snapshot_download(model, local_files_only=True))
    path = path.resolve()
    with _owners_lock:
        if path not in _owners:
            _owners[path] = LocalEmbeddingModel(path)
        return _owners[path]


class MemoryE5Embedder:
    """Eidema's small embedding interface backed by the shared Host owner."""
    def __init__(self, model: str, *, version: str = ""):
        self._model = get_local_embedding_model(model)
        snapshot = self._model.path.name
        if not version and re.fullmatch(r"[0-9a-f]{40,64}", snapshot) is None:
            raise ValueError("local_model_version_required")
        self.version = "amadeus-e5-query-passage-v1:" + (version or snapshot)
        self.dimensions = self._model.get_embedding_dimension()
        self._max_tokens = min(512, self._model.max_seq_length)
        # Provisional automatic-admission profile: only this retained, tested
        # e5 snapshot with the exact Host query/passage/L2/512-token contract.
        self.automatic_threshold = (.84 if snapshot == "614241f622f53c4eeff9890bdc4f31cfecc418b3"
            and self.dimensions == 384 and self._max_tokens == 512 else None)

    def _fits(self, value):
        return self._model.token_count(value) <= self._max_tokens

    def split_passage(self, text):
        chunks, start = [], 0
        while start < len(text):
            lo, hi, best = start + 1, min(len(text), start + 2048), start
            while lo <= hi:
                mid = (lo + hi) // 2
                if self._fits("passage: " + text[start:mid]):
                    best, lo = mid, mid + 1
                else:
                    hi = mid - 1
            if best == start:
                raise ValueError("embedding_character_too_long")
            chunks.append(text[start:best])
            start = best
        return chunks

    def embed_passages(self, texts):
        values = ["passage: " + value for value in texts]
        if not all(self._fits(value) for value in values):
            raise ValueError("passage_too_long")
        return self._model.encode(values, batch_size=8, normalize_embeddings=True,
            show_progress_bar=False).tolist()

    def embed_query(self, query):
        value = "query: " + query
        if not self._fits(value):
            raise ValueError("query_too_long")
        return self._model.encode(value, normalize_embeddings=True,
            show_progress_bar=False).tolist()

    def prepare_query(self, query, *, context=()):
        from eidema import prepare_embedding_query

        # The shared resource owns tokenizer access too. Prefix and special
        # tokens count against the model's actual finite input budget.
        with self._model._lock:
            return prepare_embedding_query(self._model.tokenizer, query,
                max_tokens=self._max_tokens, context=context)


def create_memory_embedder(model, *, backend="", version=""):
    """Select an explicit memory contract; character RAG always retains E5."""
    from config.memory_jina import JINA_MODEL_ID
    # Preserve pre-backend MODEL/MODEL_VERSION installations, including local
    # E5 directories. A custom Jina directory deliberately selects its backend.
    if not backend:
        backend = "jina-onnx-q4" if model == JINA_MODEL_ID else "e5"
    if backend == "jina-onnx-q4":
        from core.jina_embeddings import MemoryJinaEmbedder
        return MemoryJinaEmbedder(None if model == JINA_MODEL_ID else model, version=version)
    if backend == "e5":
        return MemoryE5Embedder(model, version=version)
    from config.memory_jina import MemoryModelError
    raise MemoryModelError("model_unsupported")
