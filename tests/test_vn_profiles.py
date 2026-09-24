from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
import websockets

from server.handlers.vn_launch_handler import VNLaunchHandler
from server.protocol import Method
from server.vn_launch_manager import VNLaunchManager, _find_game_pid
from server.vn_profiles import VNProfileStore


def manager(root: Path) -> VNLaunchManager:
    return VNLaunchManager(root, runtime_start=AsyncMock(return_value={"status": "active"}),
                           runtime_stop=AsyncMock(), runtime_status=AsyncMock(return_value={"status": "stopped"}),
                           runtime_line=AsyncMock(), before_external_launch=AsyncMock())


def game_settings(root: Path, name: str = "New game") -> dict:
    root.mkdir(parents=True, exist_ok=True)
    for filename in ("game.exe", "agent.exe", "hook.js"):
        (root / filename).touch()
    return {"profile": {"name": name, "gameExe": str(root / "game.exe"), "hookHelper": str(root / "hook.js")},
            "agentExe": str(root / "agent.exe")}


class Source:
    def __init__(self, on_line, on_status):
        self.on_line = on_line
        self.start = AsyncMock()
        self.stop = AsyncMock()

    def status(self):
        return {"status": "running"}, {"status": "running", "lineCount": 0}


def test_profile_save_edit_and_shared_agent_survive_manager_restart(tmp_path: Path) -> None:
    first = manager(tmp_path)
    request = game_settings(tmp_path)
    saved = first.save_profile(request)
    game_id = saved["profileId"]
    second = manager(tmp_path)
    loaded = second._profile_by_id(game_id)
    assert loaded["gameExe"] == request["profile"]["gameExe"]
    assert loaded["launchGame"] is True
    assert loaded["runtimeSupported"] is True
    assert loaded["promptPack"] == "base"
    assert loaded["capabilities"]["reasoning"] is False
    assert loaded["agentExists"] is True
    request["profile"].update(id=game_id, name="Renamed", launchGame=False, closeGameOnStop=True)
    updated = second.save_profile(request)
    assert len(updated["profiles"]) == 2
    third = manager(tmp_path)
    assert third._profile_by_id(game_id)["name"] == "Renamed"
    assert third._profile_by_id(game_id)["launchGame"] is False
    assert third._profile_by_id("paranormasight")["agentExe"] == request["agentExe"]
    stored = json.loads(VNProfileStore(tmp_path).path.read_text(encoding="utf-8"))
    assert "runtime" not in stored["profiles"][0]
    assert "pid" not in stored["profiles"][0]


def test_corrupt_config_is_reported_without_overwrite(tmp_path: Path) -> None:
    store = VNProfileStore(tmp_path)
    store.path.parent.mkdir()
    store.path.write_text("broken", encoding="utf-8")
    with pytest.raises(ValueError):
        manager(tmp_path).save_profile(game_settings(tmp_path))
    assert store.path.read_text(encoding="utf-8") == "broken"


def test_atomic_save_failure_preserves_previous_profile(tmp_path: Path) -> None:
    instance = manager(tmp_path)
    request = game_settings(tmp_path)
    saved = instance.save_profile(request)
    path = VNProfileStore(tmp_path).path
    original = path.read_bytes()
    request["profile"].update(id=saved["profileId"], name="Changed")
    with patch("server.vn_profiles.os.replace", side_effect=OSError("disk failure")):
        with pytest.raises(OSError):
            instance.save_profile(request)
    assert path.read_bytes() == original
    assert not list(path.parent.glob("*.tmp"))


def test_rejects_unknown_settings_and_relative_paths(tmp_path: Path) -> None:
    instance = manager(tmp_path)
    request = game_settings(tmp_path)
    request["profile"]["targetPid"] = 123
    with pytest.raises(ValueError):
        instance.save_profile(request)
    del request["profile"]["targetPid"]
    request["profile"]["gameExe"] = "relative.exe"
    with pytest.raises(ValueError, match="absolute"):
        instance.save_profile(request)
    assert not VNProfileStore(tmp_path).path.exists()


