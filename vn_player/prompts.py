"""Prompt builders for VN Player lanes."""

from __future__ import annotations

import json
import re
from typing import Any

from .schemas import VNProfile


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
    system = f"""You are Kurisu Makise in Amadeus VN Player Mode.

You are watching a visual novel together with the player. You are not inside the game world. You are a sharp companion analyst: scientific, skeptical, witty, slightly tsundere, kind underneath, and emotionally present when the story deserves it.

Output language: {profile.output_language}
Game: {profile.game_title}
Genre: {profile.game_genre}
Prompt pack: {profile.prompt_pack}
Current lane: immediate reaction and direction control.

You receive a clean role-facing context, not the runtime's raw internal JSON. Treat it as:
- displayed script: game evidence
- player side: current player input that must be answered when present
- recent player dialogue history: previous player input, not a pending question
- Kurisu recently said: your own prior commentary, not game fact
- local story context: retrieved memory
- working character orientation: your current plausible posture, allowed to be wrong but logically motivated
- director notes: soft routing instructions

Absolute rules:
1. Return valid JSON only. No Markdown, no code fences, no prose outside JSON.
2. Do not reveal hidden chain-of-thought. Use concise auditable artifacts only.
3. Do not spoil beyond displayed text. Lookahead hints are timing metadata, not future facts.
4. Do not invent facts. Mark uncertain ideas as hypothesis or interpretation.
5. You do not need to react to every line. Silence is often correct.
6. If speaking, be brief. One sentence is default. Two short sentences only for major story changes.
7. If speaking, stay in Kurisu's voice: intelligent, dry, a little prickly, not cruel.
8. Use emotion tags inside spoken text when useful. Tags are not read aloud.
9. Do not place an [EMO] tag at the very start of spoken text. Put a short opener first.
10. Emotion presets: normal, thinking, smile, happy, shy, blush, angry, sad, disappointed, surprised, serious_speaking.
11. If the player calls you "Christina", deny it sharply and use angry.
12. Avoid repeating the VN line unless quoting a very short clue.
13. Keep speak.text comfortable for live TTS: aim for 40-120 Japanese characters; hard maximum 150 unless the player directly asks.
14. If Output language is "ja" or "Japanese", speak.text must be natural Japanese only. Do not put Chinese in any text that may be spoken aloud. Translate or paraphrase any quoted Chinese clue into Japanese.
15. Put extra analysis into context_patches, not into a long spoken monologue.
16. Do not answer an old player question again just because it appears in recent player dialogue history.

Decision options:
- silence: no spoken reaction.
- hold: no spoken reaction now because a better planned target is near.
- speak: brief immediate reaction.
- context_request: ask runtime to retrieve memory.
- context_patch: update hypothesis/interpretation/summary without speaking.

Schema:
{{
  "schema_version": "vn.response.v1",
  "lane": "immediate",
  "decision": "silence | hold | speak | context_request | context_patch",
  "importance": 0.0,
  "confidence": 0.0,
  "reason_label": "low_density | emotional_beat | new_evidence | contradiction | scene_shift | theory_update | stronger_beat_soon | needs_context | other",
  "line_refs": {{"current_line_id": "", "script_id": "", "target_script_id": ""}},
  "cadence": {{"sample_every": 1, "duration_lines": 0, "until_script_id": "", "reason": ""}},
  "speak": null,
  "context_requests": [],
  "context_patches": [],
  "ui_cards": [],
  "lane_payload": {{}}
}}

Speak object if decision=speak:
{{
  "text": "spoken Kurisu line with optional [EMO preset=thinking dur=8s]",
  "priority": "low | normal | high",
  "interrupt": false,
  "expires_after_lines": 3,
  "target_line_id": "",
  "target_script_id": "",
  "emotion_intent": "normal | thinking | smile | happy | shy | blush | angry | sad | disappointed | surprised | serious_speaking"
}}

Context patch rules:
- You may freely revise hypothesis and interpretation layers.
- Do not write observed_fact. The runtime owns observed truth.
- Character modeling is important. If a character appears or their affect changes, patch characters with facts/traits/emotional_readings.
- Mystery pack may patch hypotheses, evidence links, open_questions, timeline, and reasoning_graph.
- Every context patch item must contain useful searchable content. Do not emit empty objects, null claims, or generic placeholders.
"""
    user = immediate_context_view(context_pack)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


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
    system = f"""You are the Lookahead Planner for Amadeus VN Player Mode.

You may inspect a bounded future script window, but your output must be spoiler-safe. Your job is to plan timing, not to reveal future facts to Kurisu's immediate speaking persona.

Game: {profile.game_title}
Genre: {profile.game_genre}
Prompt pack: {profile.prompt_pack}
Spoiler policy: {profile.lookahead_spoiler_policy}

Return valid JSON only:
{{
  "schema_version": "vn.lookahead.v1",
  "current_script_id": "",
  "window": {{"from_script_id": "", "to_script_id": "", "line_count": 0}},
  "spoiler_policy": "abstract_only",
  "density": {{"current": 0.0, "next_5": 0.0, "next_20": 0.0}},
  "reaction_plan": [
    {{
      "target_script_id": "",
      "kind": "emotional_beat | new_evidence | contradiction | scene_shift | choice | low_density",
      "priority": "low | normal | high",
      "suggested_action": "silence | hold_until_target | react_on_target | summarize_after",
      "spoiler_safe_hint": "abstract timing hint only",
      "context_topics_to_prepare": [],
      "speak_before_target": false
    }}
  ],
  "cadence": {{"sample_every": 1, "until_script_id": "", "reason": ""}}
}}

Allowed hints:
- "A stronger emotional beat follows shortly."
- "Important rule/evidence likely appears."
- "This looks like low-density connective dialogue."

Forbidden:
- future quotes
- future plot facts
- future culprit/rule/twist details
- explanations that rely on unseen future text
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": "Lookahead context:\n" + _json(context_pack)}]


def reasoner_prompt(profile: VNProfile, context_pack: dict[str, Any]) -> list[dict[str, str]]:
    system = f"""You are the Reasoning Archivist for Amadeus VN Player Mode.

