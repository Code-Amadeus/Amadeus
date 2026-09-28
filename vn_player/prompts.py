"""Prompt builders for VN Player lanes."""

from __future__ import annotations

import json
import re
from typing import Any

from .schemas import VNProfile
from .prompt_layers import compose_messages


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def immediate_context_view(context_pack: dict[str, Any]) -> str:
    """Render a clean, role-facing context instead of dumping runtime JSON."""
    current = context_pack.get("current_line") or {}
    attention = context_pack.get("attention") or {}
    route = attention.get("route") or {}
    lane_focus = attention.get("lane_focus") or {}
    immediate_focus = lane_focus.get("immediate") or {}
    retrospective = context_pack.get("retrospective_bias") or attention.get("retrospective_bias") or {}
    orientation = (
        immediate_focus.get("character_orientation")
        or retrospective.get("character_orientation")
        or {}
    )
    silence_pressure = attention.get("silence_pressure") or {}
    lookahead = context_pack.get("lookahead_hint") or {}
    target = ((lookahead.get("reaction_plan") or [{}])[0]) if isinstance(lookahead, dict) else {}

    parts = [
        "Clean VN context for Kurisu.",
        "",
        "Current displayed line:",
        _line_text(current),
        "",
        "Recent displayed script, oldest to newest:",
        *_bullet_lines([_line_text(line) for line in list(context_pack.get("short_memory") or [])[-22:]]),
        "",
        "Player side:",
        _player_text(context_pack.get("player_intervention")),
        "",
        "Recent player dialogue history:",
        *_bullet_lines([_player_dialogue_text(item) for item in context_pack.get("recent_player_dialogue") or []]),
        "",
        "Kurisu recently said:",
        *_bullet_lines([_speech_text(item) for item in context_pack.get("recent_kurisu_speech") or []]),
        "",
        "Local story context:",
        *_bullet_lines(_story_context_lines(context_pack)),
        "",
        "Working character orientation:",
        *_bullet_lines(_orientation_lines(orientation)),
        "",
        "Director notes:",
        f"- immediate mode: {route.get('immediate', 'skip')}",
        f"- immediate goal: {immediate_focus.get('goal', '')}",
        f"- style bias: {immediate_focus.get('retrospective_style', '')}",
        f"- abstract lookahead: {target.get('spoiler_safe_hint', '')}",
        f"- current kind: {attention.get('current_kind', '')}; density: {attention.get('density', '')}",
        f"- silence pressure: {silence_pressure.get('silent_lines', 0)} silent lines; action: {silence_pressure.get('action', 'observe')}; strength: {silence_pressure.get('strength', 0)}",
        "",
        (
            "Remember: displayed script is game evidence. Current player input must be answered when present. "
            "Recent player dialogue is history, not an unanswered request. Kurisu's prior speech is only her own commentary, not game fact."
        ),
    ]
    return "\n".join(part for part in parts if part is not None)


def immediate_prompt(profile: VNProfile, context_pack: dict[str, Any]) -> list[dict[str, str]]:
    return compose_messages(profile, "immediate", immediate_context_view(context_pack))


def _line_text(line: dict[str, Any]) -> str:
    if not isinstance(line, dict):
        return "- (none)"
    speaker = str(line.get("speaker") or "").strip()
    sid = str(line.get("script_id") or line.get("line_id") or "").strip()
    text = re.sub(r"\s+", " ", str(line.get("text") or "")).strip()
    who = f"{speaker}: " if speaker else ""
    ref = f"[{sid}] " if sid else ""
    return f"- {ref}{who}{text}" if text else f"- {ref}(empty)"


def _bullet_lines(lines: list[str]) -> list[str]:
    cleaned = [line for line in lines if str(line or "").strip() and str(line).strip() != "- (none)"]
    return cleaned or ["- (none)"]


def _player_text(player: Any) -> str:
    if not isinstance(player, dict) or not str(player.get("text") or "").strip():
        return "- no current player input requiring a direct answer"
    kind = str(player.get("kind") or "note")
    text = re.sub(r"\s+", " ", str(player.get("text") or "")).strip()
    return f"- {kind}: {text}"


