"""Alternate a saved baseline and current nested AR on one GPU to control drift.

Both decoders remain resident, so this measures paired latency and tokens,
not standalone memory. Use benchmark_t2s_nested_kv.py for memory evidence.
"""
from __future__ import annotations

import argparse
import hashlib
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "GPT_SoVITS"))
from tools.probes.benchmark_t2s_nested_kv import CASES, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-root", type=Path, help="Load model paths from an existing installation .env")
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--attention", choices=["flash", "sdpa"], default="flash")
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--cases", nargs="+", choices=[case[0] for case in CASES if case[1] and case[4] == 1],
                        default=["short_448", "unaligned_512", "medium_768"])
    parser.add_argument("--construction-order", choices=["baseline-first", "nested-first"],
                        default="baseline-first")
    parser.add_argument("--teacher-forced", action="store_true",
                        help="Compare logits under identical forced inputs; times are diagnostic only")
    parser.add_argument("--baseline-buckets", type=int, nargs="+",
                        help="Override only baseline routing for a full-bucket control")
    parser.add_argument("--precapture-buckets", type=int, nargs="+",
                        help="Limit prepared graphs without changing initial routing")
    parser.add_argument("--baseline-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.asset_root:
        from dotenv import load_dotenv
        load_dotenv(args.asset_root / ".env", override=False)
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    os.environ["TTS_KV_CACHE_LAYOUT"] = "nested"
    import torch
    from config.settings import TTS_GPT_MODEL_PATH
    import AR.models.t2s_model as nested_module
    from AR.models.t2s_flash_attn import apply_flash_attn_patch
    spec = importlib.util.spec_from_file_location("AR.models.t2s_pair_baseline", args.baseline_source)
    original_module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = original_module
    spec.loader.exec_module(original_module)
    checkpoint = torch.load(TTS_GPT_MODEL_PATH, map_location="cpu", weights_only=True)
    weights = {key[len("model."):]: value for key, value in checkpoint["weight"].items()}
    result = {"purpose": __doc__, "device": args.device, "attention": args.attention,
              "torch": torch.__version__, "cuda": torch.version.cuda,
              "checkpoint_sha256": digest(TTS_GPT_MODEL_PATH),
              "baseline_sha256": digest(args.baseline_source),
              "source_sha256": digest(ROOT / "GPT_SoVITS/AR/models/t2s_model.py"),
              "construction_order": args.construction_order, "teacher_forced": args.teacher_forced,
              "precapture_buckets": args.precapture_buckets, "baseline_buckets": args.baseline_buckets,
              "precapture": {}, "runs": [], "summary": []}
    device = torch.device(f"cuda:{args.device}")
    with torch.cuda.device(device), torch.inference_mode():
        result["gpu"] = torch.cuda.get_device_name(device)
        models = {}
        traces = {}
        modules = [("original", original_module), ("nested", nested_module)]
        if args.construction_order == "nested-first":
            modules.reverse()
        for name, module in modules:
            model = module.Text2SemanticDecoder(checkpoint["config"])
            model.load_state_dict(weights)
            model = model.half().to(device).eval()
            model.use_static_kv_cache = model.cuda_graph_enabled = True
            if name == "original" and args.baseline_buckets:
                model.kv_cache_buckets = args.baseline_buckets
            model.cuda_graph_replay_sync = False
            if args.attention == "flash":
                assert apply_flash_attn_patch(model)
                for block in model.t2s_transformer_static.blocks:
                    def strict_decode(x, k, v, pos, mask, valid=-1, sdpa=True, block=block):
                        return block._finish_attention(x, block._flash_valid_len(x, k, v, pos)), k, v
                    block.decode_next_token_with_static_cache = strict_decode
            torch.cuda.synchronize(device)
            before = torch.cuda.memory_allocated(device)
            started = time.perf_counter()
            with redirect_stdout(io.StringIO()):
                assert all(model.precapture_cuda_graph(
                    [bucket for bucket in (args.precapture_buckets or model.kv_cache_buckets)
                     if bucket in model.kv_cache_buckets]).values())
            torch.cuda.synchronize(device)
            kv_storages = {}
            for inputs in model.bucket_static_inputs.values():
                for field in ("k_cache", "v_cache"):
                    for tensor in inputs[field]:
                        storage = tensor.untyped_storage()
                        kv_storages[storage.data_ptr()] = storage.nbytes()
            result["precapture"][name] = {
                "seconds": time.perf_counter() - started,
                "graph_count": len(model.bucket_graphs),
                "unique_kv_storage_bytes": sum(kv_storages.values()),
                "allocated_delta_bytes": torch.cuda.memory_allocated(device) - before,
                "graph_keys": sorted(list(key) if isinstance(key, tuple) else key
                                     for key in model.bucket_graphs)}
            eos = module._should_stop_on_eos
            module._should_stop_on_eos = lambda *values, eos=eos: eos(*values) and False
            module.tqdm = lambda iterable: iterable
            traces[name] = []
            if args.teacher_forced:
                def forced_sample(logits, history, name=name, **_kwargs):
                    traces[name].append(logits.clone())
                    return torch.full((1, 1), (history.shape[1] + 3) % models[name].EOS,
                                      device=logits.device, dtype=torch.int32), None
                module.sample = forced_sample
            models[name] = model
        for case, prompt_len, text_len, steps, batch in CASES:
            if case not in args.cases:
                continue
            generator = torch.Generator().manual_seed(20261014 + prompt_len + text_len)
            x = torch.randint(1, models['nested'].phoneme_vocab_size, (batch, text_len), generator=generator).to(device)
            prompt = torch.randint(0, models['nested'].EOS, (batch, prompt_len), generator=generator).to(device)
            lens = torch.full((batch,), text_len, device=device)
            bert = torch.zeros(batch, 1024, text_len, dtype=torch.float16, device=device)

            def run(name, seed):
                traces[name].clear()
                torch.cuda.manual_seed(seed)
                torch.cuda.synchronize(device)
                with redirect_stdout(io.StringIO()):
                    started = time.perf_counter()
                    tokens, _ = models[name].infer_panel_naive(
                        x, lens, prompt, bert, top_k=5, top_p=1.0, temperature=0.6,
                        repetition_penalty=1.35, early_stop_num=steps - 1)
                    torch.cuda.synchronize(device)
                    elapsed = (time.perf_counter() - started) * 1000
                model = models[name]
                stats = {key: model.cuda_graph_stats.get(key) for key in (
                    "initial_kv", "start_bucket", "final_bucket", "promotions", "generated_tokens",
                    "graph_replay_steps", "static_steps", "dynamic_steps", "capture_attempts")}
                return elapsed, tokens.cpu(), stats, len(model.bucket_graphs)

            for _ in range(3):
                for name in models:
                    run(name, 20261014)
            pairs = []
            for repeat in range(args.repeats):
                order = ["original", "nested"] if repeat % 2 == 0 else ["nested", "original"]
                values = {name: run(name, 20261014 + repeat) for name in order}
                equal = torch.equal(values["original"][1], values["nested"][1])
                different = torch.nonzero(values["original"][1] != values["nested"][1])
                row = {"case": case, "seed": 20261014 + repeat, "order": order,
                       "original_ms": values['original'][0], "nested_ms": values['nested'][0],
                       "nested_elapsed_change_percent": (values['nested'][0] / values['original'][0] - 1) * 100,
                       "token_sha256": hashlib.sha256(values['original'][1].numpy().tobytes()).hexdigest(),
                       "tokens_equal": equal,
                       "first_different_token": int(different[0, 1]) if len(different) else None,
                       "different_tokens": len(different),
                       "original_stats": values["original"][2], "nested_stats": values["nested"][2],
                       "original_graph_count": values["original"][3],
                       "nested_graph_count": values["nested"][3]}
                if args.teacher_forced:
                    errors = [(before.float() - after.float()).abs().max().item()
                              for before, after in zip(traces["original"], traces["nested"])]
                    boundary = models["nested"]._select_bucket(prompt_len + text_len) - prompt_len - text_len + 1
                    row["logits"] = {
                        "steps": len(errors), "promotion_boundary_step": boundary,
                        "maximum_absolute_error": max(errors),
                        "before_promotion_maximum_error": max(errors[:boundary]),
                        "after_promotion_maximum_error": max(errors[boundary:], default=0.0),
                        "first_nonzero_error_step": next((i for i, value in enumerate(errors) if value), None)}
                pairs.append(row)
            result['runs'].extend(pairs)
            summary = {"case": case, "pairs": len(pairs),
                       "original_median_ms": statistics.median(row['original_ms'] for row in pairs),
                       "nested_median_ms": statistics.median(row['nested_ms'] for row in pairs),
                       "paired_median_elapsed_change_percent": statistics.median(row['nested_elapsed_change_percent'] for row in pairs),
                       "all_tokens_equal": all(row["tokens_equal"] for row in pairs),
                       "different_pairs": sum(not row["tokens_equal"] for row in pairs)}
            result['summary'].append(summary)
            print(json.dumps(summary), flush=True)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')


if __name__ == "__main__":
    main()
