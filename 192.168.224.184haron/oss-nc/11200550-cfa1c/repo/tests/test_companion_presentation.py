"""The compact card changes presentation only, leaving Work and audio untouched."""
import asyncio

from server.handlers.wallpaper_handler import WallpaperHandler
from wallpaper.wallpaper_engine_bridge import WallpaperEngineBridgeHost


def test_companion_reopen_restores_latest_caption_without_reviving_wallpaper_subtitles():
    host = WallpaperEngineBridgeHost()
    host.set_subtitle("上一句")
    host.set_speaking(False)
    host.set_subtitle("")

    def subtitles(client):
        events = [client.get_nowait() for _ in range(client.qsize())]
        return [event["args"][0] for event in events if event["method"] == "setSubtitle"]

    wallpaper = host._state.add_client()
    card = host._state.add_client(retain_subtitle=True)
    assert subtitles(wallpaper) == [""]
    assert subtitles(card) == ["上一句"]
    host._state.remove_client(card)

    # Speech can advance while the card is closed; reopening must not use a
    # stale renderer cache or turn a retained caption into active speech.
    host.set_subtitle("关闭期间的新一句")
    host.set_subtitle("")
    reopened = host._state.add_client(retain_subtitle=True)
    events = [reopened.get_nowait() for _ in range(reopened.qsize())]
    assert {event["method"]: event["args"] for event in events} == {
        "setSubtitle": ["关闭期间的新一句"], "setSpeaking": [False],
    }
    assert subtitles(wallpaper) == ["关闭期间的新一句", ""]
    host._state.remove_client(wallpaper)
    host._state.remove_client(reopened)

    fresh = WallpaperEngineBridgeHost()
    fresh.set_subtitle("")
    client = fresh._state.add_client(retain_subtitle=True)
    assert subtitles(client) == [""]
    fresh._state.remove_client(client)


def test_companion_open_close_replays_visibility_without_routing_work():
    async def run():
        handler = WallpaperHandler()
        host = WallpaperEngineBridgeHost()
        handler._wallpaper_host = host
        routed = []
        handler._canvas_action_fn = lambda data: routed.append(data)
        host.set_subtitle("当前说话的内容")
        host.set_speaking(True)
        before = dict(host._state.last_calls)
        for active in (True, False):
            result = await handler._route_canvas_action({"target": "presentation", "action": "companion", "active": active})
            assert result == {"ok": True, "active": active}
            assert host._state.last_calls["companion"]["args"] == [active]
            assert all(host._state.last_calls[key] == value for key, value in before.items())
        assert routed == []
        client = host._state.add_client()
        replay = [client.get_nowait() for _ in range(client.qsize())]
        assert any(event["method"] == "setCompanionActive" and event["args"] == [False] for event in replay)
        host._state.remove_client(client)
    asyncio.run(run())


def test_invalid_or_offline_companion_request_cannot_change_presentation():
    async def run():
        handler = WallpaperHandler()
        assert await handler._route_canvas_action({"target": "presentation", "action": "companion", "active": "true"}) == {"ok": False, "error": "invalid_companion_visibility"}
        assert await handler._route_canvas_action({"target": "presentation", "action": "companion", "active": True}) == {"ok": False, "error": "wallpaper_not_running"}
    asyncio.run(run())
