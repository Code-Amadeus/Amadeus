"""Offline Jina Q4 Eidema adapter, independent of the character E5/FAISS stack."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as package_version
from bisect import bisect_right
import json
from pathlib import Path
from threading import RLock

from config.memory_jina import (JINA_CONTRACT, JINA_DEFAULT_PATH, JINA_MEMORY_VERSION,
                                MemoryModelError, verify_jina_snapshot)

_owners = {}
_owners_lock = RLock()


class _QueryTokenizer:
    """The existing Eidema bounded-query API over a Rust tokenizers instance."""
    def __init__(self, tokenizer):
        self.tokenizer = tokenizer

    def encode(self, value, **_kwargs):
        return self.tokenizer.encode(value).ids

    def __call__(self, value, **_kwargs):
        return {"offset_mapping": self.tokenizer.encode(value).offsets}


class _JinaModel:
    def __init__(self, path):
        # Pinned runtime is part of the calibrated numerical profile, not an
        # invitation to alter the ASR/TTS/Transformers dependency families.
        for name, expected in (("onnxruntime", "1.26.0"), ("tokenizers", "0.22.1"), ("numpy", "1.26.4")):
            try:
                actual = package_version(name)
            except PackageNotFoundError:
                raise ImportError("memory_runtime_missing") from None
            if actual != expected:
                raise MemoryModelError("model_unsupported")
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.path, self.np, self._lock = path, np, RLock()
        self.tokenizer = Tokenizer.from_file(str(path / "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        config = json.loads((path / "tokenizer_config.json").read_text(encoding="utf-8"))
        pad_token = config["pad_token"]
        pad_id = self.tokenizer.token_to_id(pad_token)
        if pad_id is None or config.get("padding_side", "right") != "right":
            raise MemoryModelError("model_unsupported")
        self.padding = {"direction": "right", "pad_id": pad_id, "pad_token": pad_token}
        self.query_tokenizer = _QueryTokenizer(self.tokenizer)
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        try:
            self.session = ort.InferenceSession(str(path / "onnx/model_q4.onnx"),
                sess_options=options, providers=["CPUExecutionProvider"])
        except Exception:
            raise MemoryModelError("model_unsupported") from None
        self.session.disable_fallback()
        self.inputs = self.session.get_inputs()
        outputs = {item.name: item for item in self.session.get_outputs()}
        if (set(item.name for item in self.inputs) != {"input_ids", "attention_mask"}
                or any(item.type != "tensor(int64)" for item in self.inputs)
                or "sentence_embedding" not in outputs
                or outputs["sentence_embedding"].shape[-1] != 768):
            raise MemoryModelError("model_unsupported")

    def fits(self, value, *, max_tokens=JINA_CONTRACT["max_tokens"]):
        return len(self.tokenizer.encode(value).ids) <= max_tokens

    def encode(self, values, *, overflow, max_tokens=JINA_CONTRACT["max_tokens"]):
        with self._lock:
            # Never silently truncate a stored passage or direct query call.
            if not all(self.fits(value, max_tokens=max_tokens) for value in values):
                raise ValueError(overflow)
            if not values:
                return []
            np = self.np
            vectors = []
            try:
                self.tokenizer.enable_padding(**self.padding)
                for start in range(0, len(values), 8):
                    encoded = self.tokenizer.encode_batch(values[start:start + 8])
                    feeds = {"input_ids": np.asarray([x.ids for x in encoded], dtype=np.int64),
                        "attention_mask": np.asarray([x.attention_mask for x in encoded], dtype=np.int64)}
                    output = np.asarray(self.session.run(["sentence_embedding"], feeds)[0], dtype=np.float32)
                    if output.shape != (len(encoded), 768) or not np.isfinite(output).all():
                        raise ValueError("invalid_embedding")
                    norms = np.linalg.norm(output, axis=1, keepdims=True)
                    if (norms == 0).any():
                        raise ValueError("zero_embedding")
                    vectors.extend((output / norms).tolist())
            finally:
                self.tokenizer.no_padding()
            return vectors


def _get_model(path):
    path = verify_jina_snapshot(path)
    with _owners_lock:
        if path not in _owners:
            _owners[path] = _JinaModel(path)
        return _owners[path]


class MemoryJinaEmbedder:
    dimensions = 768
    version = JINA_MEMORY_VERSION

    def __init__(self, model_path: str | Path | None = None, *, version: str = ""):
        if version and version != self.version:
            raise MemoryModelError("model_unsupported")
        self._model = _get_model(model_path or JINA_DEFAULT_PATH)
        from core.memory_admission import automatic_threshold
        self.automatic_threshold = automatic_threshold(self.version)
        self.threshold_calibrated = self.automatic_threshold is not None

    def split_passage(self, text):
        chunks, start = [], 0
        prefix, limit = JINA_CONTRACT["document_prefix"], JINA_CONTRACT["max_passage_tokens"]
        with self._model._lock:
            tokenizer = self._model.tokenizer
            # Tokenize the whole source once; byte tokens may share an offset.
            # Offsets choose boundaries, while exact prefixed measurements own fit.
            ends = [end for begin, end in tokenizer.encode(text).offsets if end > begin]
            available = limit - len(tokenizer.encode(prefix).ids)
            if available < 1:
                raise ValueError("embedding_character_too_long")
            while start < len(text):
                index = bisect_right(ends, start)
                end = (ends[index + available - 1]
                    if index + available < len(ends) else len(text))
                end = max(start + 1, end)
                # Prefix/boundary retokenization can add tokens. Retain a complete
                # codepoint and verify it instead of assuming token additivity.
                while end > start and not self._model.fits(prefix + text[start:end], max_tokens=limit):
                    end -= 1
                if end == start:
                    raise ValueError("embedding_character_too_long")
                chunks.append(text[start:end])
                start = end
        return chunks

    def embed_passages(self, texts):
        return self._model.encode([JINA_CONTRACT["document_prefix"] + text for text in texts],
            overflow="passage_too_long", max_tokens=JINA_CONTRACT["max_passage_tokens"])

    def embed_query(self, query):
        return self._model.encode([JINA_CONTRACT["query_prefix"] + query], overflow="query_too_long")[0]

    def prepare_query(self, query, *, context=()):
        from eidema import prepare_embedding_query
        with self._model._lock:
            tokenizer, prefix = self._model.tokenizer, JINA_CONTRACT["query_prefix"]
            maximum, history_cap = JINA_CONTRACT["max_tokens"], JINA_CONTRACT["max_history_tokens"]
            current = prepare_embedding_query(self._model.query_tokenizer, query,
                max_tokens=maximum, prefix=prefix)
            # Current input owns its unchanged 256-token projection. History can
            # use only a separately capped remainder; it cannot displace detail.
            recent = [text for text in context if isinstance(text, str) and text.strip()][:2]
            if current != query or not recent:
                return current
            current_tokens = len(tokenizer.encode(prefix + current).ids)
            budget = min(history_cap, maximum - current_tokens)
            if budget <= 0:
                return current
            target = current_tokens + budget
            combined = current + "\n" + "\n".join(recent)
            if len(tokenizer.encode(prefix + combined).ids) <= target:
                return combined

            special = len(tokenizer.encode("").ids)
            def added_tokens(text):
                return len(tokenizer.encode("\n" + text).ids) - special
            costs = [added_tokens(text) for text in recent]
            shares = [min(cost, budget // len(recent) + (index < budget % len(recent)))
                for index, cost in enumerate(costs)]
            # Give an unused half to the other turn rather than wasting it.
            spare = budget - sum(shares)
            for index, cost in enumerate(costs):
                extra = min(spare, cost - shares[index])
                shares[index] += extra
                spare -= extra
            offsets = [[(begin, end) for begin, end in tokenizer.encode(text).offsets if end > begin]
                for text in recent]
            marker = "\n[…]\n"
            marker_cost = added_tokens(marker)
            def excerpt(index):
                text, share = recent[index], shares[index]
                if costs[index] <= share:
                    return text
                body = share - marker_cost
                while body >= 2:
                    head, tail = (body + 1) // 2, body // 2
                    spans = offsets[index]
                    end = spans[min(head, len(spans)) - 1][1]
                    begin = spans[max(0, len(spans) - tail)][0]
                    bounded = text if end >= begin else text[:end] + marker + text[begin:]
                    if added_tokens(bounded) <= share:
                        return bounded
                    body -= 1
                return ""
            while True:
                excerpts = [excerpt(index) for index in range(len(recent))]
                prepared = current + "".join("\n" + text for text in excerpts if text)
                if len(tokenizer.encode(prefix + prepared).ids) <= target:
                    return prepared
                # Final measurement includes cross-boundary tokenization. Trim
                # the larger allocation until the whole actual input fits.
                index = max(range(len(shares)), key=shares.__getitem__)
                shares[index] -= 1
