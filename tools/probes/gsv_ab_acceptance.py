"""Local A/B acceptance suite for opt-in v3 MLX semantic inference.

Each backend/case/block runs in a separate process. CPU is functional and
numerical evidence only; the speed gate requires Apple Silicon Metal/MPS.
Private reference-derived fixtures, semantic IDs, and WAVs stay under output/.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
PROBE = ROOT / "tools" / "probes" / "gsv_backend_probe.py"

CASES = (
    ("short_first", "ああ。", True),
    ("medium_followup", "今日は実験の結果を一緒に確認しましょう。", False),
    ("long_followup", "まず測定結果を落ち着いて見直してから、音の途切れや不自然な繰り返しがないか、一つずつ丁寧に確認していきましょう。", False),
    ("weak_first", "うーん……", True),
    ("continuation_followup", "ええと、まず条件を整理しましょう。それから次の手順を決めます。", False),
)


def backend_order(block):
    return ("torch", "mlx") if block % 2 == 0 else ("mlx", "torch")


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def percentile(values):
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    return {"n": len(ordered), "min": ordered[0],
            "p50": (ordered[(len(ordered)-1)//2] + ordered[len(ordered)//2])/2,
            "p95": ordered[min(len(ordered)-1, int(0.95*len(ordered)))],
            "max": ordered[-1]}


def verify_pair_identity(torch_report, mlx_report, expected):
    for backend, item in (("torch", torch_report), ("mlx", mlx_report)):
        if item.get("backend") != backend or item.get("semantic", {}).get("semantic_backend") != backend:
            raise ValueError("A/B child resolved a different semantic backend")
        if item.get("acoustic_device") != expected["acoustic_device"]:
            raise ValueError("A/B child resolved a different acoustic device")
        if item.get("acoustic_dtype") != expected["acoustic_dtype"]:
            raise ValueError("A/B child resolved a different acoustic dtype")
        for key in ("candidate_sha", "working_tree_dirty", "code_sha256"):
            if item.get(key) != expected[key]:
                raise ValueError(f"A/B child {key} differs from suite source identity")
    if torch_report.get("semantic_dtype") != mlx_report.get("semantic_dtype"):
        raise ValueError("A/B semantic inference dtype differs")
    fields = ("source_checkpoint_sha256", "sovits_checkpoint_sha256",
              "reference_audio_sha256", "text_sha256", "prompt_text_sha256")
    if any(torch_report.get(key) != mlx_report.get(key) for key in fields):
        raise ValueError("A/B pair uses different source, reference, or text identity")
    for key in ("source_checkpoint_sha256", "sovits_checkpoint_sha256",
                "reference_audio_sha256"):
        if torch_report.get(key) != expected[key]:
            raise ValueError(f"A/B pair {key} differs from requested asset")
    if torch_report.get("parameters") != mlx_report.get("parameters"):
        raise ValueError("A/B pair resolved different generation/acoustic parameters")
    if torch_report.get("controlled_acoustic_rng") != mlx_report.get("controlled_acoustic_rng"):
        raise ValueError("A/B pair used different acoustic RNG policy")


def longest_run(tokens):
    longest = current = 0
    previous = None
    for token in tokens:
        current = current + 1 if token == previous else 1
        longest = max(longest, current)
        previous = token
    return longest


def audio_metrics(path, token_segments):
    import numpy as np
    import soundfile as sf

    audio, rate = sf.read(path, dtype="float32", always_2d=False)
    subtype = sf.info(path).subtype
    if audio.ndim != 1 or audio.size == 0:
        raise ValueError("A/B WAV is empty or not mono")
    finite = bool(np.isfinite(audio).all())
    magnitude = np.abs(audio)
    active = np.flatnonzero(magnitude >= 1e-3)
    leading = (int(active[0]) if active.size else len(audio)) / rate
    trailing = (len(audio) - 1 - int(active[-1]) if active.size else len(audio)) / rate
    token_runs = [longest_run(segment["tokens"]) for segment in token_segments]
    budget_hit = any(segment.get("budget_boundary_reached") for segment in token_segments)
    duration = len(audio) / rate
    peak = float(np.max(magnitude))
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    clipping = float(np.mean(magnitude >= 0.999))
    flags = []
    if not finite:
        flags.append("nonfinite")
    if peak < 1e-3 or rms < 1e-4:
        flags.append("unexpected_silence")
    if clipping > 0.001:
        flags.append("clipping")
    if duration < 0.2:
        flags.append("very_short_audio")
    if budget_hit:
        flags.append("semantic_budget_boundary")
    if token_runs and max(token_runs) >= 20:
        flags.append("semantic_repetition_run")
    return {"sample_rate": rate, "wav_subtype": subtype,
            "samples": len(audio), "duration_s": duration,
            "peak_abs": peak, "rms": rms, "clipping_fraction": clipping,
            "leading_silence_s": leading, "trailing_silence_s": trailing,
            "longest_semantic_run": max(token_runs, default=0),
            "quality_flags": flags, "wav_sha256": sha256(path)}


def controlled_comparison(torch_wav, mlx_wav, torch_tokens, mlx_tokens, torch_report, mlx_report):
    import numpy as np
    import soundfile as sf

    if not (torch_report.get("controlled_acoustic_rng") and mlx_report.get("controlled_acoustic_rng")):
        raise ValueError("Controlled comparison requires isolated acoustic RNG")
    left = [segment["tokens"] for segment in torch_tokens]
    right = [segment["tokens"] for segment in mlx_tokens]
    if left != right:
        return {"semantic_ids_match": False, "waveform_comparison": "not_applicable_semantic_divergence",
                "first_divergent_segment": next((i for i, (a, b) in enumerate(zip(left, right)) if a != b),
                                                 min(len(left), len(right)))}
    a, sr_a = sf.read(torch_wav, dtype="float32")
    b, sr_b = sf.read(mlx_wav, dtype="float32")
    subtype_a = sf.info(torch_wav).subtype
    subtype_b = sf.info(mlx_wav).subtype
    if sr_a != sr_b:
        return {"semantic_ids_match": True, "waveform_comparison": "sample_rate_mismatch"}
    if subtype_a != subtype_b:
        return {"semantic_ids_match": True, "waveform_comparison": "wav_subtype_mismatch"}
    overlap = min(len(a), len(b))
    delta = a[:overlap].astype(np.float64) - b[:overlap].astype(np.float64)
    return {"semantic_ids_match": True, "waveform_comparison": "measured",
            "comparison_domain": "decoded_saved_wav_samples_after_encoding",
            "wav_subtype": subtype_a,
            "sample_rate": sr_a, "sample_count_torch": len(a), "sample_count_mlx": len(b),
            "max_abs": float(np.max(np.abs(delta))) if overlap else None,
            "rmse": float(np.sqrt(np.mean(delta**2))) if overlap else None,
            "bit_identical": bool(np.array_equal(a, b)),
            "saved_wav_sha256_equal": sha256(torch_wav) == sha256(mlx_wav)}


def _run_child(command, report_path, log_path, *, timeout):
    started = time.perf_counter()
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                errors="replace", timeout=timeout)
        exit_code = result.returncode
        log = result.stdout + "\n" + result.stderr
    except subprocess.TimeoutExpired as exc:
        exit_code = None
        log = f"Timed out after {timeout}s; command terminated.\n{exc}"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(log, encoding="utf-8")
    report = None
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"exit_code": exit_code, "elapsed_ms": (time.perf_counter()-started)*1000,
            "report": report, "report_sha256": sha256(report_path) if report is not None else None,
            "log_name": log_path.name}


def _probe_command(args, subcommand):
    return [str(Path(args.python).resolve()), str(PROBE), subcommand]


def _asset_args(args):
    result = ["--checkpoint", str(Path(args.checkpoint).resolve()),
              "--sovits", str(Path(args.sovits).resolve()),
              "--reference-audio", str(Path(args.reference_audio).resolve())]
    return result


def _audio_command(args, case, profile, backend, phase, seed, report, token_output, wav):
    _, text, _ = case
    command = _probe_command(args, "audio") + _asset_args(args)
    command += ["--backend", backend, "--acoustic-device", "cpu" if args.device == "cpu" else "mps",
                "--reference-text", args.reference_text, "--text", text,
                "--top-k", "1" if phase == "controlled" else str(profile["top_k"]),
                "--top-p", str(profile["top_p"]), "--temperature", str(profile["temperature"]),
                "--sample-steps", str(profile["sample_steps"]),
                "--speed", str(profile["speed"]), "--pause-second", str(profile["pause_second"]),
                "--how-to-cut", profile["how_to_cut"],
                "--chunk-seconds", str(args.chunk_seconds),
                "--warmup", str(args.warmup),
                "--runs", str(args.controlled_runs if phase == "controlled" else args.runs_per_block),
                "--seed", str(seed), "--output", str(report),
                "--token-output", str(token_output), "--wav", str(wav)]
    if profile.get("max_sec_override") is not None:
        command += ["--max-sec-override", str(profile["max_sec_override"])]
    if args.assets_root:
        command += ["--assets-root", str(Path(args.assets_root).resolve())]
    if args.device == "cpu" and backend == "mlx":
        command.append("--probe-mlx-cpu")
    if phase == "controlled":
        command.append("--controlled")
    return command


def _child_paths(root, phase, case_name, block, backend):
    stem = f"{phase}-{case_name}-b{block}-{backend}"
    folder = root / "private" / "runs"
    folder.mkdir(parents=True, exist_ok=True)
    return {"report": folder / f"{stem}.json", "tokens": folder / f"{stem}-tokens.json",
            "wav": folder / f"{stem}.wav", "log": folder / f"{stem}.log"}


def _load_tokens(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _save_suite(root, report):
    path = root / "suite-report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"A/B suite: {report['status']} -> {path}")


def run(args):
    from tools.probes.gsv_backend_probe import _identity, _environment
    from config import settings
    from tts.pipeline import get_sovits_params

    if settings.TTS_OUTPUT_LANGUAGE != "日文":
        raise ValueError("This first A/B suite qualifies the Japanese v3 frontend only")
    all_cases = {case[0]: case for case in CASES}
    selected = [all_cases[name] for name in args.cases.split(",") if name in all_cases]
    if not selected or len(selected) != len(args.cases.split(",")):
        raise ValueError("--cases must list distinct known case names")
    if len({case[0] for case in selected}) != len(selected):
        raise ValueError("--cases contains a duplicate")
    root = Path(args.output).resolve() if args.output else (
        ROOT / "output" / "diagnostics" / "gsv-ab" /
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    root.mkdir(parents=True, exist_ok=False)
    expected = {"source_checkpoint_sha256": sha256(args.checkpoint),
                "sovits_checkpoint_sha256": sha256(args.sovits),
                "reference_audio_sha256": sha256(args.reference_audio),
                "acoustic_device": "cpu" if args.device == "cpu" else "mps",
                "acoustic_dtype": "float32", **_identity()}
    report = {"schema": "amadeus.gsv_ab_acceptance.v1", "status": "running",
              "purpose": "cpu_functional_quality" if args.device == "cpu" else "mac_metal_speed_and_quality",
              "device": args.device, "candidate_sha": expected["candidate_sha"],
              "working_tree_dirty": expected["working_tree_dirty"],
              "code_sha256": expected["code_sha256"],
              "suite_code_sha256": sha256(__file__), "environment": _environment(),
              "assets": {key: expected[key] for key in (
                  "source_checkpoint_sha256", "sovits_checkpoint_sha256", "reference_audio_sha256")},
              "expected_execution": {"acoustic_device": expected["acoustic_device"],
                                     "acoustic_dtype": expected["acoustic_dtype"]},
              "reference_text_sha256": hashlib.sha256(args.reference_text.encode()).hexdigest(),
              "parameters": {"warmup_per_process": args.warmup, "blocks_per_case": args.blocks,
                             "measured_runs_per_block": args.runs_per_block,
                             "controlled_runs_per_case": args.controlled_runs,
                             "chunk_seconds": args.chunk_seconds,
                             "completed_audio_cache": "not_used", "if_freeze": False},
              "cases": {}, "invocations": [], "failures": [],
              "fixed_model": None, "controlled_pairs": [], "blind_kit": {},
              "first_voiced_device_write_ms": None, "acoustic_onset_ms": None,
              "human_listening": "pending"}
    _save_suite(root, report)
    records = {}
    all_success = {}
    for case_index, case in enumerate(selected):
        name, text, is_first = case
        profile = get_sovits_params(text, is_first_sentence=is_first)
        profile["enable_cuda_graph"] = False
        report["cases"][name] = {"text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                                  "first_sentence": is_first, "resolved_profile": profile,
                                  "backend_metrics": {}, "failures": []}
        for phase, block_count in (("controlled", 1), ("production", args.blocks)):
            for block in range(block_count):
                pair = {}
                order = backend_order(block if phase == "production" else case_index)
                for backend in order:
                    paths = _child_paths(root, phase, name, block, backend)
                    seed = args.seed_start + case_index*10000 + block*100 + (500000 if phase == "controlled" else 0)
                    command = _audio_command(args, case, profile, backend, phase,
                                             seed, paths["report"], paths["tokens"], paths["wav"])
                    child = _run_child(command, paths["report"], paths["log"], timeout=args.timeout)
                    report["invocations"].append({"case": name, "phase": phase, "block": block,
                                                  "backend": backend, "order": list(order),
                                                  "exit_code": child["exit_code"],
                                                  "report_sha256": child["report_sha256"],
                                                  "log_name": child["log_name"]})
                    if child["exit_code"] != 0 or not child["report"] or child["report"].get("status") != "passed":
                        failure = {"case": name, "phase": phase, "block": block,
                                   "backend": backend, "reason": "child_failed_or_report_missing"}
                        report["failures"].append(failure)
                        report["cases"][name]["failures"].append(failure)
                    else:
                        try:
                            token_trace = _load_tokens(paths["tokens"])
                            if len(token_trace) != len(child["report"]["runs"]):
                                raise ValueError("token trace count differs from measured runs")
                            pair[backend] = {"report": child["report"], "paths": paths,
                                             "tokens": token_trace}
                        except (OSError, ValueError, KeyError):
                            report["failures"].append({"case": name, "phase": phase,
                                                       "block": block, "backend": backend,
                                                       "reason": "private_token_trace_missing_or_invalid"})
                        else:
                            all_success[(phase, name, block, backend)] = pair[backend]
                if len(pair) == 2:
                    try:
                        verify_pair_identity(pair["torch"]["report"], pair["mlx"]["report"], expected)
                        records[(phase, name, block)] = pair
                    except ValueError as exc:
                        report["failures"].append({"case": name, "phase": phase, "block": block,
                                                   "reason": str(exc)})
                _save_suite(root, report)
    _build_pair_results(args, root, report, records, all_success)
    _run_fixed_model(args, root, report, selected[0])
    report["status"] = "recorded_with_failures" if report["failures"] else "recorded_listening_pending"
    report["speed_gate"] = "not_run_cpu" if args.device == "cpu" else "measured_pending_review"
    report["voice_quality_gate"] = "human_listening_pending"
    report["sample_target_met"] = (
        args.blocks >= 2 and len(selected) >= 2 and
        all(sum(item["backend_metrics"][backend]["paired_comparable_runs"]
                for item in report["cases"].values()) >= 20
            for backend in ("torch", "mlx")))
    _save_suite(root, report)
    return report


def _build_pair_results(args, root, report, records, all_success):
    blind_dir = root / "private" / "blind"
    blind_dir.mkdir(parents=True, exist_ok=True)
    for (phase, case, block, backend), item in all_success.items():
        for index, row in enumerate(item["report"]["runs"]):
            wav = item["paths"]["wav"].with_name(f"{item['paths']['wav'].stem}-{index+1}.wav")
            if not wav.is_file():
                report["failures"].append({"case": case, "phase": phase, "block": block,
                                           "backend": backend, "reason": "wav_missing"})
                continue
            try:
                row["audio_checks"] = audio_metrics(wav, item["tokens"][index]["segments"])
            except (OSError, ValueError, IndexError, KeyError):
                report["failures"].append({"case": case, "phase": phase, "block": block,
                                           "backend": backend, "reason": "audio_metrics_failed"})
    blind = random.Random(args.blind_seed)
    review_rows, answer_key = [], {}
    paired_rows = {(case, backend): [] for case in report["cases"] for backend in ("torch", "mlx")}
    for (phase, case, block), pair in records.items():
        left, right = pair["torch"], pair["mlx"]
        torch_rows = left["report"]["runs"]
        mlx_rows = right["report"]["runs"]
        if len(torch_rows) != len(mlx_rows):
            report["failures"].append({"case": case, "phase": phase, "block": block,
                                       "reason": "measured_run_count_mismatch"})
        for index in range(min(len(torch_rows), len(mlx_rows))):
            t_row, m_row = torch_rows[index], mlx_rows[index]
            if t_row["seed"] != m_row["seed"]:
                report["failures"].append({"case": case, "phase": phase, "block": block,
                                           "reason": "seed_mismatch"})
                continue
            wavs = {}
            token_sets = {}
            for backend, item in pair.items():
                wav = item["paths"]["wav"].with_name(
                    f"{item['paths']['wav'].stem}-{index+1}.wav")
                if not wav.is_file():
                    report["failures"].append({"case": case, "phase": phase, "block": block,
                                               "backend": backend, "reason": "wav_missing"})
                    break
                token_sets[backend] = item["tokens"][index]["segments"]
                wavs[backend] = wav
            if len(wavs) != 2:
                continue
            if phase == "production":
                paired_rows[(case, "torch")].append(t_row)
                paired_rows[(case, "mlx")].append(m_row)
            if phase == "controlled":
                try:
                    comparison = controlled_comparison(
                        wavs["torch"], wavs["mlx"], token_sets["torch"], token_sets["mlx"],
                        left["report"], right["report"])
                except (OSError, ValueError):
                    report["failures"].append({"case": case, "phase": phase, "block": block,
                                               "reason": "controlled_comparison_failed"})
                    continue
                report["controlled_pairs"].append({"case": case, "seed": t_row["seed"],
                                                   **comparison})
            pair_id = f"pair_{len(answer_key)+1:03d}"
            labels = ["torch", "mlx"]
            blind.shuffle(labels)
            assignment = {"A": labels[0], "B": labels[1]}
            answer_key[pair_id] = {"case": case, "phase": phase, "seed": t_row["seed"],
                                   "assignment": assignment}
            for label, backend in assignment.items():
                shutil.copyfile(wavs[backend], blind_dir / f"{pair_id}_{label}.wav")
            review_rows.append({"pair_id": pair_id, "case": case, "mode": phase,
                                "expected_text": report["cases"][case]["text"],
                                "A_file": f"{pair_id}_A.wav", "B_file": f"{pair_id}_B.wav",
                                "intelligibility_A": "", "intelligibility_B": "",
                                "timbre_A": "", "timbre_B": "", "prosody_A": "", "prosody_B": "",
                                "artifacts_A": "", "artifacts_B": "", "preference": "", "notes": ""})
    with (blind_dir / "review-sheet.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(review_rows[0]) if review_rows else
                                ["pair_id", "case", "mode", "expected_text", "A_file", "B_file",
                                 "intelligibility_A", "intelligibility_B", "timbre_A", "timbre_B",
                                 "prosody_A", "prosody_B", "artifacts_A", "artifacts_B", "preference", "notes"])
        writer.writeheader()
        writer.writerows(review_rows)
    (root / "private" / "answer-key.private.json").write_text(
        json.dumps(answer_key, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["blind_kit"] = {"pairs": len(answer_key), "review_sheet": "private/blind/review-sheet.csv",
                           "answer_key": "private/answer-key.private.json", "listening": "pending"}

    def metrics(rows):
        return {
            "first_emitted_chunk_ms": percentile([row["first_emitted_chunk_ms"] for row in rows]),
            "total_synthesis_ms": percentile([row["total_synthesis_ms"] for row in rows]),
            "semantic_total_ms": percentile([row["semantic_total_ms"] for row in rows]),
            "rtf": percentile([row["rtf"] for row in rows]),
            "semantic_tokens": [row["semantic_tokens"] for row in rows],
            "guard_attempts": [row["semantic_attempts"] for row in rows],
            "audio_seconds": [row["audio_seconds"] for row in rows],
            "rss_bytes": [row["rss_bytes"] for row in rows],
            "quality_flags": [row.get("audio_checks", {}).get("quality_flags", []) for row in rows],
        }

    for case, case_report in report["cases"].items():
        for backend in ("torch", "mlx"):
            raw_rows = [row for (phase, name, _, selected_backend), child in all_success.items()
                        if phase == "production" and name == case and selected_backend == backend
                        for row in child["report"]["runs"]]
            comparable = paired_rows[(case, backend)]
            expected_runs = args.blocks * args.runs_per_block
            case_report["backend_metrics"][backend] = {
                "attempted_runs": expected_runs, "raw_successful_runs": len(raw_rows),
                "failed_or_missing_runs": expected_runs - len(raw_rows),
                "paired_comparable_runs": len(comparable),
                "unpaired_successful_runs": len(raw_rows) - len(comparable),
                "paired_comparison": metrics(comparable),
                "unpaired_descriptive": metrics(raw_rows),
            }


def _run_fixed_model(args, root, report, case):
    # One actual frontend fixture supplies the same fixed history to both
    # model-only runs. Their free-generation rows remain separate from audio.
    name, text, _ = case
    folder = root / "private" / "fixed"
    folder.mkdir(parents=True, exist_ok=True)
    fixture = folder / "inputs.npz"
    input_report = folder / "inputs-report.json"
    command = _probe_command(args, "inputs") + _asset_args(args)
    command += ["--reference-text", args.reference_text, "--text", text,
                "--acoustic-device", "cpu" if args.device == "cpu" else "mps",
                "--history-steps", str(args.fixed_steps), "--fixture-output", str(fixture),
                "--output", str(input_report)]
    if args.assets_root:
        command += ["--assets-root", str(Path(args.assets_root).resolve())]
    child = _run_child(command, input_report, folder / "inputs.log", timeout=args.timeout)
    if child["exit_code"] != 0:
        report["failures"].append({"phase": "fixed_inputs", "reason": "frontend_fixture_failed"})
        return
    fixed = {"fixture_sha256": sha256(fixture), "results": {}, "order": list(backend_order(0))}
    for backend in backend_order(0):
        result = folder / f"bench-{backend}.json"
        command = _probe_command(args, "bench") + [
            "--backend", backend, "--device", args.device,
            "--checkpoint", str(Path(args.checkpoint).resolve()), "--inputs", str(fixture),
            "--sovits", str(Path(args.sovits).resolve()),
            "--reference-audio", str(Path(args.reference_audio).resolve()),
            "--runs", str(args.fixed_runs), "--warmup", str(args.fixed_warmup),
            "--budget", str(args.fixed_budget), "--output", str(result)]
        child = _run_child(command, result, folder / f"bench-{backend}.log", timeout=args.timeout)
        if child["exit_code"] != 0 or not child["report"] or child["report"].get("status") != "passed":
            report["failures"].append({"phase": "fixed_bench", "backend": backend,
                                       "reason": "model_benchmark_failed"})
        else:
            item = child["report"]
            if (item.get("backend") != backend
                    or item.get("candidate_sha") != report["candidate_sha"]
                    or item.get("working_tree_dirty") != report["working_tree_dirty"]
                    or item.get("code_sha256") != report["code_sha256"]
                    or item.get("source_checkpoint_sha256") != report["assets"]["source_checkpoint_sha256"]
                    or item.get("inputs_sha256") != sha256(fixture)):
                report["failures"].append({"phase": "fixed_bench", "backend": backend,
                                           "reason": "model_benchmark_identity_mismatch"})
            else:
                fixed["results"][backend] = item
    if len(fixed["results"]) == 2 and (
            fixed["results"]["torch"].get("sampling_parameters")
            != fixed["results"]["mlx"].get("sampling_parameters")):
        report["failures"].append({"phase": "fixed_bench",
                                   "reason": "model_benchmark_parameters_mismatch"})
    report["fixed_model"] = fixed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--device", choices=("cpu", "metal"), default="metal")
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--sovits", required=True)
    parser.add_argument("--reference-audio", required=True)
    parser.add_argument("--reference-text", required=True)
    parser.add_argument("--assets-root", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--cases", default=",".join(case[0] for case in CASES))
    parser.add_argument("--blocks", type=int, default=2)
    parser.add_argument("--runs-per-block", type=int, default=2)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--controlled-runs", type=int, default=1)
    parser.add_argument("--seed-start", type=int, default=1701)
    parser.add_argument("--blind-seed", type=int, default=7831)
    parser.add_argument("--chunk-seconds", type=float, default=0.0)
    parser.add_argument("--fixed-steps", type=int, default=32)
    parser.add_argument("--fixed-runs", type=int, default=5)
    parser.add_argument("--fixed-warmup", type=int, default=1)
    parser.add_argument("--fixed-budget", type=int, default=400)
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args(argv)
    if min(args.blocks, args.runs_per_block, args.controlled_runs, args.fixed_steps,
           args.fixed_runs, args.timeout) < 1 or args.warmup < 0 or args.fixed_warmup < 0:
        parser.error("counts and timeout must be positive; warmup may be zero")
    os.environ["TTS_BACKEND"] = "gpt_sovits"
    os.environ["TTS_DEVICE"] = "cpu" if args.device == "cpu" else "mps"
    os.environ["ENABLE_CUDA_GRAPH"] = "0"
    os.environ["FIRST_SENTENCE_AUDIO_CACHE_ENABLED"] = "0"
    os.environ["TTS_OUTPUT_LANGUAGE"] = "日文"
    report = run(args)
    if report["failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
