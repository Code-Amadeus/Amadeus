"""Pinned Jina model, artifact integrity and numerical profile for Amadeus."""
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from threading import RLock

from config.memory_jina import (JINA_CONTRACT, JINA_DEFAULT_PATH, JINA_MEMORY_VERSION,
                                MemoryModelError, verify_jina_snapshot)

_owners = {}
_owners_lock = RLock()


def _load_model(path):
    for name, expected in (("onnxruntime", "1.26.0"), ("tokenizers", "0.22.1"), ("numpy", "1.26.4")):
        try:
            actual = package_version(name)
        except PackageNotFoundError:
            raise ImportError("memory_runtime_missing") from None
        if actual != expected:
            raise MemoryModelError("model_unsupported")

    from eidema import MemoryValidationError
    from eidema.onnx import OnnxTextEmbedder

    try:
        return OnnxTextEmbedder(path / "onnx/model_q4.onnx", path / "tokenizer.json",
            path / "tokenizer_config.json", version=JINA_MEMORY_VERSION,
            dimensions=JINA_CONTRACT["dimensions"], query_prefix=JINA_CONTRACT["query_prefix"],
            document_prefix=JINA_CONTRACT["document_prefix"], max_tokens=JINA_CONTRACT["max_tokens"],
            max_passage_tokens=JINA_CONTRACT["max_passage_tokens"],
            max_history_tokens=JINA_CONTRACT["max_history_tokens"], output_name=JINA_CONTRACT["output"])
    except (MemoryValidationError, OSError, ValueError):
        raise MemoryModelError("model_unsupported") from None


def create_jina_embedder(model_path: str | Path | None = None, *, version: str = ""):
    if version and version != JINA_MEMORY_VERSION:
        raise MemoryModelError("model_unsupported")
    path = verify_jina_snapshot(model_path or JINA_DEFAULT_PATH)
    with _owners_lock:
        if path not in _owners:
            _owners[path] = _load_model(path)
        return _owners[path]
