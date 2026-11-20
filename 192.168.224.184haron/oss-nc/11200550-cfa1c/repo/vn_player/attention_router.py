"""Attention routing for VN Player mode.

The router turns live script density + spoiler-safe lookahead into scheduling
signals for each lane. It is deliberately deterministic so it can guard and
explain the LLM lanes instead of becoming another source of drift.
"""

from __future__ import annotations

import re
from typing import Any

from .schemas import VNProfile
from .text import looks_like_topic_label, strip_vn_tags


TUTORIAL_MARKERS = {
    "点击",
    "操作",
    "右摇杆",
    "触摸屏",
    "拖动屏幕",
    "环顾四周",
    "可以通过",
    "按键",
    "按钮",
}
RULE_MARKERS = {
    "规则",
    "条件",
    "必须",
    "不能",
    "如果",
    "之前",
    "之后",
    "获得",
    "失去",
    "真货",
    "实际存在",
    "能看到",
    "能够看到",
    "看得见",
    "看不见",
    "复活秘术",
    "只能",
    "使用1次",
    "使用１次",
}
SCENE_MARKERS = {"这里是", "位于", "时间", "午夜", "公园", "车站", "周围"}
MICRO_EMOTION_MARKERS = {"哇", "啊", "咦", "喂", "糟", "危险", "没事吧"}
NAME_SUFFIX_MARKERS = {"君", "小姐", "先生", "同学", "前辈", "后辈"}


