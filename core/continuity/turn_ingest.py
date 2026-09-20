"""Session-owned turn recovery helpers for C2 consolidation.

Continuity never scans/imports arbitrary old transcripts.  This module is used
only for a turn already present in the durable consolidation journal.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from core.continuity.models import TurnEvidence


def storage_session_id(session_id: str) -> str:
    value = str(session_id or "").strip()
    return value or "__unsessioned__"


def session_path(session_dir: str | Path, session_id: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", str(session_id or "default"))
    return Path(session_dir) / f"{safe}.json"


def _timestamp(value: str) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.astimezone()
        return float(parsed.timestamp())
    except Exception:
        return None


def load_turn_evidence(
    session_dir: str | Path,
    *,
    session_id: str,
    turn_id: str,
) -> TurnEvidence | None:
    """Read one completed turn from its authoritative Session JSON."""

    if not session_id or session_id == "__unsessioned__" or not turn_id:
        return None
    path = session_path(session_dir, session_id)
    if not path.exists():
        return None
    try:
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    user: dict[str, Any] | None = None
    assistant: dict[str, Any] | None = None
    for entry in payload.get("dialog") or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("turn_id") or "") != str(turn_id):
            continue
        role = str(entry.get("role") or "")
        if role == "user" and user is None:
            user = entry
        elif role == "assistant":
            assistant = entry

    if user is None or assistant is None:
        return None
    user_text = str(user.get("content") or "")
    assistant_text = str(assistant.get("content") or "")
    if not user_text:
        return None
    user_created_at = str(user.get("created_at") or "")
    assistant_created_at = str(assistant.get("created_at") or "")
    observed_at = _timestamp(user_created_at) or _timestamp(assistant_created_at)
    return TurnEvidence(
        session_id=str(session_id),
        turn_id=str(turn_id),
        user_text=user_text,
        assistant_text=assistant_text,
        user_created_at=user_created_at,
        assistant_created_at=assistant_created_at,
        observed_at=observed_at,
    )


def evidence_source_hash(evidence: TurnEvidence) -> str:
    payload = "\x1f".join(
        (
            str(evidence.session_id or ""),
            str(evidence.turn_id or ""),
            str(evidence.user_text or ""),
            str(evidence.assistant_text or ""),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
