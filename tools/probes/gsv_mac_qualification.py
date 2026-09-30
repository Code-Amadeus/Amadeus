"""One-command, resumable, local qualification of experimental MLX T2S.

Run with the installed voice/MLX environment and trusted local assets; nothing
is downloaded or uploaded. ``--device cpu --smoke`` exercises every phase at
reduced counts and cannot qualify Metal performance. Reports and logs under
private/ are never added to the share archive. Listening WAV export is opt-in.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import signal
import statistics
import subprocess
import sys
import time
import zipfile


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.probes.gsv_ab_acceptance import CASES
from tools.probes.gsv_backend_probe import _environment, _identity, _percentiles

PROBE = ROOT / "tools" / "probes" / "gsv_backend_probe.py"
SCHEMA = "amadeus.gsv_mac_qualification.v1"
# These are probe-only identities. They never become product settings.
VARIANT_SPECS = {
    "torch_fp32": ("torch", "float32", "not_applicable"),
    "torch_fp16": ("torch", "float16", "not_applicable"),
    "mlx_audit_fp32": ("mlx", "float32", "audit"),
    "mlx_unpadded_fp32": ("mlx", "float32", "unpadded"),
    "mlx_unpadded_fp16": ("mlx", "float16", "unpadded"),
    "mlx_padded_fp32": ("mlx", "float32", "current"),
    "mlx_padded_fp16": ("mlx", "float16", "current"),
}
VARIANTS = tuple(VARIANT_SPECS)
MLX_VARIANTS = VARIANTS[2:]
PADDING_PAIRS = {dtype: (f"mlx_unpadded_fp{bits}", f"mlx_padded_fp{bits}")
                 for dtype, bits in (("float32", 32), ("float16", 16))}
PROFILE_CASES = CASES[:3]
SMOKE_TEXT = "ああ。"
SMOKE_MAX_SEC = 0.5
FRONTEND_DIRS = (
    "chinese-roberta-wwm-ext-large", "chinese-hubert-base",
    "models--nvidia--bigvgan_v2_24khz_100band_256x",
)
DEPENDENCIES = ("torch", "torchaudio", "mlx", "mlx-metal", "mlx-cpu", "numpy",
                "safetensors", "transformers", "huggingface-hub", "soundfile",
                "librosa", "psutil", "pyopenjtalk-plus")
MEMORY_FIELDS = ("rss_bytes", "mlx_active_bytes", "mlx_cache_bytes", "mlx_peak_bytes",
                 "torch_mps_allocated_bytes", "torch_mps_driver_bytes")
IDENTITY_FIELDS = ("candidate_sha", "working_tree_dirty", "code_sha256")
EXECUTION_ENV = ("PYTORCH_ENABLE_MPS_FALLBACK", "PYTORCH_MPS_FAST_MATH", "PYTORCH_MPS_PREFER_METAL",
                 "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS")
ASSET_FIELDS = ("source_checkpoint_sha256", "sovits_checkpoint_sha256", "reference_audio_sha256")
FIXED_FIELDS = ("seed", "prefill_ms", "fixed_ar_work_ms", "fixed_step_ms", "final_drain_ms",
                "sampled_ids_sha256", "decode_calls", "sampler_calls", "host_stop_reads",
                "final_audio_length")
AUDIO_FIELDS = ("seed", "first_emitted_chunk_ms", "total_synthesis_ms", "audio_seconds", "rtf",
                "sample_rate", "chunks", "semantic_tokens", "semantic_attempts", "semantic_segments",
                "semantic_budget_boundary_segments",
                "semantic_total_ms", "bridge_in_ms", "bridge_out_ms", "peak_abs_sample", "rms",
                "clipping_fraction", "rss_bytes", "first_chunk_semantic_ms")
VALIDATION_FIELDS = ("step", "tensor", "finite", "passed", "max_abs", "rmse", "atol", "rtol",
                     "top_logit_margin_reference", "top_logit_margin_candidate", "top5_overlap",
                     "top1_agrees")
STAGES = {"frontend_bert", "cfm", "denorm", "bigvgan"}
OFFLINE_ENV = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
               "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1", "TTS_OUTPUT_LANGUAGE": "日文",
               "FIRST_SENTENCE_AUDIO_CACHE_ENABLED": "0", "TTS_RUNTIME_WARMUP": "0",
               "TTS_SESSION_WARMUP": "0", "ENABLE_CUDA_GRAPH_PRECAPTURE": "0"}


def _sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def variant_order(block, variants=VARIANTS):
    """Forward/reverse blocks balance each paired variant's process order."""
    return tuple(variants) if block % 2 == 0 else tuple(reversed(variants))


def _profiles():
    from tts.pipeline import get_sovits_params

    keys = ("top_k", "top_p", "temperature", "sample_steps", "speed", "pause_second",
            "how_to_cut", "max_sec_override")
    result = {}
    for name, text, first in CASES:
        resolved = get_sovits_params(text, is_first_sentence=first)
        result[name] = {key: resolved.get(key) for key in keys}
    if [result[case[0]]["sample_steps"] for case in PROFILE_CASES] != [4, 16, 32]:
        raise ValueError("production_profile_changed: expected first/medium/long CFM 4/16/32")
    return result


def _tree_identity(path):
    if not path.is_dir():
        raise ValueError("frontend_asset_directory_missing")
    files = sorted(item for item in path.rglob("*") if item.is_file())
    if not files:
        raise ValueError("frontend_asset_directory_empty")
    digest, total = hashlib.sha256(), 0
    for item in files:
        digest.update(item.relative_to(path).as_posix().encode())
        digest.update(bytes.fromhex(_sha(item)))
        total += item.stat().st_size
    return {"sha256": digest.hexdigest(), "files": len(files), "bytes": total}


def _versions():
    result = {}
    for package in DEPENDENCIES:
        try:
            result[package] = {"status": "available", "version": importlib.metadata.version(package)}
        except importlib.metadata.PackageNotFoundError:
            result[package] = {"status": "not_installed"}
    return result


def _installed_versions():
    # Bind the whole environment, including indirect frontend/acoustic packages;
    # distribution metadata exposes versions without install paths or URLs.
    return {distribution.metadata["Name"].lower(): distribution.version
            for distribution in importlib.metadata.distributions()
            if re.fullmatch(r"[A-Za-z0-9_.-]+", distribution.metadata.get("Name", ""))
            and re.fullmatch(r"[A-Za-z0-9.+_-]+", distribution.version)}


def _inference_source_sha():
    paths = [ROOT / "local_tts_infer.py"]
    for directory in ("tts", "GPT_SoVITS", "config", "tools/probes"):
        paths.extend((ROOT / directory).rglob("*.py"))
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(bytes.fromhex(_sha(path)))
    return digest.hexdigest()


def _variant_sources():
    from tools.probes.gsv_qualification_variants import source_identity

    return {variant: source_identity(backend, revision if backend == "mlx" else "current")
            for variant, (backend, _, revision) in VARIANT_SPECS.items()}


