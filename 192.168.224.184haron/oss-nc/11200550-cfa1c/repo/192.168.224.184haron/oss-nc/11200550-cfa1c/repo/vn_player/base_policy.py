"""Generic VN interpretation when no genre or complete script is assumed."""

from __future__ import annotations

from typing import Any

from .text import strip_vn_tags


def lookahead_plan(line_event: dict[str, Any], future_window: list[dict[str, Any]]) -> dict[str, Any]:
    script_id = str(line_event.get("script_id") or "")
    return {
        "schema_version": "vn.lookahead.v1",
        "current_script_id": script_id,
        "window": {
            "from_script_id": str(future_window[0].get("script_id") or "") if future_window else script_id,
            "to_script_id": str(future_window[-1].get("script_id") or "") if future_window else script_id,
            "line_count": len(future_window),
        },
        "spoiler_policy": "abstract_only",
        "density": {"current": 0.0, "next_5": 0.0, "next_20": 0.0},
        "reaction_plan": [],
        "cadence": {"sample_every": 1, "until_script_id": "", "reason": "awaiting_model_plan"},
        "source": "base_neutral",
    }


def attention_route(line_event: dict[str, Any], line_count: int, max_context_lines: int) -> dict[str, Any]:
    # The model interprets dialogue. The host only schedules the ordinary
    # lanes; it does not infer clues, suspects, rules, or emotional states.
    return {
        "schema_version": "vn.attention.v1",
        "density": "unspecified",
        "current_kind": "dialogue",
        "current_score": 0.0,
        "route": {
            "immediate": "react",
            "summary": "append_later",
            "fact_extractor": "skip",
            "character_modeler": "skip",
            "reasoner": "skip",
            "verifier": "skip",
        },
        "budget": {
            "max_context_lines": max_context_lines,
            "next_summary_after_lines": 12,
            "summary_cooldown_lines": 5,
        },
        "lane_focus": {},
        "target": {},
        "retrospective_bias": {},
        "reasons": ["generic displayed dialogue"],
    }


def summary_patch(short_memory: list[dict[str, Any]], line_event: dict[str, Any]) -> dict[str, Any]:
    recent = list(short_memory or [])[-12:]
    displayed = [strip_vn_tags(str(item.get("text") or "")).strip() for item in recent]
    summary = " / ".join(text[:120] for text in displayed[-5:] if text)
    speakers = list(dict.fromkeys(str(item.get("speaker") or "") for item in recent if item.get("speaker")))
    return {
        "layer": "summary",
        "target": "story_summary_log",
        "action": "append",
        "item": {
            "id": f"story_{line_event.get('line_id')}",
            "summary": summary,
            "important_beats": [],
            "active_characters": speakers[-8:],
            "open_questions": [],
            "evidence_refs": [{"line_id": line_event.get("line_id"), "script_id": line_event.get("script_id")}],
            "affect_anchor": "",
            "dramatic_function": "rolling_scene_summary",
        },
    }
