"""Offline, local-only same-PCM Qwen replay. Never records or plays audio."""
from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
import math
from pathlib import Path
import platform
import random
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
from asr.qwen_inference import AUDIO_FA2_TEXT_SDPA_CONFIGS, ENGINES, qwen_attention_configs, set_qwen_attention_configs
from core.cuda_execution import speech_cuda

_ATTENTION_PRESETS = {
    "audio_fa2_text_sdpa": AUDIO_FA2_TEXT_SDPA_CONFIGS,
}


def edit_distance(reference, hypothesis):
    row = list(range(len(hypothesis) + 1))
    for i, left in enumerate(reference, 1):
        next_row = [i]
        for j, right in enumerate(hypothesis, 1):
            next_row.append(min(next_row[-1] + 1, row[j] + 1, row[j - 1] + (left != right)))
        row = next_row
    return row[-1]


def quality(reference: str, hypothesis: str):
    # Exact Unicode chars and whitespace-separated words. No hidden punctuation,
    # numeral, case, or CJK token normalization; always report the denominator.
    chars, words = list(reference), reference.split()
    return {"cer": edit_distance(chars, list(hypothesis)) / len(chars) if chars else None,
            "wer": edit_distance(words, hypothesis.split()) / len(words) if words else None,
            "reference_characters": len(chars), "reference_words": len(words)}


def summarize(records):
    groups = {}
    for record in records:
        key = (record["group"], record.get("attention", "current"), record["engine"], record["sample_id"])
        groups.setdefault(key, []).append(record["metrics"]["total_wall_ms"])
    return [{"group": group, "attention": attention, "engine": engine, "sample_id": sample,
             "n": len(values), "median_ms": statistics.median(values),
             "p95_ms": sorted(values)[math.ceil(0.95 * len(values)) - 1],
             "min_ms": min(values), "max_ms": max(values)}
            for (group, attention, engine, sample), values in groups.items()]


def apply_attention_override(backend, implementation, *, current_configs=None):
    """An offline experiment, separate from engine ablation and production config."""
    if implementation == "current" and current_configs is None:
        return
    with backend._lock:
        model = backend._model.model
        targets = (current_configs if implementation == "current" else
                   _ATTENTION_PRESETS.get(implementation, dict.fromkeys(qwen_attention_configs(model), implementation)))
        set_qwen_attention_configs(model, targets)


def load_samples(manifest_path: Path, language: str, context: str, max_tokens: int | None):
    import soundfile as sf
    from asr.backends.qwen3_asr import _calc_max_tokens, _qwen_language
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    samples = []
    for item in manifest["samples"]:
        path = Path(item["path"])
        if not path.is_absolute():
            path = manifest_path.parent / path
        audio, sr = sf.read(path, dtype="float32", always_2d=False)
        if sr <= 0 or not len(audio):
            raise ValueError(f"Empty/invalid audio for sample {item['id']}")
        # Keep original sample rate/channel PCM: the official normalizer owns
        # resampling and mono conversion identically for every engine.
        samples.append({"id": item["id"], "audio": audio, "sample_rate": sr,
                        "pcm_sha256": hashlib.sha256(audio.tobytes()).hexdigest(),
                        "audio_ms": len(audio) / sr * 1000,
                        "language": _qwen_language(item.get("language", language)),
                        "context": item.get("context", context),
                        "max_tokens": int(item.get("max_tokens", max_tokens or _calc_max_tokens(len(audio) / sr))),
                        "reference": item.get("reference")})
    if not samples or len({s["id"] for s in samples}) != len(samples):
        raise ValueError("Manifest needs at least one sample with unique IDs")
    if any(s["max_tokens"] < 1 for s in samples):
        raise ValueError("max_tokens must be positive")
    return samples


