"""Starting Wallpaper replays the existing shared scene, never a new Work claim."""
import asyncio

import pytest

from server.character_presentation import CharacterPresentationCoordinator
from server.event_bus import EventBus
from server.handlers.wallpaper_handler import WallpaperHandler
from server.protocol import Method


@pytest.mark.parametrize("owners", [(), ("work",), ("auip",), ("work", "auip")])
def test_late_wallpaper_start_reads_shared_activity_and_last_release_clears_it(tmp_path, monkeypatch, owners):
    async def run():
        bus = EventBus()
        monkeypatch.setattr("server.handlers.wallpaper_handler.bus", bus)
        created = []

        class Bridge:
            def __init__(self, *, slice_host):
                self.slice_host = slice_host
                self.started = False
                self.activities = []
                created.append(self)

            def start(self):
                self.started = True

            def stop(self):
                self.started = False

            def set_activity(self, activity):
                assert self.started
                self.activities.append(activity)

            def set_canvas_presentation(self, _payload):
                pass

            def set_canvas(self, _payload):
                pass

        class Animator:
            def __init__(self, _bridge):
                pass

            def start(self):
                pass

            def stop(self):
                pass

        monkeypatch.setattr("wallpaper.wallpaper_engine_bridge.WallpaperEngineBridgeHost", Bridge)
        monkeypatch.setattr("render.spriteforge_animator.SpriteForgeAnimator", Animator)
        presentation = CharacterPresentationCoordinator(bus.emit, emit_now=bus.emit_now)
        handler = WallpaperHandler()
        handler.configure(project_root=tmp_path, current_activity=presentation.current_activity)
        scene_events = []

        async def capture(_method, params):
            scene_events.append(params["activity"])

        bus.on(Method.WALLPAPER_ACTIVITY, capture)
        for owner in owners:
            await presentation.claim(source_kind=owner, source_id=owner + "-owner",
                label="work", scenario="computer-use")
        # Foreground speaking must not hide the underlying Work/AUIP scene fact.
        await presentation.claim(source_kind="chat", source_id="speaking",
            label="thinking", tier="utterance")
        assert presentation.effective_owner.source_kind == "chat"
        assert presentation.current_activity() == ("work" if owners else "")
        before = list(scene_events)
        assert created == []  # The initial activity event had no Wallpaper host.
        result = await handler._start({"slice_host":"electron"})
        assert result["status"] == "started"
        bridge = created[0]
        assert bridge.activities == ["work" if owners else ""]
        assert scene_events == before  # Restoring output never repeats a claim/event.
        for index, owner in enumerate(owners):
            await presentation.release(source_kind=owner, source_id=owner + "-owner",
                scenario="computer-use")
            if index < len(owners) - 1:
                assert presentation.current_activity() == "work"
                assert bridge.activities == ["work"]
        assert presentation.current_activity() == ""
        assert bridge.activities == (["work", ""] if owners else [""])
        # Reading the same owner again has no state or event side effects.
        after = list(scene_events)
        assert presentation.current_activity() == ""
        assert scene_events == after
        await handler._stop({})

    asyncio.run(run())