def _machine_environment():
    # The probe helper omits hostname/user identity. Project only known fields;
    # configured environment strings and raw pmset output remain private.
    original = _environment()
    result = {key: original.get(key) for key in (
        "os", "os_release", "macos_version", "architecture", "chip", "system_memory_bytes", "python")}
    chip = re.search(r"Apple M\d+(?: (?:Pro|Max|Ultra))?", str(result["chip"]))
    result["chip"] = chip.group() if chip else None
    result["chip_tier"] = (chip.group().split()[-1] if chip and chip.group().split()[-1]
                           in {"Pro", "Max", "Ultra"} else "base" if chip else "unknown")
    result.update(power_source="unknown", low_power_mode=None, power_detection="not_available")
    if original.get("os") == "Darwin":
        try:
            battery = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True,
                                     check=True, timeout=10).stdout
            power = subprocess.run(["pmset", "-g"], capture_output=True, text=True,
                                   check=True, timeout=10).stdout
            result["power_source"] = ("ac" if "'AC Power'" in battery else
                                      "battery" if "'Battery Power'" in battery else "unknown")
            low = re.search(r"\blowpowermode\s+([01])\b", power)
            result["low_power_mode"] = bool(int(low.group(1))) if low else None
            result["power_detection"] = "detected" if low and result["power_source"] != "unknown" else "partial"
        except (OSError, subprocess.SubprocessError):
            pass
    result["missing_fields"] = [key for key in ("chip", "macos_version", "system_memory_bytes")
                                if result.get(key) is None or result.get(key) == ""]
    result["execution_environment"] = {}
    for key in EXECUTION_ENV:
        raw = os.environ.get(key)
        valid = raw is not None and (raw in {"0", "1"} if key.startswith("PYTORCH_")
                                    else raw.isdigit() and int(raw) > 0)
        result["execution_environment"][key] = ({"status": "set", "value": raw} if valid else
                                                {"status": "unset"} if raw is None else {"status": "unrecognized_value"})
    if "MLX_METAL_FAST_MATH" in os.environ:
        result["unrecognized_user_env"] = ["MLX_METAL_FAST_MATH"]
    return result


def _binding(args, profiles, environment):
    base = Path(args.assets_root).resolve() if args.assets_root else ROOT
    pretrained = base / "assets" / "models" / "gpt-sovits" / "pretrained"
    assets = {key: _sha(path) for key, path in zip(
        ASSET_FIELDS, (args.checkpoint, args.sovits, args.reference_audio))}
    # v3 initialization also loads this base acoustic checkpoint when a selected
    # fine-tuned SoVITS checkpoint was supplied. It is a separate dependency.
    assets["pretrained_sovits_sha256"] = _sha(pretrained / "s2Gv3.pth")
    frontend = {name: _tree_identity(pretrained / name) for name in FRONTEND_DIRS}
    # Hash configuration, never its contents. Dotenv/environment changes must
    # invalidate a resume even if they do not change the decoder source digest.
    config_files = ("config/settings.py", "config/environment.py", "uv.lock", ".env")
    configuration = {name: _sha(ROOT / name) if (ROOT / name).is_file() else "absent"
                     for name in config_files}
    configured = {key: value for key, value in os.environ.items() if key.startswith(
        ("TTS_", "FIRST_SENTENCE_", "BIGVGAN_", "ENABLE_CUDA_", "OMP_", "MKL_", "OPENBLAS_", "PYTORCH_"))}
    configured.update(OFFLINE_ENV)
    if "MLX_METAL_FAST_MATH" in os.environ:
        configured["MLX_METAL_FAST_MATH"] = os.environ["MLX_METAL_FAST_MATH"]
    protocol = {key: getattr(args, key) for key in (
        "device", "smoke", "history_steps", "stage_runs", "fixed_blocks", "fixed_runs",
        "natural_blocks", "natural_runs", "soak_runs", "warmup", "seed", "chunk_seconds",
        "include_listening_wavs", "blind_seed", "timeout")}
    return {"schema": SCHEMA, "source": {**_identity(), "runner_sha256": _sha(__file__),
                                        "inference_source_sha256": _inference_source_sha()},
            "assets": assets, "frontend_assets": frontend,
            "reference_text_sha256": hashlib.sha256(args.reference_text.encode()).hexdigest(),
            "configuration_sha256": _digest({"files": configuration, "environment": configured}),
            "dependencies": _versions(), "installed_dependencies_sha256": _digest(_installed_versions()),
            "variant_sources": _variant_sources(),
            "machine": {key: environment[key] for key in (
                "os", "os_release", "macos_version", "architecture", "chip", "chip_tier",
                "system_memory_bytes", "python")}, "protocol": protocol, "profiles": profiles,
            "case_text_sha256": {name: hashlib.sha256(text.encode()).hexdigest()
                                 for name, text, _ in CASES},
            "smoke_protocol": ({"audio_text_sha256": hashlib.sha256(SMOKE_TEXT.encode()).hexdigest(),
                                "max_sec_override": SMOKE_MAX_SEC,
                                "cfm_steps": [4, 16, 32], "qualifying": False} if args.smoke else None)}


def _require(condition, code):
    if not condition:
        raise ValueError(code)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _nonnegative(value):
    return _number(value) and value >= 0


def _is_hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _check_execution(report, spec, binding, *, fixed=False, sampling=True):
    backend, dtype, revision = VARIANT_SPECS[spec["variant"]]
    execution = report.get("execution", {})
    _require(report.get("backend") == backend and execution.get("inference_dtype") == dtype
             and execution.get("mlx_revision") == revision, "child_variant_identity_mismatch")
    if sampling:
        _require(execution.get("sampler_dtype") == ("float32" if backend == "mlx" else dtype),
                 "child_sampler_dtype_mismatch")
    _require(all(execution.get(key) == value for key, value in binding["variant_sources"][spec["variant"]].items()),
             "child_variant_provenance_mismatch")
    if fixed:
        _require(execution.get("fixed_only") is True and report.get("free_generation_outputs") == [],
                 "fixed_benchmark_included_free_generation")


