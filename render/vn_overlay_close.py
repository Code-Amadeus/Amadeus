"""Close notification for the repository-owned portrait card.

Companion-only launches own the card process from the backend. When the user
closes the card, that owner must end the same session instead of leaving a
backend and its audio resources behind. This module is the whole close policy,
so it is testable without starting a Tk window.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any, Callable

logger = logging.getLogger(__name__)

CARD_CLOSE_PATH = "/companion/card-close"


def close_target(backend_url: str) -> str:
    """Map the card's backend /ws endpoint to its companion close endpoint."""
    url = str(backend_url or "").strip()
    if not url:
        return ""
    http_url = url.replace("wss://", "https://", 1) if url.startswith("wss://") else url.replace("ws://", "http://", 1)
    return http_url.split("/ws", 1)[0].rstrip("/") + CARD_CLOSE_PATH


def close_endpoint(on_close: str, backend_url: str) -> str:
    """The endpoint this card must report to, or empty when it owns nothing."""
    if str(on_close or "exit").strip().lower() != "card-close":
        return ""
    return close_target(backend_url)


def post_close(
    on_close: str,
    backend_url: str,
    *,
    post: Callable[[str, dict[str, Any]], Any] | None = None,
) -> bool:
    """Report a user close to the owning backend exactly once, best effort."""
    target = close_endpoint(on_close, backend_url)
    if not target:
        return False
    body: dict[str, Any] = {}
    try:
        if post is not None:
            post(target, body)
            return True
        request = urllib.request.Request(
            target,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            response.read(256)
        return True
    except Exception:
        logger.debug("[companion] card close report failed: %s", target, exc_info=True)
        return False
