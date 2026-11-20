"""Shared observation helpers; waveform integrity and phones are not listening acceptance."""
from __future__ import annotations

import hashlib
import json

def reference_fingerprints(cache: dict) -> dict:
    """Record actual conditioning, not just the source WAV/text identities."""
    import torch

    result = {}
    for key, value in cache.items():
        if torch.is_tensor(value):
            raw = value.detach().contiguous().cpu()
            result[key] = {"shape": list(raw.shape), "dtype": str(raw.dtype),
                           "sha256": hashlib.sha256(raw.numpy().tobytes()).hexdigest()}
        else:
            result[key] = {"sha256": hashlib.sha256(
                json.dumps(value, sort_keys=True).encode()).hexdigest()}
    return result


def align_reference_conditioning(current: dict, baseline: dict) -> bool:
    """Freeze paired reference encoding only when all of its inputs agree.

    V3 reference encoding may differ numerically between fresh GPU runs. This
    experiment helper must never hide a real reference-phone/BERT/audio change.
    Production reference caching is unaffected.
    """
    if current.keys() != baseline.keys():
        return False
    inputs = current.keys() - {'prompt_fea_ref', 'prompt_ge'}
    if reference_fingerprints({k:current[k] for k in inputs}) != reference_fingerprints({k:baseline[k] for k in inputs}):
        return False
    import copy
    current.clear()
    current.update(copy.deepcopy(baseline))
    return True


def target_readings(nodes: list[dict], surface: str) -> list[str]:
    """Whole-node spans only; missing/ambiguous spans are unscored, not errors."""
    from config.japanese_dictionary import latin_word_key
    # MeCab normalizes ASCII apostrophes to curly apostrophes. Match the same
    # lexical identity as admission/boundary code, without changing speech.
    normalize = lambda value: latin_word_key(value).strip()
    target = normalize(surface)
    found = []
    for start in range(len(nodes)):
        word, reading = "", ""
        for node in nodes[start:]:
            word += normalize(node["string"])
            reading += node["read"]
            if word == target:
                found.append(reading)
                break
            if not target.startswith(word):
                break
    return found
