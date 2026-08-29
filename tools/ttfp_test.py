"""Benchmark first-sentence TTS latency across early-cut and BigVGAN kernel modes.

This script focuses on the current non-chunked first-sentence path:
- `chunk_size_seconds=None`
- CUDA Graph optionally enabled through `ENABLE_CUDA_GRAPH`
- BigVGAN CUDA kernel forced on/off through `BIGVGAN_USE_CUDA_KERNEL`

It compares two first-sentence shapes:
- `full`: full first sentence as-is
- `early_cut`: a proxy for main.py's early-cut behavior, truncating the first
  sentence to `FIRST_SENTENCE_EARLY_CUT_CHARS` visible chars and appending a
  sentence terminator when needed

Run:
    python tools/ttfp_test.py
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Iterable


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = sys.executable
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("PYTHONUTF8", "1")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REF_AUDIO = os.path.join(ROOT, "reference audio", "kurisu_reference.wav")
REF_TEXT = (
    "そういえば,正式に自己紹介していませんでしたね……"
    "牧瀬紅莉栖です.改めてまして,よろしく。"
)
TEXT_LANGUAGE = "日文"
PROMPT_LANGUAGE = "日文"

ENABLE_CUDA_GRAPH = os.environ.get("ENABLE_CUDA_GRAPH", "0") == "1"
EARLY_CUT_CHARS = int(os.environ.get("FIRST_SENTENCE_EARLY_CUT_CHARS", "11") or "11")
REPEATS = int(os.environ.get("TTFP_TEST_REPEATS", "3") or "3")
KERNEL_MODES = ("off", "on")

SENTENCE_ENDINGS = "。！？?!."


@dataclass(frozen=True)
class Case:
    label: str
    variant: str
    text: str


BASE_CASES = [
    ("short", "実験？"),
    ("medium", "何の実験だか気になるけど、まあいいわ。"),
    (
        "long",
        "Paxosが提案ベースでメッセージフローが複雑なのに対して、"
        "Raftは明確なリーダー選出とログ複製のフェーズに分けられているわ。",
    ),
]


def _build_early_cut_text(text: str, cut_chars: int) -> str:
    stripped = (text or "").strip()
    if not stripped:
        return stripped
    if len(stripped) <= cut_chars:
        return stripped

    prefix = stripped[:cut_chars].rstrip()
    if prefix and prefix[-1] not in SENTENCE_ENDINGS:
        prefix += "。"
    return prefix


def _build_cases(cut_chars: int) -> list[Case]:
    cases: list[Case] = []
    for label, text in BASE_CASES:
        cases.append(Case(label=label, variant="full", text=text))
        early = _build_early_cut_text(text, cut_chars)
        if early != text:
            cases.append(Case(label=label, variant="early_cut", text=early))
    return cases


def _format_chunk_size_arg(chunk_size_seconds: float | None) -> str:
    return "none" if chunk_size_seconds is None else str(float(chunk_size_seconds))


def _parse_chunk_size_arg(value: str | None) -> float | None:
    if value is None:
        return None
    raw = str(value).strip().lower()
    if raw in ("", "none", "null", "off", "false", "0"):
        return None
    return float(raw)


def _run_worker(
    kernel_mode: str,
    cut_chars: int,
    repeats: int,
    chunk_size_seconds: float | None,
) -> dict:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["BIGVGAN_USE_CUDA_KERNEL"] = "1" if kernel_mode == "on" else "0"
    env["TTS_STREAM_SYNC_TIMING"] = "0"

    proc = subprocess.run(
        [
            PYTHON,
            "-X",
            "utf8",
            os.path.join(ROOT, "tools", "ttfp_test.py"),
            "--worker",
            "--kernel",
            kernel_mode,
            "--early-cut-chars",
            str(cut_chars),
            "--repeats",
            str(repeats),
            "--chunk-size-seconds",
            _format_chunk_size_arg(chunk_size_seconds),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=3600,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"worker failed for kernel={kernel_mode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )

    result_line = None
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT_JSON="):
            result_line = line[len("RESULT_JSON="):]
    if result_line is None:
        raise RuntimeError(f"missing RESULT_JSON for kernel={kernel_mode}\n{proc.stdout}")

    print(proc.stdout, end="")
    return json.loads(result_line)


def _median_ms(values: Iterable[float]) -> float:
    seq = list(values)
    return statistics.median(seq) if seq else 0.0


def _print_controller_summary(results: list[dict]) -> None:
    print("=" * 84)
    print(
        f"{'kernel':<8} {'chunk':<7} {'case':<8} {'variant':<10} {'chars':>5} "
        f"{'median_ttfp':>12} {'median_total':>13} {'samples':>10} {'text'}"
    )
    print("-" * 84)
    for group in results:
        kernel_mode = group["kernel_mode"]
        chunk_label = _format_chunk_size_arg(group.get("chunk_size_seconds"))
        for item in group["results"]:
            print(
                f"{kernel_mode:<8} {chunk_label:<7} {item['label']:<8} {item['variant']:<10} "
                f"{item['chars']:>5} {item['median_ttfp_ms']:>10.1f}ms "
                f"{item['median_total_ms']:>11.1f}ms {item['samples']:>10} {item['text']}"
            )
    print("=" * 84)
    print("Notes:")
    print("- early_cut is a proxy for main.py's first-sentence early cut, not a full LLM benchmark.")
    print("- TTFP here means infer_stream() -> first non-empty audio chunk in the current non-chunked path.")
    print("- Use this to compare TTS-stage benefit of earlier first-sentence handoff and kernel mode.")


def _run_single_measurement(inferencer, text: str, params: dict) -> tuple[float, float, int]:
    t0 = time.perf_counter()
    first_chunk_ms = None
    total_samples = 0

    for sr, chunk, _text_item in inferencer.infer_stream(text=text, **params):
        if chunk is None or len(chunk) == 0:
            continue
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if first_chunk_ms is None:
            first_chunk_ms = elapsed_ms
        total_samples += len(chunk)

    total_ms = (time.perf_counter() - t0) * 1000.0
    return float(first_chunk_ms or total_ms), float(total_ms), int(total_samples)


def _run_worker_mode(
    kernel_mode: str,
    cut_chars: int,
    repeats: int,
    chunk_size_seconds: float | None,
) -> None:
    os.environ["BIGVGAN_USE_CUDA_KERNEL"] = "1" if kernel_mode == "on" else "0"
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")

    from config.settings import TTS_GPT_MODEL_PATH, TTS_SOVITS_MODEL_PATH
    from local_tts_infer import TTSInferencer

    print(
        f"[worker] loading models | kernel={kernel_mode} | "
        f"cuda_graph={'ON' if ENABLE_CUDA_GRAPH else 'OFF'} | "
        f"early_cut_chars={cut_chars} | chunk_size_seconds={_format_chunk_size_arg(chunk_size_seconds)}"
    )
    t_load = time.perf_counter()
    inferencer = TTSInferencer(
        sovits_path=TTS_SOVITS_MODEL_PATH,
        gpt_path=TTS_GPT_MODEL_PATH,
    )
    inferencer._stream_sync_timing_enabled = False
    print(f"[worker] model load complete: {time.perf_counter() - t_load:.2f}s")

    params = dict(
        ref_audio_path=REF_AUDIO,
        prompt_text=REF_TEXT,
        text_language=TEXT_LANGUAGE,
        prompt_language=PROMPT_LANGUAGE,
        how_to_cut="不切",
        top_k=5,
        top_p=1,
        temperature=0.6,
        sample_steps=4,
        speed=1.1,
        pause_second=0.1,
        if_freeze=False,
        if_sr=False,
        enable_cuda_graph=ENABLE_CUDA_GRAPH,
        enable_static_kv=True,
        max_sec_override=3.5,
        chunk_size_seconds=chunk_size_seconds,
    )

    warmups = [
        "これはテストです。",
        "何の実験だか気になるけど、まあいいわ。",
    ]
    print("[worker] warmup start")
    for warmup_text in warmups:
        for _ in inferencer.infer_stream(text=warmup_text, **params):
            pass
    print("[worker] warmup done")

    cases = _build_cases(cut_chars)
    rows = []
    print("-" * 84)
    print(
        f"{'kernel':<8} {'chunk':<7} {'case':<8} {'variant':<10} {'chars':>5} "
        f"{'median_ttfp':>12} {'median_total':>13} {'samples':>10} {'text'}"
    )
    print("-" * 84)
    for case in cases:
        ttfp_values = []
        total_values = []
        sample_values = []
        for _ in range(repeats):
            ttfp_ms, total_ms, samples = _run_single_measurement(inferencer, case.text, params)
            ttfp_values.append(ttfp_ms)
            total_values.append(total_ms)
            sample_values.append(samples)

        row = {
            "label": case.label,
            "variant": case.variant,
            "text": case.text,
            "chars": len(case.text.strip()),
            "median_ttfp_ms": _median_ms(ttfp_values),
            "median_total_ms": _median_ms(total_values),
            "samples": int(_median_ms(sample_values)),
            "all_ttfp_ms": ttfp_values,
            "all_total_ms": total_values,
        }
        rows.append(row)
        print(
            f"{kernel_mode:<8} {_format_chunk_size_arg(chunk_size_seconds):<7} "
            f"{case.label:<8} {case.variant:<10} "
            f"{row['chars']:>5} {row['median_ttfp_ms']:>10.1f}ms "
            f"{row['median_total_ms']:>11.1f}ms {row['samples']:>10} {case.text}"
        )

    print("-" * 84)
    print(
        "RESULT_JSON="
        + json.dumps(
            {
                "kernel_mode": kernel_mode,
                "cuda_graph": ENABLE_CUDA_GRAPH,
                "early_cut_chars": cut_chars,
                "chunk_size_seconds": chunk_size_seconds,
                "repeats": repeats,
                "results": rows,
            },
            ensure_ascii=False,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--kernel", choices=["off", "on", "both"], default="both")
    parser.add_argument("--early-cut-chars", type=int, default=EARLY_CUT_CHARS)
    parser.add_argument("--repeats", type=int, default=REPEATS)
    parser.add_argument(
        "--chunk-size-seconds",
        default=os.environ.get("TTS_CHUNK_SIZE_SECONDS"),
        help="Streaming chunk size in seconds; use none/off/0 for the legacy non-chunked path.",
    )
    args = parser.parse_args()
    chunk_size_seconds = _parse_chunk_size_arg(args.chunk_size_seconds)

    if args.worker:
        _run_worker_mode(
            args.kernel if args.kernel != "both" else "off",
            args.early_cut_chars,
            args.repeats,
            chunk_size_seconds,
        )
        return

    kernel_modes = KERNEL_MODES if args.kernel == "both" else (args.kernel,)
    all_results = []
    for kernel_mode in kernel_modes:
        all_results.append(
            _run_worker(kernel_mode, args.early_cut_chars, args.repeats, chunk_size_seconds)
        )
    _print_controller_summary(all_results)


if __name__ == "__main__":
    main()