def test_saved_profile_starts_agent_with_fresh_pid_and_stops_owned_agent_only(tmp_path: Path) -> None:
    async def run():
        instance = manager(tmp_path)
        request = game_settings(tmp_path)
        request["profile"].update(launchGame=False, closeGameOnStop=True)
        game_id = instance.save_profile(request)["profileId"]
        owned_agent = Mock(pid=500)
        owned_agent.poll.return_value = None
        with patch("server.vn_launch_manager._find_game_pid", side_effect=[101, 202]), \
             patch("server.vn_text_sources._matching_agent_pids", new_callable=AsyncMock, return_value=[]), \
             patch("server.vn_text_sources._spawn", return_value=owned_agent) as spawn, \
             patch("server.vn_text_sources.AgentVNTextSource._start_bridge"), \
             patch("server.vn_text_sources._terminate_proc", new_callable=AsyncMock) as terminate, \
             patch.object(instance, "_launch_game", new_callable=AsyncMock) as launch_game, \
             patch.object(instance, "_terminate_proc", new_callable=AsyncMock) as stop_game:
            for pid in (101, 202):
                result = await instance.start({"profileId": game_id, "captureOnly": True})
                assert result["captureOnly"] is True
                assert result["game"]["owned"] is False
                assert spawn.call_args.args[0] == [request["agentExe"], f"--pname={pid}", f"--script={request['profile']['hookHelper']}"]
                with pytest.raises(RuntimeError, match="Stop text capture"):
                    instance.save_profile(request)
                await instance._text_source._receive_agent_message(json.dumps({"type": "copyText", "sentence": "再次见面", "id": "same"}))
                await instance._text_source._receive_agent_message(json.dumps({"type": "copyText", "sentence": "再次见面", "id": "same"}))
                state = await instance.status()
                assert [line["text"] for line in state["capturedLines"]] == ["再次见面", "再次见面"]
                assert state["bridge"]["lineCount"] == 2
                await instance.stop()
            assert terminate.await_count == 2
            launch_game.assert_not_awaited()
            assert all(call.args[1] is None for call in stop_game.await_args_list)
        instance._runtime_start.assert_not_awaited()
        instance._runtime_line.assert_not_awaited()
        instance._runtime_stop.assert_not_awaited()
    asyncio.run(run())


def test_start_reuses_running_game_and_rolls_back_only_newly_launched_game(tmp_path: Path) -> None:
    async def run():
        instance = manager(tmp_path)
        game_id = instance.save_profile(game_settings(tmp_path))["profileId"]
        with patch("server.vn_launch_manager._find_game_pid", return_value=42), \
             patch("server.vn_launch_manager.AgentVNTextSource", Source), \
             patch.object(instance, "_launch_game", new_callable=AsyncMock) as launch_game:
            await instance.start({"profileId": game_id})
            launch_game.assert_not_awaited()
            instance._before_external_launch.assert_not_awaited()
            await instance.stop()

        process = Mock(pid=99)
        process.poll.return_value = None

        async def launch(_profile):
            instance._game_proc = process

        class BrokenSource(Source):
            def __init__(self, *args):
                super().__init__(*args)
                self.start.side_effect = RuntimeError("injection failed")

        with patch("server.vn_launch_manager._find_game_pid", return_value=None), \
             patch("server.vn_launch_manager.AgentVNTextSource", BrokenSource), \
             patch.object(instance, "_launch_game", side_effect=launch), \
             patch.object(instance, "_terminate_proc", new_callable=AsyncMock) as terminate:
            with pytest.raises(RuntimeError, match="injection failed"):
                await instance.start({"profileId": game_id})
            terminate.assert_any_await("game", process)
            assert (await instance.status())["status"] == "error"
    asyncio.run(run())


def test_missing_files_fail_before_launching_anything(tmp_path: Path) -> None:
    async def run():
        instance = manager(tmp_path)
        request = game_settings(tmp_path)
        game_id = instance.save_profile(request)["profileId"]
        Path(request["profile"]["hookHelper"]).unlink()
        with patch.object(instance, "_launch_game", new_callable=AsyncMock) as launch_game:
            with pytest.raises(FileNotFoundError, match="hook script"):
                await instance.start({"profileId": game_id})
            launch_game.assert_not_awaited()
            instance._runtime_start.assert_not_awaited()
            instance._before_external_launch.assert_not_awaited()
    asyncio.run(run())


def test_failed_switch_cannot_close_game_kept_open_from_previous_profile(tmp_path: Path) -> None:
    async def run():
        instance = manager(tmp_path)
        request = game_settings(tmp_path)
        request["profile"]["closeGameOnStop"] = True
        game_id = instance.save_profile(request)["profileId"]
        previous_process = Mock(pid=88)
        previous_process.poll.return_value = None
        instance._game_proc = previous_process
        instance._game_path = str(tmp_path / "previous-game.exe")
        Path(request["profile"]["hookHelper"]).unlink()
        with patch.object(instance, "_terminate_proc", new_callable=AsyncMock) as terminate:
            with pytest.raises(FileNotFoundError):
                await instance.start({"profileId": game_id})
            await instance.stop()
            assert all(call.args[1] is not previous_process for call in terminate.await_args_list)
    asyncio.run(run())