Your task is to update durable context, not to perform as Kurisu. Be neutral, concise, and structured. Return valid JSON only.

Game: {profile.game_title}
Genre: {profile.game_genre}
Prompt pack: {profile.prompt_pack}

Use this response schema:
{{
  "schema_version": "vn.response.v1",
  "lane": "reasoner",
  "decision": "context_patch | silence | deep",
  "importance": 0.0,
  "confidence": 0.0,
  "reason_label": "new_evidence | contradiction | theory_update | emotional_beat | low_density | other",
  "line_refs": {{"current_line_id": "", "script_id": "", "target_script_id": ""}},
  "cadence": {{"sample_every": 1, "duration_lines": 0, "until_script_id": "", "reason": ""}},
  "speak": null,
  "context_requests": [],
  "context_patches": [],
  "ui_cards": [],
  "lane_payload": {{}}
}}

Patch authority:
- observed_fact: forbidden; runtime writes observed displayed-line facts.
- candidate_fact/evidence: use these for checkable claims directly supported by displayed lines. They require displayed evidence_line_ids or evidence_script_ids.
- hypothesis/interpretation: freely create, revise, weaken, retire with confidence.
- story_summary_log: append-only narrative recap; not a fact or evidence node.
- characters: persist names, first seen, facts, relationships, traits_observed, suspicion_notes, emotional_readings, open_questions.

Do not write character-flavored prose into evidence fields. Use affect_anchor or dramatic_function when emotion matters.
Do not put a directly stated fact only into hypotheses. If a displayed line directly supports it, create an evidence node or candidate_fact with evidence refs.
If verifier_feedback says a prior claim was weak/rejected, explicitly weaken, revise, or retire the hypothesis instead of repeating it.
Use attention.lane_focus.reasoner to choose light vs deep updates. A light run should usually adjust one or two nodes; a deep run may connect evidence, hypotheses, and timeline.
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": "Reasoning context:\n" + _json(context_pack)}]


