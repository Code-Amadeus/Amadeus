"""Compare Qwen engines with production GPT-SoVITS on the same CUDA device.

Runs offline, without a microphone, playback, chat ingress, or model downloads.
The output measures first voiced *generated* chunk, not speaker playback latency.
Use a local PCM sample and keep generated reports in output/diagnostics.
"""
from __future__ import annotations

import argparse
import hashlib
from itertools import product
import json
import math
import os
from pathlib import Path
import random
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asr.qwen_inference import ENGINES
from core.cuda_execution import speech_cuda


def summary(values):
    ordered = sorted(values)
    return {"n": len(ordered), "median": statistics.median(ordered),
            "p95": ordered[math.ceil(len(ordered) * .95) - 1]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--engines", default="raw_official,prefill,greedy")
    parser.add_argument("--schedules", default="serial,overlap")
    parser.add_argument("--prepare-cuda-graphs", action="store_true",
                        help="Capture ASR graphs after TTS warmup and before any overlapping inference")
    parser.add_argument("--graph-capacities", nargs="+", type=int,
                        help="Explicit ASR token capacities to pre-capture (otherwise production default)")
    parser.add_argument("--attention", nargs="+", choices=("current", "sdpa", "flash_attention_2", "audio_fa2_text_sdpa"), default=["current"])
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    engines = args.engines.split(",")
    schedules = args.schedules.split(",")
    if not set(engines) <= {"raw_official", *ENGINES}:
        parser.error("unknown Qwen engine")
    if not set(schedules) <= {"serial", "overlap"}:
        parser.error("unknown schedule")
    if args.prepare_cuda_graphs and len(args.attention) != 1:
        parser.error("Graph preparation requires one attention preset; switching attention invalidates captured graphs")
    if args.graph_capacities and not args.prepare_cuda_graphs:
        parser.error("--graph-capacities requires --prepare-cuda-graphs")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TTS_RUNTIME_WARMUP"] = "0"
    os.environ["TTS_SESSION_WARMUP"] = "0"
    os.environ["ENABLE_CUDA_GRAPH_PRECAPTURE"] = "0"

    import numpy as np
    import soundfile as sf
    import torch
    from scipy.signal import resample_poly
    from asr.backends.qwen3_asr import Qwen3ASRBackend, _calc_max_tokens
    from asr.qwen_inference import qwen_attention_configs, transcribe_qwen
    from tools.asr.benchmark_qwen_replay import apply_attention_override
    from config import settings
    from local_tts_infer import TTSInferencer
    from tts.pipeline import get_sovits_params, _compute_stream_chunk_seconds
    from tts.speech_onset import first_voiced_sample

    if not torch.cuda.is_available():
        raise RuntimeError("This probe requires one CUDA device for both models")
    device = "cuda:0"
    torch.cuda.set_device(device)
    pcm, sr = sf.read(args.audio, dtype="float32", always_2d=True)
    pcm = pcm.mean(axis=1)
    if sr != 16000:
        divisor = math.gcd(sr, 16000)
        pcm = resample_poly(pcm, 16000 // divisor, sr // divisor).astype(np.float32)
    sr = 16000
    pcm = np.ascontiguousarray(pcm)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    report = {"schema": 2, "device": device, "gpu": torch.cuda.get_device_name(device),
              "torch": torch.__version__, "audio_sha256": hashlib.sha256(pcm.tobytes()).hexdigest(),
              "audio_seconds": len(pcm) / sr, "rows": rows,
              "limitations": ["Offline generation, no physical playback or microphone.",
                              "Fixed synthetic Japanese TTS cases and paired seeds.",
                              "Both models share one process and one GPU; an existing application may also use it."]}

    def save():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Graph preparation is explicit and occurs only after TTS warmup below.
    asr = Qwen3ASRBackend(inference_engine="official")
    asr.load("cuda")
    loaded_attention_configs = qwen_attention_configs(asr._model.model)
    report["attention_override"] = args.attention
    report["loaded_asr_attention_configs"] = loaded_attention_configs
    infer = TTSInferencer(device=device, gpt_path=settings.TTS_GPT_MODEL_PATH,
                         sovits_path=settings.TTS_SOVITS_MODEL_PATH)
    report["tts_model_version"] = str(getattr(infer, "version", "unknown"))
    report["tts_cuda_graph"] = infer.cuda_graph_enabled
    cases = [("short", "それなら簡単よ。"),
             ("medium", "まず条件を整理して、同じ方法でもう一度確かめましょう。")]

    def runtime_flags():
        return {"tf32_matmul": torch.backends.cuda.matmul.allow_tf32,
                "tf32_cudnn": torch.backends.cudnn.allow_tf32,
                "matmul_precision": torch.get_float32_matmul_precision(),
                "threads": torch.get_num_threads(),
                "default_dtype": str(torch.get_default_dtype())}

    flags_before = runtime_flags()

    def run_asr(engine, gate=None):
        if gate:
            gate.wait()
        cpu_started = time.process_time()
        started = time.perf_counter()
        metrics = None
        with asr._lock, speech_cuda.work():
            max_tokens = _calc_max_tokens(len(pcm) / sr)
            if engine == "raw_official":
                previous = asr._model.max_new_tokens
                asr._model.max_new_tokens = max_tokens
                try:
                    result = asr._model.transcribe(audio=(pcm, sr), context=settings.ASR_CONTEXT,
                                                   language=asr._language)
                finally:
                    asr._model.max_new_tokens = previous
            else:
                result, metrics = transcribe_qwen(asr._model, pcm, sr, settings.ASR_CONTEXT,
                                           asr._language, max_tokens, engine=engine)
        text = result[0].text if result else ""
        return {"ms": (time.perf_counter() - started) * 1000,
                "process_cpu_ms": (time.process_time() - cpu_started) * 1000,
                "metrics": {k: v for k, v in metrics.items() if k != "token_ids"} if metrics else None,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest()}

    def run_tts(text, gate=None):
        params = get_sovits_params(text, is_first_sentence=True)
        params["enable_cuda_graph"] = infer.cuda_graph_enabled
        params["chunk_size_seconds"] = _compute_stream_chunk_seconds(text, params)
        if gate:
            gate.wait()
        cpu_started = time.process_time()
        started = time.perf_counter()
        first_ms = voiced_ms = None
        arrays = []
        sample_rate = None
        for part in infer.infer_stream(text=text, ref_audio_path=settings.TTS_REF_AUDIO_JA,
                                       prompt_text=settings.TTS_REF_TEXT_JA, **params):
            sample_rate, wave = part[:2]
            # Production streams announce the sample rate with a None payload.
            if wave is None:
                continue
            wave = np.asarray(wave, dtype=np.float32).reshape(-1)
            now_ms = (time.perf_counter() - started) * 1000
            if wave.size and first_ms is None:
                first_ms = now_ms
            if wave.size and voiced_ms is None and first_voiced_sample(wave, sample_rate) is not None:
                voiced_ms = now_ms
            arrays.append(wave.copy())
        full = np.concatenate(arrays) if arrays else np.zeros(0, dtype=np.float32)
        if not full.size or not np.isfinite(full).all() or voiced_ms is None:
            raise RuntimeError("TTS produced no finite voiced output")
        return {"total_ms": (time.perf_counter() - started) * 1000,
                # Process CPU time is attributable to this call only in serial mode.
                "process_cpu_ms": (time.process_time() - cpu_started) * 1000,
                "first_chunk_ms": first_ms, "first_voiced_chunk_ms": voiced_ms,
                "duration_s": len(full) / sample_rate, "samples": len(full),
                "sample_rate": sample_rate, "rms": float(np.sqrt(np.mean(full ** 2))),
                "wave_sha256": hashlib.sha256(full.tobytes()).hexdigest()}

    def seed(index):
        random.seed(7351 + index)
        np.random.seed(7351 + index)
        torch.manual_seed(7351 + index)

    try:
        # Warm every TTS case with its paired seed before any concurrent GPU work.
        for case_index, (_, text) in enumerate(cases):
            seed(case_index)
            run_tts(text)
        if args.prepare_cuda_graphs:
            apply_attention_override(asr, args.attention[0], current_configs=loaded_attention_configs)
            report["asr_graph_preparation"] = asr.prepare_cuda_graphs(
                **({"capacities": args.graph_capacities} if args.graph_capacities else {}), capture_allowed=True)
        combinations = list(product(args.attention, engines))
        for attention, engine in combinations:
            apply_attention_override(asr, attention, current_configs=loaded_attention_configs)
            run_asr(engine)
        torch.cuda.synchronize(device)
        with ThreadPoolExecutor(max_workers=2) as pool:
            for repetition in range(args.repeats):
                for case_index, (case, text) in enumerate(cases):
                    for schedule in schedules:
                        offset = (repetition + case_index) % len(combinations)
                        order = combinations[offset:] + combinations[:offset]
                        for attention, engine in order:
                            apply_attention_override(asr, attention, current_configs=loaded_attention_configs)
                            seed(case_index)
                            torch.cuda.synchronize(device)
                            if schedule == "serial":
                                asr_result = run_asr(engine)
                                tts_result = run_tts(text)
                            else:
                                gate = threading.Event()
                                asr_future = pool.submit(run_asr, engine, gate)
                                tts_future = pool.submit(run_tts, text, gate)
                                gate.set()
                                asr_result = asr_future.result()
                                tts_result = tts_future.result()
                            rows.append({"repeat": repetition, "case": case, "engine": engine,
                                         "attention": attention, "actual_attention_configs": qwen_attention_configs(asr._model.model),
                                         "schedule": schedule, "asr": asr_result, "tts": tts_result})
                            save()
                            print(f"{case} {schedule} {attention} {engine}: ASR {asr_result['ms']:.0f} ms; "
                                  f"TTS voiced {tts_result['first_voiced_chunk_ms']:.0f} ms", flush=True)
        report["flags_before"] = flags_before
        report["flags_after"] = runtime_flags()
        report["runtime_flags_unchanged"] = flags_before == report["flags_after"]
        report["summary"] = []
        for case, schedule, attention, engine in product([c[0] for c in cases], schedules, args.attention, engines):
            group = [r for r in rows if (r["case"], r["schedule"], r["attention"], r["engine"]) == (case, schedule, attention, engine)]
            report["summary"].append({"case": case, "schedule": schedule, "attention": attention, "engine": engine,
                "asr_ms": summary([r["asr"]["ms"] for r in group]),
                "tts_first_voiced_chunk_ms": summary([r["tts"]["first_voiced_chunk_ms"] for r in group]),
                "tts_total_ms": summary([r["tts"]["total_ms"] for r in group]),
                "wave_hashes": sorted({r["tts"]["wave_sha256"] for r in group})})
        report["completed"] = True
    finally:
        asr.close()
        save()


if __name__ == "__main__":
    main()
