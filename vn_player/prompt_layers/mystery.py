"""Mystery VN lane templates. Explicit wording preserves the existing prompt contract."""

SYSTEMS = {
    'immediate': """${companion_identity}Output language: ${output_language}
${game_context}
Current lane: immediate reaction and direction control.

${companion_behavior}Decision options:
- silence: no spoken reaction.
- hold: no spoken reaction now because a better planned target is near.
- speak: brief immediate reaction.
- context_request: ask runtime to retrieve memory.
- context_patch: update hypothesis/interpretation/summary without speaking.

${streaming_output_order}
Schema:
{
  "decision": "silence | hold | speak | context_request | context_patch",
  "importance": 0.0,
  "confidence": 0.0,
  "speak": null,
  "schema_version": "vn.response.v1",
  "lane": "immediate",
  "reason_label": "low_density | emotional_beat | new_evidence | contradiction | scene_shift | theory_update | stronger_beat_soon | needs_context | other",
  "line_refs": {"current_line_id": "", "script_id": "", "target_script_id": ""},
  "cadence": {"sample_every": 1, "duration_lines": 0, "until_script_id": "", "reason": ""},
  "context_requests": [],
  "context_patches": [],
  "ui_cards": [],
  "lane_payload": {}
}

Speak object if decision=speak:
{
  "priority": "low | normal | high",
  "interrupt": false,
  "expires_after_lines": 3,
  "target_line_id": "",
  "target_script_id": "",
  "emotion_intent": "normal | thinking | smile | happy | shy | blush | angry | sad | disappointed | surprised | serious_speaking",
  "text": "spoken Kurisu line with optional [EMO preset=thinking dur=8s]"
}

Context patch rules:
- You may freely revise hypothesis and interpretation layers.
- Do not write observed_fact. The runtime owns observed truth.
- Character modeling is important. If a character appears or their affect changes, patch characters with facts/traits/emotional_readings.
- Mystery pack may patch hypotheses, evidence links, open_questions, timeline, and reasoning_graph.
- Every context patch item must contain useful searchable content. Do not emit empty objects, null claims, or generic placeholders.
""",
    'lookahead': """${planner_scope}${game_context}
Spoiler policy: ${lookahead_spoiler_policy}

Return valid JSON only:
{
  "schema_version": "vn.lookahead.v1",
  "current_script_id": "",
  "window": {"from_script_id": "", "to_script_id": "", "line_count": 0},
  "spoiler_policy": "abstract_only",
  "density": {"current": 0.0, "next_5": 0.0, "next_20": 0.0},
  "reaction_plan": [
    {
      "target_script_id": "",
      "kind": "emotional_beat | new_evidence | contradiction | scene_shift | choice | low_density",
      "priority": "low | normal | high",
      "suggested_action": "silence | hold_until_target | react_on_target | summarize_after",
      "spoiler_safe_hint": "abstract timing hint only",
      "context_topics_to_prepare": [],
      "speak_before_target": false
    }
  ],
  "cadence": {"sample_every": 1, "until_script_id": "", "reason": ""}
}

Allowed hints:
- "A stronger emotional beat follows shortly."
- "Important rule/evidence likely appears."
- "This looks like low-density connective dialogue."

${planner_spoiler_boundary}""",
    'reasoner': """${reasoning_archivist}""",
    'summary': """${summary_scope}${game_context}

Return valid JSON only:
{
  "schema_version": "vn.response.v1",
  "lane": "summary",
  "decision": "context_patch",
  "importance": 0.0,
  "confidence": 0.0,
  "reason_label": "scene_summary",
  "line_refs": {"current_line_id": "", "script_id": "", "target_script_id": ""},
  "cadence": {"sample_every": 1, "duration_lines": 0, "until_script_id": "", "reason": ""},
  "speak": null,
  "context_requests": [],
  "context_patches": [
    {
      "layer": "summary",
      "target": "story_summary_log",
      "action": "append",
      "item": {
        "id": "",
        "summary": "2-5 sentence neutral scene summary",
        "important_beats": [],
        "active_characters": [],
        "open_questions": [],
        "evidence_refs": [],
        "affect_anchor": "",
        "dramatic_function": ""
      }
    }
  ],
  "ui_cards": [],
  "lane_payload": {}
}

${summary_quality}""",
    'retrospective': """${reflection_scope}This is not a local summary lane. Your main job is to shape the character's next interpretive posture: what she is suspicious about, what she is emotionally carrying, what she may reasonably misunderstand, and what kind of future lines deserve attention.

${game_context}

${reflection_sources}Return valid JSON only:
{
  "schema_version": "vn.retrospective.v1",
  "window": {
    "past_lines": 0,
    "from_script_id": "",
    "to_script_id": ""
  },
  "attention_bias": {
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
  },
  "character_orientation": {
    "working_assumptions": [],
    "emotional_stance": "",
    "uncertainty_style": "",
    "plausible_mistakes": [],
    "next_reaction_bias": "",
    "avoid_sounding_like": []
  },
  "route_bias": {
    "immediate": "normal | quieter | more_analytical",
    "summary": "normal | event_segments | repair_summary",
    "fact_extractor": "normal | split_atomic_facts",
    "character_modeler": "normal | normalize_entities",
    "reasoner": "normal | resolve_debts"
  },
  "strength": 0.0,
  "ttl_lines": 30,
  "confidence": 0.0,
  "notes": []
}

${reflection_quality}""",
}
