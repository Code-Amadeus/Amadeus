"""Alternate untouched original and nested AR on one GPU to control drift.

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
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--attention", choices=["flash", "sdpa"], default="flash")
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--cases", nargs="+", choices=[case[0] for case in CASES if case[1] and case[4] == 1],
                        default=["short_448", "unaligned_512", "medium_768"])
    parser.add_argument("--baseline-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
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
              "runs": [], "summary": []}
    device = torch.device(f"cuda:{args.device}")
    with torch.cuda.device(device), torch.inference_mode():
        result["gpu"] = torch.cuda.get_device_name(device)
        models = {}
        for name, module in [("original", original_module), ("nested", nested_module)]:
            model = module.Text2SemanticDecoder(checkpoint["config"])
            model.load_state_dict(weights)
            model = model.half().to(device).eval()
            model.use_static_kv_cache = model.cuda_graph_enabled = True
            model.cuda_graph_replay_sync = False
            if args.attention == "flash":
                assert apply_flash_attn_patch(model)
                for block in model.t2s_transformer_static.blocks:
                    def strict_decode(x, k, v, pos, mask, valid=-1, sdpa=True, block=block):
                        return block._finish_attention(x, block._flash_valid_len(x, k, v, pos)), k, v
                    block.decode_next_token_with_static_cache = strict_decode
            with redirect_stdout(io.StringIO()):
                assert all(model.precapture_cuda_graph(list(model.kv_cache_buckets)).values())
            eos = module._should_stop_on_eos
            module._should_stop_on_eos = lambda *values, eos=eos: eos(*values) and False
            module.tqdm = lambda iterable: iterable
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
                torch.cuda.manual_seed(seed)
                torch.cuda.synchronize(device)
                with redirect_stdout(io.StringIO()):
                    started = time.perf_counter()
                    tokens, _ = models[name].infer_panel_naive(
                        x, lens, prompt, bert, top_k=5, top_p=1.0, temperature=0.6,
                        repetition_penalty=1.35, early_stop_num=steps - 1)
                    torch.cuda.synchronize(device)
                    elapsed = (time.perf_counter() - started) * 1000
                return elapsed, tokens.cpu()

            for _ in range(3):
                for name in models:
                    run(name, 20261014)
            pairs = []
            for repeat in range(args.repeats):
                order = ["original", "nested"] if repeat % 2 == 0 else ["nested", "original"]
                values = {name: run(name, 20261014 + repeat) for name in order}
                torch.testing.assert_close(values['original'][1], values['nested'][1], rtol=0, atol=0)
                row = {"case": case, "seed": 20261014 + repeat, "order": order,
                       "original_ms": values['original'][0], "nested_ms": values['nested'][0],
                       "nested_elapsed_change_percent": (values['nested'][0] / values['original'][0] - 1) * 100,
                       "token_sha256": hashlib.sha256(values['original'][1].numpy().tobytes()).hexdigest(),
                       "tokens_equal": True}
                pairs.append(row)
            result['runs'].extend(pairs)
            summary = {"case": case, "pairs": len(pairs),
                       "original_median_ms": statistics.median(row['original_ms'] for row in pairs),
                       "nested_median_ms": statistics.median(row['nested_ms'] for row in pairs),
                       "paired_median_elapsed_change_percent": statistics.median(row['nested_elapsed_change_percent'] for row in pairs),
                       "all_tokens_equal": True}
            result['summary'].append(summary)
            print(json.dumps(summary), flush=True)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')


if __name__ == "__main__":
    main()
