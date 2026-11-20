# -*- coding: utf-8 -*-
"""Quick T2S decoding throughput benchmark.

Measures raw T2S it/s on the current TTS device by patching the tqdm
progress bar to record per-step timings, then reports peak / avg / median.

Run:
    .venv\\Scripts\\python.exe -X utf8 tools\\t2s_speed_bench.py
"""
from __future__ import annotations

import os
import argparse
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("PYTHONUTF8", "1")
os.environ["ENABLE_CUDA_GRAPH"] = "1"
os.environ["ENABLE_CUDA_GRAPH_PRECAPTURE"] = "1"
os.environ["CUDA_GRAPH_PRECAPTURE_BUCKETS"] = "128,256,448,512,768,1024"
os.environ.pop("CUDA_GRAPH_REPLAY_SYNC", None)


def _parse_args():
    parser = argparse.ArgumentParser(description="Quick T2S decoding throughput benchmark.")
    parser.add_argument(
        "--preset",
        choices=("demo", "raw"),
        default=os.environ.get("T2S_BENCH_PRESET", "demo"),
        help="demo uses punctuation slicing like the playback demo; raw keeps the whole text unsliced.",
    )
    parser.add_argument("--repeats", type=int, default=int(os.environ.get("T2S_BENCH_REPEATS", "3")))
    parser.add_argument("--chunk-size-seconds", type=float, default=0.25)
    return parser.parse_args()


ARGS = _parse_args()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import torch

# ---------------------------------------------------------------------------
# Patch tqdm to capture per-step timings before any model import.
# ---------------------------------------------------------------------------
_step_times: list[float] = []
_t_last: list[float] = [0.0]

import tqdm as _tqdm_mod

_OrigTqdm = _tqdm_mod.tqdm


class _TimingTqdm(_OrigTqdm):
    def update(self, n=1):
        now = time.perf_counter()
        if _t_last[0] > 0:
            dt = now - _t_last[0]
            if dt > 0:
                _step_times.append(n / dt)
        _t_last[0] = now
        return super().update(n)


_tqdm_mod.tqdm = _TimingTqdm
try:
    import tqdm.auto as _tqdm_auto

    _tqdm_auto.tqdm = _TimingTqdm
except Exception:
    pass

# ---------------------------------------------------------------------------
# Load model.
# ---------------------------------------------------------------------------
from config.settings import TTS_DEVICE, TTS_GPT_MODEL_PATH, TTS_SOVITS_MODEL_PATH
from local_tts_infer import TTSInferencer

print(f"\n[bench] TTS device : {TTS_DEVICE}")
print(f"[bench] GPT model  : {TTS_GPT_MODEL_PATH}")
print(f"[bench] SoVITS     : {TTS_SOVITS_MODEL_PATH}")
print(
    f"[bench] preset     : {ARGS.preset} | chunk={ARGS.chunk_size_seconds:.2f}s | "
    f"buckets={os.environ.get('CUDA_GRAPH_PRECAPTURE_BUCKETS')}"
)
print("[bench] Loading models ...")
t0 = time.perf_counter()
inferencer = TTSInferencer(
    sovits_path=TTS_SOVITS_MODEL_PATH,
    gpt_path=TTS_GPT_MODEL_PATH,
)
print(f"[bench] Load done in {time.perf_counter() - t0:.2f}s\n")

REF_AUDIO = os.path.join(ROOT, "reference audio", "kurisu_reference.wav")
REF_TEXT = (
    "そういえば,正式に自己紹介していませんでしたね……牧瀬紅莉栖です."
    "改めてまして,よろしく。"
)
PARAMS = dict(
    ref_audio_path=REF_AUDIO,
    prompt_text=REF_TEXT,
    text_language="日文",
    prompt_language="日文",
    how_to_cut="按标点符号切" if ARGS.preset == "demo" else "不切",
    top_k=5,
    top_p=1,
    temperature=0.6,
    sample_steps=4,
    speed=1.1,
    pause_second=0.1,
    if_freeze=False,
    if_sr=False,
    enable_cuda_graph=True,
    enable_static_kv=True,
    max_sec_override=3.5,
    chunk_size_seconds=ARGS.chunk_size_seconds if ARGS.preset == "demo" else None,
    collect_t2s_stats=True,
)

WARMUP_TEXTS = [
    "これはテストです。",
    "何の実験だか気になるけど、まあいいわ。",
]
BENCH_TEXTS = [
    ("short", "実験？"),
    ("medium", "何の実験だか気になるけど、まあいいわ。"),
    (
        "long",
        "Paxosが提案ベースでメッセージフローが複雑なのに対して、"
        "Raftは明確なリーダー選出とログ複製のフェーズに分けられているわ。",
    ),
]
REPEATS = max(1, ARGS.repeats)

print("[bench] Warmup ...")
for wt in WARMUP_TEXTS:
    for _ in inferencer.infer_stream(text=wt, **PARAMS):
        pass
print("[bench] Warmup done\n")

def _summarize_t2s(stats: list[dict]) -> tuple[int, float, float]:
    tokens = sum(int(stat.get("tokens", 0)) for stat in stats)
    elapsed = sum(float(stat.get("elapsed_sec", 0.0)) for stat in stats)
    rate = tokens / elapsed if elapsed > 0 else 0.0
    return tokens, elapsed, rate


# ---------------------------------------------------------------------------
# Benchmark.
# ---------------------------------------------------------------------------
print(f"{'case':<8} {'chars':>5}  {'t2s_live':>10}  {'tokens':>7}  {'elapsed':>9}  text")
print("-" * 80)

for label, text in BENCH_TEXTS:
    for rep in range(REPEATS):
        _step_times.clear()
        _t_last[0] = 0.0
        t2s_start = len(getattr(inferencer, "t2s_stats", []))

        for _ in inferencer.infer_stream(text=text, **PARAMS):
            pass

        stats = getattr(inferencer, "t2s_stats", [])[t2s_start:]
        tokens, elapsed, rate = _summarize_t2s(stats)
        if not stats:
            continue

        print(
            f"{label:<8} {len(text.strip()):>5}  "
            f"{rate:>7.0f} it/s  {tokens:>7}  {elapsed:>7.3f}s  {text[:30]}"
            f"  [rep {rep + 1}]"
        )

print("-" * 80)
if torch.cuda.is_available() and str(TTS_DEVICE).startswith("cuda"):
    device_index = int(str(TTS_DEVICE).split(":")[-1])
    gpu_name = torch.cuda.get_device_name(device_index)
else:
    gpu_name = "N/A"
print(f"\n[bench] Device: {TTS_DEVICE}  |  GPU: {gpu_name}")
