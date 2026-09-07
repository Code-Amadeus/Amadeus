"""Standalone ASR/TTS checks using Amadeus's real JSONL sidecar protocol.

Run with the project's selected local-rocm Python. This script starts and
terminates only its own test child; it does not play audio.
"""
from __future__ import annotations

import argparse
import base64
from collections import deque
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time


def existing_file(value: str) -> Path:
    path = Path(value).resolve()
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"File not found: {path}")
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=300, help="Seconds per protocol wait, including cold load.")
    sub = parser.add_subparsers(dest="mode", required=True)
    asr = sub.add_parser("asr", help="Transcribe one known short recording twice in the same process.")
    asr.add_argument("--model", required=True, type=Path)
    asr.add_argument("--audio", required=True, type=existing_file)
    asr.add_argument("--language", default="Chinese", help="Full language name, not an ISO code.")
    asr.add_argument("--repeat", type=int, default=2)
    tts = sub.add_parser("tts", help="Generate and validate a WAV; no speaker playback.")
    tts.add_argument("--gpt", required=True, type=existing_file)
    tts.add_argument("--sovits", required=True, type=existing_file)
    tts.add_argument("--reference", required=True, type=existing_file)
    tts.add_argument("--reference-text", required=True)
    tts.add_argument("--text", required=True)
    tts.add_argument("--language", choices=["ja", "en"], default="ja")
    tts.add_argument("--output", required=True, type=Path)
    tts.add_argument("--graph", action="store_true", help="Opt in to Graph; off for baseline verification.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    repo = args.repo.resolve()
    relative_script = "asr/qwen3_asr_sidecar.py" if args.mode == "asr" else "tts/gpt_sovits_sidecar.py"
    script = repo / relative_script
    if not script.is_file():
        raise SystemExit(f"Missing sidecar: {script}; verify the adapter patch was applied.")
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive.")
    if args.mode == "asr" and (not args.model.is_dir() or args.repeat < 1):
        raise SystemExit("ASR needs an existing model directory and --repeat >= 1.")
    if args.mode == "tts" and args.output.exists():
        raise SystemExit("Output already exists; choose a new output filename (nothing was overwritten).")

    import numpy as np
    import soundfile as sf
    import torch

    if not Path(torch.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
        raise SystemExit("torch comes from another environment; selected-environment validation failed.")
    if not torch.version.hip or not torch.cuda.is_available():
        raise SystemExit("A usable ROCm PyTorch GPU is required; run verify_gpu.py first.")

    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(key, None)
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", PYTHONNOUSERSITE="1")
    if args.mode == "asr":
        env.update(QWEN3_ASR_DEVICE="cuda", QWEN3_ASR_REQUIRE_CUDA="true", QWEN3_ASR_WARMUP="1",
                   QWEN3_ASR_MODEL_PATH=str(args.model.resolve()), HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                   TTS_BACKEND="disabled")
        samples, sample_rate = sf.read(args.audio, dtype="float32", always_2d=True)
        audio = samples.mean(axis=1)
        if sample_rate != 16000:
            from scipy.signal import resample_poly
            divisor = math.gcd(int(sample_rate), 16000)
            audio = resample_poly(audio, 16000 // divisor, int(sample_rate) // divisor)
        audio = np.asarray(audio, dtype="<f4")
        if not len(audio) or not np.isfinite(audio).all() or len(audio) > 16000 * 30:
            raise SystemExit("Use a non-empty, finite recording of at most 30 seconds.")
        request = {"audio_b64": base64.b64encode(audio.tobytes()).decode("ascii"), "sample_rate": 16000,
                   "language": args.language, "context": ""}
    else:
        graph = "1" if args.graph else "0"
        language_label = "日文" if args.language == "ja" else "英文"
        env.setdefault("NLTK_DATA", str(repo / "assets" / "nltk_data"))
        env.update(TTS_BACKEND="gpt_sovits", TTS_DEVICE="cuda:0", TTS_REQUIRE_CUDA="1",
                   TTS_GPT_MODEL_PATH=str(args.gpt), TTS_SOVITS_MODEL_PATH=str(args.sovits),
                   TTS_OUTPUT_LANGUAGE="日文" if args.language == "ja" else "英文",
                   TTS_REF_AUDIO_JA=str(args.reference), TTS_REF_AUDIO_EN=str(args.reference),
                   TTS_REF_TEXT_JA=args.reference_text, TTS_REF_TEXT_EN=args.reference_text,
                   ENABLE_CUDA_GRAPH=graph, ENABLE_CUDA_GRAPH_PRECAPTURE=graph,
                   TTS_T2S_FLASH_ATTN="0", BIGVGAN_USE_CUDA_KERNEL="0", TTS_STARTUP_INFERENCE_WARMUP="0")
        request = {"type": "infer_stream", "request_id": "rocm-tutorial-test", "request": {
            "text": args.text, "language": args.language, "reference_language": args.language,
            "reference_audio": str(args.reference), "reference_text": args.reference_text,
            "speed": 1.0, "chunk_size_seconds": 0.8,
            "options": {"text_language": language_label, "prompt_language": language_label,
                        "sample_steps": 16, "how_to_cut": "不切", "if_sr": False,
                        "enable_cuda_graph": args.graph, "enable_static_kv": True}}}

    messages: queue.Queue = queue.Queue()
    stderr_tail: deque[str] = deque(maxlen=35)
    started = time.perf_counter()
    proc = subprocess.Popen([sys.executable, "-u", str(script)], cwd=repo, env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def read_stdout() -> None:
        for line in proc.stdout:
            messages.put(line)
        messages.put(None)

    def read_stderr() -> None:
        for line in proc.stderr:
            stderr_tail.append(line.rstrip())

    threading.Thread(target=read_stdout, daemon=True).start()
    threading.Thread(target=read_stderr, daemon=True).start()

    def receive() -> dict:
        deadline = time.perf_counter() + args.timeout
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise TimeoutError("Sidecar did not produce a protocol message before the timeout.")
            try:
                raw = messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError("Sidecar timeout; inspect the diagnostic tail below.") from exc
            if raw is None:
                raise RuntimeError(f"Sidecar closed stdout (exit={proc.poll()}).")
            try:
                message = json.loads(raw)
            except json.JSONDecodeError as exc:
                stderr_tail.append("NON-JSON STDOUT: " + raw.rstrip())
                raise RuntimeError("Non-JSON data on protocol stdout; the real adapter would reject it.") from exc
            if not isinstance(message, dict):
                raise RuntimeError("Sidecar returned a non-object JSON message.")
            if message.get("type") == "error":
                raise RuntimeError(str(message.get("msg", message)))
            return message

    def send(payload: dict) -> None:
        proc.stdin.write(json.dumps(payload, ensure_ascii=True) + "\n")
        proc.stdin.flush()

    try:
        ready = receive()
        if ready.get("type") != "ready" or not str(ready.get("device", "")).startswith("cuda"):
            raise RuntimeError(f"Expected GPU ready; received {ready}")
        print(json.dumps({"ready": ready, "load_and_warmup_seconds": round(time.perf_counter() - started, 3)}, ensure_ascii=False))
        if args.mode == "asr":
            for index in range(args.repeat):
                begin = time.perf_counter()
                send(request)
                result = receive()
                if result.get("type") != "result" or not str(result.get("text", "")).strip():
                    raise RuntimeError(f"Expected non-empty transcription; received {result}")
                elapsed = time.perf_counter() - begin
                print(json.dumps({"trial": index + 1, "text": result["text"], "audio_seconds": len(audio) / 16000,
                                  "inference_seconds": round(elapsed, 3), "rtf": round(elapsed / (len(audio) / 16000), 3)}, ensure_ascii=False))
            print("PASS: protocol/finite input/non-empty output. Human comparison with the recording is still required.")
        else:
            chunks = []
            sample_rate = None
            first_chunk_seconds = None
            begin = time.perf_counter()
            send(request)
            while True:
                item = receive()
                if item.get("request_id") != request["request_id"]:
                    raise RuntimeError("Unexpected TTS request_id.")
                if item.get("type") == "done":
                    break
                if item.get("type") != "chunk":
                    raise RuntimeError(f"Unexpected TTS message type: {item.get('type')}")
                chunk = np.frombuffer(base64.b64decode(item["audio_b64"], validate=True), dtype="<f4").copy()
                rate = int(item["sample_rate"])
                if not len(chunk) or not np.isfinite(chunk).all() or rate <= 0:
                    raise RuntimeError("Empty/non-finite PCM or invalid sample rate.")
                if sample_rate is not None and sample_rate != rate:
                    raise RuntimeError("Sample rate changed within an utterance.")
                sample_rate = rate
                if first_chunk_seconds is None:
                    first_chunk_seconds = time.perf_counter() - begin
                chunks.append(chunk)
            elapsed = time.perf_counter() - begin
            if not chunks:
                raise RuntimeError("TTS returned done without audio.")
            result_audio = np.concatenate(chunks)
            if float(np.max(np.abs(result_audio))) < 1e-6:
                raise RuntimeError("TTS audio is effectively silent.")
            duration = len(result_audio) / sample_rate
            output = args.output.resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            sf.write(output, result_audio, sample_rate, subtype="PCM_16")
            print(json.dumps({"output": str(output), "chunks": len(chunks), "sample_rate": sample_rate,
                              "first_chunk_seconds": round(first_chunk_seconds, 3), "synthesis_seconds": round(elapsed, 3),
                              "audio_seconds": round(duration, 3), "rtf": round(elapsed / duration, 3)}, ensure_ascii=False))
            print("PASS: finite, non-empty, non-silent WAV. Listen to it; this does not test the live microphone or UI.")
    except Exception:
        print("\n--- Sidecar diagnostic tail ---", file=sys.stderr)
        print("\n".join(stderr_tail), file=sys.stderr)
        raise
    finally:
        if proc.stdin and not proc.stdin.closed:
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


if __name__ == "__main__":
    main()
