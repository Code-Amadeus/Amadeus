"""Optional explicit action-existence envelope for the inline role stream.

The feature is a transport canary, not a second routing system.  A valid
``delegate=true`` envelope is decoded to the existing DELEGATE action shape at
the parser boundary; every downstream authority and dispatcher remains
unchanged.  ``delegate=false`` records an explicit no-control outcome and has
no executable representation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


def control_envelope_enabled() -> bool:
    try:
        from config import settings

        return bool(
            getattr(settings, "ACTION_EXISTENCE_CONTROL_ENVELOPE_ENABLED", False)
        ) and bool(
            getattr(settings, "CONTROL_DECISION_AUTHORITY_ENABLED", False)
        ) and not bool(getattr(settings, "LLM_DELEGATE_TOOL_CALLS", False))
    except Exception:
        return False


def control_envelope_prompt_addon(*, language: str = "en") -> str:
    """Render the one complete inline output contract used in CONTROL mode.

    The surrounding prompt describes control semantics without naming a wire
    format.  Keeping the entire envelope here prevents the model from seeing a
    DELEGATE contract and a later instruction that tries to override it.
    """

    ja = str(language or "").strip().lower() == "ja"
    if ja:
        return (
            "\n\n[CONTROL OUTCOME CONTRACT]\n"
            "各ターンは、(1) 短い自然な最初の一文、(2) その直後の機械可読な制御結果、"
            "(3) 必要なら自然な返答の続き、の順で出力する。最初の一文の直後に制御結果を"
            "必ず一つだけ出力すること。"
            "Host の制御が不要なら [CONTROL delegate=\"false\"]。"
            "Host の制御が必要なら "
            "[CONTROL delegate=\"true\" provider=\"...\" intent=\"...\" task=\"...\" ...] とし、"
            "上記の意味・provider・intent・安全性・routing 規則が要求する属性をすべて同じタグへ"
            "入れる。CONTROL 以外の制御タグは出力しない。CONTROL は読み上げず、説明もしない。"
        )
    return (
        "\n\n[CONTROL OUTCOME CONTRACT]\n"
        "Every turn is ordered as (1) one brief natural opening sentence, (2) exactly "
        "one machine-readable control outcome immediately after that sentence, and "
        "(3) more natural reply only when useful. When no Host control is needed, "
        "emit [CONTROL delegate=\"false\"]. When Host control is needed, emit "
        "[CONTROL delegate=\"true\" provider=\"...\" intent=\"...\" task=\"...\" ...] "
        "and include every attribute required by the semantic, provider, intent, "
        "safety, and routing rules above. Emit no other control tag. Never explain "
        "or read CONTROL aloud."
    )


def finalize_control_envelope_prompt(
    system_prompt: str,
    *,
    language: str = "en",
    include: bool = True,
) -> str:
    """Keep the optional outcome contract after every dynamic prompt block.

    ``get_system_prompt`` also exposes the contract to direct callers and
    probes.  The live role prompt subsequently appends workspace, Work, and
    experience facts; leaving CONTROL above those blocks made the most recent
    source-local instructions win by position.  Move the one canonical addon
    instead of duplicating it or teaching each feature about CONTROL.
    """

    prompt = str(system_prompt or "").rstrip()
    if not control_envelope_enabled():
        return prompt
    addon = control_envelope_prompt_addon(language=language).strip()
    without_existing = prompt.replace(addon, "").rstrip()
    if not include:
        return without_existing
    return f"{without_existing}\n\n{addon}" if without_existing else addon


@dataclass(frozen=True, slots=True)
class DecodedControlEnvelope:
    seen: bool
    valid: bool
    delegate: bool | None
    action: Mapping[str, Any] | None
    error: str = ""


def decode_control_envelope(action: Mapping[str, Any]) -> DecodedControlEnvelope:
    if str(action.get("type") or "").strip().upper() != "CONTROL":
        return DecodedControlEnvelope(False, False, None, None, "not a CONTROL action")
    attrs = dict(action.get("attrs") or {})
    value = str(attrs.pop("delegate", "")).strip().lower()
    if value not in {"true", "false"}:
        return DecodedControlEnvelope(
            True,
            False,
            None,
            None,
            "delegate must be true or false",
        )
    if value == "false":
        if attrs:
            return DecodedControlEnvelope(
                True,
                False,
                False,
                None,
                "delegate=false must not carry action attributes",
            )
        return DecodedControlEnvelope(True, True, False, None)
    missing = [key for key in ("provider", "intent") if not str(attrs.get(key) or "").strip()]
    if missing:
        return DecodedControlEnvelope(
            True,
            False,
            True,
            None,
            "delegate=true omitted " + ", ".join(missing),
        )
    return DecodedControlEnvelope(
        True,
        True,
        True,
        {
            "type": "DELEGATE",
            "attrs": attrs,
            # Preserve the actual envelope in history when authority is off.
            "raw": str(action.get("raw") or ""),
        },
    )


def as_control_tag_text(attrs: Mapping[str, Any], *, delegate: bool = True) -> str:
    if not delegate:
        return '[CONTROL delegate="false"]'
    from llm.delegate_tool import as_tag_text

    delegate_tag = as_tag_text(dict(attrs))
    body = delegate_tag[len("[DELEGATE") :]
    return '[CONTROL delegate="true"' + body
