"""Paired-seed stability probe for Amadeus's GPT-SoVITS T2S decoder.

The probe intentionally stops after semantic-token generation.  That makes it
cheap enough to sweep intermittent failures while still observing the layer
that owns EOS, repetition, and the static/CUDA-Graph KV-cache variants.

Example:

    .venv\\Scripts\\python.exe -X utf8 tools\\probes\\probe_gsv_stability.py \
        --cuda-visible-devices 0 --device cuda:0 --trials 20

Results are written below ``output/diagnostics`` by default.  The probe does
not play audio or mutate runtime/model state outside its own process.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import os
import random
import statistics
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ProbeMode:
    name: str
    enable_cuda_graph: bool
    enable_static_kv: bool


MODES = {
    "graph_static": ProbeMode("graph_static", True, True),
    "static": ProbeMode("static", False, True),
    "dynamic": ProbeMode("dynamic", False, False),
}


@dataclass(frozen=True)
class ProbeCase:
    name: str
    text: str
    family: str


CASES = (
    ProbeCase("aa_weak", "ああ、", "standalone_weak"),
    ProbeCase("eeto_weak", "ええと、", "standalone_weak"),
    ProbeCase("uun_weak", "うーん、", "standalone_weak"),
    ProbeCase("uun_ellipsis", "うーん……", "standalone_ellipsis"),
    ProbeCase("aa_strong", "ああ。", "standalone_strong"),
    ProbeCase("eeto_strong", "ええと。", "standalone_strong"),
    ProbeCase("uun_strong", "うーん。", "standalone_strong"),
    ProbeCase("aa_continuation", "ああ、それなら簡単よ。", "continuation"),
    ProbeCase("eeto_continuation", "ええと、まず条件を整理しましょう。", "continuation"),
    ProbeCase(
        "uun_continuation",
        "うーん……正直、まだ完全には分からないわ。",
        "continuation",
    ),
    ProbeCase("control_plain", "それなら簡単よ。", "control"),
)

QUICK_CASE_NAMES = {
    "aa_weak",
    "uun_ellipsis",
    "uun_continuation",
    "control_plain",
}


@dataclass
class SemanticContext:
    case: ProbeCase
    synthesis_text: str
    normalized_text: str
    target_phone_count: int
    all_phoneme_ids: Any
    all_phoneme_len: Any
    prompt_semantic: Any
    bert: Any


def _parse_csv(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run paired-seed GPT-SoVITS semantic stability comparisons."
    )
    parser.add_argument("--trials", type=int, default=20, help="Seeds per case and mode.")
    parser.add_argument("--seed-start", type=int, default=1000)
    parser.add_argument(
        "--modes",
        default="graph_static,static,dynamic",
        help="Comma-separated subset of graph_static, static, dynamic.",
    )
    parser.add_argument(
        "--cases",
        default="all",
        help="all, quick, or comma-separated case names.",
    )
    parser.add_argument(
        "--max-sec",
        type=float,
        default=12.0,
        help="Diagnostic hard stop. A short-case hit is already an anomaly.",
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--top-p", type=float, default=1.0)
    parser.add_argument("--temperature", type=float, default=0.6)
    parser.add_argument("--repetition-penalty", type=float, default=1.35)
    parser.add_argument(
        "--semantic-retries",
        type=int,
        default=0,
        help="Retry rejected semantic candidates this many times (0 preserves raw decoder A/B).",
    )
    parser.add_argument("--retry-repetition-penalty", type=float, default=1.5)
    parser.add_argument(
        "--cuda-visible-devices",
        default=None,
        help="Set before importing torch; useful for isolating the probe on a spare GPU.",
    )
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--output", default="")
    parser.add_argument("--verbose-model", action="store_true")
    parser.add_argument("--skip-graph-warmup", action="store_true")
    parser.add_argument(
        "--load-vocoder",
        action="store_true",
        help="Load the production BigVGAN stack even though trials stop after T2S.",
    )
    return parser.parse_args()


def _configure_process(args: argparse.Namespace) -> None:
    if args.cuda_visible_devices is not None:
        os.environ["CUDA_VISIBLE_DEVICES"] = str(args.cuda_visible_devices)
    os.environ["TTS_DEVICE"] = str(args.device)
    os.environ["ENABLE_CUDA_GRAPH"] = "1" if "graph_static" in _parse_csv(args.modes) else "0"
    os.environ["ENABLE_CUDA_GRAPH_PRECAPTURE"] = "0"
    os.environ["TTS_RUNTIME_WARMUP"] = "0"
    os.environ["TTS_SESSION_WARMUP"] = "0"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PYTHONUTF8"] = "1"


def _select_modes(value: str) -> list[ProbeMode]:
    names = _parse_csv(value)
    unknown = [name for name in names if name not in MODES]
    if unknown:
        raise ValueError(f"unknown modes: {', '.join(unknown)}")
    if not names:
        raise ValueError("at least one mode is required")
    return [MODES[name] for name in names]


def _select_cases(value: str) -> list[ProbeCase]:
    clean = str(value or "").strip().lower()
    if clean == "all":
        return list(CASES)
    if clean == "quick":
        return [case for case in CASES if case.name in QUICK_CASE_NAMES]
    names = set(_parse_csv(value))
    known = {case.name for case in CASES}
    unknown = sorted(names - known)
    if unknown:
        raise ValueError(f"unknown cases: {', '.join(unknown)}")
    selected = [case for case in CASES if case.name in names]
    if not selected:
        raise ValueError("at least one case is required")
    return selected


def _set_seed(seed: int, torch_module: Any) -> None:
    random.seed(seed)
    try:
        import numpy as np

        np.random.seed(seed)
    except Exception:
        pass
    torch_module.manual_seed(seed)
    if torch_module.cuda.is_available():
        torch_module.cuda.manual_seed_all(seed)


def _longest_equal_run(tokens: list[int]) -> int:
    return _longest_equal_run_with_token(tokens)[0]


def _longest_equal_run_with_token(tokens: list[int]) -> tuple[int, int | None]:
    best = 0
    best_token: int | None = None
    current = 0
    previous: int | None = None
    for token in tokens:
        if token == previous:
            current += 1
        else:
            previous = token
            current = 1
        if current > best:
            best = current
            best_token = token
    return best, best_token


def _periodic_run(tokens: list[int], max_period: int = 8) -> tuple[int, int]:
    """Return (longest run, period) for short repeating token patterns."""

    best_run = 0
    best_period = 0
    for period in range(1, max_period + 1):
        current = 0
        for index in range(period, len(tokens)):
            if tokens[index] == tokens[index - period]:
                current = current + 1 if current else period + 1
            else:
                current = 0
            if current > best_run:
                best_run = current
                best_period = period
    return best_run, best_period


def _token_metrics(tokens: list[int]) -> dict[str, float | int]:
    if not tokens:
        return {
            "token_count": 0,
            "unique_ratio": 0.0,
            "adjacent_repeat_ratio": 0.0,
            "top_token_ratio": 0.0,
            "longest_equal_run": 0,
            "longest_equal_run_token": -1,
            "longest_periodic_run": 0,
            "periodic_run_period": 0,
        }
    counts = Counter(tokens)
    adjacent = sum(left == right for left, right in zip(tokens, tokens[1:]))
    equal_run, equal_run_token = _longest_equal_run_with_token(tokens)
    periodic_run, periodic_period = _periodic_run(tokens)
    return {
        "token_count": len(tokens),
        "unique_ratio": len(counts) / len(tokens),
        "adjacent_repeat_ratio": adjacent / max(1, len(tokens) - 1),
        "top_token_ratio": max(counts.values()) / len(tokens),
        "longest_equal_run": equal_run,
        "longest_equal_run_token": -1 if equal_run_token is None else equal_run_token,
        "longest_periodic_run": periodic_run,
        "periodic_run_period": periodic_period,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quantile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def _median_absolute_deviation(values: list[float]) -> float:
    if not values:
        return 0.0
    median = statistics.median(values)
    return float(statistics.median(abs(value - median) for value in values))


def _mark_anomalies(rows: list[dict[str, Any]]) -> None:
    by_case: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("error"):
            continue
        by_case.setdefault(str(row["case"]), []).append(row)

    for case_rows in by_case.values():
        lengths = [float(row["token_count"]) for row in case_rows]
        median = statistics.median(lengths) if lengths else 0.0
        mad = _median_absolute_deviation(lengths)
        relative_limit = max(64.0, median * 2.0, median + 6.0 * max(1.0, mad))
        for row in case_rows:
            reasons: list[str] = []
            if row["termination"] != "eos":
                reasons.append(str(row["termination"]))
            if float(row["token_count"]) > relative_limit:
                reasons.append("length_outlier")
            run_token = int(row.get("longest_equal_run_token", -1))
            run_limit = 75 if run_token == 486 else 32
            if int(row["longest_equal_run"]) >= run_limit:
                reasons.append("equal_token_run")
            if int(row["longest_periodic_run"]) >= 48:
                reasons.append("periodic_token_run")
            if int(row["token_count"]) >= 32 and float(row["top_token_ratio"]) >= 0.55:
                reasons.append("token_concentration")
            row["case_token_median"] = float(median)
            row["case_token_mad"] = float(mad)
            row["case_length_limit"] = float(relative_limit)
            row["anomaly_reasons"] = reasons
            row["anomalous"] = bool(reasons)


def _mark_paired_differences(rows: list[dict[str, Any]]) -> None:
    paired: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}
    for row in rows:
        if row.get("error"):
            continue
        key = (str(row["case"]), int(row["seed"]))
        paired.setdefault(key, {})[str(row["mode"])] = row

    for modes in paired.values():
        dynamic = modes.get("dynamic")
        static = modes.get("static")
        for row in modes.values():
            if dynamic is None or row is dynamic:
                row["paired_dynamic_match"] = None
                row["paired_dynamic_token_delta"] = None
            else:
                row["paired_dynamic_match"] = row["token_sha256"] == dynamic["token_sha256"]
                row["paired_dynamic_token_delta"] = int(row["token_count"]) - int(dynamic["token_count"])
            if static is None or row is static:
                row["paired_static_match"] = None
                row["paired_static_token_delta"] = None
            else:
                row["paired_static_match"] = row["token_sha256"] == static["token_sha256"]
                row["paired_static_token_delta"] = int(row["token_count"]) - int(static["token_count"])


def _summarize(rows: list[dict[str, Any]], trials: int) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault((str(row["case"]), str(row["mode"])), []).append(row)

    summary: list[dict[str, Any]] = []
    for (case_name, mode_name), group in sorted(groups.items()):
        valid = [row for row in group if not row.get("error")]
        lengths = [float(row["token_count"]) for row in valid]
        runtimes = [float(row["elapsed_sec"]) for row in valid]
        anomalies = [row for row in valid if row.get("anomalous")]
        eos_count = sum(row.get("termination") == "eos" for row in valid)
        paired_rows = [row for row in valid if row.get("paired_dynamic_match") is not None]
        paired_deltas = [abs(float(row["paired_dynamic_token_delta"])) for row in paired_rows]
        static_paired_rows = [row for row in valid if row.get("paired_static_match") is not None]
        summary.append(
            {
                "case": case_name,
                "mode": mode_name,
                "requested_trials": trials,
                "completed": len(valid),
                "errors": len(group) - len(valid),
                "eos_rate": eos_count / len(valid) if valid else None,
                "anomaly_count": len(anomalies),
                "anomaly_rate": len(anomalies) / len(valid) if valid else None,
                "token_median": statistics.median(lengths) if lengths else None,
                "token_p95": _quantile(lengths, 0.95),
                "token_max": max(lengths) if lengths else None,
                "elapsed_median_sec": statistics.median(runtimes) if runtimes else None,
                "elapsed_p95_sec": _quantile(runtimes, 0.95),
                "paired_dynamic_match_rate": (
                    sum(bool(row["paired_dynamic_match"]) for row in paired_rows) / len(paired_rows)
                    if paired_rows
                    else None
                ),
                "paired_dynamic_abs_token_delta_median": (
                    statistics.median(paired_deltas) if paired_deltas else None
                ),
                "paired_static_match_rate": (
                    sum(bool(row["paired_static_match"]) for row in static_paired_rows)
                    / len(static_paired_rows)
                    if static_paired_rows
                    else None
                ),
            }
        )
    return summary


def _write_markdown(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# GPT-SoVITS stability probe",
        "",
        f"Generated: `{payload['metadata']['generated_at']}`",
        "",
        "| Case | Mode | Done | EOS | Anomalies | Token median / p95 / max | Time median / p95 | Match dynamic |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in payload["summary"]:
        eos = "-" if item["eos_rate"] is None else f"{item['eos_rate']:.1%}"
        anomaly = "-" if item["anomaly_rate"] is None else f"{item['anomaly_count']} ({item['anomaly_rate']:.1%})"
        token_values = " / ".join(
            "-" if item[key] is None else f"{item[key]:.0f}"
            for key in ("token_median", "token_p95", "token_max")
        )
        time_values = " / ".join(
            "-" if item[key] is None else f"{item[key]:.3f}s"
            for key in ("elapsed_median_sec", "elapsed_p95_sec")
        )
        match_dynamic = (
            "-"
            if item["paired_dynamic_match_rate"] is None
            else f"{item['paired_dynamic_match_rate']:.1%}"
        )
        lines.append(
            f"| {item['case']} | {item['mode']} | {item['completed']} | {eos} | "
            f"{anomaly} | {token_values} | {time_values} | {match_dynamic} |"
        )
    lines.extend(["", "## Anomalous trials", ""])
    anomalous = [row for row in payload["rows"] if row.get("anomalous") or row.get("error")]
    if not anomalous:
        lines.append("None detected by the probe thresholds.")
    else:
        lines.append("| Case | Mode | Seed | Tokens | Stop | Reasons / error |")
        lines.append("|---|---:|---:|---:|---:|---|")
        for row in anomalous:
            detail = row.get("error") or ", ".join(row.get("anomaly_reasons") or [])
            lines.append(
                f"| {row['case']} | {row['mode']} | {row['seed']} | "
                f"{row.get('token_count', '-')} | {row.get('termination', '-')} | {detail} |"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _model_output_context(verbose: bool):
    return contextlib.nullcontext() if verbose else contextlib.redirect_stdout(io.StringIO())


def _build_contexts(inferencer: Any, cases: Iterable[ProbeCase], torch_module: Any) -> list[SemanticContext]:
    from config import settings

    text_language = settings.TTS_OUTPUT_LANGUAGE
    prompt_language = settings.TTS_OUTPUT_LANGUAGE
    reference_audio = (
        settings.TTS_REF_AUDIO_EN
        if str(settings.TTS_OUTPUT_LANGUAGE).strip() == "英文"
        else settings.TTS_REF_AUDIO_JA
    )
    prompt_text = (
        settings.TTS_REF_TEXT_EN
        if str(settings.TTS_OUTPUT_LANGUAGE).strip() == "英文"
        else settings.TTS_REF_TEXT_JA
    )
    reference_path = Path(reference_audio)
    if not reference_path.is_absolute():
        reference_path = ROOT / reference_path
    if not reference_path.exists():
        raise FileNotFoundError(f"reference audio not found: {reference_path}")

    prompt_text = str(prompt_text or "").strip()
    if prompt_text and prompt_text[-1] not in inferencer.splits:
        prompt_text += "." if prompt_language == "英文" else "。"

    prompt_code = inferencer.dict_language.get(prompt_language)
    text_code = inferencer.dict_language.get(text_language)
    if prompt_code is None or text_code is None:
        raise KeyError(
            f"language mapping unavailable: prompt={prompt_language!r} text={text_language!r}"
        )

    session = inferencer._build_session_cache(str(reference_path), prompt_text, prompt_code)
    prompt_semantic = session.get("prompt")
    if prompt_semantic is None:
        raise RuntimeError("reference session did not produce prompt semantic tokens")
    if "phones1" in session and "bert1" in session:
        phones1, bert1 = session["phones1"], session["bert1"]
    else:
        phones1, bert1, _ = inferencer.get_phones_and_bert(prompt_text, prompt_code)

    contexts: list[SemanticContext] = []
    for case in cases:
        synthesis_text = case.text.strip()
        if synthesis_text and synthesis_text[-1] not in inferencer.splits:
            synthesis_text += "." if text_language == "英文" else "。"
        phones2, bert2, normalized = inferencer.get_phones_and_bert(synthesis_text, text_code)
        all_ids = torch_module.LongTensor(phones1 + phones2).to(inferencer.device).unsqueeze(0)
        all_len = torch_module.tensor([all_ids.shape[-1]]).to(inferencer.device)
        bert = torch_module.cat([bert1, bert2], 1).to(inferencer.device).unsqueeze(0)
        contexts.append(
            SemanticContext(
                case=case,
                synthesis_text=synthesis_text,
                normalized_text=str(normalized),
                target_phone_count=len(phones2),
                all_phoneme_ids=all_ids,
                all_phoneme_len=all_len,
                prompt_semantic=prompt_semantic,
                bert=bert,
            )
        )
    return contexts


def _run_trial(
    *,
    inferencer: Any,
    context: SemanticContext,
    mode: ProbeMode,
    seed: int,
    early_stop_num: int,
    args: argparse.Namespace,
    torch_module: Any,
    t2s_module: Any,
) -> dict[str, Any]:
    tracker = {"eos": False, "checks": 0}
    original_eos = t2s_module._should_stop_on_eos

    def tracked_eos(logits: Any, samples: Any, eos: int) -> bool:
        result = original_eos(logits, samples, eos)
        tracker["checks"] += 1
        tracker["eos"] = bool(tracker["eos"] or result)
        return result

    t2s_module._should_stop_on_eos = tracked_eos
    _set_seed(seed, torch_module)
    started = time.perf_counter()
    try:
        from tts.semantic_stability import assess_semantic_candidate

        rejected_attempts: list[dict[str, Any]] = []
        penalties = [args.repetition_penalty] + [
            args.retry_repetition_penalty
        ] * args.semantic_retries
        final_values: dict[str, Any] | None = None
        if torch_module.cuda.is_available() and str(inferencer.device).startswith("cuda"):
            torch_module.cuda.synchronize(inferencer.device)

        for attempt, penalty in enumerate(penalties, start=1):
            tracker = {"eos": False, "checks": 0}
            with torch_module.inference_mode(), _model_output_context(args.verbose_model):
                prediction, idx = inferencer.t2s_model.model.infer_panel(
                    context.all_phoneme_ids,
                    context.all_phoneme_len,
                    context.prompt_semantic,
                    context.bert,
                    top_k=args.top_k,
                    top_p=args.top_p,
                    temperature=args.temperature,
                    repetition_penalty=penalty,
                    early_stop_num=early_stop_num,
                    enable_cuda_graph=mode.enable_cuda_graph,
                    enable_static_kv=mode.enable_static_kv,
                )
            generated_count = max(0, int(idx))
            if generated_count:
                tokens = prediction[0, -generated_count:].detach().cpu().long().tolist()
            else:
                tokens = []
            metrics = _token_metrics(tokens)
            token_bytes = ",".join(str(token) for token in tokens).encode("ascii")
            if tracker["eos"]:
                termination = "eos"
            elif generated_count >= early_stop_num:
                termination = "early_stop"
            elif generated_count >= 1499:
                termination = "loop_limit"
            else:
                termination = "unknown"
            assessment = assess_semantic_candidate(
                tokens,
                target_phone_count=context.target_phone_count,
                max_generation_tokens=early_stop_num,
            )
            final_values = {
                "termination": termination,
                "eos_checks": int(tracker["checks"]),
                **metrics,
                "token_sha256": hashlib.sha256(token_bytes).hexdigest(),
                "token_preview": tokens[:16],
                "token_tail": tokens[-16:],
                "guard_reasons": list(assessment.reasons),
                "guard_exhausted": not assessment.accepted,
                "semantic_attempts": attempt,
            }
            if assessment.accepted or attempt == len(penalties):
                break
            rejected_attempts.append(
                {
                    "attempt": attempt,
                    "repetition_penalty": penalty,
                    "token_count": metrics["token_count"],
                    "termination": termination,
                    "reasons": list(assessment.reasons),
                }
            )

        if torch_module.cuda.is_available() and str(inferencer.device).startswith("cuda"):
            torch_module.cuda.synchronize(inferencer.device)
        elapsed = time.perf_counter() - started
        if final_values is None:
            raise RuntimeError("semantic probe produced no candidate")
        return {
            "case": context.case.name,
            "family": context.case.family,
            "text": context.case.text,
            "synthesis_text": context.synthesis_text,
            "normalized_text": context.normalized_text,
            "target_phone_count": context.target_phone_count,
            "mode": mode.name,
            "seed": seed,
            "elapsed_sec": elapsed,
            "rejected_attempts": rejected_attempts,
            **final_values,
        }
    except Exception as exc:
        if torch_module.cuda.is_available() and str(inferencer.device).startswith("cuda"):
            try:
                torch_module.cuda.synchronize(inferencer.device)
            except Exception:
                pass
        return {
            "case": context.case.name,
            "family": context.case.family,
            "text": context.case.text,
            "mode": mode.name,
            "seed": seed,
            "elapsed_sec": time.perf_counter() - started,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        t2s_module._should_stop_on_eos = original_eos


def main() -> int:
    args = _parse_args()
    if args.trials < 1:
        raise ValueError("--trials must be positive")
    if args.max_sec <= 0:
        raise ValueError("--max-sec must be positive")
    if args.semantic_retries < 0:
        raise ValueError("--semantic-retries cannot be negative")
    modes = _select_modes(args.modes)
    cases = _select_cases(args.cases)
    _configure_process(args)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    import torch

    from config import settings
    from local_tts_infer import TTSInferencer

    class SemanticOnlyInferencer(TTSInferencer):
        def _load_bigvgan_model(self) -> None:
            self.bigvgan_model = None

    print(
        f"[probe] device={args.device} visible={os.environ.get('CUDA_VISIBLE_DEVICES', 'all')} "
        f"modes={[mode.name for mode in modes]} cases={len(cases)} trials={args.trials}"
    )
    load_started = time.perf_counter()
    inferencer_class = TTSInferencer if args.load_vocoder else SemanticOnlyInferencer
    with _model_output_context(args.verbose_model):
        inferencer = inferencer_class(
            device=args.device,
            gpt_path=settings.TTS_GPT_MODEL_PATH or None,
            sovits_path=settings.TTS_SOVITS_MODEL_PATH or None,
        )
        contexts = _build_contexts(inferencer, cases, torch)
    import importlib

    # The vendored package supports two import roots.  Resolve the module from
    # the live decoder class so the EOS hook and tqdm replacement affect the
    # exact globals used by inference rather than a duplicate module object.
    t2s_module = importlib.import_module(inferencer.t2s_model.model.__class__.__module__)
    t2s_module.tqdm = lambda values: values
    print(f"[probe] models and contexts ready in {time.perf_counter() - load_started:.1f}s")

    early_stop_num = int(inferencer.hz * args.max_sec)
    graph_mode = next((mode for mode in modes if mode.enable_cuda_graph), None)
    if graph_mode is not None and not args.skip_graph_warmup:
        print("[probe] warming CUDA Graph keys (discarded trials)")
        for index, context in enumerate(contexts):
            row = _run_trial(
                inferencer=inferencer,
                context=context,
                mode=graph_mode,
                seed=args.seed_start - 10000 - index,
                early_stop_num=early_stop_num,
                args=args,
                torch_module=torch,
                t2s_module=t2s_module,
            )
            if row.get("error"):
                print(f"[probe] graph warmup failed for {context.case.name}: {row['error']}")

    rows: list[dict[str, Any]] = []
    total = len(contexts) * len(modes) * args.trials
    completed = 0
    run_started = time.perf_counter()
    for offset in range(args.trials):
        seed = args.seed_start + offset
        for context in contexts:
            for mode in modes:
                row = _run_trial(
                    inferencer=inferencer,
                    context=context,
                    mode=mode,
                    seed=seed,
                    early_stop_num=early_stop_num,
                    args=args,
                    torch_module=torch,
                    t2s_module=t2s_module,
                )
                rows.append(row)
                completed += 1
                if completed == total or completed % max(1, min(25, total // 10)) == 0:
                    elapsed = time.perf_counter() - run_started
                    print(f"[probe] {completed}/{total} trials ({elapsed:.1f}s)")

    _mark_anomalies(rows)
    _mark_paired_differences(rows)
    summary = _summarize(rows, args.trials)
    now = datetime.now().astimezone()
    output_path = Path(args.output) if args.output else (
        ROOT / "output" / "diagnostics" / f"gsv_stability_{now:%Y%m%d_%H%M%S}.json"
    )
    if not output_path.is_absolute():
        output_path = ROOT / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    gpt_path = Path(settings.TTS_GPT_MODEL_PATH)
    sovits_path = Path(settings.TTS_SOVITS_MODEL_PATH)
    payload = {
        "metadata": {
            "generated_at": now.isoformat(),
            "device": str(args.device),
            "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
            "gpu_name": (
                torch.cuda.get_device_name(torch.device(args.device))
                if torch.cuda.is_available() and str(args.device).startswith("cuda")
                else None
            ),
            "torch_version": torch.__version__,
            "trials_per_case_mode": args.trials,
            "seed_start": args.seed_start,
            "max_sec": args.max_sec,
            "early_stop_num": early_stop_num,
            "semantic_retries": args.semantic_retries,
            "retry_repetition_penalty": args.retry_repetition_penalty,
            "top_k": args.top_k,
            "top_p": args.top_p,
            "temperature": args.temperature,
            "repetition_penalty": args.repetition_penalty,
            "load_vocoder": bool(args.load_vocoder),
            "modes": [asdict(mode) for mode in modes],
            "cases": [asdict(case) for case in cases],
            "model_max_sec": float(inferencer.max_sec),
            "gpt_model": str(gpt_path),
            "gpt_sha256": _sha256(gpt_path),
            "sovits_model": str(sovits_path),
            "sovits_sha256": _sha256(sovits_path),
            "elapsed_sec": time.perf_counter() - run_started,
        },
        "summary": summary,
        "rows": rows,
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path = output_path.with_suffix(".md")
    _write_markdown(markdown_path, payload)

    total_anomalies = sum(bool(row.get("anomalous")) for row in rows)
    total_errors = sum(bool(row.get("error")) for row in rows)
    print(
        f"[probe] done: rows={len(rows)} anomalies={total_anomalies} errors={total_errors}\n"
        f"[probe] json={output_path}\n[probe] report={markdown_path}"
    )
    return 0 if total_errors == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
