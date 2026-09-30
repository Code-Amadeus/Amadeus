"""Local-only GPT-SoVITS v3 semantic backend qualification.

CPU results are numerical evidence. Metal/MPS and audible output must be run
on Apple Silicon. Reports contain hashes, shapes, timings, and status only;
frontend .npz inputs and WAV files stay in the caller's private output folder.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import random
import statistics
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _git(*args):
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _identity():
    from tts.semantic_mlx.weights import REFERENCE_SHA, OMINIX_SHA

    try:
        candidate = _git("rev-parse", "HEAD")
        dirty = bool(_git("status", "--porcelain"))
    except (OSError, subprocess.CalledProcessError):
        candidate, dirty = None, None
    return {
        "base_sha": REFERENCE_SHA,
        "candidate_sha": candidate,
        "working_tree_dirty": dirty,
        "code_sha256": _code_sha256(),
        "ominix_reference_sha": OMINIX_SHA,
    }


def _code_sha256():
    paths = (
        "local_tts_infer.py", "tts/semantic_runtime.py",
        "tts/semantic_mlx/weights.py", "tts/semantic_mlx/model.py",
        "tts/semantic_mlx/generation.py", "tts/semantic_mlx/runtime.py",
        "tools/probes/gsv_backend_probe.py", "tts/pipeline.py",
    )
    digest = hashlib.sha256()
    for relative in paths:
        digest.update(relative.encode("utf-8"))
        digest.update(bytes.fromhex(_sha(ROOT / relative)))
    return digest.hexdigest()


def _output(args):
    if args.output:
        path = Path(args.output)
        return path if path.suffix == ".json" else path / "report.json"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ROOT / "output" / "diagnostics" / "gsv-mlx-t2s" / stamp / "report.json"


def _environment():
    def version(package):
        try:
            return importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            return None

    try:
        import psutil
        memory_bytes = psutil.virtual_memory().total
    except ImportError:
        memory_bytes = None
    chip = None
    if platform.system() == "Darwin":
        try:
            chip = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                                  capture_output=True, text=True, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            chip = platform.processor() or None
    torch_module = sys.modules.get("torch")
    return {"os": platform.system(), "os_release": platform.release(),
            "architecture": platform.machine(), "chip": chip,
            "system_memory_bytes": memory_bytes,
            "python": platform.python_version(), "torch": version("torch"),
            "torchaudio": version("torchaudio"), "mlx": version("mlx"),
            "mlx_metal": version("mlx-metal"), "mlx_cpu": version("mlx-cpu"),
            "torch_num_threads": (torch_module.get_num_threads()
                                  if torch_module is not None else None),
            "configured_thread_environment": {
                key: os.environ.get(key) for key in
                ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS")}}


def _save(args, data):
    report = {"schema": "amadeus.gsv_mlx_probe.v1", "command": args.command,
              "utc": datetime.now(timezone.utc).isoformat(), **_identity(),
              "environment": _environment(), **data}
    path = _output(args)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{args.command}: {report.get('status', 'recorded')} -> {path}")
    return report


def _failure_reason(exc):
    message = str(exc)
    if "Apple Silicon macOS" in message:
        return "requires_apple_silicon", "Run Metal qualification on Apple Silicon macOS; use --device cpu for numerical tests"
    if "MLX Metal" in message or "MLX metal" in message:
        return "mlx_metal_unavailable", "Install working mlx-t2s dependencies and use TTS_DEVICE=mps"
    if "Torch MPS" in message:
        return "torch_mps_unavailable", "Use a Torch build with working MPS for the acoustic v3 path"
    if "mlx-t2s dependencies" in message or isinstance(exc, ImportError):
        return "optional_dependency_unavailable", "Install the locked optional MLX and Torch dependencies"
    if "matrix calculation failed" in message:
        return "device_calculation_failed", "Inspect the local MLX/Torch device installation"
    if isinstance(exc, (FileNotFoundError, KeyError, ValueError)):
        return "local_input_or_artifact_invalid", "Inspect the local console for the rejected checkpoint or fixture field"
    return "probe_failed", "Inspect the local console; no remote report was sent"


def _validate_probe_mlx_cpu(args):
    if args.probe_mlx_cpu and (args.backend != "mlx" or args.acoustic_device != "cpu"):
        raise ValueError("--probe-mlx-cpu requires --backend mlx and --acoustic-device cpu")


def _device(name):
    import mlx.core as mx

    return mx.cpu if name == "cpu" else mx.gpu


def _qualify_device(name):
    import mlx.core as mx
    import numpy as np

    if name == "metal":
        import torch
        from tts.semantic_mlx.runtime import require_metal

        require_metal(torch.device("mps"))
        if not torch.backends.mps.is_available():
            raise RuntimeError("Torch MPS is unavailable; acoustic v3 cannot run")
        torch_check = (torch.ones((2, 2), device="mps") @
                       torch.ones((2, 2), device="mps")).sum()
        torch.mps.synchronize()
        if float(torch_check.item()) != 8.0:
            raise RuntimeError("Torch MPS matrix calculation failed")
    device = _device(name)
    with mx.stream(device):
        value = mx.sum(mx.ones((2, 2)) @ mx.ones((2, 2)))
        mx.eval(value)
        if not np.isfinite(float(value.item())) or float(value.item()) != 8.0:
            raise RuntimeError(f"MLX {name} matrix calculation failed")
    return device


def _inputs(args, config):
    import numpy as np
    from tools.probes.gsv_semantic_reference import synthetic_inputs

    if args.inputs:
        with np.load(args.inputs, allow_pickle=False) as saved:
            data = {key: np.array(saved[key], copy=True) for key in ("phones", "prompt", "bert", "history")}
            if str(saved["fixture_schema"].item()) != "amadeus.gsv_frontend_fixture.v1":
                raise ValueError("Unsupported frontend fixture schema")
            source_sha = str(saved["source_checkpoint_sha256"].item())
            sovits_sha = str(saved["sovits_checkpoint_sha256"].item())
            reference_sha = str(saved["reference_audio_sha256"].item())
        if source_sha != _sha(args.checkpoint):
            raise ValueError("Frontend fixture belongs to another GPT checkpoint")
        if getattr(args, "sovits", "") and sovits_sha != _sha(args.sovits):
            raise ValueError("Frontend fixture belongs to another SoVITS checkpoint")
        if getattr(args, "reference_audio", "") and reference_sha != _sha(args.reference_audio):
            raise ValueError("Frontend fixture belongs to another reference audio")
        kind = "frontend_private"
        digest = _sha(args.inputs)
    else:
        data = synthetic_inputs(config, phone_length=args.phones,
                                prompt_length=args.prompt, steps=args.steps, seed=args.seed)
        kind = "synthetic"
        digest = hashlib.sha256(b"".join(data[key].tobytes() for key in sorted(data))).hexdigest()
    if (any(data[key].dtype.kind not in "iu" for key in ("phones", "prompt", "history"))
            or data["bert"].dtype.kind != "f" or not np.isfinite(data["bert"]).all()
            or data["phones"].ndim != 2 or data["prompt"].ndim != 2
            or data["phones"].shape[0] != 1 or data["prompt"].shape[0] != 1
            or data["phones"].shape[1] < 1 or data["prompt"].shape[1] < 1
            or data["bert"].shape != (1, config.bert_dim, data["phones"].shape[1])
            or data["history"].ndim != 2 or data["history"].shape[0] != 1
            or data["phones"].min() < 0 or data["phones"].max() >= config.phoneme_vocab_size
            or data["prompt"].min() < 0 or data["prompt"].max() >= config.eos
            or (data["history"].size and (data["history"].min() < 0
                                             or data["history"].max() >= config.eos))):
        raise ValueError("Input fixture shape or token range does not match checkpoint")
    metadata = {"fixture_kind": kind, "inputs_sha256": digest,
                  "phone_length": int(data["phones"].shape[1]),
                  "reference_tokens": int(data["prompt"].shape[1]),
                  "fixed_decode_steps": int(data["history"].shape[1])}
    if kind == "frontend_private":
        metadata.update({"sovits_checkpoint_sha256": sovits_sha,
                         "reference_audio_sha256": reference_sha})
    return data, metadata


def _artifact(args):
    from tts.semantic_mlx.weights import export_checkpoint, validate_artifact

    cache = Path(args.cache) if args.cache else ROOT / ".cache" / "gsv-mlx-t2s"
    artifact = export_checkpoint(args.checkpoint, cache)
    config, manifest = validate_artifact(artifact)
    return artifact, config, manifest


def doctor(args):
    import importlib.metadata
    import numpy as np
    import torch

    _qualify_device(args.device)
    data = {
        "status": "passed", "purpose": "numerical_test" if args.device == "cpu" else "production_experiment",
        "os": platform.system(), "os_release": platform.release(),
        "architecture": platform.machine(), "python": platform.python_version(),
        "torch": torch.__version__, "mlx": importlib.metadata.version("mlx"),
        "numpy": np.__version__, "torch_mps_available": bool(torch.backends.mps.is_available()),
        "mlx_device": args.device, "dtype": "float32", "compile": False,
        "cache_impl": "mlx_dynamic_reference", "cuda_graph": "not_applicable",
        "source_checkpoint_sha256": _sha(args.checkpoint) if args.checkpoint else None,
        "first_sentence_audio_cache": "disabled_for_mlx",
        "reference": "required",
    }
    return _save(args, data)


def export(args):
    started = time.perf_counter()
    _, config, manifest = _artifact(args)
    return _save(args, {
        "status": "passed", "source_checkpoint_sha256": manifest["source_checkpoint_sha256"],
        "converted_weights_sha256": manifest["weights_sha256"],
        "converter_revision": manifest["converter_revision"],
        "source_dtypes": manifest["source_dtypes"], "inference_dtype": "float32",
        "config": vars(config), "export_or_verify_ms": (time.perf_counter() - started) * 1000,
    })


def validate(args):
    from tools.probes.gsv_semantic_reference import validate_numerics

    _qualify_device(args.device)
    artifact, config, manifest = _artifact(args)
    inputs, fixture = _inputs(args, config)
    started = time.perf_counter()
    result = validate_numerics(args.checkpoint, artifact, inputs,
                               device="cpu" if args.device == "cpu" else "gpu",
                               atol=args.atol, rtol=args.rtol)
    failed = [row for row in result["rows"] if not row["passed"]]
    return _save(args, {
        "status": result["status"], "purpose": "numerical_test" if args.device == "cpu" else "metal_numerical",
        "mlx_device": args.device, "source_checkpoint_sha256": manifest["source_checkpoint_sha256"],
        "converted_weights_sha256": manifest["weights_sha256"], **fixture,
        "comparisons": result["comparisons"], "failed_comparisons": failed,
        "rows": result["rows"],
        "max_abs": max(row["max_abs"] for row in result["rows"]),
        "elapsed_ms": (time.perf_counter() - started) * 1000,
    })


def _percentiles(values):
    ordered = sorted(values)
    return {"runs": len(ordered), "min": ordered[0], "p50": statistics.median(ordered),
            "p95": ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], "max": ordered[-1]}


def _set_sampling_seed(seed, *, mlx):
    import numpy as np
    import torch

    random.seed(seed)
    np.random.seed(seed % (2**32))
    torch.manual_seed(seed)
    if mlx:
        import mlx.core as mx
        mx.random.seed(seed)


def _trial_seed(args, iteration):
    return (args.seed + iteration - args.warmup if iteration >= args.warmup
            else args.seed + 1_000_000 + iteration)


def _memory_snapshot(device):
    import mlx.core as mx
    import psutil
    import torch

    snapshot = {"rss_bytes": psutil.Process().memory_info().rss,
                "mlx_active_bytes": None, "mlx_cache_bytes": None,
                "mlx_peak_bytes": None, "torch_mps_allocated_bytes": None,
                "torch_mps_driver_bytes": None}
    if device == "metal":
        snapshot.update({
            "mlx_active_bytes": mx.metal.get_active_memory(),
            "mlx_cache_bytes": mx.metal.get_cache_memory(),
            "mlx_peak_bytes": mx.metal.get_peak_memory(),
            "torch_mps_allocated_bytes": torch.mps.current_allocated_memory(),
            "torch_mps_driver_bytes": torch.mps.driver_allocated_memory(),
        })
    return snapshot


def bench(args):
    import mlx.core as mx
    import numpy as np
    import torch
    from tools.probes.gsv_semantic_reference import TorchTrace, load_reference
    from tts.semantic_mlx.runtime import MLXSemanticDecoder

    _qualify_device(args.device)
    artifact, config, manifest = _artifact(args)
    inputs, fixture = _inputs(args, config)
    device = torch.device("cpu" if args.device == "cpu" else "mps")
    torch_inputs = {key: torch.from_numpy(value).to(device) for key, value in inputs.items()}
    torch_inputs["bert"] = torch_inputs["bert"].float()
    lengths = torch.tensor([inputs["phones"].shape[1]], device=device)
    times, output_tokens = [], []
    if args.backend == "torch":
        oracle = TorchTrace(load_reference(args.checkpoint, device=str(device)), capture_trace=False)
        source = oracle.model
        if device.type == "mps":
            torch.mps.synchronize()
        for iteration in range(args.runs + args.warmup):
            trial_seed = _trial_seed(args, iteration)
            started = time.perf_counter()
            with torch.inference_mode():
                logits, state, _ = oracle.prefill(
                    torch_inputs["phones"], torch_inputs["prompt"], torch_inputs["bert"])
            if device.type == "mps":
                torch.mps.synchronize()
            prefill_ms = (time.perf_counter() - started) * 1000
            started = time.perf_counter()
            for step in range(inputs["history"].shape[1]):
                with torch.inference_mode():
                    logits, state, _ = oracle.decode_step(
                        torch_inputs["history"][:, step:step + 1], state)
            if device.type == "mps":
                torch.mps.synchronize()
            decode_ms = (time.perf_counter() - started) * 1000
            _set_sampling_seed(trial_seed, mlx=False)
            started = time.perf_counter()
            with torch.inference_mode():
                prediction, idx = source.infer_panel(
                    torch_inputs["phones"], lengths, torch_inputs["prompt"], torch_inputs["bert"],
                    top_k=args.top_k, top_p=args.top_p, temperature=args.temperature,
                    repetition_penalty=args.repetition_penalty, early_stop_num=args.budget,
                    enable_cuda_graph=False, enable_static_kv=False)
            if device.type == "mps":
                torch.mps.synchronize()
            free_ms = (time.perf_counter() - started) * 1000
            if iteration >= args.warmup:
                times.append({"prefill_ms": prefill_ms, "fixed_decode_ms": decode_ms,
                              "free_generation_ms": free_ms})
                output_tokens.append({"seed": trial_seed, "idx": int(idx),
                                      "generated_tokens": int(idx),
                                      "returned_tokens": int(prediction.shape[1]),
                                      "budget_boundary_reached": (
                                          args.budget != -1 and int(idx) >= args.budget)})
    else:
        with mx.stream(_device(args.device)):
            mlx_inputs = {key: mx.array(value.astype(np.float32 if key == "bert" else np.int32))
                          for key, value in inputs.items()}
            decoder = (MLXSemanticDecoder.for_numerical_test(artifact)
                       if args.device == "cpu" else
                       MLXSemanticDecoder.from_checkpoint(args.checkpoint,
                                                           acoustic_device=device,
                                                           cache_root=artifact.parent.parent))
            model = decoder.model
            mx.eval(list(mlx_inputs.values()))
            if device.type == "mps":
                torch.mps.synchronize()
            for iteration in range(args.runs + args.warmup):
                trial_seed = _trial_seed(args, iteration)
                started = time.perf_counter()
                logits, cache = model.prefill(mlx_inputs["phones"], mlx_inputs["prompt"], mlx_inputs["bert"])
                mx.eval(logits, [item for layer in cache.layers for item in layer])
                prefill_ms = (time.perf_counter() - started) * 1000
                started = time.perf_counter()
                for step in range(inputs["history"].shape[1]):
                    token = mlx_inputs["history"][:, step:step + 1]
                    logits, cache = model.decode_step(token, cache)
                mx.eval(logits, [item for layer in cache.layers for item in layer])
                decode_ms = (time.perf_counter() - started) * 1000
                _set_sampling_seed(trial_seed, mlx=True)
                prediction, idx = decoder.infer_panel(
                    torch_inputs["phones"], lengths, torch_inputs["prompt"], torch_inputs["bert"],
                    top_k=args.top_k, top_p=args.top_p, temperature=args.temperature,
                    repetition_penalty=args.repetition_penalty, early_stop_num=args.budget)
                if iteration >= args.warmup:
                    times.append({"prefill_ms": prefill_ms, "fixed_decode_ms": decode_ms,
                                  "free_generation_ms": decoder.last_timings["semantic_total_ms"],
                                  **decoder.last_timings})
                    output_tokens.append({"seed": trial_seed, "idx": int(idx),
                                          "generated_tokens": int(idx),
                                          "returned_tokens": int(prediction.shape[1]),
                                          "budget_boundary_reached": (
                                              args.budget != -1 and int(idx) >= args.budget)})
            decoder.close()
    metrics = {key: _percentiles([row[key] for row in times]) for key in times[0]}
    return _save(args, {
        "status": "passed", "purpose": "numerical_test" if args.device == "cpu" else "model_only_benchmark",
        "backend": args.backend, "mlx_device": args.device if args.backend == "mlx" else "not_applicable",
        "torch_device": str(device), "source_checkpoint_sha256": manifest["source_checkpoint_sha256"],
        "converted_weights_sha256": manifest["weights_sha256"], **fixture,
        "execution": {"cuda_graph": False, "static_kv": False, "compile": False,
                      "cache_impl": "mlx_dynamic_reference" if args.backend == "mlx" else "torch_dynamic"},
        "sampling_parameters": {"top_k": args.top_k, "top_p": args.top_p,
                                "temperature": args.temperature,
                                "repetition_penalty": args.repetition_penalty,
                                "early_stop_num": args.budget},
        "timings_ms": metrics, "free_generation_outputs": output_tokens,
        "warmup_runs_excluded": args.warmup,
        "measured_seed_start": args.seed,
        "clock_domain": "local process perf_counter; fixed prefill and complete decode each synchronized once",
    })


def soak(args):
    import torch
    from tools.probes.gsv_semantic_reference import synthetic_inputs
    from tts.semantic_mlx.runtime import MLXSemanticDecoder

    _qualify_device(args.device)
    artifact, config, manifest = _artifact(args)
    device = torch.device("cpu" if args.device == "cpu" else "mps")
    private = None
    fixture = {"fixture_kind": "synthetic"}
    if args.inputs:
        private, fixture = _inputs(args, config)
    failures, timings, counts, memory, lengths = [], [], [], [], []
    memory_start = _memory_snapshot(args.device)
    decoder = None
    for index in range(args.requests):
        try:
            if decoder is None:
                decoder = (MLXSemanticDecoder.for_numerical_test(artifact)
                           if args.device == "cpu" else
                           MLXSemanticDecoder.from_checkpoint(args.checkpoint,
                                                               acoustic_device=device,
                                                               cache_root=artifact.parent.parent))
            if private is None:
                phones_count = args.short_phones if index % 2 == 0 else args.long_phones
                prompt_count = args.short_prompt if index % 2 == 0 else args.long_prompt
                data = synthetic_inputs(config, phone_length=phones_count,
                                        prompt_length=prompt_count, steps=1, seed=args.seed + index)
            else:
                fraction = 0.5 if index % 2 == 0 else 1.0
                phones_count = max(1, int(private["phones"].shape[1] * fraction))
                prompt_count = max(1, int(private["prompt"].shape[1] * fraction))
                data = {"phones": private["phones"][:, :phones_count],
                        "prompt": private["prompt"][:, :prompt_count],
                        "bert": private["bert"][:, :, :phones_count]}
            lengths.append({"phones": phones_count, "reference_tokens": prompt_count})
            phones = torch.from_numpy(data["phones"]).to(device)
            prompt = torch.from_numpy(data["prompt"]).to(device)
            bert = torch.from_numpy(data["bert"]).to(device)
            _set_sampling_seed(args.seed + index, mlx=True)
            started = time.perf_counter()
            result, idx = decoder.infer_panel(
                phones, torch.tensor([phones.shape[1]], device=device), prompt, bert,
                top_k=args.top_k, top_p=args.top_p, temperature=args.temperature,
                repetition_penalty=args.repetition_penalty, early_stop_num=args.budget)
            timings.append((time.perf_counter() - started) * 1000)
            counts.append({"seed": args.seed + index, "idx": int(idx),
                           "returned_tokens": int(result.shape[1])})
        except Exception as exc:
            failures.append({"request": index, "error_type": type(exc).__name__})
        memory.append(_memory_snapshot(args.device))
        if decoder is not None and (index + 1) % args.reload_every == 0:
            decoder.close()
            decoder = None
    if decoder is not None:
        decoder.close()
    memory_end = _memory_snapshot(args.device)
    warm_index = min(len(memory) - 1, max(0, args.requests // 5))
    return _save(args, {
        "status": "failed" if failures else "passed",
        "purpose": "numerical_test" if args.device == "cpu" else "metal_model_soak",
        "mlx_device": args.device, "source_checkpoint_sha256": manifest["source_checkpoint_sha256"],
        "converted_weights_sha256": manifest["weights_sha256"], "requests": args.requests,
        "reload_every": args.reload_every, "failures": failures,
        "seed_start": args.seed,
        "request_ms": _percentiles(timings) if timings else None,
        "output_counts": counts, "input_lengths": lengths, **fixture,
        "memory_start": memory_start, "memory_by_request": memory,
        "memory_end": memory_end,
        "rss_growth_after_initial_fifth_bytes": (
            memory_end["rss_bytes"] - memory[warm_index]["rss_bytes"]),
    })


def frontend_inputs(args):
    """Create a private real frontend/reference fixture with the current Torch stack."""
    import numpy as np
    import torch
    from config import settings
    import local_tts_infer
    from tools.probes.probe_gsv_stability import ProbeCase, _build_contexts

    if args.assets_root:
        local_tts_infer.root_dir = str(Path(args.assets_root).resolve())
    settings.TTS_REF_AUDIO_JA = args.reference_audio
    settings.TTS_REF_TEXT_JA = args.reference_text
    settings.TTS_OUTPUT_LANGUAGE = "日文"

    class SemanticOnlyInferencer(local_tts_infer.TTSInferencer):
        def _load_bigvgan_model(self):
            self.bigvgan_model = None

    inferencer = SemanticOnlyInferencer(device=args.acoustic_device,
                                       gpt_path=args.checkpoint, sovits_path=args.sovits)
    if inferencer.semantic_decoder.backend != "torch":
        raise RuntimeError("frontend fixture extraction requires the Torch backend")
    contexts = _build_contexts(inferencer, [ProbeCase("fixture", args.text, "user_private")], torch)
    context = contexts[0]
    with torch.inference_mode():
        prediction, idx = inferencer.t2s_model.model.infer_panel(
            context.all_phoneme_ids, context.all_phoneme_len,
            context.prompt_semantic, context.bert, top_k=1, top_p=1.0,
            temperature=1.0, repetition_penalty=1.35,
            early_stop_num=args.history_steps,
            enable_cuda_graph=False, enable_static_kv=False)
    history = prediction[:, -int(idx):].detach().cpu().numpy() if idx else np.zeros((1, 0), dtype=np.int64)
    output = Path(args.fixture_output)
    output.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(output,
                        fixture_schema="amadeus.gsv_frontend_fixture.v1",
                        source_checkpoint_sha256=_sha(args.checkpoint),
                        sovits_checkpoint_sha256=_sha(args.sovits),
                        reference_audio_sha256=_sha(args.reference_audio),
                        phones=context.all_phoneme_ids.detach().cpu().numpy(),
                        prompt=context.prompt_semantic.detach().cpu().numpy(),
                        bert=context.bert.detach().float().cpu().numpy(),
                        history=history)
    return _save(args, {
        "status": "passed", "fixture_kind": "frontend_private",
        "fixture_sha256": _sha(output), "reference_audio_sha256": _sha(args.reference_audio),
        "source_checkpoint_sha256": _sha(args.checkpoint),
        "phone_length": int(context.all_phoneme_ids.shape[1]),
        "reference_tokens": int(context.prompt_semantic.shape[1]),
        "fixed_decode_steps": int(history.shape[1]),
        "privacy": "private reference-derived fixture; do not attach to public issue or PR",
    })


def audio(args):
    """Warm real synthesis in one process; no capture, playback, or network."""
    import numpy as np
    import psutil
    import soundfile as sf
    import torch
    from config import settings
    import local_tts_infer

    if args.assets_root:
        local_tts_infer.root_dir = str(Path(args.assets_root).resolve())
    if args.probe_mlx_cpu and (args.backend != "mlx" or settings.TTS_DEVICE != "cpu"):
        raise ValueError("--probe-mlx-cpu requires --backend mlx and --acoustic-device cpu")

    class ProbeInferencer(local_tts_infer.TTSInferencer):
        """Probe instrumentation; production load and generation stay unchanged."""

        def _load_gpt_model(self):
            if args.probe_mlx_cpu:
                from tts.semantic_mlx.runtime import MLXSemanticDecoder
                from tts.semantic_mlx.weights import export_checkpoint

                artifact = export_checkpoint(self.gpt_path, ROOT / ".cache" / "gsv-mlx-t2s")
                self.t2s_model = None
                self.semantic_decoder = MLXSemanticDecoder.for_numerical_test(artifact)
                self.gpt_config = None
                self.hz = 50
                self.max_sec = self.semantic_decoder.model.config.max_sec
            else:
                super()._load_gpt_model()
            self.probe_attempts = []
            self.probe_tokens = []
            original = self.semantic_decoder.infer_panel

            def traced_infer_panel(*call_args, **call_kwargs):
                started = time.perf_counter()
                prediction, idx = original(*call_args, **call_kwargs)
                timing = {"elapsed_ms": (time.perf_counter() - started) * 1000}
                if self.semantic_decoder.backend == "mlx":
                    timing.update(self.semantic_decoder.last_timings)
                self.probe_attempts.append(timing)
                return prediction, idx

            self.semantic_decoder.infer_panel = traced_infer_panel

        def _infer_semantic_with_guard(self, **kwargs):
            if args.controlled:
                cpu_state = torch.random.get_rng_state()
                device = torch.device(self.device)
                if device.type == "mps":
                    device_state = torch.mps.get_rng_state()
                elif device.type == "cuda":
                    device_state = torch.cuda.get_rng_state(device)
                else:
                    device_state = None
            try:
                prediction, idx, attempts = super()._infer_semantic_with_guard(**kwargs)
            finally:
                if args.controlled:
                    torch.random.set_rng_state(cpu_state)
                    if device.type == "mps":
                        torch.mps.set_rng_state(device_state)
                    elif device.type == "cuda":
                        torch.cuda.set_rng_state(device_state, device)
            tokens = prediction.detach().reshape(-1).cpu().long().tolist()
            self.probe_tokens.append({"tokens": tokens, "idx": int(idx),
                                      "guard_attempts": int(attempts),
                                      "budget_boundary_reached": (
                                          int(idx) >= int(self.hz * kwargs["effective_max_sec"]))})
            return prediction, idx, attempts

    if settings.TTS_T2S_BACKEND != args.backend:
        raise RuntimeError("Resolved TTS_T2S_BACKEND differs from requested backend")
    inferencer = ProbeInferencer(device=settings.TTS_DEVICE,
                                gpt_path=args.checkpoint or settings.TTS_GPT_MODEL_PATH,
                                sovits_path=args.sovits or settings.TTS_SOVITS_MODEL_PATH)
    if args.probe_mlx_cpu and inferencer.t2s_model is not None:
        raise RuntimeError("Probe MLX CPU unexpectedly constructed a Torch T2S model")
    rows = []
    private_tokens = []
    try:
        for iteration in range(args.runs + args.warmup):
            trial_seed = _trial_seed(args, iteration)
            _set_sampling_seed(trial_seed, mlx=args.backend == "mlx")
            inferencer.t2s_stats.clear()
            inferencer.probe_attempts.clear()
            inferencer.probe_tokens.clear()
            started = time.perf_counter()
            chunks, first_chunk_ms, sample_rate = [], None, None
            for rate, samples, _ in inferencer.infer_stream(
                text=args.text, ref_audio_path=args.reference_audio,
                prompt_text=args.reference_text, text_language="日文", prompt_language="日文",
                top_k=args.top_k, top_p=args.top_p, temperature=args.temperature,
                speed=args.speed, pause_second=args.pause_second,
                chunk_size_seconds=args.chunk_seconds if args.chunk_seconds > 0 else None,
                how_to_cut=args.how_to_cut,
                max_sec_override=args.max_sec_override,
                enable_cuda_graph=False, enable_static_kv=True,
                if_freeze=False, sample_steps=args.sample_steps,
                collect_t2s_stats=True):
                sample_rate = int(rate)
                if samples is None:
                    continue  # The source stream first yields sample-rate metadata.
                chunk = np.asarray(samples, dtype=np.float32).reshape(-1)
                if not chunk.size:
                    continue
                if not np.isfinite(chunk).all():
                    raise RuntimeError("Synthesis produced nonfinite audio")
                if first_chunk_ms is None:
                    first_chunk_ms = (time.perf_counter() - started) * 1000
                chunks.append(chunk)
            # This clock stops when synthesis drains; WAV encoding is outside it.
            total_ms = (time.perf_counter() - started) * 1000
            if not chunks:
                raise RuntimeError("Synthesis produced no audio chunks")
            waveform = np.concatenate(chunks)
            if float(np.max(np.abs(waveform))) <= 1e-5:
                raise RuntimeError("Synthesis produced only silence")
            duration = len(waveform) / sample_rate
            wav_sha = None
            if args.wav and iteration >= args.warmup:
                target = Path(args.wav)
                target = target.with_name(f"{target.stem}-{iteration - args.warmup + 1}{target.suffix}")
                target.parent.mkdir(parents=True, exist_ok=True)
                sf.write(target, waveform, sample_rate, subtype="PCM_16")
                wav_sha = _sha(target)
            if iteration >= args.warmup:
                token_hashes = [hashlib.sha256(np.asarray(item["tokens"], dtype="<i8").tobytes()).hexdigest()
                                for item in inferencer.probe_tokens]
                private_tokens.append({"seed": trial_seed, "segments": list(inferencer.probe_tokens)})
                rows.append({
                    "seed": trial_seed,
                    "first_emitted_chunk_ms": first_chunk_ms,
                    "total_synthesis_ms": total_ms,
                    "audio_seconds": duration,
                    "rtf": total_ms / (duration * 1000),
                    "sample_rate": sample_rate,
                    "chunks": len(chunks),
                    "semantic_tokens": sum(item["tokens"] for item in inferencer.t2s_stats),
                    "semantic_attempts": sum(item["semantic_attempts"] for item in inferencer.t2s_stats),
                    "semantic_segments": len(inferencer.t2s_stats),
                    "semantic_token_hashes": token_hashes,
                    "semantic_attempt_timings": list(inferencer.probe_attempts),
                    "semantic_total_ms": sum(item["elapsed_ms"] for item in inferencer.probe_attempts),
                    "bridge_in_ms": sum(item.get("bridge_in_ms", 0) for item in inferencer.probe_attempts),
                    "bridge_out_ms": sum(item.get("bridge_out_ms", 0) for item in inferencer.probe_attempts),
                    "peak_abs_sample": float(np.max(np.abs(waveform))),
                    "rms": float(np.sqrt(np.mean(waveform.astype(np.float64) ** 2))),
                    "clipping_fraction": float(np.mean(np.abs(waveform) >= 0.999)),
                    "rss_bytes": psutil.Process().memory_info().rss,
                    "wav_sha256": wav_sha,
                    "wav_subtype": "PCM_16" if wav_sha else None,
                })
    finally:
        inferencer.close()
    if args.token_output:
        target = Path(args.token_output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(private_tokens, separators=(",", ":")) + "\n", encoding="utf-8")
    return _save(args, {
        "status": "passed", "purpose": "real_audio_local",
        "probe_only_cpu_mixed_chain": bool(args.probe_mlx_cpu),
        "controlled_acoustic_rng": bool(args.controlled),
        "backend": inferencer.semantic_decoder.backend,
        "semantic": inferencer.semantic_decoder.info,
        "semantic_dtype": (str(next(inferencer.t2s_model.model.parameters()).dtype).removeprefix("torch.")
                           if inferencer.t2s_model is not None else inferencer.semantic_decoder.info["dtype"]),
        "acoustic_device": str(inferencer.device),
        "acoustic_dtype": "float16" if inferencer.is_half else "float32",
        "source_checkpoint_sha256": _sha(args.checkpoint or settings.TTS_GPT_MODEL_PATH),
        "sovits_checkpoint_sha256": _sha(args.sovits or settings.TTS_SOVITS_MODEL_PATH),
        "reference_audio_sha256": _sha(args.reference_audio),
        "text_sha256": hashlib.sha256(args.text.encode("utf-8")).hexdigest(),
        "prompt_text_sha256": hashlib.sha256(args.reference_text.encode("utf-8")).hexdigest(),
        "parameters": {"top_k": args.top_k, "top_p": args.top_p,
                       "temperature": args.temperature, "speed": args.speed,
                       "pause_second": args.pause_second, "sample_steps": args.sample_steps,
                       "chunk_seconds": args.chunk_seconds if args.chunk_seconds > 0 else None,
                       "how_to_cut": args.how_to_cut,
                       "max_sec_override": args.max_sec_override,
                       "semantic_guard": inferencer._semantic_guard_enabled()},
        "private_token_trace_sha256": _sha(args.token_output) if args.token_output else None,
        "warmup_runs_excluded": args.warmup,
        "measured_seed_start": args.seed,
        "runs": rows,
        "first_emitted_chunk_ms": _percentiles([row["first_emitted_chunk_ms"] for row in rows]),
        "total_synthesis_ms": _percentiles([row["total_synthesis_ms"] for row in rows]),
        "rtf": _percentiles([row["rtf"] for row in rows]),
        "first_voiced_device_write_ms": None, "acoustic_onset_ms": None,
    })


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "export", "validate", "bench", "soak", "inputs", "audio"):
        sub = commands.add_parser(name)
        sub.add_argument("--output", default="", help="Local report .json or directory")
        sub.add_argument("--checkpoint", default="", help="Trusted local GPT checkpoint")
        if name in {"doctor", "validate", "bench", "soak"}:
            sub.add_argument("--device", choices=("cpu", "metal"), default="cpu")
        if name in {"export", "validate", "bench", "soak"}:
            sub.add_argument("--cache", default="")
        if name in {"validate", "bench"}:
            sub.add_argument("--inputs", default="", help="Private .npz from inputs; else synthetic")
            sub.add_argument("--sovits", default="", help="Verify fixture acoustic checkpoint")
            sub.add_argument("--reference-audio", default="", help="Verify fixture reference")
            sub.add_argument("--phones", type=int, default=48)
            sub.add_argument("--prompt", type=int, default=337)
            sub.add_argument("--steps", type=int, default=8)
            sub.add_argument("--seed", type=int, default=73)
        if name in {"bench", "soak", "audio"}:
            sub.add_argument("--top-k", type=int, default=5)
            sub.add_argument("--top-p", type=float, default=1.0)
            sub.add_argument("--temperature", type=float, default=0.6)
        if name in {"bench", "soak"}:
            sub.add_argument("--repetition-penalty", type=float, default=1.35)
            sub.add_argument("--budget", type=int, default=16)
        if name == "validate":
            sub.add_argument("--atol", type=float, default=1e-3)
            sub.add_argument("--rtol", type=float, default=1e-3)
        if name == "bench":
            sub.add_argument("--backend", choices=("torch", "mlx"), required=True)
            sub.add_argument("--runs", type=int, default=3)
            sub.add_argument("--warmup", type=int, default=1)
        if name == "soak":
            sub.add_argument("--requests", type=int, default=100)
            sub.add_argument("--reload-every", type=int, default=20)
            sub.add_argument("--seed", type=int, default=731)
            sub.add_argument("--inputs", default="", help="Private real frontend fixture")
            sub.add_argument("--sovits", default="", help="Verify fixture acoustic checkpoint")
            sub.add_argument("--reference-audio", default="", help="Verify fixture reference")
            sub.add_argument("--short-phones", type=int, default=16)
            sub.add_argument("--long-phones", type=int, default=96)
            sub.add_argument("--short-prompt", type=int, default=48)
            sub.add_argument("--long-prompt", type=int, default=337)
        if name == "inputs":
            sub.add_argument("--sovits", required=True)
            sub.add_argument("--assets-root", default="")
            sub.add_argument("--acoustic-device", default="cpu")
            sub.add_argument("--reference-audio", required=True)
            sub.add_argument("--reference-text", required=True)
            sub.add_argument("--text", required=True)
            sub.add_argument("--fixture-output", required=True)
            sub.add_argument("--history-steps", type=int, default=8)
        if name == "audio":
            sub.add_argument("--backend", choices=("torch", "mlx"), required=True)
            sub.add_argument("--sovits", default="")
            sub.add_argument("--assets-root", default="")
            sub.add_argument("--acoustic-device", default="")
            sub.add_argument("--probe-mlx-cpu", action="store_true",
                             help="Probe-only: real MLX CPU semantics through Torch CPU v3 acoustic chain")
            sub.add_argument("--reference-audio", required=True)
            sub.add_argument("--reference-text", required=True)
            sub.add_argument("--text", required=True)
            sub.add_argument("--sample-steps", type=int, default=16)
            sub.add_argument("--how-to-cut", default="按标点符号切")
            sub.add_argument("--max-sec-override", type=float, default=None)
            sub.add_argument("--speed", type=float, default=1.0)
            sub.add_argument("--pause-second", type=float, default=0.3)
            sub.add_argument("--chunk-seconds", type=float, default=0.25)
            sub.add_argument("--runs", type=int, default=3)
            sub.add_argument("--warmup", type=int, default=1)
            sub.add_argument("--seed", type=int, default=1000)
            sub.add_argument("--wav", default="")
            sub.add_argument("--token-output", default="", help="Private semantic ID trace JSON")
            sub.add_argument("--controlled", action="store_true",
                             help="Top-k=1 quality comparison; preserve acoustic Torch RNG around semantic generation")
    args = parser.parse_args(argv)
    if args.command in {"export", "validate", "bench", "soak", "inputs"} and not args.checkpoint:
        parser.error(f"{args.command} requires --checkpoint")
    if args.command == "bench" and (args.runs < 1 or args.warmup < 0):
        parser.error("--runs must be positive and --warmup nonnegative")
    if args.command == "soak" and (args.requests < 1 or args.reload_every < 1):
        parser.error("--requests and --reload-every must be positive")
    if args.command == "inputs" and args.history_steps < 1:
        parser.error("--history-steps must be positive")
    if args.command == "audio" and (args.runs < 1 or args.warmup < 0):
        parser.error("--runs must be positive and --warmup nonnegative")
    if args.command == "audio" and args.controlled and args.top_k != 1:
        parser.error("--controlled requires --top-k 1")
    if args.command == "audio":
        _validate_probe_mlx_cpu(args)
    if args.command in {"inputs", "audio"}:
        os.environ["TTS_BACKEND"] = "gpt_sovits"
        os.environ["TTS_T2S_BACKEND"] = "torch" if args.command == "inputs" else args.backend
        os.environ["TTS_RUNTIME_WARMUP"] = "0"
        os.environ["TTS_SESSION_WARMUP"] = "0"
        os.environ["ENABLE_CUDA_GRAPH_PRECAPTURE"] = "0"
    if args.command == "audio" and args.acoustic_device:
        os.environ["TTS_DEVICE"] = args.acoustic_device
    try:
        report = globals()[{"inputs": "frontend_inputs"}.get(args.command, args.command)](args)
    except Exception as exc:
        reason_code, action = _failure_reason(exc)
        _save(args, {"status": "failed", "phase": args.command,
                     "error_type": type(exc).__name__, "reason_code": reason_code,
                     "action": action})
        print(f"{args.command} failed ({reason_code}): {action}", file=sys.stderr)
        raise SystemExit(1) from exc
    if report["status"] != "passed":
        raise SystemExit(1)
    return report


if __name__ == "__main__":
    main()