def _player_dialogue_text(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
    if not text:
        return ""
    kind = str(item.get("kind") or "input")
    return f"- {kind}: {text}"


def _speech_text(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    text = re.sub(r"\[EMO[^\]]*\]", "", str(item.get("text") or "")).strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return ""
    sid = str(item.get("script_id") or "").strip()
    return f"- [{sid}] {text}" if sid else f"- {text}"


def _story_context_lines(context_pack: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    story = list(context_pack.get("story_summary_log") or [])[-3:]
    if story:
        lines.append("Recent story summary:")
        for item in story:
            summary = re.sub(r"\s+", " ", str((item or {}).get("summary") or "")).strip()
            sid = str((item or {}).get("source_script_id") or "")
            if summary:
                lines.append(f"- [{sid}] {summary[:360]}")
    evidence = list(context_pack.get("evidence_nodes") or [])[-6:]
    if evidence:
        lines.append("Relevant evidence candidates:")
        for item in evidence:
            claim = re.sub(r"\s+", " ", str((item or {}).get("claim") or "")).strip()
            tags = ", ".join(str(v) for v in ((item or {}).get("tags") or [])[:3])
            if claim:
                lines.append(f"- {claim[:220]} ({tags})")
    hypotheses = list(context_pack.get("hypotheses") or [])[-6:]
    if hypotheses:
        lines.append("Open working hypotheses:")
        for item in hypotheses:
            claim = re.sub(r"\s+", " ", str((item or {}).get("claim") or "")).strip()
            confidence = (item or {}).get("confidence")
            if claim:
                lines.append(f"- {claim[:220]} confidence={confidence}")
    chars = ((context_pack.get("characters") or {}).get("characters") or [])[-8:]
    if chars:
        names = []
        for item in chars:
            for name in (item or {}).get("names") or []:
                if name and name not in names:
                    names.append(str(name))
        if names:
            lines.append("Known character names:")
            lines.append("- " + ", ".join(names[:12]))
    feedback = list(context_pack.get("verifier_feedback") or [])[-4:]
    if feedback:
        lines.append("Verifier cautions:")
        for item in feedback:
            status = str((item or {}).get("status") or "")
            messages = "; ".join(str(v) for v in ((item or {}).get("messages") or [])[:2])
            if status:
                lines.append(f"- {status}: {messages}")
    return lines or ["No durable context yet."]


def _orientation_lines(orientation: Any) -> list[str]:
    if not isinstance(orientation, dict) or not orientation:
        return ["No retrospective orientation yet. Stay skeptical and avoid overclaiming."]
    lines: list[str] = []
    for key, title in [
        ("working_assumptions", "working assumptions"),
        ("plausible_mistakes", "plausible mistakes"),
        ("avoid_sounding_like", "avoid sounding like"),
    ]:
        values = [str(v) for v in (orientation.get(key) or []) if str(v).strip()]
        if values:
            lines.append(f"{title}: " + " | ".join(values[:5]))
    for key, title in [
        ("emotional_stance", "emotional stance"),
        ("uncertainty_style", "uncertainty style"),
        ("next_reaction_bias", "next reaction bias"),
    ]:
        value = str(orientation.get(key) or "").strip()
        if value:
            lines.append(f"{title}: {value}")
    return lines or ["No retrospective orientation yet. Stay skeptical and avoid overclaiming."]


def lookahead_prompt(profile: VNProfile, context_pack: dict[str, Any]) -> list[dict[str, str]]:
    prefix = "Planner context:" if profile.prompt_pack == "base" else "Lookahead context:"
    return compose_messages(profile, "lookahead", prefix + "\n" + _json(context_pack))


def reasoner_prompt(profile: VNProfile, context_pack: dict[str, Any]) -> list[dict[str, str]]:
    prefix = 'Reasoning context:'
    return compose_messages(profile, "reasoner", prefix + "\n" + _json(context_pack))


def summary_prompt(profile: VNProfile, context_pack: dict[str, Any]) -> list[dict[str, str]]:
    prefix = 'Summary context:'
    return compose_messages(profile, "summary", prefix + "\n" + _json(context_pack))


def retrospective_prompt(profile: VNProfile, context_pack: dict[str, Any]) -> list[dict[str, str]]:
    prefix = "Reflection context:" if profile.prompt_pack == "base" else "Retrospective context:"
    return compose_messages(profile, "retrospective", prefix + "\n" + _json(context_pack))