def _check_report(report, kind, spec, binding, fixture):
    _require(isinstance(report, dict) and report.get("schema") == "amadeus.gsv_mlx_probe.v1"
             and report.get("command") == kind, "child_report_schema_mismatch")
    allowed = "recorded" if kind == "validate" and spec["dtype"] == "float16" else "passed"
    _require(report.get("status") == allowed, "child_report_status_rejected")
    for key in IDENTITY_FIELDS:
        _require(key in report and report[key] == binding["source"][key], "child_source_identity_mismatch")
    _require(report.get("source_checkpoint_sha256") == binding["assets"][ASSET_FIELDS[0]],
             "child_checkpoint_identity_mismatch")
    child_environment = report.get("environment", {})
    for key in ("os", "os_release", "macos_version", "architecture", "python"):
        _require(key in child_environment and child_environment[key] == binding["machine"][key],
                 "child_environment_identity_mismatch")
    for package, child_key in (("torch", "torch"), ("torchaudio", "torchaudio"), ("mlx", "mlx"),
                               ("mlx-metal", "mlx_metal"), ("mlx-cpu", "mlx_cpu")):
        version = binding["dependencies"][package]
        _require(child_key in child_environment and child_environment[child_key] == version.get("version"),
                 "child_dependency_identity_mismatch")
    device = binding["protocol"]["device"]
    if kind == "doctor":
        _require(report.get("mlx_device") == device and report.get("dtype") == "float32",
                 "doctor_execution_mismatch")
        _require(all(binding["dependencies"][package]["status"] == "available"
                     for package in ("torch", "mlx", "numpy")), "required_dependency_version_missing")
        if device == "metal":
            _require(report.get("torch_mps_available") is True and binding["machine"]["os"] == "Darwin"
                     and binding["machine"]["architecture"] == "arm64" and binding["machine"]["chip"],
                     "metal_device_not_verified")
    elif kind == "export":
        _require(_is_hash(report.get("converted_weights_sha256")), "conversion_hash_missing")
    elif kind == "inputs":
        _require(report.get("fixture_kind") == "frontend_private"
                 and report.get("reference_audio_sha256") == binding["assets"][ASSET_FIELDS[2]]
                 and _is_hash(report.get("fixture_sha256"))
                 and type(report.get("fixed_decode_steps")) is int and report["fixed_decode_steps"] > 0
                 and type(report.get("reference_tokens")) is int and report["reference_tokens"] > 0,
                 "frontend_fixture_identity_or_length_mismatch")
    elif kind in {"validate", "bench"}:
        for key in ASSET_FIELDS:
            _require(report.get(key) == binding["assets"][key], "child_fixture_asset_mismatch")
        _require(report.get("inputs_sha256") == fixture["fixture_sha256"]
                 and report.get("fixed_decode_steps") == fixture["fixed_decode_steps"]
                 and report.get("reference_tokens") == fixture["reference_tokens"]
                 and report.get("fixture_kind") == "frontend_private", "child_fixture_identity_mismatch")
        if kind == "validate":
            _check_execution(report, spec, binding, sampling=False)
            rows = report.get("rows", [])
            logits = [row for row in rows if row.get("tensor") == "logits"]
            _require(report.get("inference_dtype") == spec["dtype"] and report.get("mlx_device") == device,
                     "validation_execution_mismatch")
            _require(rows and report.get("comparisons") == len(rows)
                     and all(row.get("finite") is True and _nonnegative(row.get("max_abs"))
                             and _nonnegative(row.get("rmse")) for row in rows)
                     and all(type(row.get("step")) is int and isinstance(row.get("tensor"), str)
                             and re.fullmatch(r"[A-Za-z0-9_.]+", row["tensor"]) for row in rows),
                     "validation_nonfinite_or_incomplete")
            _require(report.get("logit_steps") == len(logits) == fixture["fixed_decode_steps"] + 1
                     and report.get("top1_agreements") == sum(row.get("top1_agrees") is True for row in logits)
                     and _nonnegative(report.get("max_logit_abs")), "validation_logit_count_mismatch")
            _require(report.get("failed_comparisons") == [row for row in rows if row.get("passed") is not True]
                     and report.get("strict_tolerance_status") in {"passed", "failed"},
                     "validation_strict_evidence_missing")
            if spec["dtype"] == "float32":
                _require(report["top1_agreements"] == report["logit_steps"]
                         and report["strict_tolerance_status"] == "passed"
                         and not report["failed_comparisons"], "fp32_strict_gate_failed")
        else:
            _check_execution(report, spec, binding, fixed=True)
            backend = VARIANT_SPECS[spec["variant"]][0]
            _require(report.get("torch_device") == ("cpu" if device == "cpu" else "mps")
                     and report.get("mlx_device") == (device if backend == "mlx" else "not_applicable"),
                     "benchmark_variant_identity_mismatch")
            raw = report.get("raw_fixed_runs", [])
            n = fixture["fixed_decode_steps"]
            _require(len(raw) == spec["runs"] and report.get("warmup_runs_excluded") == spec["warmup"]
                     and report.get("measured_seed_start") == spec["seed"]
                     and len(report.get("per_position_ms", [])) == n, "benchmark_count_mismatch")
            _require(report.get("sampling_parameters") == spec["sampling"], "benchmark_sampling_mismatch")
            for index, row in enumerate(raw):
                _require(row.get("seed") == spec["seed"] + index
                         and all(row.get(key) == n for key in ("decode_calls", "sampler_calls", "host_stop_reads"))
                         and row.get("final_audio_length") == fixture["reference_tokens"] + n
                         and len(row.get("fixed_step_ms", [])) == n
                         and all(_nonnegative(value) for value in row["fixed_step_ms"])
                         and all(_nonnegative(row.get(key)) for key in (
                             "prefill_ms", "fixed_ar_work_ms", "final_drain_ms"))
                         and row["fixed_ar_work_ms"] >= row["final_drain_ms"]
                         and _is_hash(row.get("sampled_ids_sha256")), "benchmark_work_or_seed_mismatch")
    elif kind == "audio":
        _check_execution(report, spec, binding)
        backend, dtype, _ = VARIANT_SPECS[spec["variant"]]
        for key in ASSET_FIELDS:
            _require(report.get(key) == binding["assets"][key], "audio_asset_identity_mismatch")
        _require(report.get("backend") == backend and report.get("semantic", {}).get("semantic_backend") == backend
                 and report.get("semantic_dtype") == dtype and report.get("acoustic_dtype") == "float32"
                 and report.get("acoustic_device") == ("cpu" if device == "cpu" else "mps")
                 and report.get("sampler_dtype") == ("float32" if backend == "mlx" else dtype),
                 "audio_variant_identity_mismatch")
        _require(report.get("text_sha256") == spec["text_sha256"]
                 and report.get("prompt_text_sha256") == binding["reference_text_sha256"], "audio_text_identity_mismatch")
        parameters = report.get("parameters", {})
        _require(all(key in parameters and parameters[key] == value for key, value in spec["parameters"].items())
                 and type(parameters.get("semantic_guard")) is bool and report.get("controlled_acoustic_rng") is False,
                 "audio_profile_mismatch")
        _require(report.get("stage_profile") is spec["stage"] and report.get("soak_memory") is spec["soak"]
                 and report.get("worker_thread") is spec["soak"], "audio_instrumentation_mismatch")
        rows, warmup = report.get("runs", []), report.get("warmup_rows", [])
        _require(len(rows) == spec["runs"] and len(warmup) == spec["warmup"]
                 and report.get("warmup_runs_excluded") == spec["warmup"]
                 and report.get("measured_seed_start") == spec["seed"]
                 and report.get("cold_run") == warmup[0], "audio_count_or_cold_run_mismatch")
        for measured, items in ((True, rows), (False, warmup)):
            for index, row in enumerate(items):
                seed = spec["seed"] + index + (0 if measured else 1_000_000)
                _require(row.get("seed") == seed and all(_nonnegative(row.get(key)) for key in AUDIO_FIELDS
                         if key != "first_chunk_semantic_ms") and row["audio_seconds"] > 0
                         and row["first_emitted_chunk_ms"] <= row["total_synthesis_ms"], "audio_measurement_or_seed_invalid")
                _require(type(row.get("semantic_budget_boundary_segments")) is int
                         and 0 <= row["semantic_budget_boundary_segments"] <= row["semantic_segments"],
                         "audio_budget_boundary_evidence_missing")
                for key in ("stage_timings", "first_chunk_stage_timings"):
                    timings = row.get(key)
                    _require(isinstance(timings, list) and all(item.get("stage") in STAGES
                             and _nonnegative(item.get("elapsed_ms")) for item in timings), "audio_stage_evidence_invalid")
                if spec["stage"]:
                    _require(_nonnegative(row.get("first_chunk_semantic_ms"))
                             and {item["stage"] for item in row["stage_timings"]} >= {"frontend_bert", "cfm", "bigvgan"},
                             "audio_stage_evidence_missing")
                else:
                    _require(row.get("first_chunk_semantic_ms") is None
                             and row["stage_timings"] == [] and row["first_chunk_stage_timings"] == [],
                             "natural_audio_was_synchronized")
                if spec["soak"]:
                    memory = row.get("memory", {})
                    _require(all(key in memory and (memory[key] is None or _nonnegative(memory[key]))
                                 for key in MEMORY_FIELDS) and _nonnegative(memory["rss_bytes"]), "soak_memory_evidence_missing")
                    if device == "metal":
                        _require(all(_nonnegative(memory[key]) for key in MEMORY_FIELDS), "metal_memory_metric_unavailable")


