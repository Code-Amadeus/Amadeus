from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from render.headless_bridge import HeadlessRenderBridge
from render.spriteforge_animator import (
    GRAPH_CONFIG_PATH,
    SpriteForgeAnimator,
    _CLOSED_EYE_SPEAKING_TRIGGER_LABEL,
    _DEFAULT_SPEAKING_TRIGGER_LABEL,
    _EMOTION_SPEAKING_ENTRY_BY_INTENT,
)
from render.spriteforge_intent import (
    RANDOM_TRIGGER_ROUTES,
    TRIGGER_ALIASES,
    resolve_spriteforge_trigger_label,
    spriteforge_intent_payload,
)
from server.event_bus import bus
from server.handlers.work_activity_handler import WorkActivityCoordinator
from server.protocol import Method


def test_thinking_and_work_choose_the_original_two_graph_entries() -> None:
    first = lambda routes: routes[0]
    last = lambda routes: routes[-1]

    assert resolve_spriteforge_trigger_label("thinking", chooser=first) == "speaking_trans"
    assert resolve_spriteforge_trigger_label("thinking", chooser=last) == "thinking_trans"
    assert resolve_spriteforge_trigger_label("work", chooser=first) == "speaking_trans"
    assert resolve_spriteforge_trigger_label("tool_call", chooser=last) == "thinking_trans"


def test_intent_payload_preserves_semantics_but_broadcasts_explicit_entry() -> None:
    payload = spriteforge_intent_payload("work", source="test")

    assert payload["label"] in {"speaking_trans", "thinking_trans"}
    assert payload["semantic_label"] == "work"
    assert payload["source"] == "test"


def test_headless_bridge_resolves_before_render_event_fanout() -> None:
    bridge = HeadlessRenderBridge()
    emitted: list[tuple[Method, dict]] = []
    bridge._emit = lambda method, params: emitted.append((method, params))

    bridge.trigger_spriteforge_intent("thinking")

    assert emitted[0][0] == Method.RENDER_SPRITEFORGE_INTENT
    assert emitted[0][1]["label"] in {"speaking_trans", "thinking_trans"}
    assert emitted[0][1]["semantic_label"] == "thinking"


async def _assert_work_activity_bypass_uses_authoritative_router() -> None:
    captured: list[dict] = []

    async def capture(_method: str, params: dict) -> None:
        captured.append(params)

    coordinator = WorkActivityCoordinator()
    bus.on(Method.RENDER_SPRITEFORGE_INTENT, capture)
    try:
        await coordinator._emit_behavior_intent(reason="tool.call", run_id="run-routing")
    finally:
        bus.off(Method.RENDER_SPRITEFORGE_INTENT, capture)

    assert len(captured) == 1
    assert captured[0]["label"] in {"speaking_trans", "thinking_trans"}
    assert captured[0]["semantic_label"] == "work"
    assert captured[0]["source"] == "character_presentation"
    assert captured[0]["presentation_source_kind"] == "work"
    assert captured[0]["presentation_source_id"] == "active-work"


def test_work_activity_bypass_uses_the_same_authoritative_router() -> None:
    asyncio.run(_assert_work_activity_bypass_uses_authoritative_router())


def test_frontend_payload_has_deterministic_semantic_fallback() -> None:
    animator = object.__new__(SpriteForgeAnimator)
    animator._graph = {"nodes": [], "edges": []}
    animator._root_node_id = None
    animator._loop_durations = {}

    config = animator._frontend_runtime_payload()["config"]

    assert config["semanticTriggerDefaults"]["thinking"] == "thinking_trans"


def test_all_configured_intents_reach_every_graph_node() -> None:
    if not GRAPH_CONFIG_PATH.exists():
        return

    graph = json.loads(GRAPH_CONFIG_PATH.read_text(encoding="utf-8"))
    labels_by_id = {node["id"]: node["label"] for node in graph["nodes"]}
    ids_by_label = {label: node_id for node_id, label in labels_by_id.items()}
    root_label = next(node["label"] for node in graph["nodes"] if node.get("isRoot"))

    valid_semantic_targets = set(ids_by_label) | set(_EMOTION_SPEAKING_ENTRY_BY_INTENT)
    unresolved_aliases = {
        alias: target
        for alias, target in TRIGGER_ALIASES.items()
        if target not in valid_semantic_targets and target not in RANDOM_TRIGGER_ROUTES
    }
    invalid_routes = {
        semantic: [target for target in routes if target not in valid_semantic_targets]
        for semantic, routes in RANDOM_TRIGGER_ROUTES.items()
        if any(target not in valid_semantic_targets for target in routes)
    }
    assert unresolved_aliases == {}
    assert invalid_routes == {}

    seed_labels = {
        root_label,
        _DEFAULT_SPEAKING_TRIGGER_LABEL,
        _CLOSED_EYE_SPEAKING_TRIGGER_LABEL,
        *_EMOTION_SPEAKING_ENTRY_BY_INTENT.values(),
        *(target for routes in RANDOM_TRIGGER_ROUTES.values() for target in routes),
    }
    reachable = {ids_by_label[label] for label in seed_labels if label in ids_by_label}
    changed = True
    while changed:
        changed = False
        for edge in graph["edges"]:
            if (
                edge["from"] in reachable
                and float(edge.get("prob", 0)) > 0
                and edge["to"] not in reachable
            ):
                reachable.add(edge["to"])
                changed = True

    unreachable = set(labels_by_id) - reachable
    assert {labels_by_id[node_id] for node_id in unreachable} == set()


async def main() -> None:
    test_thinking_and_work_choose_the_original_two_graph_entries()
    test_intent_payload_preserves_semantics_but_broadcasts_explicit_entry()
    test_headless_bridge_resolves_before_render_event_fanout()
    await _assert_work_activity_bypass_uses_authoritative_router()
    test_frontend_payload_has_deterministic_semantic_fallback()
    test_all_configured_intents_reach_every_graph_node()


if __name__ == "__main__":
    asyncio.run(main())
