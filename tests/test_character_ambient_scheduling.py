from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from render import spriteforge_animator as animation
from wallpaper import scene_assets


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("case", [
    "idle_delay", "speech_and_intent", "idle_scenario_clock",
    "static_cadence", "animated_cadence", "activity_interrupt",
])
def test_frontend_ambient_scheduling(case: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for renderer contracts")
    subprocess.run(
        [node, str(ROOT / "tests/character_ambient_scheduling.test.js"), case],
        cwd=ROOT, check=True, capture_output=True, text=True, timeout=10,
    )


def test_python_graph_waits_from_speech_end_before_side_entry(monkeypatch) -> None:
    class Engine:
        def set_emotion(self, _label):
            pass

        def set_speaking(self, _speaking):
            pass

    clock = [0.0]
    monkeypatch.setattr(animation.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(animation.random, "random", lambda: 0.999)
    animator = animation.SpriteForgeAnimator(Engine())
    monkeypatch.setattr(animator, "_schedule_post_speech_hold_locked", lambda: None)
    animator._graph = {
        "nodes": [
            {"id": "idle", "label": "idle"},
            {"id": "variant", "label": "idle1"},
            {"id": "standby", "label": "trans_standby"},
            {"id": "speaking", "label": "speaking_short"},
        ],
        "edges": [
            {"from": source, "to": target, "prob": probability}
            for source in ("idle", "variant")
            for target, probability in ((source, 0.95), ("standby", 0.05))
        ],
    }
    animator._current_node_id = "idle"
    animator._play_node("idle")
    clock[0] = 29.9
    assert animator._next_auto_node("idle") == "idle"
    animator._play_node("variant")
    clock[0] = 30.0
    assert animator._next_auto_node("variant") == "standby"
    animator.on_speaking(True)
    assert animator._pending_expression == "speaking_short"
    assert animator._next_auto_node("idle") == "idle"
    clock[0] = 40.0
    animator.on_speaking(False)
    clock[0] = 41.0
    animator._play_node("idle")
    clock[0] = 50.0
    animator.on_speaking(False)
    clock[0] = 69.9
    assert animator._next_auto_node("idle") == "idle"
    clock[0] = 70.0
    assert animator._next_auto_node("idle") == "standby"
    clock[0] = 80.0
    animator.trigger_expression("idle")
    assert animator._next_auto_node("idle") == "standby"
    config = animator._frontend_runtime_payload()["config"]
    assert config["idleStandbyEntryLabel"] == "trans_standby"
    assert config["idleStandbyDelaySec"] == 30.0


@pytest.mark.parametrize(("interval", "expected"), [
    (None, (45.0, 60.0)),
    ("48", (48.0, 48.0)),
])
def test_wallpaper_payload_carries_the_dwell_range(tmp_path, monkeypatch, interval, expected) -> None:
    (tmp_path / "scenario_graph.json").write_text(
        json.dumps({"nodes": [{"id": "rest", "resource": "rest.png"}], "edges": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(scene_assets, "_RUNTIME_SCENARIOS", tmp_path)
    monkeypatch.setattr(scene_assets, "_prepare_scenario_backplate", lambda *_: (None, None))
    monkeypatch.setattr(scene_assets, "_scenario_resource_payload", lambda *_: {"type": "image"})
    if interval is None:
        monkeypatch.delenv("AMADEUS_SCENARIO_STATIC_SECONDS", raising=False)
    else:
        monkeypatch.setenv("AMADEUS_SCENARIO_STATIC_SECONDS", interval)
    payload = scene_assets._prepare_scenario_payload(17777)
    assert (payload["staticHoldSeconds"], payload["staticHoldMaxSeconds"]) == expected
