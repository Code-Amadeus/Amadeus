"""
Qwen3-ASR-0.6B sidecar 子进程

运行环境：仓库根 `.venv`（L4 local-cu124 梯级，包含 qwen-asr / torch 2.6.0+cu124 / transformers 4.57.6）

IPC 协议（stdin/stdout JSON Lines）：
  父进程 → 子进程：{
      "audio_b64":   "<base64 float32 LE>",
      "sample_rate": 16000,
      "context":     "...",          # 可选热词/domain prompt
      "language":    "zh"            # 可选，默认 "zh"；传 null 强制自动检测
  }
  子进程 → 父进程：{"type": "ready"}
                   {"type": "result", "text": "..."}
                   {"type": "error",  "msg":  "..."}
"""
import sys
import json
import base64
import os
import time
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# PyTorch 2.1 兼容 shim（保险起见保留）
try:
    import torch.utils._pytree as _pt
    if not hasattr(_pt, "register_pytree_node") and hasattr(_pt, "_register_pytree_node"):
        _orig = _pt._register_pytree_node
        def _compat(cls, f, u, *, serialized_type_name=None, **kw):
            return _orig(cls, f, u)
        _pt.register_pytree_node = _compat
except Exception:
    pass

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
from config.local_model_loading import enforce_local_model_loading

enforce_local_model_loading()

# 每秒语音对应的预期最大 token 数（中文约 4-6 字/秒 × 1.5× 余量）
_TOKENS_PER_SEC = 10
# 绝对上限：与原始设置持平，30s × 6字/s + 标点 ≈ 220，256 有足够安全边际
_MAX_TOKENS_CAP = 256
# 绝对下限：保证短音频也有足够空间
_MAX_TOKENS_FLOOR = 32


