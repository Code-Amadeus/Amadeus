"""Render a small set of paired audio samples from a GSV stability sweep."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.probes.probe_gsv_stability import CASES, MODES, _set_seed, _token_metrics


DEFAULT_SELECTIONS = (
    "eeto_weak:dynamic:2013",
    "eeto_strong:dynamic:2013",
    "uun_continuation:static:2001",
    "uun_continuation:dynamic:2001",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render selected paired GSV probe samples.")
    parser.add_argument("--selection", action="append", default=[])
    parser.add_argument("--cuda-visible-devices", default=None)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--max-sec", type=float, default=8.0)
    parser.add_argument("--output-dir", default="output/diagnostics/gsv_stability_audio")
    parser.add_argument("--verbose-model", action="store_true")
    return parser.parse_args()


def _configure(args: argparse.Namespace, selections: list[tuple[str, str, int]]) -> None:
    if args.cuda_visible_devices is not None:
        os.environ["CUDA_VISIBLE_DEVICES"] = str(args.cuda_visible_devices)
    os.environ["TTS_DEVICE"] = str(args.device)
    os.environ["ENABLE_CUDA_GRAPH"] = (
        "1" if any(MODES[mode].enable_cuda_graph for _, mode, _ in selections) else "0"
    )
    os.environ["ENABLE_CUDA_GRAPH_PRECAPTURE"] = "0"
    os.environ["TTS_RUNTIME_WARMUP"] = "0"
    os.environ["TTS_SESSION_WARMUP"] = "0"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PYTHONUTF8"] = "1"


def _parse_selections(values: list[str]) -> list[tuple[str, str, int]]:
    parsed: list[tuple[str, str, int]] = []
    known_cases = {case.name for case in CASES}
    for value in values or list(DEFAULT_SELECTIONS):
        try:
            case, mode, seed_text = value.rsplit(":", 2)
            seed = int(seed_text)
        except ValueError as exc:
            raise ValueError(f"invalid selection {value!r}; expected case:mode:seed") from exc
        if case not in known_cases:
            raise ValueError(f"unknown case in selection: {case}")
        if mode not in MODES:
            raise ValueError(f"unknown mode in selection: {mode}")
        parsed.append((case, mode, seed))
    return parsed


def _audio_metrics(audio: Any, sample_rate: int) -> dict[str, float | int]:
    import numpy as np

    values = np.asarray(audio, dtype=np.float32).reshape(-1)
    return {
        "sample_rate": int(sample_rate),
        "samples": int(values.size),
        "duration_sec": float(values.size / max(1, sample_rate)),
        "peak": float(np.max(np.abs(values))) if values.size else 0.0,
        "rms": float(np.sqrt(np.mean(values * values))) if values.size else 0.0,
        "near_silence_ratio": float(np.mean(np.abs(values) < 1e-3)) if values.size else 1.0,
    }


def main() -> int:
    args = _parse_args()
    selections = _parse_selections(args.selection)
    _configure(args, selections)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    import numpy as np
    import soundfile as sf
    import torch

    from config import settings
    from local_tts_infer import TTSInferencer

    cases = {case.name: case for case in CASES}
    reference_audio = (
        settings.TTS_REF_AUDIO_EN
        if str(settings.TTS_OUTPUT_LANGUAGE).strip() == "英文"
        else settings.TTS_REF_AUDIO_JA
    )
    reference_text = (
        settings.TTS_REF_TEXT_EN
        if str(settings.TTS_OUTPUT_LANGUAGE).strip() == "英文"
        else settings.TTS_REF_TEXT_JA
    )
    reference_path = Path(reference_audio)
    if not reference_path.is_absolute():
        reference_path = ROOT / reference_path

    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[render] loading full v3 stack on {args.device}")
    inferencer = TTSInferencer(
        device=args.device,
        gpt_path=settings.TTS_GPT_MODEL_PATH or None,
        sovits_path=settings.TTS_SOVITS_MODEL_PATH or None,
    )
    decoder = inferencer.t2s_model.model
    decoder_class = type(decoder)
    original_infer_panel = decoder_class.infer_panel
    current_semantic: dict[str, Any] = {}

    def tracked_infer_panel(self: Any, *call_args: Any, **call_kwargs: Any):
        prediction, idx = original_infer_panel(self, *call_args, **call_kwargs)
        count = max(0, int(idx))
        tokens = (
            prediction[0, -count:].detach().cpu().long().tolist()
            if count
            else []
        )
        metrics = _token_metrics(tokens)
        attempts = current_semantic.setdefault("semantic_attempts", [])
        attempts.append(
            {
                **metrics,
                "repetition_penalty": float(call_kwargs.get("repetition_penalty", 1.35)),
                "token_preview": tokens[:16],
                "token_tail": tokens[-16:],
            }
        )
        current_semantic.update(metrics)
        current_semantic["semantic_attempt_count"] = len(attempts)
        current_semantic["token_preview"] = tokens[:16]
        current_semantic["token_tail"] = tokens[-16:]
        return prediction, idx

    decoder_class.infer_panel = tracked_infer_panel
    manifest: list[dict[str, Any]] = []
    try:
        for case_name, mode_name, seed in selections:
            case = cases[case_name]
            mode = MODES[mode_name]
            current_semantic.clear()
            _set_seed(seed, torch)
            started = time.perf_counter()
            model_output = contextlib.nullcontext() if args.verbose_model else contextlib.redirect_stdout(io.StringIO())
            with model_output:
                sample_rate, audio = inferencer.infer(
                    text=case.text,
                    ref_audio_path=str(reference_path),
                    prompt_text=reference_text,
                    text_language=settings.TTS_OUTPUT_LANGUAGE,
                    prompt_language=settings.TTS_OUTPUT_LANGUAGE,
                    how_to_cut="不切",
                    top_k=5,
                    top_p=1.0,
                    temperature=0.6,
                    speed=1.0,
                    sample_steps=16,
                    pause_second=0.05,
                    if_sr=False,
                    enable_cuda_graph=mode.enable_cuda_graph,
                    enable_static_kv=mode.enable_static_kv,
                    max_sec_override=args.max_sec,
                )
            elapsed = time.perf_counter() - started
            audio_values = np.asarray(audio, dtype=np.float32).reshape(-1)
            filename = f"{case_name}__{mode_name}__seed{seed}.wav"
            audio_path = output_dir / filename
            sf.write(audio_path, audio_values, int(sample_rate), subtype="PCM_16")
            row = {
                "case": case_name,
                "family": case.family,
                "text": case.text,
                "mode": mode_name,
                "seed": seed,
                "elapsed_sec": elapsed,
                "audio_path": str(audio_path),
                **current_semantic,
                **_audio_metrics(audio_values, int(sample_rate)),
            }
            manifest.append(row)
            print(
                f"[render] {case_name}/{mode_name}/seed{seed}: "
                f"tokens={row.get('token_count')} duration={row['duration_sec']:.3f}s "
                f"run={row.get('longest_equal_run')} -> {audio_path.name}"
            )
    finally:
        decoder_class.infer_panel = original_infer_panel

    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().astimezone().isoformat(),
                "device": args.device,
                "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
                "max_sec": args.max_sec,
                "samples": manifest,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[render] manifest={manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