def build_attention_route(
    *,
    profile: VNProfile,
    line_event: dict[str, Any],
    lookahead: dict[str, Any],
    current_score: float,
    current_kind: str,
    line_count: int,
    retrospective_bias: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text = strip_vn_tags(str(line_event.get("text") or ""))
    kind = _classify_kind(text, current_kind)
    density = _density(current_score, kind, lookahead)
    target = _target(lookahead)
    active_bias = _active_retrospective_bias(retrospective_bias or {}, line_count)
    stronger_soon = (
        target.get("suggested_action") == "hold_until_target"
        and target.get("distance_lines") is not None
        and int(target.get("distance_lines") or 99) <= 6
    )
    target_distance = int(target.get("distance_lines") or 99) if target.get("distance_lines") is not None else 99

    route = {
        "immediate": "skip",
        "summary": "append_later",
        "fact_extractor": "skip",
        "character_modeler": "skip",
        "reasoner": "skip",
        "verifier": "skip",
    }
    reasons: list[str] = []

    if stronger_soon and current_score < 3.0:
        route["immediate"] = "hold" if current_score >= 1.0 or target_distance <= 2 else "skip"
        route["summary"] = "append_later"
        reasons.append("stronger planned beat is near" if route["immediate"] == "hold" else "connective line before stronger planned beat")
    elif kind == "topic_label":
        route["summary"] = "append_later"
        reasons.append("menu/topic label compressed into attention cue")
    elif kind in {"evidence", "rule", "contradiction", "choice", "scene_shift", "rule_anomaly"}:
        strong_evidence = kind != "evidence" or current_score >= 3.0
        live_reactable_evidence = kind == "evidence" and current_score >= 3.2
        if kind in {"contradiction", "choice", "rule", "rule_anomaly"} or live_reactable_evidence:
            route["immediate"] = "react"
        else:
            route["immediate"] = "skip"
        route["summary"] = "append_now" if strong_evidence or kind in {"contradiction", "choice", "rule", "rule_anomaly"} else "append_later"
        reasons.append(f"current kind is {kind}")
    elif kind == "emotional_beat":
        strong_emotion = _is_strong_emotional_beat(text, current_score)
        route["immediate"] = "react" if strong_emotion else "skip"
        route["summary"] = "append_now" if strong_emotion else "append_later"
        reasons.append("strong emotional beat" if strong_emotion else "weak emotional cue for context only")
    elif density == "high":
        route["immediate"] = "react"
        route["summary"] = "append_now"
        reasons.append("current density is high")
    elif kind == "tutorial":
        route["summary"] = "append_later"
        reasons.append("tutorial/system line should be compressed")
    else:
        reasons.append("low-density connective dialogue")

    if kind in {"evidence", "rule", "contradiction", "choice", "rule_anomaly"} or current_score >= 3.2:
        route["fact_extractor"] = "run"
        route["character_modeler"] = "run"
        route["verifier"] = "run"
        route["reasoner"] = "run_deep" if kind in {"contradiction", "choice", "rule", "rule_anomaly"} else "run_light"
        reasons.append("fact/evidence lane should inspect this line")
    elif kind == "emotional_beat":
        route["character_modeler"] = "run" if _has_character_hint(text) else route["character_modeler"]
        route["reasoner"] = "run_light"
        route["verifier"] = "run"
        reasons.append("emotional beat may affect character state")
    elif _has_character_hint(text):
        route["character_modeler"] = "run"
        reasons.append("line contains a likely character mention")
    elif density == "medium" and line_count % 4 == 0:
        route["reasoner"] = "run_light"
        reasons.append("medium-density periodic reasoning")

    if kind == "tutorial":
        route["fact_extractor"] = "skip"
        route["reasoner"] = "skip" if density != "high" else route["reasoner"]
    if kind == "topic_label":
        route["immediate"] = "skip"
        route["summary"] = "append_later"
        route["fact_extractor"] = "skip"
        route["character_modeler"] = "skip"
        route["reasoner"] = "skip"
        route["verifier"] = "skip"

    _apply_retrospective_bias(text, kind, density, route, reasons, active_bias)
    budget = _budget(density, route, kind)
    _adjust_budget_for_bias(budget, active_bias)
    return {
        "schema_version": "vn.attention.v1",
        "density": density,
        "current_kind": kind,
        "current_score": round(float(current_score), 2),
        "route": route,
        "budget": budget,
        "lane_focus": _lane_focus(kind, density, route, target, active_bias),
        "target": target,
        "retrospective_bias": _public_retrospective_bias(active_bias),
        "reasons": reasons,
        "profile": {
            "game_genre": profile.game_genre,
            "prompt_pack": profile.prompt_pack,
        },
    }


def _classify_kind(text: str, current_kind: str) -> str:
    if current_kind == "topic_label" or looks_like_topic_label(text):
        return "topic_label"
    if _is_trailing_contrast_setup(text):
        return "banter"
    if any(marker in text for marker in TUTORIAL_MARKERS):
        return "tutorial"
    if _is_micro_emotional_beat(text):
        return "emotional_beat"
    if current_kind == "new_evidence":
        if any(marker in text for marker in RULE_MARKERS):
            return "rule"
        return "evidence"
    if current_kind in {"contradiction", "choice", "emotional_beat", "rule_anomaly"}:
        return current_kind
    if any(marker in text for marker in SCENE_MARKERS):
        return "scene_shift"
    return "banter" if text else "low_density"


def _is_trailing_contrast_setup(text: str) -> bool:
    compact = re.sub(r"[\s。.!！?？…‥]+$", "", text.strip())
    return compact.endswith(("但是", "可是")) and len(compact) <= 18


def _is_micro_emotional_beat(text: str) -> bool:
    compact = text.strip()
    if not compact:
        return False
    has_pulse = "！" in compact or "!" in compact or "？" in compact or "?" in compact or "…" in compact
    return has_pulse and any(marker in compact for marker in MICRO_EMOTION_MARKERS)


def _is_strong_emotional_beat(text: str, current_score: float) -> bool:
    compact = text.strip()
    if current_score >= 2.4:
        return True
    if any(marker in compact for marker in {"没事吧", "危险", "害怕", "痛苦", "求你", "不可能"}):
        return True
    if "哇" in compact and ("！" in compact or "!" in compact):
        return True
    return compact.count("！") + compact.count("!") + compact.count("？") + compact.count("?") >= 2


def _has_character_hint(text: str) -> bool:
    compact = text.strip()
    if not compact:
        return False
    return any(marker in compact for marker in NAME_SUFFIX_MARKERS)


def _density(current_score: float, kind: str, lookahead: dict[str, Any]) -> str:
    if kind in {"topic_label", "tutorial"}:
        return "low"
    if kind in {"evidence", "rule", "contradiction", "choice", "rule_anomaly"} or current_score >= 3.4:
        return "high"
    if kind in {"emotional_beat", "scene_shift"} or current_score >= 1.8:
        return "medium"
    density = lookahead.get("density") if isinstance(lookahead, dict) else {}
    try:
        next_5 = float(density.get("next_5") or 0.0)
        next_20 = float(density.get("next_20") or 0.0)
    except Exception:
        next_5 = 0.0
        next_20 = 0.0
    if next_5 >= 0.45 or next_20 >= 0.35:
        return "medium"
    return "low"


def _target(lookahead: dict[str, Any]) -> dict[str, Any]:
    plan = []
    if isinstance(lookahead, dict):
        plan = list(lookahead.get("reaction_plan") or [])
    first = plan[0] if plan and isinstance(plan[0], dict) else {}
    return {
        "script_id": str(first.get("target_script_id") or ""),
        "kind": str(first.get("kind") or ""),
        "priority": str(first.get("priority") or ""),
        "suggested_action": str(first.get("suggested_action") or ""),
        "distance_lines": first.get("distance_lines"),
        "spoiler_safe_hint": str(first.get("spoiler_safe_hint") or ""),
    }


def _budget(density: str, route: dict[str, str], kind: str) -> dict[str, Any]:
    if density == "high":
        summary_after = 4
        reasoner_after = 1
        max_context = 50
        speak_cooldown = 2
        summary_cooldown = 10
    elif density == "medium":
        summary_after = 8
        reasoner_after = 4
        max_context = 35
        speak_cooldown = 4
        summary_cooldown = 10
    else:
        summary_after = 14
        reasoner_after = 8
        max_context = 20
        speak_cooldown = 7
        summary_cooldown = 12
    if route.get("summary") == "append_now":
        summary_after = 1
    if kind in {"evidence", "rule", "contradiction", "choice"}:
        summary_cooldown = max(summary_cooldown, 10)
    elif kind == "rule_anomaly":
        summary_cooldown = max(summary_cooldown, 10)
        speak_cooldown = max(speak_cooldown, 6)
    elif kind == "scene_shift":
        summary_cooldown = max(summary_cooldown, 10)
    elif kind == "emotional_beat":
        summary_cooldown = max(summary_cooldown, 10)
    if route.get("reasoner") == "run_deep":
        reasoner_after = 1
    if route.get("immediate") == "hold":
        speak_cooldown = max(speak_cooldown, 6)
    return {
        "next_summary_after_lines": summary_after,
        "next_reasoner_after_lines": reasoner_after,
        "max_context_lines": max_context,
        "reasoner_depth": route.get("reasoner", "skip"),
        "speak_cooldown_lines": speak_cooldown,
        "summary_cooldown_lines": summary_cooldown,
    }


def _lane_focus(
    kind: str,
    density: str,
    route: dict[str, str],
    target: dict[str, Any],
    retrospective_bias: dict[str, Any],
) -> dict[str, Any]:
    """Explain what each lane should care about without leaking future text."""
    immediate_mode = route.get("immediate", "skip")
    summary_mode = route.get("summary", "append_later")
    reasoner_mode = route.get("reasoner", "skip")
    target_hint = str(target.get("spoiler_safe_hint") or "")
    bias = retrospective_bias.get("attention_bias") if isinstance(retrospective_bias, dict) else {}
    route_bias = retrospective_bias.get("route_bias") if isinstance(retrospective_bias, dict) else {}
    if not isinstance(bias, dict):
        bias = {}
    if not isinstance(route_bias, dict):
        route_bias = {}
    orientation = retrospective_bias.get("character_orientation") if isinstance(retrospective_bias, dict) else {}
    if not isinstance(orientation, dict):
        orientation = {}
    return {
        "immediate": {
            "mode": immediate_mode,
            "voice": "kurisu_persona",
            "goal": _immediate_goal(kind, immediate_mode, target_hint),
            "retrospective_style": str(bias.get("reaction_style") or ""),
            "character_orientation": orientation,
            "route_bias": str(route_bias.get("immediate") or "normal"),
            "max_sentences": 1 if density != "high" else 2,
            "avoid": ["future facts", "full recap", "evidence-table prose"],
        },
        "summary": {
            "mode": summary_mode,
            "goal": "append linear story state only when this line changes scene, affect, rule, or active thread",
            "debt": list(bias.get("summary_debt") or [])[:5],
            "not_proof": True,
        },
        "fact_extractor": {
            "mode": route.get("fact_extractor", "skip"),
            "goal": "write only directly displayed, checkable claims with evidence refs",
            "debt": list(bias.get("evidence_debt") or [])[:5],
            "strict_layers": ["candidate_fact", "evidence"],
        },
        "character_modeler": {
            "mode": route.get("character_modeler", "skip"),
            "goal": "track names, appearances, relationships, and affect anchors without turning them into proof",
            "debt": list(bias.get("character_debt") or [])[:5],
        },
        "reasoner": {
            "mode": reasoner_mode,
            "goal": "revise hypotheses and links; promote only grounded claims into evidence nodes",
            "depth": "deep" if reasoner_mode == "run_deep" else ("light" if reasoner_mode == "run_light" else "none"),
            "debt": list(bias.get("reasoning_debt") or [])[:5],
        },
        "verifier": {
            "mode": route.get("verifier", "skip"),
            "goal": "reject or weaken claims that are not grounded in displayed text",
        },
    }


def _immediate_goal(kind: str, mode: str, target_hint: str) -> str:
    if mode == "hold":
        return target_hint or "stay silent because a stronger nearby beat is planned"
    if mode == "skip":
        if kind == "topic_label":
            return "treat this as a navigation/topic cue; do not speak or write hard evidence"
        return "stay silent unless the player explicitly asks"
    if kind in {"evidence", "rule", "contradiction", "choice", "rule_anomaly"}:
        return "make one concise analytical reaction and preserve emotional color"
    if kind == "emotional_beat":
        return "acknowledge the affect briefly without over-explaining"
    return "react only if the line is unusually salient"


def _active_retrospective_bias(bias: dict[str, Any], line_count: int) -> dict[str, Any]:
    if not isinstance(bias, dict):
        return {}
    try:
        strength = float(bias.get("strength") or 0.0)
    except Exception:
        strength = 0.0
    if strength <= 0:
        return {}
    try:
        expires_at = int(bias.get("expires_at_line_count") or 0)
    except Exception:
        expires_at = 0
    if expires_at and line_count > expires_at:
        return {}
    return bias


def _apply_retrospective_bias(
    text: str,
    kind: str,
    density: str,
    route: dict[str, str],
    reasons: list[str],
    bias: dict[str, Any],
) -> None:
    if not bias:
        return
    attention_bias = bias.get("attention_bias") if isinstance(bias, dict) else {}
    route_bias = bias.get("route_bias") if isinstance(bias, dict) else {}
    if not isinstance(attention_bias, dict):
        attention_bias = {}
    if not isinstance(route_bias, dict):
        route_bias = {}
    suppress_kinds = {str(v) for v in attention_bias.get("suppress_kinds") or []}
    boost_kinds = {str(v) for v in attention_bias.get("boost_kinds") or []}
    boost_topics = [str(v) for v in attention_bias.get("boost_topics") or [] if str(v)]
    watch_for = [str(v) for v in attention_bias.get("watch_for") or [] if str(v)]
    topic_hit = any(topic and topic in text for topic in boost_topics + watch_for)

    if kind == "topic_label":
        reasons.append("retrospective bias ignored for compressed topic label")
        return
    if kind in suppress_kinds and route.get("immediate") == "react" and kind not in {"evidence", "rule", "contradiction", "choice", "rule_anomaly"}:
        route["immediate"] = "skip"
        reasons.append("retrospective bias suppressed repeated low-value reaction")
    if route_bias.get("immediate") == "quieter" and kind == "emotional_beat" and density != "high":
        route["immediate"] = "skip"
        reasons.append("retrospective bias asks for quieter emotional beats")
    if topic_hit or kind in boost_kinds:
        if route.get("reasoner") == "skip":
            route["reasoner"] = "run_light"
        if kind in {"evidence", "rule", "contradiction", "choice", "rule_anomaly"}:
            route["fact_extractor"] = "run"
            route["verifier"] = "run"
        reasons.append("retrospective bias boosted this topic/kind")
    if route_bias.get("fact_extractor") == "split_atomic_facts" and kind in {"evidence", "rule"}:
        route["fact_extractor"] = "run"
        route["verifier"] = "run"
        reasons.append("retrospective bias requests atomic evidence extraction")
    if route_bias.get("character_modeler") == "normalize_entities" and _has_character_hint(text):
        route["character_modeler"] = "run"
        reasons.append("retrospective bias requests entity normalization")
    if route_bias.get("reasoner") == "resolve_debts" and route.get("reasoner") == "skip" and density != "low":
        route["reasoner"] = "run_light"
        reasons.append("retrospective bias asks reasoner to resolve context debt")


def _adjust_budget_for_bias(budget: dict[str, Any], bias: dict[str, Any]) -> None:
    if not bias:
        return
    route_bias = bias.get("route_bias") if isinstance(bias, dict) else {}
    attention_bias = bias.get("attention_bias") if isinstance(bias, dict) else {}
    if not isinstance(route_bias, dict):
        route_bias = {}
    if not isinstance(attention_bias, dict):
        attention_bias = {}
    if route_bias.get("immediate") == "quieter":
        budget["speak_cooldown_lines"] = int(budget.get("speak_cooldown_lines") or 4) + 2
    if attention_bias.get("summary_debt"):
        budget["max_context_lines"] = max(int(budget.get("max_context_lines") or 20), 35)
    if attention_bias.get("evidence_debt") or attention_bias.get("reasoning_debt"):
        budget["next_reasoner_after_lines"] = min(int(budget.get("next_reasoner_after_lines") or 8), 4)


def _public_retrospective_bias(bias: dict[str, Any]) -> dict[str, Any]:
    if not bias:
        return {}
    attention_bias = bias.get("attention_bias") if isinstance(bias, dict) else {}
    route_bias = bias.get("route_bias") if isinstance(bias, dict) else {}
    return {
        "source": bias.get("source", ""),
        "strength": bias.get("strength", 0.0),
        "confidence": bias.get("confidence", 0.0),
        "expires_at_line_count": bias.get("expires_at_line_count", 0),
        "attention_bias": attention_bias if isinstance(attention_bias, dict) else {},
        "character_orientation": bias.get("character_orientation") if isinstance(bias.get("character_orientation"), dict) else {},
        "route_bias": route_bias if isinstance(route_bias, dict) else {},
    }
