"""Microbenchmarks for T2S decode attention shapes.

This script intentionally does not import the GPT-SoVITS model. It isolates the
current autoregressive decode shape used by the T2S static KV cache:

    batch=1, heads=8, head_dim=64, q_len=1, bucket in {448,512,768,1024}

It compares PyTorch SDPA with flash_attn_with_kvcache in eager and CUDA Graph
replay modes, plus a synthetic single-block decode approximation.
"""

from __future__ import annotations

import argparse

import torch
from torch.nn import functional as F

try:
    from flash_attn import flash_attn_func, flash_attn_with_kvcache
except ImportError as exc:  # pragma: no cover - diagnostics script
    raise SystemExit("flash-attn is not installed in this environment") from exc


def _bench_eager(fn, warmup: int, iters: int) -> float:
    for _ in range(warmup):
        fn()
    torch.cuda.synchronize()
    start = torch.cuda.Event(enable_timing=True)
    end = torch.cuda.Event(enable_timing=True)
    start.record()
    for _ in range(iters):
        fn()
    end.record()
    torch.cuda.synchronize()
    return start.elapsed_time(end) / iters


def _bench_graph(fn, warmup: int, iters: int) -> float:
    for _ in range(warmup):
        fn()
    torch.cuda.synchronize()

    graph = torch.cuda.CUDAGraph()
    with torch.cuda.graph(graph):
        fn()
    torch.cuda.synchronize()

    for _ in range(warmup):
        graph.replay()
    torch.cuda.synchronize()

    start = torch.cuda.Event(enable_timing=True)
    end = torch.cuda.Event(enable_timing=True)
    start.record()
    for _ in range(iters):
        graph.replay()
    end.record()
    torch.cuda.synchronize()
    return start.elapsed_time(end) / iters


def attention_bench(buckets: list[int], warmup: int, iters: int) -> None:
    batch, heads, head_dim = 1, 8, 64
    print("attention-only, q_len=1")
    print(
        f"{'bucket':>6} {'sdpa_eager':>12} {'fa_func':>12} "
        f"{'fa_kv_eager':>12} {'sdpa_graph':>12} {'fa_kv_graph':>12}"
    )
    for bucket in buckets:
        q_bhsd = torch.randn(batch, heads, 1, head_dim, device="cuda", dtype=torch.float16)
        k_bhsd = torch.randn(batch, heads, bucket, head_dim, device="cuda", dtype=torch.float16)
        v_bhsd = torch.randn(batch, heads, bucket, head_dim, device="cuda", dtype=torch.float16)
        q_bshd = q_bhsd.transpose(1, 2).contiguous()
        k_bshd = k_bhsd.transpose(1, 2).contiguous()
        v_bshd = v_bhsd.transpose(1, 2).contiguous()
        cache_seqlens = torch.tensor([bucket], device="cuda", dtype=torch.int32)

        def sdpa():
            return F.scaled_dot_product_attention(q_bhsd, k_bhsd, v_bhsd)

        def flash_func():
            return flash_attn_func(q_bshd, k_bshd, v_bshd, dropout_p=0.0, causal=False)

        def flash_kv():
            return flash_attn_with_kvcache(
                q_bshd, k_bshd, v_bshd, cache_seqlens=cache_seqlens, causal=False
            )

        sdpa_eager = _bench_eager(sdpa, warmup, iters)
        fa_func = _bench_eager(flash_func, warmup, iters)
        fa_kv_eager = _bench_eager(flash_kv, warmup, iters)
        sdpa_graph = _bench_graph(sdpa, warmup, iters)
        fa_kv_graph = _bench_graph(flash_kv, warmup, iters)

        print(
            f"{bucket:6d} {sdpa_eager * 1000:10.3f}us {fa_func * 1000:10.3f}us "
            f"{fa_kv_eager * 1000:10.3f}us {sdpa_graph * 1000:10.3f}us "
            f"{fa_kv_graph * 1000:10.3f}us"
        )