def replay(backend, sample, engine, profile=False):
    """Shared model, lifecycle lock, PCM/config; raw baseline remains untouched."""
    from asr.backend import asr_call_context
    from asr.qwen_inference import transcribe_qwen
    started = time.perf_counter()
    with backend._lock, speech_cuda.work(), asr_call_context("benchmark"):
        lock_wait_ms = (time.perf_counter() - started) * 1000
        parked = backend.parked
        restore_started = time.perf_counter()
        backend._restore_device_locked()
        restore_ms = (time.perf_counter() - restore_started) * 1000 if parked else 0.0
        wrapper = backend._model
        if engine == "raw_official":
            previous = wrapper.max_new_tokens
            wrapper.max_new_tokens = sample["max_tokens"]
            try:
                results = wrapper.transcribe(audio=(sample["audio"], sample["sample_rate"]),
                                             context=sample["context"], language=sample["language"])
            finally:
                wrapper.max_new_tokens = previous
            metrics = {"engine": engine, "generated_tokens": None, "stop_reasons": None,
                       "token_ids": None, "wall_ms": {}, "gpu_ms": {}, "profile": False}
        else:
            results, metrics = transcribe_qwen(
                wrapper, sample["audio"], sample["sample_rate"], sample["context"],
                sample["language"], sample["max_tokens"], engine=engine, profile=profile)
        metrics.update(total_wall_ms=(time.perf_counter() - started) * 1000,
                       lock_wait_ms=lock_wait_ms, restore_exposed_ms=restore_ms)
        text = (results[0].text if results else "").strip()
    return text, metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--engines", nargs="+", choices=("raw_official", *ENGINES),
                        default=["raw_official", "official", "prefill", "greedy"])
    parser.add_argument("--device", choices=("cpu", "cuda", "cuda:0"), default="cuda:0")
    parser.add_argument("--attention", nargs="+", choices=("current", "sdpa", "flash_attention_2", *_ATTENTION_PRESETS), default=["current"],
                        help="Offline recursive attention override; keep separate from engine comparisons")
    parser.add_argument("--language", default="auto")
    parser.add_argument("--context", default=None, help="Defaults to the existing ASR_CONTEXT")
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--restore-repeats", type=int, default=0)
    parser.add_argument("--cold-repeats", type=int, default=0, help="Reload separately for each cold call")
    parser.add_argument("--profile", action="store_true", help="CUDA events; report separately from normal timing")
    parser.add_argument("--prepare-cuda-graphs", action="store_true",
                        help="Capture before replay in this offline process's exclusive CUDA window")
    parser.add_argument("--graph-capacities", nargs="+", type=int,
                        help="Explicit token capacities to pre-capture (otherwise production default)")
    parser.add_argument("--include-text", action="store_true", help="Store transcript/token differences in local JSON")
    parser.add_argument("--seed", type=int, default=20261014)
    args = parser.parse_args()
    if args.repeats < 1 or min(args.warmup, args.restore_repeats, args.cold_repeats) < 0:
        parser.error("repeats must be positive and other counts nonnegative")
    if args.prepare_cuda_graphs and (len(args.attention) != 1 or args.cold_repeats):
        parser.error("Graph preparation requires one attention preset and no cold reloads; measure lifecycle separately")
    if args.graph_capacities and not args.prepare_cuda_graphs:
        parser.error("--graph-capacities requires --prepare-cuda-graphs")
    import torch
    from asr.backends.qwen3_asr import Qwen3ASRBackend
    from config.settings import ASR_CONTEXT
    from asr.qwen_model import resolve_qwen_model_source
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    context = ASR_CONTEXT if args.context is None else args.context
    samples = load_samples(args.manifest.resolve(), args.language, context, args.max_tokens)
    # Baseline loading must not inherit the production graph preparation policy.
    backend = Qwen3ASRBackend(inference_engine="official")
    records = []
    load_started = time.perf_counter()
    backend._load_inprocess(args.device)
    loaded_attention_configs = qwen_attention_configs(backend._model.model)
    load_ms = (time.perf_counter() - load_started) * 1000
    graph_preparation = None
    if args.prepare_cuda_graphs:
        apply_attention_override(backend, args.attention[0], current_configs=loaded_attention_configs)
        graph_preparation = backend.prepare_cuda_graphs(
            **({"capacities": args.graph_capacities} if args.graph_capacities else {}), capture_allowed=True)
    device = torch.device(backend._device)
    runtime = {"python": platform.python_version(), "os": platform.platform(), "architecture": platform.machine(),
               "packages": {name: metadata.version(name) for name in ("torch", "transformers", "qwen-asr")},
               "torch_cuda": torch.version.cuda, "torch_hip": torch.version.hip,
               "device": str(device), "device_name": torch.cuda.get_device_name(device) if device.type == "cuda" else "CPU",
               "attention": backend._attn_impl, "dtype": str(backend._model.model.dtype),
               "loader_attention_requested": backend._attn_impl, "attention_override": args.attention,
               "attention_configs": loaded_attention_configs,
               "model_source": str(resolve_qwen_model_source()),
               "thinker_generation_config": backend._model.model.thinker.generation_config.to_dict(),
               "initial_load_ms": load_ms, "graph_preparation": graph_preparation}

    def run_group(group, repeats):
        combinations = [(attention, engine) for attention in args.attention for engine in args.engines]
        for repeat in range(repeats):
            for sample_index, sample in enumerate(samples):
                offset = (repeat + sample_index) % len(combinations)
                ordered = combinations[offset:] + combinations[:offset]
                for attention, engine in ordered:
                    cold_load_ms = None
                    if group == "cold":
                        backend.close()
                        begin = time.perf_counter()
                        backend._load_inprocess(args.device)
                        cold_load_ms = (time.perf_counter() - begin) * 1000
                    apply_attention_override(backend, attention, current_configs=loaded_attention_configs)
                    if group == "restore" and not backend.release_vram():
                        raise RuntimeError("Restore measurements require an in-process CUDA model")
                    text, metrics = replay(backend, sample, engine, args.profile)
                    records.append({"group": group, "repeat": repeat, "sample_id": sample["id"],
                                    "attention": attention, "actual_attention_configs": qwen_attention_configs(backend._model.model),
                                    "engine": engine, "text": text, "metrics": metrics,
                                    "cold_load_ms": cold_load_ms,
                                    "accuracy": quality(sample["reference"], text) if sample["reference"] is not None else None})
    try:
        run_group("warmup", args.warmup)
        run_group("warm", args.repeats)
        run_group("restore", args.restore_repeats)
        run_group("cold", args.cold_repeats)
        comparisons = []
        baseline_engine = "raw_official" if "raw_official" in args.engines else "official"
        baseline = {(r["group"], r["attention"], r["repeat"], r["sample_id"]): r for r in records if r["engine"] == baseline_engine}
        # Token comparison uses instrumented official (raw API does not expose IDs).
        official = {(r["group"], r["attention"], r["repeat"], r["sample_id"]): r for r in records if r["engine"] == "official"}
        for record in records:
            key = (record["group"], record["attention"], record["repeat"], record["sample_id"])
            base = baseline.get(key)
            if base is None or record["engine"] == baseline_engine:
                continue
            ids_base = official.get(key, {}).get("metrics", {}).get("token_ids")
            ids = record["metrics"].get("token_ids")
            comparisons.append({"group": key[0], "attention": key[1], "repeat": key[2], "sample_id": key[3], "engine": record["engine"],
                                "baseline": baseline_engine, "text_equal": base["text"] == record["text"],
                                "text_char_edits": edit_distance(base["text"], record["text"]),
                                "tokens_equal_to_official": ids_base == ids if ids_base is not None and ids is not None else None,
                                "wall_speedup": base["metrics"]["total_wall_ms"] / record["metrics"]["total_wall_ms"]})
        sample_metadata = [{k: v for k, v in sample.items() if k not in {"audio", "context", "reference"}}
                           for sample in samples]
        for meta, sample in zip(sample_metadata, samples):
            meta.update(context_characters=len(sample["context"]),
                        context_sha256=hashlib.sha256(sample["context"].encode("utf-8")).hexdigest(),
                        has_reference=sample["reference"] is not None)
        if not args.include_text:
            for record in records:
                record.pop("text")
                record["metrics"].pop("token_ids", None)
        payload = {"schema_version": 2, "runtime": runtime, "seed": args.seed,
                   "repeats": args.repeats, "warmup": args.warmup, "profile": args.profile,
                   "quality_claim": "reference accuracy only where supplied; otherwise transcription consistency",
                   "timing_note": "wall_ms is host elapsed time; gpu_ms is explicit CUDA event elapsed time, not kernel busy time",
                   "samples": sample_metadata, "summary": summarize(records), "comparisons": comparisons, "records": records}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"output": str(args.output), "calls": len(records),
                          "text_differences": sum(not c["text_equal"] for c in comparisons)}, ensure_ascii=True))
    finally:
        backend.close()


if __name__ == "__main__":
    main()
