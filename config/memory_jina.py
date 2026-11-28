"""Pinned noncommercial memory model; runtime never downloads artifacts."""
from pathlib import Path
import hashlib
import json

JINA_MODEL_ID = "jinaai/jina-embeddings-v5-text-nano-retrieval"
JINA_REVISION = "ac5d898c8d382b17167c33e5c8af644a3519b47d"
JINA_LICENSE = "CC-BY-NC-4.0"
# SHA-256 and byte lengths checked against the pinned upstream artifact manifests.
JINA_FILES = {
    "tokenizer.json": (17210235, "98d4a1d32152d6cedf85b5e88f3b205106dca1fe72aaab34e0ac13c238421069"),
    "tokenizer_config.json": (487, "6c4640d432db970b2436a4386d3ee992b99e756b62c37446c3f581c8d09cbb05"),
    "onnx/model_q4.onnx": (129099, "a44811eb61f90bfcc24720c04e774ce9f5dc1d1539fe32a814aa40b16efbaa1f"),
    "onnx/model_q4.onnx_data": (137824256, "6f63e71508c585ac8221f0d6a3c1afd3200ab2952cbd08421e10e9aa1f9e7c46"),
    "README.md": (9223, "9f2433969e7962fffd5cd2bfef18bb319032dc973090d5449628d9bb4d219381"),
}
JINA_CONTRACT = {
    "model": JINA_MODEL_ID, "revision": JINA_REVISION,
    "artifacts": JINA_FILES, "precision": "onnx-q4",
    "query_prefix": "Query: ", "document_prefix": "Document: ",
    "output": "sentence_embedding", "pooling": "graph-last-token",
    "normalization": "L2-float32", "dimensions": 768,
    "max_tokens": 256, "max_passage_tokens": 64, "max_history_tokens": 64,
    "truncation": "reject-overflow;query-current-first-history-fair-head-tail-v2;passage-lossless-offsets-v2",
    "runtime": "onnxruntime-1.26.0;tokenizers-0.22.1;numpy-1.26.4;cpu-intra4-inter1-sequential",
}
JINA_MEMORY_VERSION = "amadeus-jina-onnx-q4-v1:" + hashlib.sha256(
    json.dumps(JINA_CONTRACT, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
JINA_DEFAULT_PATH = Path(__file__).resolve().parents[1] / "runtime" / "models" / "jina-nano-q4" / JINA_REVISION


class MemoryModelError(ValueError):
    """Public reason code, without local paths or model input."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def verify_jina_snapshot(path):
    """Verify all pinned bytes before handing any artifact to native loaders."""
    path = Path(path).expanduser().resolve()
    if not path.is_dir():
        raise MemoryModelError("model_missing")
    for name, (size, expected) in JINA_FILES.items():
        artifact = path / name
        if not artifact.is_file():
            raise MemoryModelError("model_missing")
        with artifact.open("rb") as stream:
            if artifact.stat().st_size != size or hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise MemoryModelError("model_corrupt")
    return path
