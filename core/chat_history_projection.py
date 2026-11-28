"""Canonical source text and model-history projection.

Live ingress and delivery own admission, Session identity and interruptions.
"""

from __future__ import annotations

import re
from typing import Any

from config.settings import (
    EMO_HISTORY_POLICIES,
    EMO_HISTORY_POLICY,
)

# Offsets into this text are Python/Unicode code-point offsets. Never normalize
# Unicode, whitespace, punctuation or case; a rule change requires a new version.
MEMORY_TEXT_PROJECTION_VERSION = "amadeus-memory-text-v1"
_MEMORY_ASSISTANT_SYNTAX = re.compile(
    r"<think>.*?(?:</think>|$)|</think>|"
    r"\[(?:PARAM|EXPR|HOTKEY|EMO|ANIM|DELEGATE|CONTROL|AUIP)\b[^\]]*(?:\]|$)",
    re.IGNORECASE | re.DOTALL,
)


def exclude_source_spans(message: dict[str, Any], spans: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Mask canonical text; accepted execution facts are supplied independently.

    Canonical offsets never index raw assistant text. An unavailable old
    revision/projection cannot be mapped safely, so omit that message.
    """
    if not spans:
        return dict(message)
    if any(span["revision"] != message.get("revision")
            or span["projection_version"] != MEMORY_TEXT_PROJECTION_VERSION for span in spans):
        return None
    raw = message["content"]
    kept = []
    cursor = 0
    if message["role"] == "assistant":
        for syntax in _MEMORY_ASSISTANT_SYNTAX.finditer(raw):
            kept.extend(range(cursor, syntax.start()))
            cursor = syntax.end()
    kept.extend(range(cursor, len(raw)))
    canonical = "".join(raw[index] for index in kept)
    removed = {}
    for span in spans:
        start, end = span.get("start_cp"), span.get("end_cp")
        if start is None or end is None:
            return None
        if not 0 <= start <= end <= len(kept):
            return None
        removed.update({index:span.get("reason") for index in range(start, end)})
    # For an excluded assistant source, use its canonical text view first:
    # hidden thought/control syntax can contain copies outside the canonical
    # coordinates. The durable controls/Host execution ledger stay untouched.
    # An omission boundary must not manufacture adjacent words or phrases.
    output = []
    in_omission = False
    for index, char in enumerate(canonical):
        if index in removed:
            if not in_omission:
                output.append({"preference_changed":"[earlier assertion superseded]",
                    "corrected_by_user":"[earlier assertion corrected]"}.get(removed[index], "[memory source omitted]"))
            in_omission = True
        else:
            output.append(char)
            in_omission = False
    content = "".join(output)
    return {**message, "content":content} if content.strip() else None


def project_role_history(rows) -> list[dict[str, Any]]:
    """Apply current exclusion facts to a frozen role-history snapshot."""
    from core.session_manager import project_model_history

    result = []
    for row in rows:
        source = row.get("_source")
        text = row.get("text", "")
        if source is not None:
            projected = project_model_history(source["session_id"], [{
                **source, "role":"user" if row["source"] == "user" else "assistant",
                "content":text}])
            if not projected:
                continue
            text = projected[0]["content"]
        result.append({**{key:value for key, value in row.items() if key != "_source"}, "text":text})
    return result


def project_memory_source(session_id: str, message: dict[str, Any], *,
                          expected_revision: int | None = None,
                          incarnation: str | None = None) -> dict[str, Any]:
    """Project one identified Session revision, without changing model history.

    Call through Session's durable source reader. This function never allocates
    identity or infers historical time, and never substitutes current content
    for a requested older revision. M0 retains only the current revision.
    """
    revision = message.get("revision")
    if (not message.get("message_id") or type(revision) is not int or revision < 1
            or (expected_revision is not None and revision != expected_revision)):
        raise ValueError("Session source identity/revision is unavailable")
    role, content = message["role"], message["content"]
    text = _MEMORY_ASSISTANT_SYNTAX.sub("", content) if role == "assistant" else content
    projected = {
        "session_id": session_id, "message_id": message["message_id"],
        "turn_id": message.get("turn_id"), "role": role, "revision": revision,
        "created_at_ms": message.get("created_at_ms"),
        "utc_offset_minutes": message.get("utc_offset_minutes"),
        "projection_version": MEMORY_TEXT_PROJECTION_VERSION, "text": text,
    }
    if incarnation is not None:
        projected["incarnation"] = incarnation
    return projected


def project_inline_role_history(
    ordered_parts: tuple[tuple[str, object], ...] | list[tuple[str, object]],
    *,
    policy: str | None = None,
) -> str:
    """Project visible text plus policy-selected EMO into model history.

    The parser supplies exact stream order. Execution controls remain excluded
    here because their effective, Host-adjudicated form is appended separately.
    GUI and TTS never consume this projection.
    """

    selected = str(policy or EMO_HISTORY_POLICY).strip().lower()
    if selected not in EMO_HISTORY_POLICIES:
        raise ValueError(f"unsupported EMO history policy: {selected!r}")
    projected: list[str] = []
    for kind, value in ordered_parts:
        if kind == "text":
            projected.append(str(value or ""))
            continue
        if kind != "action" or not isinstance(value, dict):
            continue
        if str(value.get("type") or "").strip().upper() != "EMO":
            continue
        if selected == "strip":
            continue
        attrs = value.get("attrs") if isinstance(value.get("attrs"), dict) else {}
        preset = str(attrs.get("preset") or "").strip().casefold()
        if selected == "expressive_only" and (not preset or preset == "normal"):
            continue
        projected.append(str(value.get("raw") or ""))
    return "".join(projected)


def project_completed_role_history(text: str) -> str:
    """Apply the same inline-history policy to an already completed role line."""
    from llm.stream_parser import StreamTagParser

    _visible, _actions, parts = StreamTagParser(stop_after_control=False).process_chunk_parts(text)
    return project_inline_role_history(parts)
