"""Canonical SpriteForge semantic-intent routing.

Semantic labels such as ``thinking`` and ``work`` must be resolved once on the
backend before the render event is fanned out to GUI and wallpaper clients.
Resolving independently in each browser would allow the same intent to choose
different random branches on different surfaces.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Sequence
from typing import Any

logger = logging.getLogger(__name__)


RANDOM_TRIGGER_ROUTES: dict[str, tuple[str, ...]] = {
    # Keep the original behavior: a generic thinking intent chooses between
    # the serious-speaking performance and the dedicated thinking branch.
    "thinking": ("speaking_trans", "thinking_trans"),
}

TRIGGER_ALIASES: dict[str, str] = {
    "work": "thinking",
    "working": "thinking",
    "provider_work": "thinking",
    "tool_call": "thinking",
    "coding": "thinking",
    "serious": "speaking_trans",
    "serious_speaking": "speaking_trans",
    "speaking_serious": "speaking_trans",
    "speaking_seriously": "speaking_trans",
    "seriously_speaking": "speaking_trans",
    "严肃": "speaking_trans",
    "严肃讲话": "speaking_trans",
    "认真": "speaking_trans",
    "认真讲话": "speaking_trans",
    "shy": "shy",
    "害羞": "shy",
    "羞涩": "shy",
    "surprised": "surprise",
    "surprise": "surprise",
    "惊讶": "surprise",
    "びっくり": "surprise",
    "angry": "angry",
    "mad": "angry",
    "furious": "angry",
    "生气": "angry",
    "怒り": "angry",
    "disappointed": "sad",
    "sadness": "sad",
    "sorrow": "sad",
    "depressed": "sad",
    "gloomy": "sad",
    "がっかり": "sad",
    "落ち込む": "sad",
    "悲しい": "sad",
    "悲しみ": "sad",
    "失望": "sad",
    "沮丧": "sad",
    "失落": "sad",
    "难过": "sad",
    "悲伤": "sad",
}


def resolve_spriteforge_trigger_label(
    label: str,
    *,
    chooser: Callable[[Sequence[str]], str] | None = None,
) -> str:
    """Resolve an alias and its optional random semantic route.

    ``chooser`` is injectable so routing can be tested deterministically while
    production continues to use ``random.choice``.
    """

    raw = str(label or "").strip()
    normalized = TRIGGER_ALIASES.get(raw.lower(), raw)
    routes = RANDOM_TRIGGER_ROUTES.get(normalized.lower())
    if routes:
        return (chooser or random.choice)(routes)
    return normalized


def spriteforge_intent_payload(label: str, **metadata: Any) -> dict[str, Any]:
    """Build a render payload whose label is already an explicit graph entry."""

    semantic_label = str(label or "").strip()
    resolved_label = resolve_spriteforge_trigger_label(semantic_label)
    payload: dict[str, Any] = {**metadata, "label": resolved_label}
    if semantic_label and semantic_label != resolved_label:
        payload["semantic_label"] = semantic_label
        logger.info(
            "[SpriteForgeIntent] resolved semantic intent: %s -> %s",
            semantic_label,
            resolved_label,
        )
    return payload
