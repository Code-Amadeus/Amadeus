"""CPU graph/array diagnostics; these are not measured Metal kernel or traffic counts.

The historical implementation is read from the fixed, audited Git commit. It is
never installed as a second production backend. Run from a checkout with that
commit available; source-release users consume the recorded report instead.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import types

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
BASELINE = "a1873b783108eba31ababa436fd0b9ba69050016"


def graph_operations(dot):
    """Count DOT primitive rectangles, excluding array/source/sink nodes."""
    return dict(sorted(Counter(re.findall(
        r'\[label\s*=\s*"([^"\n]+)"\s*,\s*shape=rectangle\]', dot)).items()))


def historical_model():
    source = subprocess.run(
        ["git", "show", f"{BASELINE}:tts/semantic_mlx/model.py"],
        cwd=ROOT, capture_output=True, check=True).stdout
    name = "tts.semantic_mlx._audited_structure_baseline"
    module = types.ModuleType(name)
    module.__package__ = "tts.semantic_mlx"
    sys.modules[name] = module
    exec(compile(source, f"{BASELINE}:model.py", "exec"), module.__dict__)
    return module.T2SModel, hashlib.sha256(source).hexdigest()


def measure(model, arrays, *, chunked, warmup=1):
    import mlx.core as mx
    import numpy as np

    rows = []
    for trial in range(warmup + 1):
        logits, cache = model.prefill(arrays["phones"], arrays["prompt"], arrays["bert"])
        mx.eval(logits, cache.layers)
        for step in range(arrays["history"].shape[1]):
            token = arrays["history"][:, step:step + 1]
            mx.eval(token)
            valid_before = cache.text_length + cache.audio_length
            capacity_before = cache.layers[0][0].shape[2]
            started = time.perf_counter()
            logits, cache = model.decode_step(token, cache)
            build_ms = (time.perf_counter() - started) * 1000
            dot = io.StringIO()
            mx.export_to_dot(dot, logits, *[item for layer in cache.layers for item in layer])
            operations = graph_operations(dot.getvalue())
            mx.eval(logits, cache.layers)
            if not np.isfinite(np.array(logits)).all():
                raise ValueError("Nonfinite decode logits in structural diagnostic")
            if trial >= warmup:
                # Logical array sizes only: donation, allocator copies and
                # actual memory transactions require native backend profiling.
                row_bytes = sum(item.shape[0] * item.shape[1] * item.shape[-1] * item.itemsize
                                for layer in cache.layers for item in layer)
                rows.append({
                    "step": step, "kv_valid_before": valid_before,
                    "kv_capacity_before": capacity_before,
                    "kv_capacity_after": cache.layers[0][0].shape[2],
                    "graph_primitive_nodes": sum(operations.values()),
                    "graph_operations": operations, "python_graph_build_ms": build_ms,
                    "kv_new_token_bytes": row_bytes,
                    "kv_concat_result_bytes": (
                        row_bytes * (valid_before + 1) if not chunked else
                        row_bytes * cache.layers[0][0].shape[2]
                        if cache.layers[0][0].shape[2] > capacity_before else 0),
                    "kv_physical_copy_bytes": None,
                })
    return {
        "weight_array_bytes": sum(value.nbytes for value in model.weights.values()),
        "dtype": str(next(iter(model.weights.values())).dtype),
        "decode_steps": rows,
        "scope": "one forward graph; sampler and asynchronous scheduling excluded",
        "timing_scope": "Python construction before DOT export and evaluation; CPU diagnostic only",
        "physical_weight_bytes_read_per_token": None,
        "kv_copy_note": "Logical update/concatenate sizes only; slice donation and physical copies unmeasured",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--cache", default="")
    parser.add_argument("--inputs", default="")
    parser.add_argument("--phones", type=int, default=104)
    parser.add_argument("--prompt", type=int, default=191)
    parser.add_argument("--steps", type=int, default=32)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if min(args.phones, args.prompt, args.steps) < 1 or args.warmup < 0:
        parser.error("lengths must be positive; warmup must be nonnegative")

    import mlx.core as mx
    import numpy as np
    from tools.probes.gsv_backend_probe import _artifact, _environment, _identity, _inputs
    from tts.semantic_mlx.model import T2SModel

    model_path = ROOT / "tts/semantic_mlx/model.py"
    model_source_hash = hashlib.sha256(model_path.read_bytes()).hexdigest()
    mx.set_default_device(mx.cpu)
    artifact, config, manifest = _artifact(args)
    data, fixture = _inputs(args, config)
    if data["history"].shape[1] == 0:
        raise ValueError("Structural decode probe requires a nonempty token history")
    arrays = {key: mx.array(value.astype(np.float32 if key == "bert" else np.int32))
              for key, value in data.items()}
    mx.eval(arrays)
    old_type, old_hash = historical_model()
    weights = mx.load(str(artifact / "model.safetensors"))
    results = {}
    baseline = old_type(config, weights)
    results["audited_fp32"] = measure(baseline, arrays, chunked=False, warmup=args.warmup)
    del baseline
    for dtype in ("float32", "float16"):
        model = T2SModel(config, weights, inference_dtype=dtype)
        results[f"optimized_{dtype}"] = measure(model, arrays, chunked=True, warmup=args.warmup)
        del model
    old_nodes = results["audited_fp32"]["decode_steps"][0]["graph_primitive_nodes"]
    new_nodes = results["optimized_float32"]["decode_steps"][0]["graph_primitive_nodes"]
    if model_source_hash != hashlib.sha256(model_path.read_bytes()).hexdigest():
        raise RuntimeError("Model source changed during structural measurement; rerun")
    report = {
        "schema": "amadeus.gsv_mlx_structure.v1", "status": "recorded",
        "purpose": "windows_or_linux_cpu_structure_diagnostic_not_metal_speed",
        **_identity(), "environment": _environment(), **fixture,
        "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "model_source_sha256": model_source_hash,
        "source_checkpoint_sha256": manifest["source_checkpoint_sha256"],
        "baseline_sha": BASELINE, "baseline_model_sha256": old_hash,
        "warmup_runs_excluded": args.warmup, "measured_passes": 1, "variants": results,
        "first_step_graph_ratio": new_nodes / old_nodes,
        "performance_gate": "retired: CPU graph nodes cannot evaluate Metal fusion",
        "limitations": [
            "DOT primitive nodes are not Metal kernels; backend lowering can decompose operations.",
            "Weight array bytes are residency, not measured memory traffic per token.",
            "Slice updates permit donation; physical O(1) copying is not established here.",
            "Forward graph-build time excludes sampling and does not establish asynchronous overlap.",
        ],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"structure: recorded -> {output}")
    return report


if __name__ == "__main__":
    main()
