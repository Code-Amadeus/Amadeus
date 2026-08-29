"""
Qwen3-ASR-0.6B sidecar 子进程

运行环境：.venv_asr（包含 qwen-asr / torch 2.6+cu124 / transformers 4.57.6）

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
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# 每秒语音对应的预期最大 token 数（中文约 4-6 字/秒 × 1.5× 余量）
_TOKENS_PER_SEC = 10
# 绝对上限：与原始设置持平，30s × 6字/s + 标点 ≈ 220，256 有足够安全边际
_MAX_TOKENS_CAP = 256
# 绝对下限：保证短音频也有足够空间
_MAX_TOKENS_FLOOR = 32


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _calc_max_tokens(duration_s: float) -> int:
    """根据音频时长动态计算 max_new_tokens，避免为短句等待过多。"""
    estimated = int(duration_s * _TOKENS_PER_SEC)
    return max(_MAX_TOKENS_FLOOR, min(estimated, _MAX_TOKENS_CAP))


def main():
    import numpy as np
    import torch
    from qwen_asr import Qwen3ASRModel

    requested_device = os.environ.get("QWEN3_ASR_DEVICE", "auto").strip().lower()
    cuda_available = bool(torch.cuda.is_available())
    if requested_device in {"cuda", "cuda:0", "gpu"}:
        device_map = "cuda:0" if cuda_available else "cpu"
    elif requested_device == "cpu":
        device_map = "cpu"
    else:
        device_map = "cuda:0" if cuda_available else "cpu"
    dtype = torch.bfloat16 if "cuda" in device_map else torch.float32
    require_cuda = os.environ.get("QWEN3_ASR_REQUIRE_CUDA", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
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

    # 注意实现选择优先级：
    #   1. flash_attention_2 — 最快，需要 flash-attn 包（Windows 源码编译困难）
    #   2. sdpa             — PyTorch 2.0+ 内置，无额外依赖，性能接近 FA2
    #   3. eager            — 默认，最慢
    extra_kwargs = {}
    attn_impl = "eager"
    if "cuda" in device_map:
        try:
            import flash_attn  # noqa: F401
            extra_kwargs["attn_implementation"] = "flash_attention_2"
            attn_impl = "flash_attention_2"
        except ImportError:
            # sdpa：torch 2.0+ 内置，不需要额外包，加速显著
            extra_kwargs["attn_implementation"] = "sdpa"
            attn_impl = "sdpa"

    try:
        model = Qwen3ASRModel.from_pretrained(
            "Qwen/Qwen3-ASR-0.6B",
            dtype=dtype,
            device_map=device_map,
            max_inference_batch_size=1,
            max_new_tokens=_MAX_TOKENS_CAP,   # 初始上限；运行时按音频时长动态覆盖
            **extra_kwargs,
        )
        _emit({"type": "ready", "device": device_map, "attn_impl": attn_impl})
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

            # max_new_tokens 是实例属性，transcribe() 不接受该参数
            # 调用前临时覆盖，调用后恢复
            _prev_tokens = model.max_new_tokens
            model.max_new_tokens = max_tokens
            try:
                results = model.transcribe(
                    audio=(audio, sr),
                    language=language,
                    context=context,
                )
            finally:
                model.max_new_tokens = _prev_tokens
            text = (results[0].text if results else "").strip()
            _emit({"type": "result", "text": text})
        except Exception as e:
            _emit({"type": "error", "msg": str(e)})


if __name__ == "__main__":
    main()
