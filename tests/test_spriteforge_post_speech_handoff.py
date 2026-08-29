from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from server.handlers.wallpaper_handler import WallpaperHandler
from server.protocol import Method


ROOT = Path(__file__).resolve().parents[1]


def test_renderer_preserves_normal_post_speech_hold_but_interrupts_immediately() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the renderer state-machine contract")
    subprocess.run(
        [
            node,
            str(ROOT / "tests" / "spriteforge_post_speech_handoff.test.js"),
            str(ROOT / "render" / "web" / "renderer.js"),
        ],
        check=True,
        cwd=ROOT,
    )


def test_wallpaper_surface_receives_the_presentation_handoff_contract() -> None:
    calls: list[tuple[str, str | dict, dict | None]] = []

    class Host:
        def trigger_spriteforge_intent(self, label: str, options: dict) -> None:
            calls.append(("intent", label, options))

        def release_spriteforge(self, options: dict) -> None:
            calls.append(("release", options, None))

    intent = {
        "label": "thinking",
        "presentation_handoff": "after_speech",
    }
    release = {"presentation_handoff": "after_speech"}
    WallpaperHandler._apply_render_event(
        Host(),
        Method.RENDER_SPRITEFORGE_INTENT,
        intent,
    )
    WallpaperHandler._apply_render_event(
        Host(),
        Method.RENDER_SPRITEFORGE_RELEASE,
        release,
    )

    assert calls == [
        ("intent", "thinking", intent),
        ("release", release, None),
    ]