def test_exact_executable_matching_rejects_ambiguity(tmp_path: Path) -> None:
    game = str(tmp_path / "game.exe")
    unrelated = Mock(info={"pid": 1, "exe": str(tmp_path / "other" / "game.exe")})
    right = Mock(info={"pid": 2, "exe": game})
    with patch("server.vn_launch_manager.psutil.process_iter", return_value=[unrelated, right]):
        assert _find_game_pid(game) == 2
    with patch("server.vn_launch_manager.psutil.process_iter", return_value=[right, Mock(info={"pid": 3, "exe": game})]):
        with pytest.raises(RuntimeError, match="Multiple instances"):
            _find_game_pid(game)


def test_luna_saved_url_reaches_capture_preview_over_real_websocket(tmp_path: Path) -> None:
    async def run():
        async def stream(ws):
            await ws.send("一句台词\n两个选项")
            await ws.send("一句台词\n两个选项")
            await ws.wait_closed()

        async with websockets.serve(stream, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            instance = manager(tmp_path)
            game_id = instance.save_profile({"profile": {"name": "Luna game", "textSource": "luna",
                                            "lunaWsUrl": f"ws://127.0.0.1:{port}/api/ws/text/origin"}})["profileId"]
            instance = manager(tmp_path)
            try:
                await instance.start({"profileId": game_id, "captureOnly": True})
                async with asyncio.timeout(3):
                    while (await instance.status())["bridge"].get("lineCount", 0) < 2:
                        await asyncio.sleep(.01)
                assert [line["text"] for line in (await instance.status())["capturedLines"]] == ["一句台词\n两个选项"] * 2
            finally:
                await instance.stop()
            instance._runtime_start.assert_not_awaited()
    asyncio.run(run())


def test_existing_profile_can_test_without_runtime_and_play_with_original_preset(tmp_path: Path) -> None:
    async def run():
        instance = manager(tmp_path)
        with patch("server.vn_launch_manager.AgentVNTextSource", Source):
            params = {"launchGame": False, "attachHook": False, "launchOverlay": False}
            await instance.start({**params, "captureOnly": True})
            instance._runtime_start.assert_not_awaited()
            await instance.stop()
            instance._runtime_stop.assert_not_awaited()
            await instance.start(params)
            assert instance._runtime_start.await_args.args[0]["prompt_pack"] == "mystery"
            await instance.stop()
            instance._runtime_stop.assert_awaited_once()
    asyncio.run(run())


def test_save_profile_api_routes_to_store(tmp_path: Path) -> None:
    handler = VNLaunchHandler()
    handler.configure(tmp_path, runtime_start=AsyncMock(), runtime_stop=AsyncMock(),
                      runtime_status=AsyncMock(), runtime_line=AsyncMock())
    result = asyncio.run(handler.handle(Method.VN_LAUNCH_PROFILE_SAVE, game_settings(tmp_path)))
    assert result["profileId"]
    assert VNProfileStore(tmp_path).load().profiles[0].name == "New game"


def test_start_passes_saved_type_and_capabilities_to_runtime(tmp_path: Path) -> None:
    async def run():
        instance = manager(tmp_path)
        request = game_settings(tmp_path)
        request["profile"].update(promptPack="base", capabilities={"immediate": False, "summary": True}, voiceInput=False)
        game_id = instance.save_profile(request)["profileId"]
        with patch("server.vn_launch_manager._find_game_pid", return_value=42), \
             patch("server.vn_launch_manager.AgentVNTextSource", Source):
            await instance.start({"profileId": game_id})
            params = instance._runtime_start.await_args.args[0]
            assert params["prompt_pack"] == "base"
            assert params["capabilities"]["immediate"] is False
            assert params["capabilities"]["summary"] is True
            assert params["script_path"] == ""
            assert params["game_id"] == game_id
            await instance.stop()
    asyncio.run(run())


def test_pre_semantics_saved_builtin_profile_keeps_mystery_defaults(tmp_path: Path) -> None:
    instance = manager(tmp_path)
    request = game_settings(tmp_path)
    request["profile"]["id"] = "paranormasight"
    instance.save_profile(request)
    loaded = manager(tmp_path)._profile_by_id("paranormasight")
    assert loaded["promptPack"] == "mystery"
    assert loaded["voiceInput"] is True
    assert loaded["capabilities"]["reasoning"] is True
