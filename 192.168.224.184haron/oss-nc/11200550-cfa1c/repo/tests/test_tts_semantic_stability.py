from __future__ import annotations

from tts.semantic_stability import assess_semantic_candidate


def test_accepts_long_diverse_candidate_without_using_length_as_a_hard_cap() -> None:
    tokens = [index % 37 for index in range(180)]

    result = assess_semantic_candidate(
        tokens,
        target_phone_count=20,
        max_generation_tokens=400,
    )

    assert result.soft_token_budget == 96
    assert result.accepted is True
    assert result.reasons == ()


def test_rejects_checkpoint_independent_equal_token_collapse() -> None:
    tokens = list(range(24)) + [937] * 48

    result = assess_semantic_candidate(
        tokens,
        target_phone_count=48,
        max_generation_tokens=400,
    )

    assert result.accepted is False
    assert "equal_token_collapse" in result.reasons
    assert result.longest_equal_run == 48


def test_rejects_short_periodic_loop_without_token_id_exceptions() -> None:
    tokens = [11, 29, 47, 53] * 18

    result = assess_semantic_candidate(
        tokens,
        target_phone_count=6,
        max_generation_tokens=400,
    )

    assert result.accepted is False
    assert "periodic_token_collapse" in result.reasons
    assert result.periodic_run_period == 4


def test_rejects_budget_overrun_only_when_repetition_is_also_present() -> None:
    tokens = list(range(80)) + [3, 7] * 20

    result = assess_semantic_candidate(
        tokens,
        target_phone_count=6,
        max_generation_tokens=400,
    )

    assert result.token_count > result.soft_token_budget
    assert result.accepted is False
    assert result.reasons == ("length_with_repetition",)


def test_budget_overrun_with_moderate_equal_run_catches_long_filler() -> None:
    tokens = list(range(84)) + [311] * 20

    result = assess_semantic_candidate(
        tokens,
        target_phone_count=6,
        max_generation_tokens=400,
    )

    assert result.soft_token_budget == 96
    assert result.accepted is False
    assert result.reasons == ("length_with_repetition",)


def test_rejects_empty_candidate() -> None:
    result = assess_semantic_candidate(
        [],
        target_phone_count=8,
        max_generation_tokens=400,
    )

    assert result.accepted is False
    assert result.reasons == ("empty_candidate",)