def _stop_child(process):
    # Windows virtualenv launchers can own a real Python descendant. Stop only
    # this launched tree; POSIX children have a session dedicated to this run.
    if os.name == "nt":
        cleanup = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                 capture_output=True, timeout=10)
        if cleanup.returncode != 0 and process.poll() is None:
            raise RuntimeError("child_tree_cleanup_failed")
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=10)


def _run_child(command, *, timeout, env):
    process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, errors="replace", start_new_session=os.name != "nt")
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        _stop_child(process)
        # Draining the pipes also verifies the owned descendants have released
        # them. Any failure stays private and the qualification cannot resume
        # this step as complete.
        stdout, stderr = process.communicate(timeout=10)
        exc.stdout, exc.stderr = stdout, stderr
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


class Runner:
    def __init__(self, args, root, binding, environment):
        self.args, self.root, self.binding = args, root, binding
        self.started = time.perf_counter()
        self.state_path = root / "private" / "state.json"
        self.reports, self.specs, self.fixture, self.cohort = {}, {}, None, None
        fingerprint = _digest(binding)
        if self.state_path.exists():
            _require(args.resume, "output_has_state: use --resume or a new output directory")
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            _require(self.state.get("schema") == SCHEMA and self.state.get("binding_sha256") == fingerprint
                     and self.state.get("binding") == binding, "resume_binding_mismatch: code/assets/text/config/protocol/environment changed")
        else:
            _require(not args.resume, "resume_state_missing")
            _require(not any(root.iterdir()), "output_directory_not_empty")
            self.state = {"schema": SCHEMA, "binding": binding, "binding_sha256": fingerprint,
                          "started_utc": datetime.now(timezone.utc).isoformat(), "steps": {}, "sessions": []}
        self.state["sessions"].append({"started_utc": datetime.now(timezone.utc).isoformat(),
                                       "elapsed_seconds": 0, "environment": environment})
        self._save()

    def _save(self):
        self.state["sessions"][-1]["elapsed_seconds"] = time.perf_counter() - self.started
        _atomic_json(self.state_path, self.state)

    def step(self, name, kind, options, spec, artifacts=()):
        folder = self.root / "private" / "runs"
        folder.mkdir(parents=True, exist_ok=True)
        report_path, log_path = folder / f"{name}.json", folder / f"{name}.log"
        command = [sys.executable, str(PROBE), kind, *map(str, options), "--output", str(report_path)]
        signature = _digest({"command_sha256": _digest(command), "spec": spec})
        previous = self.state["steps"].get(name)
        self.specs[name] = {"kind": kind, **spec}
        if previous and previous.get("status") == "complete":
            _require(previous.get("signature") == signature, f"resume_step_mismatch:{name}")
            _require(report_path.is_file() and _sha(report_path) == previous.get("report_sha256"),
                     f"resume_report_tampered:{name}")
            _require(len(previous.get("artifacts", [])) == len(artifacts), f"resume_artifact_count_mismatch:{name}")
            for path, recorded in zip(artifacts, previous["artifacts"]):
                _require(path.is_file() and _sha(path) == recorded, f"resume_artifact_tampered:{name}")
            report = json.loads(report_path.read_text(encoding="utf-8"))
            _check_report(report, kind, spec, self.binding, self.fixture)
            self.reports[name] = report
            print(f"Reuse {name}", flush=True)
            return report
        # Failed/partial reports have no authority. Remove them before launching
        # the child so even a failed exit cannot make an old success reusable.
        for path in (report_path, *artifacts):
            path.unlink(missing_ok=True)
        self.state["steps"][name] = {"status": "running", "signature": signature}
        self._save()
        print(f"Run {name}", flush=True)
        started = time.perf_counter()
        try:
            child = _run_child(command, timeout=self.args.timeout, env={**os.environ, **OFFLINE_ENV})
            log_path.write_text(child.stdout + "\n" + child.stderr, encoding="utf-8")
            _require(child.returncode == 0, "child_exit_failed")
            _require(report_path.is_file(), "child_report_missing")
            report = json.loads(report_path.read_text(encoding="utf-8"))
            _check_report(report, kind, spec, self.binding, self.fixture)
            _require(all(path.is_file() and path.stat().st_size > 0 for path in artifacts), "child_artifact_missing")
            if kind == "inputs":
                _require(report["fixture_sha256"] == _sha(artifacts[0]), "frontend_fixture_hash_mismatch")
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
            self.state["steps"][name].update(status="failed", reason=(str(exc) if isinstance(exc, ValueError)
                                                                   else type(exc).__name__))
            log_path.write_text((getattr(exc, "stdout", "") or "") + "\n"
                                + (getattr(exc, "stderr", "") or "") + "\n" + str(exc), encoding="utf-8")
            self._save()
            raise ValueError(f"qualification_step_failed:{name}; inspect private log locally") from exc
        self.state["steps"][name].update(status="complete", report_sha256=_sha(report_path),
                                         artifacts=[_sha(path) for path in artifacts],
                                         elapsed_seconds=time.perf_counter() - started)
        self.reports[name] = report
        self._save()
        return report


def _assets(args):
    return ["--checkpoint", Path(args.checkpoint).resolve(), "--sovits", Path(args.sovits).resolve(),
            "--reference-audio", Path(args.reference_audio).resolve()]


def _variant_options(variant):
    backend, dtype, revision = VARIANT_SPECS[variant]
    options = ["--backend", backend, "--inference-dtype", dtype]
    if backend == "mlx":
        options += ["--mlx-revision", revision]
    return options


