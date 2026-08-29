"""Optional FlashAttention2 KV-cache path for GPT-SoVITS T2S decoding.

This module is intentionally opt-in.  The default T2S path keeps the existing
static-KV SDPA/CUDA-Graph behavior.  When enabled, the static decode block uses
``flash_attn_with_kvcache`` for the q_len=1 autoregressive attention step.
"""

from typing import List, Optional, Tuple

import torch
from torch.nn import functional as F

from AR.models.t2s_model import T2SBlockWithStaticCache, T2SMLP

_flash_attn_error = None
_flash_attn_with_kvcache = None

try:
    from flash_attn import flash_attn_with_kvcache as _flash_attn_with_kvcache
except Exception as exc:  # pragma: no cover - optional runtime dependency
    _flash_attn_error = exc


def is_flash_attn_available() -> bool:
    return _flash_attn_with_kvcache is not None


def get_flash_attn_error() -> Optional[Exception]:
    return _flash_attn_error


class T2SBlockWithStaticCacheFlash:
    """Static-KV decode block using ``flash_attn_with_kvcache`` when possible."""

    def __init__(
        self,
        num_heads,
        hidden_dim: int,
        sdpa_block: T2SBlockWithStaticCache,
        qkv_w,
        qkv_b,
        out_w,
        out_b,
        norm_w1,
        norm_b1,
        norm_eps1,
        norm_w2,
        norm_b2,
        norm_eps2,
        mode: str = "valid",
    ):
        self.num_heads = num_heads
        self.hidden_dim = hidden_dim
        self.head_dim = hidden_dim // num_heads
        self.sdpa_block = sdpa_block
        self.qkv_w = qkv_w
        self.qkv_b = qkv_b
        self.out_w = out_w
        self.out_b = out_b
        self.norm_w1 = norm_w1
        self.norm_b1 = norm_b1
        self.norm_eps1 = norm_eps1
        self.norm_w2 = norm_w2
        self.norm_b2 = norm_b2
        self.norm_eps2 = norm_eps2
        self.mlp = sdpa_block.mlp
        self.mode = mode if mode in {"valid", "bucket"} else "valid"

    def process_prompt(
        self,
        x: torch.Tensor,
        attn_mask: torch.Tensor,
        padding_mask: Optional[torch.Tensor] = None,
        torch_sdpa: bool = True,
    ):
        return self.sdpa_block.process_prompt(x, attn_mask, padding_mask, torch_sdpa)

    def _finish_attention(self, x: torch.Tensor, attn: torch.Tensor) -> torch.Tensor:
        batch_size = x.shape[0]
        attn = attn.reshape(batch_size, 1, self.hidden_dim)
        attn = F.linear(attn, self.out_w, self.out_b)
        x = x + attn
        x = F.layer_norm(
            x, [self.hidden_dim], self.norm_w1, self.norm_b1, self.norm_eps1
        )
        x = x + self.mlp.forward(x)
        x = F.layer_norm(
            x, [self.hidden_dim], self.norm_w2, self.norm_b2, self.norm_eps2
        )
        return x

    def _flash_valid_len(
        self,
        x: torch.Tensor,
        k_cache: torch.Tensor,
        v_cache: torch.Tensor,
        pos_idx: torch.Tensor,
    ):
        batch_size = x.shape[0]
        q, k, v = F.linear(x, self.qkv_w, self.qkv_b).chunk(3, dim=-1)
        q = q.view(batch_size, 1, self.num_heads, self.head_dim)
        k = k.view(batch_size, 1, self.num_heads, self.head_dim)
        v = v.view(batch_size, 1, self.num_heads, self.head_dim)
        k_cache_fa = k_cache.view(batch_size, k_cache.shape[1], self.num_heads, self.head_dim)
        v_cache_fa = v_cache.view(batch_size, v_cache.shape[1], self.num_heads, self.head_dim)
        cache_seqlens = pos_idx[:, 0, 0].to(dtype=torch.int32)
        return _flash_attn_with_kvcache(
            q,
            k_cache_fa,
            v_cache_fa,
            k=k,
            v=v,
            cache_seqlens=cache_seqlens,
            causal=False,
        )

    def _flash_bucket_len(
        self,
        x: torch.Tensor,
        k_cache: torch.Tensor,
        v_cache: torch.Tensor,
        pos_idx: torch.Tensor,
    ):
        batch_size = x.shape[0]
        q, k, v = F.linear(x, self.qkv_w, self.qkv_b).chunk(3, dim=-1)
        k_cache.scatter_(1, pos_idx, k)
        v_cache.scatter_(1, pos_idx, v)
        q = q.view(batch_size, 1, self.num_heads, self.head_dim)
        k_cache_fa = k_cache.view(batch_size, k_cache.shape[1], self.num_heads, self.head_dim)
        v_cache_fa = v_cache.view(batch_size, v_cache.shape[1], self.num_heads, self.head_dim)
        cache_seqlens = torch.full(
            (batch_size,), k_cache.shape[1], dtype=torch.int32, device=x.device
        )
        return _flash_attn_with_kvcache(
            q,
            k_cache_fa,
            v_cache_fa,
            cache_seqlens=cache_seqlens,
            causal=False,
        )

    def decode_next_token_with_static_cache(
        self,
        x: torch.Tensor,
        k_cache: torch.Tensor,
        v_cache: torch.Tensor,
        pos_idx: torch.Tensor,
        torch_sdpa: bool = True,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        if _flash_attn_with_kvcache is None:
            return self.sdpa_block.decode_next_token_with_static_cache(
                x, k_cache, v_cache, pos_idx, torch_sdpa
            )
        try:
            if self.mode == "bucket":
                attn = self._flash_bucket_len(x, k_cache, v_cache, pos_idx)
            else:
                attn = self._flash_valid_len(x, k_cache, v_cache, pos_idx)
        except RuntimeError:
            return self.sdpa_block.decode_next_token_with_static_cache(
                x, k_cache, v_cache, pos_idx, torch_sdpa
            )
        return self._finish_attention(x, attn), k_cache, v_cache


class T2STransformerWithStaticCacheFlash:
    def __init__(self, num_blocks: int, blocks: List[T2SBlockWithStaticCacheFlash]):
        self.num_blocks = num_blocks
        self.blocks = blocks

    def process_prompt(
        self,
        x: torch.Tensor,
        attn_mask: torch.Tensor,
        padding_mask: Optional[torch.Tensor] = None,
        torch_sdpa: bool = True,
    ):
        k_cache: List[torch.Tensor] = []
        v_cache: List[torch.Tensor] = []
        for block in self.blocks:
            x, k_cache_, v_cache_ = block.process_prompt(
                x, attn_mask, padding_mask, torch_sdpa
            )
            k_cache.append(k_cache_)
            v_cache.append(v_cache_)
        return x, k_cache, v_cache

    def decode_next_token_with_static_cache(
        self,
        x: torch.Tensor,
        k_cache: List[torch.Tensor],
        v_cache: List[torch.Tensor],
        pos_idx: torch.Tensor,
        torch_sdpa: bool = True,
    ):
        for i, block in enumerate(self.blocks):
            x, k_cache[i], v_cache[i] = block.decode_next_token_with_static_cache(
                x, k_cache[i], v_cache[i], pos_idx, torch_sdpa
            )
        return x, k_cache, v_cache


def build_flash_static_transformer(decoder, mode: str = "valid"):
    blocks = []
    for i in range(decoder.num_layers):
        layer = decoder.h.layers[i]
        mlp = T2SMLP(
            layer.linear1.weight,
            layer.linear1.bias,
            layer.linear2.weight,
            layer.linear2.bias,
        )
        sdpa_block = T2SBlockWithStaticCache(
            decoder.num_head,
            decoder.model_dim,
            mlp,
            layer.self_attn.in_proj_weight,
            layer.self_attn.in_proj_bias,
            layer.self_attn.out_proj.weight,
            layer.self_attn.out_proj.bias,
            layer.norm1.weight,
            layer.norm1.bias,
            layer.norm1.eps,
            layer.norm2.weight,
            layer.norm2.bias,
            layer.norm2.eps,
        )
        blocks.append(
            T2SBlockWithStaticCacheFlash(
                decoder.num_head,
                decoder.model_dim,
                sdpa_block,
                layer.self_attn.in_proj_weight,
                layer.self_attn.in_proj_bias,
                layer.self_attn.out_proj.weight,
                layer.self_attn.out_proj.bias,
                layer.norm1.weight,
                layer.norm1.bias,
                layer.norm1.eps,
                layer.norm2.weight,
                layer.norm2.bias,
                layer.norm2.eps,
                mode=mode,
            )
        )
    return T2STransformerWithStaticCacheFlash(decoder.num_layers, blocks)


def apply_flash_attn_patch(decoder, mode: str = "valid"):
    if _flash_attn_with_kvcache is None:
        print(f"[T2S FlashAttn] flash-attn unavailable; keeping SDPA path: {_flash_attn_error}")
        return False
    decoder.t2s_transformer_static = build_flash_static_transformer(decoder, mode=mode)
    decoder.use_flash_attn_kvcache = True
    decoder.flash_attn_kvcache_mode = mode if mode in {"valid", "bucket"} else "valid"
    print(
        "[T2S FlashAttn] enabled flash_attn_with_kvcache "
        f"(mode={decoder.flash_attn_kvcache_mode})"
    )
    return True
