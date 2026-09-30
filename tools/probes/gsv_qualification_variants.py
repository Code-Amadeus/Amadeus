"""Probe-only MLX revisions; frozen sources run without a Git checkout.

The four snapshot files are byte-for-byte ``git show`` output. Their original
relative ``.weights`` imports resolve against the current strict artifact
reader, while model math, cache layout, sampler, and scheduling stay frozen.
No application configuration selects these historical implementations.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import importlib
import importlib.util
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
REVISIONS = ("audit", "unpadded", "current")
SNAPSHOTS = {
    "audit": {
        "commit": "a1873b783108eba31ababa436fd0b9ba69050016",
        "sha256": {
            "model": "a5aa4e5ba76c26ea1c61be12ad44827439316b5b94dea52d5603a26be9e33b92",
            "generation": "31d424a3e309f2831d17b0f852b146018c75e72f4b19e116d296b534c1291993",
        },
    },
    "unpadded": {
        "commit": "184543d402c4d0907e6cd36dc58a909a0440568e",
        "sha256": {
            "model": "5d65bb38c9218c4dee55324fb5f641586f2526bfaf4e2f124efe55dd223a62c8",
            "generation": "8c10b157cffef8fe1658398b02f81ca4dc0cce6aa7d7d16d30a5cbcc3776e066",
        },
    },
}


def bundled_source_paths():
    """Files that bind the probe's variant registry and historical sources."""
    return ("tools/probes/gsv_qualification_variants.py", *(
        f"tools/probes/gsv_qualification_snapshots/{revision}_{kind}.py"
        for revision in SNAPSHOTS for kind in ("model", "generation")
    ))


def validate_selection(backend, inference_dtype, mlx_revision="current"):
    if backend not in {"torch", "mlx"} or inference_dtype not in {"float32", "float16"}:
        raise ValueError("Qualification requires a supported backend and inference dtype")
    if mlx_revision not in REVISIONS:
        raise ValueError("Unknown MLX qualification revision")
    if backend == "torch" and mlx_revision != "current":
        raise ValueError("Historical --mlx-revision requires --backend mlx")
    if backend == "mlx" and mlx_revision == "audit" and inference_dtype != "float32":
        raise ValueError("The frozen audit MLX implementation supports only float32")


def _source_path(revision, kind):
    if revision == "current":
        return ROOT / "tts" / "semantic_mlx" / f"{kind}.py"
    return ROOT / "tools" / "probes" / "gsv_qualification_snapshots" / f"{revision}_{kind}.py"


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_identity(backend, mlx_revision="current"):
    """Source provenance only; importing this helper never loads MLX/Torch."""
    validate_selection(backend, "float32", mlx_revision)
    if backend == "torch":
        from tts.semantic_mlx.weights import REFERENCE_SHA

        return {
            "mlx_revision": "not_applicable", "model_source_commit": REFERENCE_SHA,
            "source_kind": "working_tree",
            "source_sha256": {"model": _sha(ROOT / "GPT_SoVITS/AR/models/t2s_model.py"),
                              "generation": _sha(ROOT / "GPT_SoVITS/AR/models/utils.py")},
        }
    hashes = {kind: _sha(_source_path(mlx_revision, kind)) for kind in ("model", "generation")}
    if mlx_revision in SNAPSHOTS:
        snapshot = SNAPSHOTS[mlx_revision]
        if hashes != snapshot["sha256"]:
            raise ValueError(f"Frozen MLX qualification source checksum mismatch: {mlx_revision}")
        commit, kind = snapshot["commit"], "frozen_snapshot"
    else:
        try:
            commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                    capture_output=True, text=True, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            commit = None
        kind = "working_tree"
    return {"mlx_revision": mlx_revision, "model_source_commit": commit,
            "source_kind": kind, "source_sha256": hashes}


@lru_cache(maxsize=None)
def _snapshot_module(revision, kind):
    # The package name retains the snapshot's original .weights import without
    # editing its source or introducing historical artifact readers.
    name = f"tts.semantic_mlx._gsv_qualification_{revision}_{kind}"
    spec = importlib.util.spec_from_file_location(name, _source_path(revision, kind))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def generation_module(mlx_revision="current"):
    source_identity("mlx", mlx_revision)  # Verify the frozen source before use.
    if mlx_revision == "current":
        return importlib.import_module("tts.semantic_mlx.generation")
    return _snapshot_module(mlx_revision, "generation")


def load_model(artifact, *, mlx_revision="current", inference_dtype="float32",
               validated_config=None):
    validate_selection("mlx", inference_dtype, mlx_revision)
    source_identity("mlx", mlx_revision)
    module = (importlib.import_module("tts.semantic_mlx.model") if mlx_revision == "current"
              else _snapshot_module(mlx_revision, "model"))
    if mlx_revision == "audit":
        model = module.T2SModel.from_artifact(artifact)
        model.inference_dtype = "float32"
    else:
        model = module.T2SModel.from_artifact(
            artifact, inference_dtype=inference_dtype, validated_config=validated_config)
    # These attributes describe the frozen layouts to the current owned-buffer
    # bridge. They do not change the snapshot's model computation or cache.
    if mlx_revision != "current":
        model.head_dim = model.config.hidden_dim // model.config.num_heads
        model.cache_head_dim = model.head_dim
    return model


def create_decoder(artifact, *, mlx_revision="current", inference_dtype="float32",
                   device=None, purpose="numerical_test", validated_config=None):
    import mlx.core as mx
    from tts.semantic_mlx.runtime import MLXSemanticDecoder

    device = mx.cpu if device is None else device
    with mx.stream(device):
        model = load_model(artifact, mlx_revision=mlx_revision,
                           inference_dtype=inference_dtype, validated_config=validated_config)
    decoder = MLXSemanticDecoder(model, device=device, purpose=purpose,
                                 generation_fn=generation_module(mlx_revision).generate)
    decoder.info.update(source_identity("mlx", mlx_revision))
    if mlx_revision == "audit":
        decoder.info["cache_impl"] = "mlx_concatenated_valid_prefix"
    return decoder
