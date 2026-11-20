"""Privacy-safe formatting for user-derived diagnostic text.

Runtime logs are durable support artifacts.  They keep structural metadata by
default and expose text previews only when an operator explicitly opts in.
"""

from __future__ import annotations

from config import settings


def protected_text(value: object, *, limit: int = 80) -> str:
    """Return a bounded log field without persisting user text by default."""

    text = str(value or "")
    if not bool(getattr(settings, "LOG_USER_CONTENT", False)):
        return f"<redacted chars={len(text)}>"
    bounded = text[: max(0, int(limit))]
    suffix = "..." if len(text) > len(bounded) else ""
    return f"{bounded!r}{suffix}"
