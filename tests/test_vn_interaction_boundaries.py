from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from PIL import Image

from server import visual_runtime
from server.handlers.vn_player_handler import VNPlayerHandler
from server.vn_launch_manager import VNLaunchManager


def test_late_asr_cannot_cross_vn_session_boundary() -> None:
    handler = VNPlayerHandler()
    runtime = SimpleNamespace(enabled=True, profile=SimpleNamespace(session_id="current"),
                              player_intervention=AsyncMock(return_value={"status": "ok"}))
    handler._runtime = runtime

    async def run():
        for session in ("previous", "", None):
            result = await handler.handle_asr({"text": "用户的问题", "source_payload": {"session_id": session, "kind": "ask"}})
            assert result["status"] == "ignored"
        runtime.player_intervention.assert_not_awaited()
        result = await handler.handle_asr({"text": "用户的问题", "source_payload": {"session_id": "current", "kind": "ask"}})
        assert result["status"] == "ok"
        assert runtime.player_intervention.await_args.args[0] == "ask"
        runtime.enabled = False
        result = await handler.handle_asr({"text": "晚到的识别", "source_payload": {"session_id": "current"}})
        assert result["status"] == "ignored"
        assert runtime.player_intervention.await_count == 1
    asyncio.run(run())


@pytest.mark.skipif(visual_runtime.os.name != "nt", reason="Windows window capture")
def test_visual_capture_is_game_window_only_and_does_not_change_global_config(tmp_path: Path) -> None:
    game = str(tmp_path / "game.exe")
    original = visual_runtime.get_config()
    window = {"pid": 42, "hwnd": "0x1234", "title": "Game", "rect": {"left": 100, "top": 100, "width": 80, "height": 60}}
    with patch("psutil.Process", return_value=Mock(exe=Mock(return_value=game))), \
         patch.object(visual_runtime, "list_capture_windows", return_value=[window]), \
         patch.object(visual_runtime, "_window_pid", return_value=42), \
         patch("PIL.ImageGrab.grab", return_value=Image.new("RGB", (80, 60))) as grab:
        result = visual_runtime.capture_game_window(42, game)
        grab.assert_called_once_with(window=0x1234)
        assert result["actualScope"] == "game_window"
        assert result["frame"]["dataUrl"].startswith("data:image/jpeg;base64,")
        assert result["game"]["pid"] == 42
    assert visual_runtime.get_config() == original


@pytest.mark.skipif(visual_runtime.os.name != "nt", reason="Windows window capture")
def test_visual_capture_rejects_changed_process_without_desktop_fallback(tmp_path: Path) -> None:
    with patch("psutil.Process", return_value=Mock(exe=Mock(return_value=str(tmp_path / "other.exe")))), \
         patch("PIL.ImageGrab.grab") as grab:
        with pytest.raises(RuntimeError, match="process changed"):
            visual_runtime.capture_game_window(42, str(tmp_path / "game.exe"))
        grab.assert_not_called()


def test_capture_requires_active_interaction_and_uses_profile_executable(tmp_path: Path) -> None:
    async def run():
        runtime_status = AsyncMock(return_value={"visual": {"supported": True}, "capabilities": {"interaction": {"enabled": True}}})
        manager = VNLaunchManager(tmp_path, runtime_start=AsyncMock(), runtime_stop=AsyncMock(),
                                  runtime_status=runtime_status, runtime_line=AsyncMock())
        with pytest.raises(RuntimeError, match="Start a companion"):
            await manager.capture()
        manager._state.update(status="active", profileId="paranormasight", captureOnly=False)
        with patch.object(manager, "_profile_by_id", return_value={"gameExe": str(tmp_path / "game.exe")}), \
             patch("server.vn_launch_manager._find_game_pid", return_value=42) as find, \
             patch.object(visual_runtime, "capture_game_window", return_value={"frame": {"dataUrl": "frame"}}) as capture:
            result = await manager.capture()
            assert result["visual_context"]["frame"]["dataUrl"] == "frame"
            find.assert_called_once_with(str(tmp_path / "game.exe"))
            capture.assert_called_once_with(42, str(tmp_path / "game.exe"))
            find.reset_mock()
            manager._state["game"]["pid"] = 42
            await manager.capture()
            find.assert_not_called()
            runtime_status.return_value["capabilities"]["interaction"]["enabled"] = False
            with pytest.raises(RuntimeError, match="Enable player interaction"):
                await manager.capture()
            assert capture.call_count == 2
    asyncio.run(run())
