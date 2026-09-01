from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Tier contract: this suite belongs to L4 (local-cu124). Skip when torch is
# absent (L1–L3) and when the GPT_SoVITS dependency chain (torchmetrics) is
# absent (torch present without the local-cu124 extra).
pytest.importorskip("torch", reason="test requires the local-cu124 tier (torch)")
pytest.importorskip("torchmetrics", reason="test requires the local-cu124 extra (torchmetrics)")

import torch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "GPT_SoVITS"))

from AR.models.t2s_model import (  # noqa: E402
    T2SBlock,
    T2SBlockWithStaticCache,
    T2SMLP,
    T2STransformerWithStaticCache,
)


def _blocks(hidden: int = 4, heads: int = 2):
    torch.manual_seed(17)
    mlp_w1 = torch.randn(hidden * 2, hidden)
    mlp_b1 = torch.randn(hidden * 2)
    mlp_w2 = torch.randn(hidden, hidden * 2)
    mlp_b2 = torch.randn(hidden)
    qkv_w = torch.randn(hidden * 3, hidden)
    qkv_b = torch.randn(hidden * 3)
    out_w = torch.randn(hidden, hidden)
    out_b = torch.randn(hidden)
    norm_w1 = torch.randn(hidden)
    norm_b1 = torch.randn(hidden)
    norm_w2 = torch.randn(hidden)
    norm_b2 = torch.randn(hidden)
    common = (
        heads,
        hidden,
        qkv_w,
        qkv_b,
        out_w,
        out_b,
        norm_w1,
        norm_b1,
        1e-5,
        norm_w2,
        norm_b2,
        1e-5,
    )
    dynamic = T2SBlock(
        common[0],
        common[1],
        T2SMLP(mlp_w1, mlp_b1, mlp_w2, mlp_b2),
        *common[2:],
    )
    static = T2SBlockWithStaticCache(
        common[0],
        common[1],
        T2SMLP(mlp_w1, mlp_b1, mlp_w2, mlp_b2),
        *common[2:],
    )
    return dynamic, static


def test_static_mask_matches_dynamic_attention_with_gap_and_future_garbage() -> None:
    dynamic, static = _blocks()
    torch.manual_seed(23)
    x = torch.randn(1, 1, 4)
    prefix_k = torch.randn(1, 3, 4)
    prefix_v = torch.randn(1, 3, 4)

    expected, _, _ = dynamic.decode_next_token(
        x.clone(), prefix_k.clone(), prefix_v.clone()
    )

    # Positions 3-4 are an invalid graph-alignment gap; positions 6-7 mimic
    # stale/future memory.  Only prefix 0-2 and current write position 5 count.
    static_k = torch.randn(1, 8, 4) * 50
    static_v = torch.randn(1, 8, 4) * 50
    static_k[:, :3] = prefix_k
    static_v[:, :3] = prefix_v
    pos_idx = torch.full((1, 1, 4), 5, dtype=torch.long)
    valid = torch.zeros(1, 8, dtype=torch.bool)
    valid[:, :3] = True
    transformer = T2STransformerWithStaticCache(1, [static])

    actual, _, _ = transformer.decode_next_token_with_static_cache(
        x.clone(), [static_k], [static_v], pos_idx, valid
    )

    torch.testing.assert_close(actual, expected, rtol=1e-5, atol=1e-5)
    assert valid.tolist() == [[True, True, True, False, False, True, False, False]]


def test_masked_future_values_cannot_change_static_decode() -> None:
    _, static = _blocks()
    torch.manual_seed(29)
    x = torch.randn(1, 1, 4)
    prefix_k = torch.randn(1, 3, 4)
    prefix_v = torch.randn(1, 3, 4)
    pos_idx = torch.full((1, 1, 4), 3, dtype=torch.long)

    def decode(future_scale: float) -> torch.Tensor:
        k_cache = torch.randn(1, 8, 4) * future_scale
        v_cache = torch.randn(1, 8, 4) * future_scale
        k_cache[:, :3] = prefix_k
        v_cache[:, :3] = prefix_v
        valid = torch.zeros(1, 8, dtype=torch.bool)
        valid[:, :3] = True
        output, _, _ = static.decode_next_token_with_static_cache(
            x.clone(), k_cache, v_cache, pos_idx, valid
        )
        return output

    torch.testing.assert_close(decode(1.0), decode(1000.0), rtol=1e-5, atol=1e-5)


def test_non_graph_static_prefix_matches_dynamic_without_mask_kernel() -> None:
    dynamic, static = _blocks()
    torch.manual_seed(31)
    x = torch.randn(1, 1, 4)
    prefix_k = torch.randn(1, 3, 4)
    prefix_v = torch.randn(1, 3, 4)
    expected, _, _ = dynamic.decode_next_token(
        x.clone(), prefix_k.clone(), prefix_v.clone()
    )
    k_cache = torch.randn(1, 8, 4) * 100
    v_cache = torch.randn(1, 8, 4) * 100
    k_cache[:, :3] = prefix_k
    v_cache[:, :3] = prefix_v
    pos_idx = torch.full((1, 1, 4), 3, dtype=torch.long)
    valid = torch.zeros(1, 8, dtype=torch.bool)
    valid[:, :3] = True
    transformer = T2STransformerWithStaticCache(1, [static])

    actual, _, _ = transformer.decode_next_token_with_static_cache(
        x.clone(), [k_cache], [v_cache], pos_idx, valid, 4
    )

    torch.testing.assert_close(actual, expected, rtol=1e-5, atol=1e-5)
