from __future__ import annotations

from core.continuity.text_excerpt import (
    DeterministicTextCompressor,
    ModelTextCompressor,
    compress_excerpt,
)


def test_short_text_is_returned_unchanged() -> None:
    assert compress_excerpt("你好，昨天去海边了。", 100) == "你好，昨天去海边了。"
    assert compress_excerpt("", 10) == ""


def test_long_text_keeps_head_sampled_middle_and_tail_within_budget() -> None:
    text = "头" + "中" * 500 + "尾"
    excerpt = compress_excerpt(text, 120)
    assert len(excerpt) <= 120
    assert excerpt.startswith("头")
    assert excerpt.endswith("尾")
    # Whole-turn compression: head, middle windows and tail are all present
    # instead of a tail cut that loses the ending.
    assert excerpt.count("…") >= 2
    assert len(excerpt) >= 100


def test_compression_is_deterministic_and_safe_at_tiny_budgets() -> None:
    text = "abcdefghij" * 30
    assert compress_excerpt(text, 40) == compress_excerpt(text, 40)
    assert len(compress_excerpt(text, 3)) <= 3
    assert len(compress_excerpt(text, 10)) <= 10


def test_non_positive_budget_returns_empty() -> None:
    assert compress_excerpt("abc", 0) == ""
    assert compress_excerpt("abc", -5) == ""


def test_deterministic_compressor_matches_the_sampler() -> None:
    compressor = DeterministicTextCompressor()
    text = "头" + "内容" * 300 + "尾"
    assert compressor.compress(text, 120) == compress_excerpt(text, 120)
    assert compressor.requires_model is False


def test_model_compressor_uses_semantic_output_within_budget() -> None:
    calls: list[tuple[str, str]] = []

    def fake_complete(system: str, user: str) -> str:
        calls.append((system, user))
        return "语意摘要：保留了关键决定与数字 42。"

    compressor = ModelTextCompressor(fake_complete)
    text = "很长的原文" * 500
    out = compressor.compress(text, 200, speaker="用户发言")

    assert out == "语意摘要：保留了关键决定与数字 42。"
    assert compressor.requires_model is True
    assert len(calls) == 1
    assert "用户发言" in calls[0][1]


def test_model_compressor_bounds_overlong_model_output() -> None:
    compressor = ModelTextCompressor(lambda system, user: "长" * 500)
    out = compressor.compress("原文" * 500, 50)
    assert len(out) <= 50


def test_model_compressor_falls_back_when_model_fails_or_is_empty() -> None:
    def boom(system: str, user: str) -> str:
        raise RuntimeError("offline")

    text = "头" + "内容" * 300 + "尾"
    assert ModelTextCompressor(boom).compress(text, 120) == compress_excerpt(text, 120)
    assert ModelTextCompressor(lambda system, user: "   ").compress(text, 120) == compress_excerpt(
        text, 120
    )


def test_model_compressor_skips_the_model_for_text_within_budget() -> None:
    calls: list[int] = []
    compressor = ModelTextCompressor(lambda system, user: calls.append(1) or "x")
    assert compressor.compress("短文本", 100) == "短文本"
    assert calls == []


def test_model_compressor_caches_repeat_compressions() -> None:
    calls: list[int] = []

    def fake_complete(system: str, user: str) -> str:
        calls.append(1)
        return "摘要"

    compressor = ModelTextCompressor(fake_complete)
    text = "原文" * 500
    assert compressor.compress(text, 100) == "摘要"
    assert compressor.compress(text, 100) == "摘要"
    assert len(calls) == 1
