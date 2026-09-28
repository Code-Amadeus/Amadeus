"""Explicit FlashAttention import / NVIDIA kernel probe; no models or downloads."""

from __future__ import annotations

import argparse
import json
import platform


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compute", action="store_true", help="execute small GPU comparisons")
    parser.add_argument("--device", default="cuda:0")
    args = parser.parse_args()

    import torch
    import flash_attn
    # Test the native extension, not just its Python wrapper.
    import flash_attn_2_cuda  # noqa: F401

    report = {
        "python": platform.python_version(),
        "platform": platform.system(),
        "torch": torch.__version__,
        "cuda_build": torch.version.cuda,
        "hip_build": torch.version.hip,
        "cxx11_abi": torch._C._GLIBCXX_USE_CXX11_ABI,
        "flash_attn": flash_attn.__version__,
        "native_import": "passed",
        "compute": "not requested",
    }
    print(json.dumps(report), flush=True)
    if not args.compute:
        return 0
    if torch.version.hip or not torch.version.cuda:
        raise RuntimeError("this probe requires the NVIDIA CUDA FlashAttention build")
    device = torch.device(args.device)
    if device.type != "cuda" or not torch.cuda.is_available():
        raise RuntimeError("--compute requires an available CUDA device")

    from torch.nn.attention import SDPBackend, sdpa_kernel
    from torch.nn.functional import scaled_dot_product_attention

    def reference(q, k, v, *, causal):
        # Explicit math attention; do not compare one fused kernel against another.
        repeat = q.shape[2] // k.shape[2]
        with sdpa_kernel(SDPBackend.MATH):
            out = scaled_dot_product_attention(
                q.transpose(1, 2),
                k.repeat_interleave(repeat, dim=2).transpose(1, 2),
                v.repeat_interleave(repeat, dim=2).transpose(1, 2),
                is_causal=causal,
            )
        return out.transpose(1, 2)

    results = []
    torch.manual_seed(0)
    with torch.inference_mode():
        for dtype, tolerance in ((torch.float16, 0.003), (torch.bfloat16, 0.03)):
            for dim in (64, 128):
                q = torch.randn(1, 128, 8, dim, device=device, dtype=dtype)
                k = torch.randn(1, 128, 2, dim, device=device, dtype=dtype)
                v = torch.randn_like(k)
                actual = flash_attn.flash_attn_func(q, k, v, causal=True)
                expected = reference(q, k, v, causal=True)
                torch.testing.assert_close(actual, expected, atol=tolerance, rtol=tolerance)
                results.append({"case": "forward_gqa", "dtype": str(dtype), "head_dim": dim,
                                "max_abs_error": (actual - expected).abs().max().item()})

                # One decode token appended to an existing cache. The reference
                # can attend to the whole prefix including this new token.
                cache_k, cache_v = k.clone(), v.clone()
                new_q = q[:, :1]
                new_k, new_v = torch.randn_like(k[:, :1]), torch.randn_like(v[:, :1])
                expected = reference(
                    new_q, torch.cat((k[:, :32], new_k), dim=1),
                    torch.cat((v[:, :32], new_v), dim=1), causal=False,
                )
                actual = flash_attn.flash_attn_with_kvcache(
                    new_q, cache_k, cache_v, k=new_k, v=new_v,
                    cache_seqlens=torch.tensor([32], device=device, dtype=torch.int32),
                    causal=True,
                )
                torch.testing.assert_close(actual, expected, atol=tolerance, rtol=tolerance)
                torch.testing.assert_close(cache_k[:, 32:33], new_k, atol=0, rtol=0)
                torch.testing.assert_close(cache_v[:, 32:33], new_v, atol=0, rtol=0)
                results.append({"case": "kv_cache_gqa", "dtype": str(dtype), "head_dim": dim,
                                "max_abs_error": (actual - expected).abs().max().item()})
        torch.cuda.synchronize(device)
    report.update({"device": str(device), "gpu": torch.cuda.get_device_name(device),
                   "capability": torch.cuda.get_device_capability(device),
                   "compute": "passed", "comparisons": results})
    print(json.dumps(report), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
