"""Compare KV storage owners with unchanged bucket graphs and fixed AR work.

Run each layout/device/attention in a fresh process. --baseline-source accepts
an untouched local snapshot for the original implementation; it is never
imported for other layouts. Synthetic inputs are for timing/correctness only.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import gc
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "GPT_SoVITS"))

CASES = [
    ("short_448", 256, 32, 64, 1),
    ("unaligned_512", 303, 32, 96, 1),
    ("aligned_512", 304, 32, 96, 1),
    ("medium_768", 304, 128, 192, 1),
    ("long_1024", 304, 400, 192, 1),
    ("overflow_dynamic", 303, 32, 224, 1),
    ("prompt_outside_buckets", 928, 32, 32, 1),
    ("reference_free", 0, 64, 32, 1),
    ("batch_two", 256, 32, 32, 2),
]


def digest(path):
    with open(path, "rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layout", choices=["original", "graph", "bucket", "nested"], required=True)
    parser.add_argument("--attention", choices=["flash", "sdpa"], default="flash")
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--baseline-source", type=Path)
    parser.add_argument("--cases", nargs="+")
    parser.add_argument("--buckets", type=int, nargs="+",
                        help="Override routing/capture buckets, e.g. 1024 for the maximum-shape ablation")
    parser.add_argument("--skip-precapture", action="store_true",
                        help="Measure lazy capture, including the one-short-graph memory tradeoff")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.repeats < 1 or (args.layout == "original" and not args.baseline_source):
        parser.error("positive repeats and --baseline-source for original are required")
    cases = [case for case in CASES if not args.cases or case[0] in args.cases]
    if not cases:
        parser.error("no matching cases")
    if args.buckets and (min(args.buckets) < 1 or args.buckets != sorted(set(args.buckets))):
        parser.error("--buckets must be positive and strictly increasing")
    os.environ["TTS_KV_CACHE_LAYOUT"] = "graph" if args.layout == "original" else args.layout
    import torch
    from config.settings import TTS_GPT_MODEL_PATH
    import AR.models.t2s_model as module
    from AR.models import t2s_flash_attn as flash
    if args.layout == "original":
        spec = importlib.util.spec_from_file_location("AR.models.t2s_kv_baseline", args.baseline_source)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    if os.environ.get("CUDA_LAUNCH_BLOCKING", "0") != "0":
        raise RuntimeError("CUDA_LAUNCH_BLOCKING invalidates this benchmark")
    checkpoint = torch.load(TTS_GPT_MODEL_PATH, map_location="cpu", weights_only=True)
    weights = checkpoint["weight"]
    if not all(key.startswith("model.") for key in weights):
        raise RuntimeError("Expected decoder-only checkpoint")
    weights = {key[len("model."):]: value for key, value in weights.items()}
    result = {
        "purpose": "fixed-work synthetic AR; EOS evaluated but ignored; no usable speech",
        "environment": {"os": platform.platform(), "architecture": platform.machine(),
                        "torch": torch.__version__, "cuda": torch.version.cuda,
                        "flash_attn": getattr(sys.modules.get("flash_attn"), "__version__", None)},
        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_sha256": {path: digest(ROOT / path) for path in (
            "GPT_SoVITS/AR/models/t2s_model.py", "GPT_SoVITS/AR/models/t2s_flash_attn.py",
            "tools/probes/benchmark_t2s_nested_kv.py")},
        "baseline_sha256": digest(args.baseline_source) if args.layout == "original" else None,
        "checkpoint_sha256": digest(TTS_GPT_MODEL_PATH), "model_config": checkpoint["config"]["model"],
        "parameters": {"layout": args.layout, "attention": args.attention, "device": args.device,
                       "repeats": args.repeats, "dtype": "float16", "top_k": 5, "top_p": 1.0,
                       "temperature": 0.6, "repetition_penalty": 1.35, "replay_sync": False},
        "memory": {}, "runs": [], "summary": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    device = torch.device(f"cuda:{args.device}")
    with torch.cuda.device(device), torch.inference_mode():
        model = module.Text2SemanticDecoder(checkpoint["config"])
        model.load_state_dict(weights)
        model = model.half().to(device).eval()
        model.use_static_kv_cache = model.cuda_graph_enabled = True
        if args.buckets:
            model.kv_cache_buckets = args.buckets
        model.cuda_graph_replay_sync = False
        result["parameters"]["buckets"] = list(model.kv_cache_buckets)
        result["parameters"]["skip_precapture"] = args.skip_precapture
        if args.attention == "flash":
            if not flash.apply_flash_attn_patch(model, mode="valid"):
                raise RuntimeError("Requested FA2 unavailable")
            # Refuse the production wrapper's RuntimeError fallback so FA2
            # evidence cannot silently measure SDPA instead.
            for block in model.t2s_transformer_static.blocks:
                def strict_decode(x, k, v, pos, mask, valid=-1, sdpa=True, block=block):
                    attn = block._flash_valid_len(x, k, v, pos)
                    return block._finish_attention(x, attn), k, v
                block.decode_next_token_with_static_cache = strict_decode
        result["environment"]["gpu"] = torch.cuda.get_device_name(device)

        def memory():
            kv_storages = {}
            external_storages = {}
            for inputs in model.bucket_static_inputs.values():
                for field in ("k_cache", "v_cache"):
                    for tensor in inputs[field]:
                        storage = tensor.untyped_storage()
                        kv_storages[storage.data_ptr()] = storage.nbytes()
                for field in ("xy_pos", "pos_idx", "key_valid_mask"):
                    storage = inputs[field].untyped_storage()
                    external_storages[storage.data_ptr()] = storage.nbytes()
            for outputs in model.bucket_static_outputs.values():
                for tensor in outputs.values():
                    storage = tensor.untyped_storage()
                    external_storages[storage.data_ptr()] = storage.nbytes()
            return {"allocated_bytes": torch.cuda.memory_allocated(device),
                    "reserved_bytes": torch.cuda.memory_reserved(device),
                    "peak_allocated_bytes": torch.cuda.max_memory_allocated(device),
                    "peak_reserved_bytes": torch.cuda.max_memory_reserved(device),
                    "unique_kv_storage_bytes": sum(kv_storages.values()),
                    "kv_storage_count": len(kv_storages),
                    "graph_io_storage_bytes": sum(external_storages.values()),
                    "graph_count": len(model.bucket_graphs),
                    "graph_keys": sorted([list(key) for key in model.bucket_graphs])}

        torch.cuda.synchronize(device)
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats(device)
        result["memory"]["before_precapture"] = memory()
        started = time.perf_counter()
        with redirect_stdout(io.StringIO()):
            capture = {} if args.skip_precapture else model.precapture_cuda_graph(list(model.kv_cache_buckets))
        torch.cuda.synchronize(device)
        if not all(capture.values()):
            raise RuntimeError(f"Failed precapture: {capture}")
        result["precapture_seconds"] = time.perf_counter() - started
        result["memory"]["after_precapture"] = memory()
        print(json.dumps({"layout": args.layout, "attention": args.attention,
                          "precapture_seconds": result["precapture_seconds"],
                          "memory": result["memory"]["after_precapture"]}), flush=True)
        save()
        original_eos, original_tqdm = module._should_stop_on_eos, module.tqdm
        module._should_stop_on_eos = lambda *values: original_eos(*values) and False
        module.tqdm = lambda iterable: iterable
        try:
            for name, prompt_len, text_len, steps, batch_size in cases:
                generator = torch.Generator().manual_seed(20261014 + prompt_len + text_len)
                x = torch.randint(1, model.phoneme_vocab_size, (batch_size, text_len), generator=generator).to(device)
                prompt = torch.randint(0, model.EOS, (batch_size, prompt_len), generator=generator).to(device) if prompt_len else None
                lens = torch.full((batch_size,), text_len, device=device)
                bert = torch.zeros(batch_size, 1024, text_len, dtype=torch.float16, device=device)

                def run(seed):
                    torch.cuda.manual_seed(seed)
                    torch.cuda.synchronize(device)
                    start, end = [torch.cuda.Event(enable_timing=True) for _ in range(2)]
                    torch.cuda.reset_peak_memory_stats(device)
                    with redirect_stdout(io.StringIO()):
                        start.record()
                        started = time.perf_counter()
                        tokens, index = model.infer_panel_naive(
                            x, lens, prompt, bert, top_k=5, top_p=1.0, temperature=0.6,
                            repetition_penalty=1.35, early_stop_num=steps - 1)
                        end.record()
                        torch.cuda.synchronize(device)
                        wall_ms = (time.perf_counter() - started) * 1000
                    if tokens.shape[1] != prompt_len + steps - 1:
                        raise RuntimeError("Work size mismatch")
                    token_hash = hashlib.sha256(tokens.cpu().numpy().tobytes()).hexdigest()
                    return {"case": name, "seed": seed, "steps": steps, "prompt_length": prompt_len,
                            "text_length": text_len, "batch_size": batch_size, "index": index,
                            "selected_bucket": model._select_bucket(prompt_len + text_len),
                            "wall_ms": wall_ms, "stream_ms": start.elapsed_time(end),
                            "steps_per_second": steps * 1000 / wall_ms,
                            "graph_replay_steps": model.cuda_graph_stats["graph_replay_steps"],
                            "token_sha256": token_hash, "memory": memory()}

                try:
                    for _ in range(2):
                        run(20261014)
                except RuntimeError as error:
                    if args.layout != "original" or batch_size != 2:
                        raise
                    result["summary"].append({"case": name, "status": "original_batch_one_graph_rejected_batch_two",
                                              "observed_error": str(error)})
                    print(json.dumps(result["summary"][-1]), flush=True)
                    save()
                    continue
                runs = [run(20261014 + repeat) for repeat in range(args.repeats)]
                result["runs"].extend(runs)
                summary = {"case": name, "steps": steps,
                           "median_wall_ms": statistics.median(run["wall_ms"] for run in runs),
                           "median_stream_ms": statistics.median(run["stream_ms"] for run in runs),
                           "median_steps_per_second": statistics.median(run["steps_per_second"] for run in runs),
                           "peak_allocated_bytes": max(run["memory"]["peak_allocated_bytes"] for run in runs),
                           "graph_replay_steps": sorted({run["graph_replay_steps"] for run in runs})}
                result["summary"].append(summary)
                print(json.dumps(summary), flush=True)
                save()
                del x, prompt, lens, bert
            result["memory"]["after_scenarios"] = memory()
            save()
        finally:
            module._should_stop_on_eos, module.tqdm = original_eos, original_tqdm
        del model
        gc.collect()
        torch.cuda.empty_cache()
    print(f"Saved {args.output}", flush=True)


if __name__ == "__main__":
    main()