def synthetic_block_bench(buckets: list[int], warmup: int, iters: int) -> None:
    batch, heads, head_dim = 1, 8, 64
    channels = heads * head_dim
    valid_len = 352

    print()
    print("synthetic single T2S block, CUDA Graph replay")
    print(f"{'bucket':>6} {'sdpa_block':>12} {'fa_kv_block':>12} {'delta':>12}")
    for bucket in buckets:
        x = torch.randn(batch, 1, channels, device="cuda", dtype=torch.float16)
        qkv_w = torch.randn(3 * channels, channels, device="cuda", dtype=torch.float16) * 0.02
        qkv_b = torch.randn(3 * channels, device="cuda", dtype=torch.float16) * 0.02
        out_w = torch.randn(channels, channels, device="cuda", dtype=torch.float16) * 0.02
        out_b = torch.randn(channels, device="cuda", dtype=torch.float16) * 0.02
        n1w = torch.ones(channels, device="cuda", dtype=torch.float16)
        n1b = torch.zeros(channels, device="cuda", dtype=torch.float16)
        n2w = torch.ones(channels, device="cuda", dtype=torch.float16)
        n2b = torch.zeros(channels, device="cuda", dtype=torch.float16)
        mlp1w = torch.randn(4 * channels, channels, device="cuda", dtype=torch.float16) * 0.02
        mlp1b = torch.randn(4 * channels, device="cuda", dtype=torch.float16) * 0.02
        mlp2w = torch.randn(channels, 4 * channels, device="cuda", dtype=torch.float16) * 0.02
        mlp2b = torch.randn(channels, device="cuda", dtype=torch.float16) * 0.02
        pos_idx = torch.full((batch, 1, channels), valid_len, dtype=torch.long, device="cuda")
        cache_len = torch.tensor([valid_len], dtype=torch.int32, device="cuda")

        k_cache_sdpa = torch.zeros(batch, bucket, channels, device="cuda", dtype=torch.float16)
        v_cache_sdpa = torch.zeros(batch, bucket, channels, device="cuda", dtype=torch.float16)
        k_cache_sdpa[:, :valid_len, :] = torch.randn(
            batch, valid_len, channels, device="cuda", dtype=torch.float16
        )
        v_cache_sdpa[:, :valid_len, :] = torch.randn(
            batch, valid_len, channels, device="cuda", dtype=torch.float16
        )
        k_cache_fa = k_cache_sdpa.view(batch, bucket, heads, head_dim).contiguous().clone()
        v_cache_fa = v_cache_sdpa.view(batch, bucket, heads, head_dim).contiguous().clone()

        def mlp(y):
            return F.linear(F.gelu(F.linear(y, mlp1w, mlp1b)), mlp2w, mlp2b)

        def block_sdpa():
            q, k, v = F.linear(x, qkv_w, qkv_b).chunk(3, dim=-1)
            k_cache_sdpa.scatter_(1, pos_idx, k)
            v_cache_sdpa.scatter_(1, pos_idx, v)
            qh = q.view(batch, 1, heads, head_dim).transpose(1, 2)
            kh = k_cache_sdpa.view(batch, bucket, heads, head_dim).transpose(1, 2)
            vh = v_cache_sdpa.view(batch, bucket, heads, head_dim).transpose(1, 2)
            attn = F.scaled_dot_product_attention(qh, kh, vh).transpose(1, 2)
            y = x + F.linear(attn.reshape(batch, 1, channels), out_w, out_b)
            y = F.layer_norm(y, [channels], n1w, n1b, 1e-5)
            y = y + mlp(y)
            return F.layer_norm(y, [channels], n2w, n2b, 1e-5)

        def block_flash_kv():
            q, k, v = F.linear(x, qkv_w, qkv_b).chunk(3, dim=-1)
            attn = flash_attn_with_kvcache(
                q.view(batch, 1, heads, head_dim),
                k_cache_fa,
                v_cache_fa,
                k=k.view(batch, 1, heads, head_dim),
                v=v.view(batch, 1, heads, head_dim),
                cache_seqlens=cache_len,
                causal=False,
            )
            y = x + F.linear(attn.reshape(batch, 1, channels), out_w, out_b)
            y = F.layer_norm(y, [channels], n1w, n1b, 1e-5)
            y = y + mlp(y)
            return F.layer_norm(y, [channels], n2w, n2b, 1e-5)

        sdpa_ms = _bench_graph(block_sdpa, warmup, iters)
        fa_ms = _bench_graph(block_flash_kv, warmup, iters)
        print(
            f"{bucket:6d} {sdpa_ms * 1000:10.3f}us {fa_ms * 1000:10.3f}us "
            f"{(fa_ms - sdpa_ms) * 1000:+10.3f}us"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iters", type=int, default=3000)
    parser.add_argument("--warmup", type=int, default=200)
    parser.add_argument("--buckets", default="448,512,768,1024")
    args = parser.parse_args()

    if not torch.cuda.is_available():
        raise SystemExit("CUDA is required")

    buckets = [int(item.strip()) for item in args.buckets.split(",") if item.strip()]
    print(f"torch={torch.__version__} cuda={torch.version.cuda}")
    print(f"device={torch.cuda.get_device_name(0)}")
    attention_bench(buckets, args.warmup, args.iters)
    synthetic_block_bench(buckets, min(args.warmup, 50), args.iters)


if __name__ == "__main__":
    main()