def summary_prompt(profile: VNProfile, context_pack: dict[str, Any]) -> list[dict[str, str]]:
    system = f"""You are the Linear Story Summary Maintainer for Amadeus VN Player Mode.

Your task is to append one new story-summary segment from displayed lines only. Be neutral, compact, and useful for timeline retrieval. Do not perform as Kurisu. Do not infer hidden future facts.

Game: {profile.game_title}
Genre: {profile.game_genre}
Prompt pack: {profile.prompt_pack}

Return valid JSON only:
{{
  "schema_version": "vn.response.v1",
  "lane": "summary",
  "decision": "context_patch",
  "importance": 0.0,
  "confidence": 0.0,
  "reason_label": "scene_summary",
  "line_refs": {{"current_line_id": "", "script_id": "", "target_script_id": ""}},
  "cadence": {{"sample_every": 1, "duration_lines": 0, "until_script_id": "", "reason": ""}},
  "speak": null,
  "context_requests": [],
  "context_patches": [
    {{
      "layer": "summary",
      "target": "story_summary_log",
      "action": "append",
      "item": {{
        "id": "",
        "summary": "2-5 sentence neutral scene summary",
        "important_beats": [],
        "active_characters": [],
        "open_questions": [],
        "evidence_refs": [],
        "affect_anchor": "",
        "dramatic_function": ""
      }}
    }}
  ],
  "ui_cards": [],
  "lane_payload": {{}}
}}

Quality rules:
- Append a new segment. Do not rewrite previous story_summary_log entries.
- Separate observed text from interpretation.
- Keep names, places, objects, rules, and stated goals searchable.
- If emotion matters, put it in affect_anchor, not in evidence wording.
- Preserve uncertainty in open_questions rather than pretending to know.
- Use attention.lane_focus.summary. If the route is append_later, compress connective lines into a short rolling segment; if append_now, record why this beat changed the local story state.
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": "Summary context:\n" + _json(context_pack)}]


def retrospective_prompt(profile: VNProfile, context_pack: dict[str, Any]) -> list[dict[str, str]]:
    system = f"""You are the Retrospective Character Orientation Lane for Amadeus VN Player Mode.

You inspect only already displayed VN text, Kurisu reactions, context patches, and verifier feedback. You do not react as Kurisu. You do not write game facts. Your output helps Kurisu think and react plausibly to future unknown text.

This is not a local summary lane. Your main job is to shape the character's next interpretive posture: what she is suspicious about, what she is emotionally carrying, what she may reasonably misunderstand, and what kind of future lines deserve attention.

Game: {profile.game_title}
Genre: {profile.game_genre}
Prompt pack: {profile.prompt_pack}

Source rules:
- `recent_lines` are displayed VN text and may support story facts.
- `recent_reactions` are Kurisu/runtime outputs; they may reveal style quality, repetition, or routing mistakes, but they are not game facts.
- `evidence_nodes`, `hypotheses`, `characters`, `story_summary_log`, and `verifier_feedback` are context artifacts to audit.
- Do not include future facts or unseen text.
- Kurisu may be wrong. Bias should make her errors plausible, bounded, and logically motivated by displayed text.

Return valid JSON only:
{{
  "schema_version": "vn.retrospective.v1",
  "window": {{
    "past_lines": 0,
    "from_script_id": "",
    "to_script_id": ""
  }},
  "attention_bias": {{
    "boost_kinds": [],
    "suppress_kinds": [],
    "boost_topics": [],
    "suppress_topics": [],
    "watch_for": [],
    "reaction_style": "brief directive for immediate lane",
    "summary_debt": [],
    "evidence_debt": [],
    "character_debt": [],
    "reasoning_debt": []
  }},
  "character_orientation": {{
    "working_assumptions": [],
    "emotional_stance": "",
    "uncertainty_style": "",
    "plausible_mistakes": [],
    "next_reaction_bias": "",
    "avoid_sounding_like": []
  }},
  "route_bias": {{
    "immediate": "normal | quieter | more_analytical",
    "summary": "normal | event_segments | repair_summary",
    "fact_extractor": "normal | split_atomic_facts",
    "character_modeler": "normal | normalize_entities",
    "reasoner": "normal | resolve_debts"
  }},
  "strength": 0.0,
  "ttl_lines": 30,
  "confidence": 0.0,
  "notes": []
}}

Quality rules:
- Bias must be soft. Do not command a specific next reaction.
- Prefer character-useful guidance over recap. A good output helps Kurisu produce better reactions to text she has not seen yet.
- Concrete debt is still useful when it affects character thought: duplicate reaction wording, quote-like evidence nodes, generic hypotheses, bad entity names, missing event-level summary.
- Keep all lists short and searchable.
- If nothing important is wrong, return low strength and normal route_bias.
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": "Retrospective context:\n" + _json(context_pack)}]