def _audio_options(args, case, profile, variant, runs, seed, *, stage=False, soak=False, wav=None):
    backend = VARIANT_SPECS[variant][0]
    text = SMOKE_TEXT if args.smoke else case[1]
    effective_profile = {**profile}
    if args.smoke:
        # Keep the real frontend/acoustic path and CFM steps, but bound output.
        effective_profile.update(max_sec_override=SMOKE_MAX_SEC, how_to_cut="不切")
    parameters = {**effective_profile, "chunk_seconds": args.chunk_seconds if args.chunk_seconds > 0 else None}
    options = _assets(args) + _variant_options(variant) + [
               "--acoustic-device", "cpu" if args.device == "cpu" else "mps",
               "--reference-text", args.reference_text, "--text", text, "--runs", runs,
               "--warmup", args.warmup, "--seed", seed, "--chunk-seconds", args.chunk_seconds]
    for key, value in effective_profile.items():
        if value is not None:
            options += ["--" + key.replace("_", "-"), value]
    if args.assets_root:
        options += ["--assets-root", Path(args.assets_root).resolve()]
    if args.device == "cpu" and backend == "mlx":
        options += ["--probe-mlx-cpu"]
    if stage:
        options += ["--stage-profile"]
    if soak:
        options += ["--soak-memory"]
    if wav:
        options += ["--wav", wav]
    spec = {"variant": variant, "case": case[0], "parameters": parameters, "runs": runs,
            "seed": seed, "warmup": args.warmup, "stage": stage, "soak": soak,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest()}
    return options, spec


def _fixed_summaries(reports):
    result = {}
    for variant in VARIANTS:
        rows = [row for name, report in reports.items()
                if name.startswith("fixed_") and report.get("backend") == VARIANT_SPECS[variant][0]
                and name.endswith("_" + variant) for row in report["raw_fixed_runs"]]
        result[variant] = {"runs": len(rows), "fixed_ar_work_ms": _stats([row["fixed_ar_work_ms"] for row in rows]),
                           "prefill_ms": _stats([row["prefill_ms"] for row in rows]), "raw_runs": rows}
    return result


def select_cohort(fixed, *, qualifying):
    """Choose from every MLX revision; smoke never selects by elapsed time."""
    if not qualifying:
        return {"candidate": "mlx_padded_fp32", "selection_basis": "deterministic_smoke_coverage",
                "e2e_variants": ["torch_fp32", "mlx_unpadded_fp32", "mlx_padded_fp32"],
                "torch_fp16_fraction_of_best_mlx_work_saved": None}
    _require(all(fixed[variant]["fixed_ar_work_ms"] for variant in VARIANTS), "fixed_selection_incomplete")
    best = min(MLX_VARIANTS, key=lambda variant: fixed[variant]["fixed_ar_work_ms"]["p50"])
    baseline, fastest = fixed["torch_fp32"]["fixed_ar_work_ms"]["p50"], fixed[best]["fixed_ar_work_ms"]["p50"]
    _require(baseline > 0 and fastest > 0, "fixed_selection_nonpositive_work")
    saved = baseline - fastest
    fraction = (baseline - fixed["torch_fp16"]["fixed_ar_work_ms"]["p50"]) / saved if saved > 0 else None
    needed = {"torch_fp32", best, *(variant for pair in PADDING_PAIRS.values() for variant in pair)}
    if fraction is not None and fraction >= 0.8:
        needed.add("torch_fp16")
    return {"candidate": best, "selection_basis": "all_seven_fixed_history_decode_p50",
            "e2e_variants": [variant for variant in VARIANTS if variant in needed],
            "torch_fp16_fraction_of_best_mlx_work_saved": fraction}


def _measure(runner, profiles):
    args = runner.args
    runner.step("doctor", "doctor", ["--device", args.device, "--checkpoint", Path(args.checkpoint).resolve(),
                                    "--inference-dtype", "float32"], {})
    runner.step("export", "export", ["--checkpoint", Path(args.checkpoint).resolve()], {})
    fixture = runner.root / "private" / "inputs.npz"
    options = _assets(args) + ["--acoustic-device", "cpu" if args.device == "cpu" else "mps",
              "--reference-text", args.reference_text, "--text", PROFILE_CASES[0][1],
              "--history-steps", args.history_steps, "--fixture-output", fixture]
    if args.assets_root:
        options += ["--assets-root", Path(args.assets_root).resolve()]
    runner.fixture = runner.step("inputs", "inputs", options, {}, (fixture,))
    for variant in MLX_VARIANTS:
        _, dtype, revision = VARIANT_SPECS[variant]
        options = _assets(args) + ["--inputs", fixture, "--device", args.device,
                                   "--inference-dtype", dtype, "--mlx-revision", revision]
        if dtype == "float16":
            options += ["--characterize"]
        runner.step(f"validate_{variant}", "validate", options, {"dtype": dtype, "variant": variant})
    first_profile = profiles[PROFILE_CASES[0][0]]
    sampling = {key: first_profile[key] for key in ("top_k", "top_p", "temperature")}
    sampling.update(repetition_penalty=1.35, early_stop_num=runner.fixture["fixed_decode_steps"])
    for block in range(args.fixed_blocks):
        seed = args.seed + block * args.fixed_runs
        for variant in variant_order(block):
            options = _assets(args) + _variant_options(variant) + ["--fixed-only", "--device", args.device,
                       "--inputs", fixture, "--runs", args.fixed_runs, "--warmup", args.warmup, "--seed", seed,
                       "--budget", runner.fixture["fixed_decode_steps"], "--top-k", sampling["top_k"],
                       "--top-p", sampling["top_p"], "--temperature", sampling["temperature"],
                       "--repetition-penalty", sampling["repetition_penalty"]]
            runner.step(f"fixed_b{block}_{variant}", "bench", options, {"variant": variant, "block": block,
                        "runs": args.fixed_runs, "warmup": args.warmup, "seed": seed, "sampling": sampling})
    for case in PROFILE_CASES:
        options, spec = _audio_options(args, case, profiles[case[0]], "torch_fp32", args.stage_runs,
                                      args.seed + 2_000_000, stage=True)
        runner.step(f"stage_{case[0]}", "audio", options, spec)
    cohort = select_cohort(_fixed_summaries(runner.reports), qualifying=args.device == "metal" and not args.smoke)
    runner.cohort = cohort
    for block in range(args.natural_blocks):
        seed = args.seed + 3_000_000 + block * args.natural_runs
        for variant in variant_order(block, cohort["e2e_variants"]):
            name = f"natural_b{block}_{variant}"
            wav = runner.root / "private" / "wavs" / f"{name}.wav" if args.include_listening_wavs else None
            options, spec = _audio_options(args, PROFILE_CASES[0], profiles[PROFILE_CASES[0][0]], variant,
                                          args.natural_runs, seed, wav=wav)
            artifacts = tuple(wav.with_name(f"{wav.stem}-{index+1}.wav")
                              for index in range(args.natural_runs)) if wav else ()
            runner.step(name, "audio", options, {**spec, "block": block}, artifacts)
    if args.include_listening_wavs:
        # The first case reuses the natural block's first normal WAV. Add one
        # normal sample per other case/variant to cover intelligibility/endings
        # without presenting repeated short filler as a voice quality screen.
        for case_index, case in enumerate(CASES[1:], start=1):
            for variant in ("torch_fp32", cohort["candidate"]):
                name = f"listening_{case[0]}_{variant}"
                wav = runner.root / "private" / "wavs" / f"{name}.wav"
                options, spec = _audio_options(args, case, profiles[case[0]], variant, 1,
                                              args.seed + 5_000_000 + case_index * 1000, wav=wav)
                runner.step(name, "audio", options, spec, (wav.with_name(f"{wav.stem}-1.wav"),))
    # The selected candidate's full mixed chain stays on one inferencer and one worker
    # for the entire steady-state soak; stage synchronization stays disabled.
    variant = cohort["candidate"]
    options, spec = _audio_options(args, PROFILE_CASES[0], first_profile, variant,
                                  args.soak_runs, args.seed + 4_000_000, soak=True)
    runner.step(f"soak_{variant}", "audio", options, spec)


