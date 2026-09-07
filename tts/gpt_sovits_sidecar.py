"""Persistent GPT-SoVITS subprocess using a JSON-lines transport.

The parent process owns request serialization.  This process owns the heavy
PyTorch/model runtime so Amadeus can keep its regular Python environment while
speech synthesis runs in a separate CUDA/ROCm environment.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from typing import Any


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_PROTOCOL_STDOUT = None


def _emit(message: dict[str, Any]) -> None:
    stream = _PROTOCOL_STDOUT or sys.stdout
    stream.write(json.dumps(message, ensure_ascii=True) + "\n")
    stream.flush()


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _request_kwargs(payload: dict[str, Any], *, streaming: bool) -> dict[str, Any]:
    options = dict(payload.get("options") or {})
    options.pop("text_language", None)
    options.pop("prompt_language", None)
    options.pop("speed", None)
    if not streaming:
        options.pop("collect_t2s_stats", None)
    kwargs = {
        "ref_audio_path": str(payload.get("reference_audio") or ""),
        "prompt_text": str(payload.get("reference_text") or ""),
        "text_language": (payload.get("options") or {}).get(
            "text_language", payload.get("language") or "auto"
        ),
        "prompt_language": (payload.get("options") or {}).get(
            "prompt_language",
            payload.get("reference_language") or payload.get("language") or "auto",
        ),
        "speed": float(payload.get("speed", 1.0)),
        **options,
    }
    if streaming:
        kwargs["chunk_size_seconds"] = payload.get("chunk_size_seconds")
    return kwargs


def _emit_chunk(
    request_id: str,
    sample_rate: int,
    audio: Any,
    text: str = "",
) -> None:
    import numpy as np

    # infer_stream starts with a (sample_rate, None, "") metadata sentinel.
    # It is useful to embedded callers but must not be serialized as PCM:
    # np.asarray(None, dtype=float32) becomes a one-sample NaN payload.
    if audio is None:
        return
    pcm = np.ascontiguousarray(audio, dtype="<f4")
    if not np.isfinite(pcm).all():
        raise ValueError("GPT-SoVITS produced non-finite audio")
    _emit(
        {
            "type": "chunk",
            "request_id": request_id,
            "sample_rate": int(sample_rate),
            "audio_b64": base64.b64encode(pcm.tobytes()).decode("ascii"),
            "text": str(text or ""),
        }
    )


def main() -> None:
    global _PROTOCOL_STDOUT

    # GPT-SoVITS and its dependencies use print() for diagnostics.  Keep
    # stdout a protocol-only channel during model import and inference.
    _PROTOCOL_STDOUT = sys.stdout
    sys.stdout = sys.stderr

    import torch

    from config import settings
    from local_tts_infer import TTSInferencer

    requested_device = str(settings.TTS_DEVICE or "cpu")
    cuda_available = bool(torch.cuda.is_available())
    torch_version = str(getattr(torch, "__version__", "?"))
    hip_version = getattr(torch.version, "hip", None)
    if hip_version and cuda_available:
        # The Windows ROCm build exposes AMD GPUs through torch.cuda, but its
        # flash/memory-efficient SDPA paths currently fail with
        # hipErrorInvalidValue on this model.  Force the stable math kernel
        # before GPT-SoVITS constructs any attention modules.
        torch.backends.cuda.enable_flash_sdp(False)
        torch.backends.cuda.enable_mem_efficient_sdp(False)
        torch.backends.cuda.enable_math_sdp(True)
    try:
        inferencer = TTSInferencer(
            device=requested_device,
            gpt_path=settings.TTS_GPT_MODEL_PATH or None,
            sovits_path=settings.TTS_SOVITS_MODEL_PATH or None,
        )
        actual_device = str(getattr(inferencer, "device", requested_device))
        if _truthy("TTS_REQUIRE_CUDA") and (
            not cuda_available or not actual_device.lower().startswith("cuda")
        ):
            raise RuntimeError(
                "TTS_REQUIRE_CUDA=1 but the sidecar did not initialize on a GPU "
                f"(requested={requested_device}, actual={actual_device}, "
                f"cuda_available={cuda_available})"
            )
    except Exception as exc:
        _emit(
            {
                "type": "error",
                "msg": f"LOAD_FAIL: {exc}",
                "device": requested_device,
                "torch": torch_version,
                "hip": hip_version,
                "cuda_available": cuda_available,
            }
        )
        raise SystemExit(1) from exc

    _emit(
        {
            "type": "ready",
            "device": actual_device,
            "torch": torch_version,
            "hip": hip_version,
            "cuda_available": cuda_available,
        }
    )

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        request_id = ""
        try:
            message = json.loads(line)
            request_id = str(message.get("request_id") or "")
            operation = str(message.get("type") or "")
            payload = dict(message.get("request") or {})
            text = str(payload.get("text") or "")
            if not request_id:
                raise ValueError("missing request_id")

            if operation == "infer":
                sample_rate, audio = inferencer.infer(
                    text=text,
                    **_request_kwargs(payload, streaming=False),
                )
                _emit_chunk(request_id, sample_rate, audio, text)
            elif operation == "infer_stream":
                for item in inferencer.infer_stream(
                    text=text,
                    **_request_kwargs(payload, streaming=True),
                ):
                    if len(item) == 2:
                        sample_rate, audio = item
                        chunk_text = ""
                    else:
                        sample_rate, audio, chunk_text = item
                    _emit_chunk(request_id, sample_rate, audio, str(chunk_text or ""))
            else:
                raise ValueError(f"unsupported operation: {operation!r}")
            _emit({"type": "done", "request_id": request_id})
        except Exception as exc:
            _emit({"type": "error", "request_id": request_id, "msg": str(exc)})


if __name__ == "__main__":
    main()
