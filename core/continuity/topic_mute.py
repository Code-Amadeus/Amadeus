"""User-governed topic suppression for Continuity recall.

A mute is not memory truth and not explicit forget.  It filters *proactive*
surfacing of a topic inside Host-selected continuity context while the durable
memory rows and the Session transcript stay untouched.  A turn may surface a
muted topic again when the user raises it themselves, so suppression fails
toward quiet instead of destroying recall.

Matching is deterministic n-gram similarity plus containment; no model call is
involved and no topic list ever becomes prompt structure (it is rendered as
quoted data by the context renderer).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Sequence

# One shared normalization/coverage vocabulary for archive search and mutes so
# "which text is about the same topic" has a single implementation.
MUTE_COVERAGE_THRESHOLD = 0.4
MUTE_TOPIC_MAX_CHARS = 120
MUTE_MAX_ACTIVE = 100


def normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return re.sub(r"\s+", " ", text)


def text_trigrams(value: str) -> set[str]:
    text = re.sub(r"\s+", "", normalize_text(value))
    if not text:
        return set()
    if len(text) <= 3:
        return {text}
    return {text[i : i + 3] for i in range(len(text) - 2)}


def ngram_coverage(query: str, text: str) -> float:
    q = text_trigrams(query)
    t = text_trigrams(text)
    if not q or not t:
        return 0.0
    return len(q & t) / max(1, min(len(q), 24))


def normalize_topic(topic: str) -> str:
    """Normalize a user-authored mute topic without retaining prompt control chars."""

    text = unicodedata.normalize("NFKC", str(topic or "")).replace("\x00", "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:MUTE_TOPIC_MAX_CHARS].strip()


def text_matches_topic(topic: str, text: str) -> bool:
    normalized_topic = normalize_text(topic)
    normalized_text = normalize_text(text)
    if not normalized_topic or not normalized_text:
        return False
    if normalized_topic in normalized_text:
        return True
    return ngram_coverage(topic, text) >= MUTE_COVERAGE_THRESHOLD


def topic_is_raised_by_query(topic: str, query: str) -> bool:
    """Whether this user turn itself raises the muted topic (recall is allowed)."""

    return text_matches_topic(topic, query)


def muted_topics_for_text(text: str, topics: Sequence[str]) -> tuple[str, ...]:
    return tuple(topic for topic in topics if text_matches_topic(topic, text))


def text_is_muted(
    text: str,
    *,
    topics: Sequence[str],
    exempt_topics: Sequence[str] = (),
) -> bool:
    """True when `text` concerns a muted topic that this turn did not raise."""

    exempt = {normalize_text(topic) for topic in exempt_topics}
    for topic in topics:
        if normalize_text(topic) in exempt:
            continue
        if text_matches_topic(topic, text):
            return True
    return False


__all__ = [
    "MUTE_COVERAGE_THRESHOLD",
    "MUTE_MAX_ACTIVE",
    "MUTE_TOPIC_MAX_CHARS",
    "muted_topics_for_text",
    "ngram_coverage",
    "normalize_text",
    "normalize_topic",
    "text_is_muted",
    "text_matches_topic",
    "text_trigrams",
    "topic_is_raised_by_query",
]