def _stats(values):
    return _percentiles(values) if values else None


def _audio_row(row):
    safe = {key: row[key] for key in AUDIO_FIELDS}
    for key in ("stage_timings", "first_chunk_stage_timings"):
        safe[key] = [{"stage": value["stage"], "elapsed_ms": value["elapsed_ms"]} for value in row[key]]
    if row.get("memory") is not None:
        safe["memory"] = {key: row["memory"][key] for key in MEMORY_FIELDS}
    return safe


def _public_report(report, spec):
    # Only known, validated identities/measurements enter the archive. Never
    # copy the child JSON, command, environment strings, errors, or free text.
    result = {"phase": spec["kind"], "status": report["status"],
              **{key: report[key] for key in IDENTITY_FIELDS}}
    if spec["kind"] in {"validate", "bench", "audio"}:
        result["execution"] = {key: report["execution"][key] for key in (
            "inference_dtype", "sampler_dtype", "mlx_revision", "model_source_commit", "source_kind", "source_sha256")
            if key in report["execution"]}
    if spec["kind"] == "validate":
        result["variant"] = spec["variant"]
        for key in ("inference_dtype", "strict_tolerance_status", "comparisons", "logit_steps", "top1_agreements", "max_logit_abs"):
            result[key] = report[key]
        result["rows"] = [{key: row[key] for key in VALIDATION_FIELDS if key in row} for row in report["rows"]]
        result["failed_comparisons"] = [row for row in result["rows"] if row["passed"] is not True]
    elif spec["kind"] == "bench":
        result.update(variant=spec["variant"], block=spec["block"],
                      raw_fixed_runs=[{key: row[key] for key in FIXED_FIELDS} for row in report["raw_fixed_runs"]],
                      per_position_ms=[_stats([row["fixed_step_ms"][index] for row in report["raw_fixed_runs"]])
                                       for index in range(report["fixed_decode_steps"])])
    elif spec["kind"] == "audio":
        result.update(variant=spec["variant"], case=spec["case"], stage_profile=spec["stage"],
                      soak_memory=spec["soak"], runs=[_audio_row(row) for row in report["runs"]],
                      warmup_rows=[_audio_row(row) for row in report["warmup_rows"]],
                      cold_run=_audio_row(report["cold_run"]))
    elif spec["kind"] == "inputs":
        result.update({key: report[key] for key in ("fixture_sha256", "fixed_decode_steps", "reference_tokens", "phone_length")})
    elif spec["kind"] == "export":
        result["converted_weights_sha256"] = report["converted_weights_sha256"]
    elif spec["kind"] == "doctor":
        result.update(mlx_device=report["mlx_device"], torch_mps_available=report["torch_mps_available"])
    return result