def _emit(obj: dict) -> None:
    # Keep the JSONL transport ASCII-only so Windows pipe code pages cannot
    # corrupt Chinese/Japanese transcription text before the parent decodes it.
    sys.stdout.write(json.dumps(obj, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def _calc_max_tokens(duration_s: float) -> int:
    """根据音频时长动态计算 max_new_tokens，避免为短句等待过多。"""
    estimated = int(duration_s * _TOKENS_PER_SEC)
    return max(_MAX_TOKENS_FLOOR, min(estimated, _MAX_TOKENS_CAP))


def main():
    import numpy as np
    import torch
    from asr.backend import asr_call_context
    from asr.qwen_inference import ENGINES, apply_qwen_attention_policy, qwen_attention_configs, transcribe_qwen
    from asr.qwen_cuda_graph import prepare_qwen_cuda_graphs, qwen_cuda_graph_support_reason
    from qwen_asr import Qwen3ASRModel
    from asr.qwen_model import resolve_qwen_model_source

    requested_device = os.environ["QWEN3_ASR_DEVICE"]
    cuda_available = bool(torch.cuda.is_available())
    if requested_device in {"cuda", "cuda:0", "gpu"}:
        device_map = "cuda:0" if cuda_available else "cpu"
    elif requested_device == "cpu":
        device_map = "cpu"
    else:
        device_map = "cuda:0" if cuda_available else "cpu"
    dtype = torch.bfloat16 if "cuda" in device_map else torch.float32
    # Parent supplies its parsed startup snapshot, alongside the resolved device.
    require_cuda = os.environ["QWEN3_ASR_REQUIRE_CUDA"] == "true"
    if requested_device in {"cuda", "cuda:0", "gpu"} and device_map == "cpu" and require_cuda:
        _emit({
            "type": "error",
            "msg": (
                "LOAD_FAIL: requested CUDA but torch reports CUDA unavailable "
                f"(exe={sys.executable}, torch={getattr(torch, '__version__', '?')}, "
                f"torch_cuda={getattr(torch.version, 'cuda', None)}, "
                f"visible={os.environ.get('CUDA_VISIBLE_DEVICES')})"
            ),
        })
        sys.exit(1)

    # Select an available loader backend. The shared post-load policy below
    # uses FA2 audio with SDPA text on supported NVIDIA installations; loader
    # availability alone does not establish which attention path runs fastest.
    extra_kwargs = {}
    attn_impl = "eager"
    if "cuda" in device_map:
        # PyTorch ROCm intentionally exposes AMD GPUs through the torch.cuda API.
        # The current Windows ROCm build can return invalid or silently incorrect
        # results through its default SDPA kernels, so prefer the stable eager path.
        if getattr(torch.version, "hip", None):
            extra_kwargs["attn_implementation"] = "eager"
            attn_impl = "eager"
        else:
            try:
                import flash_attn  # noqa: F401
                extra_kwargs["attn_implementation"] = "flash_attention_2"
                attn_impl = "flash_attention_2"
            except ImportError:
                # sdpa：torch 2.0+ 内置，不需要额外包，加速显著
                extra_kwargs["attn_implementation"] = "sdpa"
                attn_impl = "sdpa"

    try:
        engine = os.environ.get("QWEN3_ASR_INFERENCE_ENGINE", "official")
        if engine not in ENGINES:
            raise ValueError(f"Unknown Qwen inference engine: {engine}")
        model = Qwen3ASRModel.from_pretrained(
            resolve_qwen_model_source(),
            local_files_only=True,
            dtype=dtype,
            device_map=device_map,
            max_inference_batch_size=1,
            max_new_tokens=_MAX_TOKENS_CAP,   # 初始上限；运行时按音频时长动态覆盖
            **extra_kwargs,
        )
        if getattr(torch.version, "hip", None):
            # qwen-asr 0.0.6 creates its nested thinker/audio/text configs after
            # the initial attention setter runs. Re-apply the public Transformers
            # setter after loading so every nested model leaves the broken ROCm
            # SDPA kernels and uses eager attention.
            hf_model = getattr(model, "model", None)
            set_attention = getattr(hf_model, "set_attn_implementation", None)
            if callable(set_attention):
                set_attention("eager")
            else:
                torch.backends.cuda.enable_flash_sdp(False)
                torch.backends.cuda.enable_mem_efficient_sdp(False)
                torch.backends.cuda.enable_math_sdp(True)
        else:
            apply_qwen_attention_policy(model.model, attn_impl)
        warmup_ms = 0
        warmup_setting = os.environ.get("QWEN3_ASR_WARMUP")
        warmup_enabled = bool(getattr(torch.version, "hip", None)) if warmup_setting is None else (
            warmup_setting.strip().lower() not in {"0", "false", "no", "off"}
        )
        if "cuda" in device_map and warmup_enabled:
            # Compile/initialise ROCm kernels before the first microphone request.
            # Two seconds approximates a short utterance while keeping startup bounded.
            warmup_started = time.perf_counter()
            previous_tokens = model.max_new_tokens
            model.max_new_tokens = _MAX_TOKENS_FLOOR
            try:
                model.transcribe(
                    audio=(np.zeros(32000, dtype=np.float32), 16000),
                    language="Chinese",
                    context="",
                )
                torch.cuda.synchronize()
            finally:
                model.max_new_tokens = previous_tokens
            warmup_ms = round((time.perf_counter() - warmup_started) * 1000)
        graph_preparation = None
        if engine == "cuda_graph" and qwen_cuda_graph_support_reason(model.model) is None:
            graph_preparation = prepare_qwen_cuda_graphs(model.model, capture_allowed=True)
        _emit({
            "type": "ready",
            "device": device_map,
            "attn_impl": attn_impl,
            "loader_attention_requested": attn_impl,
            "attention_configs": qwen_attention_configs(model.model),
            "warmup_ms": warmup_ms,
            "graph_preparation": graph_preparation,
        })
    except Exception as e:
        _emit({"type": "error", "msg": f"LOAD_FAIL: {e}"})
        sys.exit(1)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            audio_bytes = base64.b64decode(req["audio_b64"])
            audio = np.frombuffer(audio_bytes, dtype=np.float32).copy()
            sr = int(req.get("sample_rate", 16000))

            # 按音频时长动态计算 max_new_tokens
            duration_s = len(audio) / sr
            max_tokens = _calc_max_tokens(duration_s)

            context = req.get("context", "")
            # language: 默认 "Chinese"，父进程可传 null 强制自动检测
            # 注意：qwen-asr 用全名（"Chinese"/"English"），不接受 ISO 代码（"zh"/"en"）
            lang_val = req.get("language", "Chinese")
            language = lang_val if lang_val else None

            with asr_call_context(req.get("request_kind", "direct"), req.get("request_id")):
                results, metrics = transcribe_qwen(
                    model, audio, sr, context, language, max_tokens,
                    engine=req.get("engine", engine), profile=bool(req.get("profile", False)))
            text = (results[0].text if results else "").strip()
            metrics["loader_attention_requested"] = attn_impl
            _emit({"type": "result", "text": text, "metrics": metrics})
        except Exception as e:
            _emit({"type": "error", "msg": str(e)})


if __name__ == "__main__":
    main()
