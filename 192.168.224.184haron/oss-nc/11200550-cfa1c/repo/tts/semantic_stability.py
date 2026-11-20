"""Model-agnostic admission checks for GPT-SoVITS semantic candidates.

The decoder samples discrete semantic tokens before SoVITS/BigVGAN renders
audio.  A collapsed candidate must be rejected at this boundary: once it is
rendered, a repeated token run becomes the very long vowel/noise that can hide
the rest of an utterance.

The checks intentionally use only sequence structure and a phone-relative
budget.  They do not encode checkpoint-specific token IDs or words.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class SemanticCandidateAssessment:
    accepted: bool
    reasons: tuple[str, ...]
    token_count: int
    longest_equal_run: int
    longest_periodic_run: int
    periodic_run_period: int
    top_token_ratio: float
    soft_token_budget: int


class SemanticGenerationError(RuntimeError):
    """Raised when every bounded semantic-generation attempt collapses."""


def _longest_equal_run(tokens: Sequence[int]) -> int:
    best = 0
    current = 0
    previous = None
    for token in tokens:
        if token == previous:
            current += 1
        else:
            previous = token
            current = 1
        best = max(best, current)
    return best


def _longest_periodic_run(
    tokens: Sequence[int], max_period: int = 8
) -> tuple[int, int]:
    best_run = 0
    best_period = 0
    for period in range(1, max_period + 1):
        current = 0
        for index in range(period, len(tokens)):
            if tokens[index] == tokens[index - period]:
                current = current + 1 if current else period + 1
            else:
                current = 0
            if current > best_run:
                best_run = current
                best_period = period
    return best_run, best_period


def assess_semantic_candidate(
    tokens: Sequence[int],
    *,
    target_phone_count: int,
    max_generation_tokens: int,
) -> SemanticCandidateAssessment:
    """Decide whether a semantic sequence is safe to send to the vocoder.

    Thresholds were selected against the paired-seed filler stress cohort.  A
    candidate is not rejected for length alone: long, diverse speech remains
    valid.  Length becomes a signal only when it coincides with repetition or
    concentration, which keeps the guard independent of language and token ID.
    """

    clean_tokens = [int(token) for token in tokens]
    token_count = len(clean_tokens)
    phone_count = max(1, int(target_phone_count))
    configured_limit = max(1, int(max_generation_tokens))
    soft_token_budget = min(
        configured_limit,
        max(96, int(math.ceil(phone_count * 4.0))),
    )

    if not clean_tokens:
        return SemanticCandidateAssessment(
            accepted=False,
            reasons=("empty_candidate",),
            token_count=0,
            longest_equal_run=0,
            longest_periodic_run=0,
            periodic_run_period=0,
            top_token_ratio=0.0,
            soft_token_budget=soft_token_budget,
        )

    counts = Counter(clean_tokens)
    equal_run = _longest_equal_run(clean_tokens)
    periodic_run, periodic_period = _longest_periodic_run(clean_tokens)
    top_token_ratio = max(counts.values()) / token_count
    equal_run_ratio = equal_run / token_count

    reasons: list[str] = []
    if equal_run >= 48 or (equal_run >= 32 and equal_run_ratio >= 0.18):
        reasons.append("equal_token_collapse")
    if periodic_run >= 64:
        reasons.append("periodic_token_collapse")
    if token_count >= 64 and top_token_ratio >= 0.55:
        reasons.append("token_concentration")
    if token_count > soft_token_budget and (
        # A 20-token identical run is roughly 0.4 s at the 50 Hz semantic
        # rate.  It is only actionable here when the complete candidate has
        # also exceeded its phone-relative budget; either signal alone is
        # intentionally accepted.
        top_token_ratio >= 0.35 or equal_run >= 20 or periodic_run >= 32
    ):
        reasons.append("length_with_repetition")

    return SemanticCandidateAssessment(
        accepted=not reasons,
        reasons=tuple(reasons),
        token_count=token_count,
        longest_equal_run=equal_run,
        longest_periodic_run=periodic_run,
        periodic_run_period=periodic_period,
        top_token_ratio=top_token_ratio,
        soft_token_budget=soft_token_budget,
    )