def _memory_summary(rows):
    result = {"assessment": "human_review_required; allocator plateaus and endpoint deltas do not prove stability",
              "curves": [{"request": index + 1, **{key: row["memory"][key] for key in MEMORY_FIELDS}}
                         for index, row in enumerate(rows)], "metrics": {}}
    for key in MEMORY_FIELDS:
        values = [row["memory"][key] for row in rows]
        if any(value is None for value in values):
            result["metrics"][key] = {"status": "unavailable"}
            continue
        x_mean, y_mean = (len(values) - 1) / 2, statistics.mean(values)
        divisor = sum((index - x_mean)**2 for index in range(len(values)))
        slope = sum((index - x_mean) * (value - y_mean) for index, value in enumerate(values)) / divisor if divisor else None
        windows = [values[index * len(values)//4:(index + 1) * len(values)//4] for index in range(4)]
        result["metrics"][key] = {"status": "recorded", "distribution": _stats(values),
                                   "linear_slope_bytes_per_request": slope,
                                   "quarter_means_bytes": [statistics.mean(window) if window else None for window in windows]}
    return result


def padding_assessment(fixed, natural, *, qualifying):
    """Padding is useful only when both paired decode and first chunk improve."""
    result = {"source_mutation_performed": False, "other_optimizations": "retained",
              "recommendation": "not_assessed", "by_dtype": {}}
    if not qualifying:
        result["reason"] = "cpu_or_smoke_or_incomplete"
        return result
    for dtype, (unpadded, padded) in PADDING_PAIRS.items():
        decode = {name: fixed[name]["fixed_ar_work_ms"]["p50"] for name in (unpadded, padded)}
        first = {name: natural[name]["first_emitted_chunk_ms"]["p50"] for name in (unpadded, padded)}
        decode_better, first_better = decode[padded] < decode[unpadded], first[padded] < first[unpadded]
        result["by_dtype"][dtype] = {
            "unpadded_variant": unpadded, "padded_variant": padded,
            "decode_p50_ms": decode, "first_chunk_p50_ms": first,
            "decode_p50_improved": decode_better, "first_chunk_p50_improved": first_better,
            "recommendation": "retain" if decode_better and first_better else "remove"}
    result["recommendation"] = "pending_deployment_dtype"
    result["scope"] = "padding_only; owner_reviews_evidence_before_any_source_change"
    return result


def _aggregate(runner, completed):
    binding, args = runner.binding, runner.args
    qualifying = completed and args.device == "metal" and not args.smoke
    public = {name: _public_report(report, runner.specs[name]) for name, report in runner.reports.items()}
    fixed, natural, stage, soak = {}, {}, {}, {}
    for variant in VARIANTS:
        fixed_rows = [row for name, report in public.items() if name.startswith("fixed_")
                      and report["variant"] == variant for row in report["raw_fixed_runs"]]
        audio_rows = [row for name, report in public.items() if name.startswith("natural_")
                      and report["variant"] == variant for row in report["runs"]]
        fixed[variant] = {"runs": len(fixed_rows), "fixed_ar_work_ms": _stats([row["fixed_ar_work_ms"] for row in fixed_rows]),
                          "prefill_ms": _stats([row["prefill_ms"] for row in fixed_rows]),
                          "per_position_ms": [_stats([row["fixed_step_ms"][index] for row in fixed_rows])
                                              for index in range(runner.fixture["fixed_decode_steps"])] if fixed_rows else [],
                          "raw_runs": fixed_rows}
        natural[variant] = {"runs": len(audio_rows), "first_emitted_chunk_ms": _stats([row["first_emitted_chunk_ms"] for row in audio_rows]),
                            "total_synthesis_ms": _stats([row["total_synthesis_ms"] for row in audio_rows]), "raw_runs": audio_rows}
    for name, report in public.items():
        if name.startswith("stage_"):
            stage[report["case"]] = {"cfm_steps": runner.specs[name]["parameters"]["sample_steps"],
                                      "cold_run": report["cold_run"], "warmup_rows": report["warmup_rows"],
                                      "runs": report["runs"], "first_chunk_semantic_fraction": _stats([
                                          row["first_chunk_semantic_ms"] / row["first_emitted_chunk_ms"]
                                          for row in report["runs"] if row["first_emitted_chunk_ms"] > 0])}
        if name.startswith("soak_"):
            soak[report["variant"]] = _memory_summary(report["runs"])
    paired = []
    cohort = runner.cohort
    natural_variants = cohort["e2e_variants"] if cohort else [variant for variant in VARIANTS if natural[variant]["runs"]]
    if completed:
        _require(cohort is not None and all(fixed[variant]["runs"] == args.fixed_blocks * args.fixed_runs
                                           for variant in VARIANTS), "completed_fixed_protocol_missing")
        fixed_seeds = [row["seed"] for row in fixed["torch_fp32"]["raw_runs"]]
        _require(len(set(fixed_seeds)) == len(fixed_seeds) and all(
            [row["seed"] for row in fixed[variant]["raw_runs"]] == fixed_seeds for variant in VARIANTS),
            "fixed_paired_seeds_mismatch")
        _require(all(natural[variant]["runs"] == args.natural_blocks * args.natural_runs for variant in natural_variants)
                 and all(len(stage[case[0]]["runs"]) == args.stage_runs for case in PROFILE_CASES)
                 and len(soak[cohort["candidate"]]["curves"]) == args.soak_runs, "completed_audio_protocol_missing")
    if "torch_fp32" in natural_variants and all(natural[variant]["runs"] for variant in natural_variants):
        indexed = {variant: {row["seed"]: row for row in natural[variant]["raw_runs"]} for variant in natural_variants}
        baseline_seeds = list(indexed["torch_fp32"])
        if completed:
            _require(all(len(indexed[variant]) == natural[variant]["runs"]
                         and list(indexed[variant]) == baseline_seeds for variant in natural_variants),
                     "natural_paired_seeds_mismatch")
        paired = [{"seed": seed, "first_emitted_chunk_ms": {variant: indexed[variant][seed]["first_emitted_chunk_ms"]
                                                             for variant in natural_variants}} for seed in baseline_seeds
                  if all(seed in indexed[variant] for variant in natural_variants)]
    suggestions = {"eligibility": "metal_measurements_pending_review" if qualifying else "nonqualifying_cpu_or_smoke_or_incomplete",
                   "product_promotion": "pending_owner_review", "memory_gate": "pending_full_curve_review",
                   "human_quality_gate": "pending_blind_listening_and_asr", "candidates": {}}
    if qualifying:
        baseline = fixed["torch_fp32"]["fixed_ar_work_ms"]["p50"]
        first = natural["torch_fp32"]["first_emitted_chunk_ms"]
        candidates = [variant for variant in natural_variants if variant in MLX_VARIANTS]
        for variant in candidates:
            work, latency = fixed[variant]["fixed_ar_work_ms"]["p50"], natural[variant]["first_emitted_chunk_ms"]
            suggestions["candidates"][variant] = {"fixed_p50_speedup": baseline/work,
                "fixed_speedup_at_least_2x": baseline/work >= 2,
                "first_chunk_p50_reduction_fraction": 1-latency["p50"]/first["p50"],
                "first_chunk_p50_reduction_at_least_15_percent": latency["p50"] <= first["p50"] * 0.85,
                "first_chunk_p95_no_worse": latency["p95"] <= first["p95"]}
        best = cohort["candidate"]
        fraction = cohort["torch_fp16_fraction_of_best_mlx_work_saved"]
        suggestions.update(best_fixed_mlx_variant=best, torch_fp16_fraction_of_best_mlx_work_saved=fraction,
                           torch_fp16_captures_at_least_80_percent=(fraction >= 0.8 if fraction is not None else None),
                           stage_semantic_below_25_percent=stage[PROFILE_CASES[0][0]]["first_chunk_semantic_fraction"]["p50"] < 0.25)
    elapsed = sum(session["elapsed_seconds"] for session in runner.state["sessions"])
    result = {"schema": SCHEMA, "status": "recorded_review_pending" if qualifying else "recorded_nonqualifying" if completed else "failed",
              "device": args.device, "smoke": args.smoke, "metal_speed_gate": "measured_pending_review" if qualifying else "not_qualified",
              "binding_sha256": runner.state["binding_sha256"], **binding,
              "sessions": runner.state["sessions"], "active_elapsed_seconds": elapsed,
              "elapsed_since_start_seconds": (datetime.now(timezone.utc) - datetime.fromisoformat(runner.state["started_utc"])).total_seconds(),
              "runtime_target_seconds": 45 * 60, "runtime_target_met": elapsed <= 45 * 60 if qualifying else None,
              "runtime_target_is_guarantee": False, "fixture": ({key: runner.fixture[key] for key in (
                  "fixture_sha256", "fixed_decode_steps", "reference_tokens", "phone_length")} if runner.fixture else None),
              "completed_steps": list(public), "failed_steps": [name for name, item in runner.state["steps"].items() if item["status"] != "complete"],
              "stage_profiles": stage, "fixed_model": fixed, "natural_first_chunk": natural,
              "natural_pairs": paired, "soak": soak, "suggested_numeric_gates": suggestions,
              "selection": cohort if completed else {"candidate": None, "selection_basis": "incomplete"},
              "padding_assessment": padding_assessment(fixed, natural, qualifying=qualifying),
              "chunk_seconds_override": args.chunk_seconds if args.chunk_seconds > 0 else None,
              "human_listening": "pending", "asr": "pending", "blind_kit": {"status": "not_requested"},
              "limitations": ["CPU smoke is functional evidence only; it has no speed selection or performance conclusions.",
                  "Smoke replaces audio texts with a short case and bounds generation; budget flags are retained, utterance quality is not qualified.",
                  "Stage profiles synchronize diagnostics; natural latency has diagnostic synchronization disabled.",
                  "Runtime/session helpers are disabled; explicit real full-chain requests warm every process, including MLX semantics.",
                  "The first explicit request is reported separately; model load is excluded and loader kernels may already have run.",
                  "Fixed work uses the actual shared fixture N and includes final drain; pipeline intervals must not be summed as total work.",
                  "Unconstrained audio token counts can differ between variants; first emitted infer_stream chunks are measured directly.",
                  "Mixed soak uses one persistent inferencer/worker for the selected candidate with sequential T2S then acoustic work; concurrent command-buffer stress is untested.",
                  "Memory and listening gates require review; this runner never promotes a product backend or dtype."]}
    return result, public


def _blind_kit(runner, share):
    blind = random.Random(runner.args.blind_seed)
    target = share / "listening"
    target.mkdir(parents=True, exist_ok=True)
    answers, rows, files = {}, [], []
    for case_index, case in enumerate(CASES):
        pair_id = f"pair_{len(rows)+1:04d}"
        variants = ["torch_fp32", runner.cohort["candidate"]]
        blind.shuffle(variants)
        for label, variant in zip(("A", "B"), variants):
            stem = f"listening_{case[0]}_{variant}" if case_index else f"natural_b0_{variant}"
            source = runner.root / "private" / "wavs" / f"{stem}-1.wav"
            name = f"{pair_id}_{label}.wav"
            shutil.copyfile(source, target / name)
            files.append(target / name)
        answers[pair_id] = {"A": variants[0], "B": variants[1], "case": case[0], "run": 1}
        rows.append({"pair_id": pair_id, "expected_text": SMOKE_TEXT if runner.args.smoke else case[1],
                     "A_file": f"{pair_id}_A.wav", "B_file": f"{pair_id}_B.wav", "preference": "",
                     "intelligibility": "", "timbre": "", "prosody": "", "artifacts": "", "notes": ""})
    sheet = target / "review.csv"
    with sheet.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    files.append(sheet)
    _atomic_json(runner.root / "private" / "listening-answer-key.json", answers)
    return {"status": "human_listening_pending", "pairs": len(rows), "cases": len(CASES),
            "scope": "smoke_functional_samples_only" if runner.args.smoke else "five_case_normal_sampling_samples",
            "answer_key": "kept_private_outside_share_archive"}, files


def _package(runner, completed):
    runner._save()
    aggregate, reports = _aggregate(runner, completed)
    share = runner.root / "share"
    share.mkdir(parents=True, exist_ok=True)
    files = []
    if completed and runner.args.include_listening_wavs:
        aggregate["blind_kit"], files = _blind_kit(runner, share)
    for name, report in reports.items():
        path = share / "reports" / f"{name}.json"
        _atomic_json(path, report)
        files.append(path)
    aggregate_path = share / "qualification.json"
    _atomic_json(aggregate_path, aggregate)
    files.append(aggregate_path)
    # Build a fresh archive from this explicit file list, never walking output/.
    # A stray log, checkpoint, reference, NPZ, answer key, or .env cannot enter.
    archive = runner.root / "share.zip"
    temporary = archive.with_name(".share.zip.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for path in files:
                bundle.write(path, arcname=path.relative_to(share).as_posix())
        os.replace(temporary, archive)
    finally:
        temporary.unlink(missing_ok=True)
    return aggregate


def run(args):
    _require(args.device != "cpu" or args.smoke, "cpu_requires_smoke_functional_protocol")
    _require(args.warmup >= 1 and args.history_steps >= 1 and args.timeout > 0
             and args.chunk_seconds >= 0 and all(getattr(args, key) >= 1 for key in (
                 "stage_runs", "fixed_blocks", "fixed_runs", "natural_blocks", "natural_runs", "soak_runs")),
             "counts_must_be_positive_and_full_chain_warmup_required")
    _require(args.fixed_blocks % 2 == 0 and args.natural_blocks % 2 == 0, "forward_reverse_blocks_must_be_paired")
    if not args.smoke:
        _require(args.stage_runs >= 10 and args.fixed_blocks >= 2 and args.fixed_blocks * args.fixed_runs >= 20
                 and args.natural_blocks >= 2 and args.natural_blocks * args.natural_runs >= 20
                 and args.soak_runs >= 100, "formal_protocol_requires_10_stage_20_fixed_20_pairs_100_soak")
    _require(bool(args.reference_text.strip()), "reference_text_required")
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    profiles = _profiles()
    environment = _machine_environment()
    binding = _binding(args, profiles, environment)
    runner = Runner(args, root, binding, environment)
    try:
        _measure(runner, profiles)
        aggregate = _package(runner, True)
    except (ValueError, OSError, KeyboardInterrupt):
        _package(runner, False)
        raise
    print(f"Qualification {aggregate['status']}; local share.zip ready; active time {aggregate['active_elapsed_seconds']:.1f}s", flush=True)
    return aggregate


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--checkpoint", required=True, help="Trusted local GPT v3 checkpoint, including public s1v3.ckpt")
    result.add_argument("--sovits", required=True, help="Trusted local v3 acoustic checkpoint, including public s2Gv3.pth")
    result.add_argument("--reference-audio", required=True, help="Authorized local reference; never put in the share ZIP")
    result.add_argument("--reference-text", required=True, help="Japanese reference transcript; retained only as a hash")
    result.add_argument("--assets-root", default="", help="Installation root containing assets/models/gpt-sovits/pretrained")
    result.add_argument("--output", required=True, help="Local output directory; share.zip is the sanitized result")
    result.add_argument("--device", choices=("cpu", "metal"), default="metal")
    result.add_argument("--resume", action="store_true", help="Continue only the identical code/assets/config/protocol")
    result.add_argument("--smoke", action="store_true", help="Reduced counts, always nonqualifying")
    result.add_argument("--include-listening-wavs", action="store_true", help="Opt-in export of anonymous synthesized WAVs for an authorized voice")
    result.add_argument("--stage-runs", type=int, default=None)
    result.add_argument("--fixed-blocks", type=int, default=None)
    result.add_argument("--fixed-runs", type=int, default=None)
    result.add_argument("--natural-blocks", type=int, default=None)
    result.add_argument("--natural-runs", type=int, default=None)
    result.add_argument("--soak-runs", type=int, default=None)
    result.add_argument("--history-steps", type=int, default=None)
    result.add_argument("--warmup", type=int, default=None, help="Explicit full-chain requests per process; first is cold")
    result.add_argument("--seed", type=int, default=17000)
    result.add_argument("--blind-seed", type=int, default=1937)
    result.add_argument("--chunk-seconds", type=float, default=0.0, help="Optional explicit chunk override; zero preserves production profiles")
    result.add_argument("--timeout", type=float, default=1200, help="Per-child timeout seconds; no automatic retry")
    return result


def parse_args(argv=None):
    args = parser().parse_args(argv)
    defaults = {"stage_runs": 10, "fixed_blocks": 2, "fixed_runs": 10, "natural_blocks": 2,
                "natural_runs": 10, "soak_runs": 100, "history_steps": 32, "warmup": 2}
    smoke = {**defaults, "stage_runs": 1, "fixed_runs": 1, "natural_runs": 1, "soak_runs": 2,
             "history_steps": 2, "warmup": 1}
    for key, value in (smoke if args.smoke else defaults).items():
        if getattr(args, key) is None:
            setattr(args, key, value)
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        return run(args)
    except (ValueError, OSError, KeyboardInterrupt) as exc:
        # Exception details can contain local paths. Those stay in private logs.
        reason = str(exc).split(";")[0] if isinstance(exc, ValueError) else type(exc).__name__
        print(f"Qualification stopped: {reason}. No upload occurred.", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
