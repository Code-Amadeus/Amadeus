"""The compact card changes presentation only, leaving Work and audio untouched."""
import asyncio

from server.handlers.wallpaper_handler import WallpaperHandler
from wallpaper.wallpaper_engine_bridge import WallpaperEngineBridgeHost


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
