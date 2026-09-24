from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
import psutil
import websockets

from server.handlers.vn_launch_handler import VNLaunchHandler
from server.protocol import Method
from server.vn_launch_manager import VNLaunchManager, _find_game_pid, _open_steam_game
from server.vn_profiles import VNProfileStore


def manager(root: Path) -> VNLaunchManager:
    instance = VNLaunchManager(root, runtime_start=AsyncMock(return_value={"status": "active"}),
                           runtime_stop=AsyncMock(), runtime_status=AsyncMock(return_value={"status": "stopped"}),
                           runtime_line=AsyncMock(), before_external_launch=AsyncMock())
    instance.vn_root = root / "external-vn-fixture"
    return instance


def install_builtin(instance):
    executable = instance.vn_root / "PARANORMASIGHT/PARANORMASIGHT.exe"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.touch()


def game_settings(root: Path, name: str = "New game") -> dict:
    root.mkdir(parents=True, exist_ok=True)
    for filename in ("game.exe", "agent.exe", "hook.js"):
        (root / filename).touch()
    return {"profile": {"name": name, "gameExe": str(root / "game.exe"), "hookHelper": str(root / "hook.js"), "launchOverlay": False},
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
    del request["profile"]["launchOverlay"]
    saved = first.save_profile(request)
    game_id = saved["profileId"]
    second = manager(tmp_path)
    loaded = second._profile_by_id(game_id)
    assert loaded["gameExe"] == request["profile"]["gameExe"]
    assert loaded["launchGame"] is True
    assert loaded["launchOverlay"] is True
    assert loaded["runtimeSupported"] is True
    assert loaded["promptPack"] == "base"
    assert loaded["capabilities"]["reasoning"] is False
    assert loaded["agentExists"] is True
    request["profile"].update(id=game_id, name="Renamed", launchGame=False, closeGameOnStop=True, launchOverlay=False)
    updated = second.save_profile(request)
    assert len(updated["profiles"]) == 1
    third = manager(tmp_path)
    assert third._profile_by_id(game_id)["name"] == "Renamed"
    assert third._profile_by_id(game_id)["launchGame"] is False
    assert third._profile_by_id(game_id)["launchOverlay"] is False
    assert all(profile["agentExe"] == request["agentExe"] for profile in third.profiles()["profiles"])
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
        install_builtin(instance)
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


def test_game_type_owns_capabilities_even_with_stale_start_overrides(tmp_path: Path) -> None:
    async def run():
        instance = manager(tmp_path)
        request = game_settings(tmp_path)
        request["profile"].update(promptPack="base", voiceInput=False, visionMode="on_question")
        game_id = instance.save_profile(request)["profileId"]
        with patch("server.vn_launch_manager._find_game_pid", return_value=42), \
             patch("server.vn_launch_manager.AgentVNTextSource", Source):
            await instance.start({"profileId": game_id, "runtime": {"prompt_pack": "mystery", "capabilities": {"immediate": False}}})
            params = instance._runtime_start.await_args.args[0]
            assert params["prompt_pack"] == "base"
            assert params["capabilities"]["immediate"] is True
            assert params["capabilities"]["summary"] is True
            assert params["capabilities"]["reasoning"] is False
            assert params["voice_input"] is False
            assert params["vision_mode"] == "on_question"
            assert params["script_path"] == ""
            assert params["game_id"] == game_id
            await instance.stop()
            request["profile"].update(id=game_id, promptPack="mystery")
            instance.save_profile(request)
            await instance.start({"profileId": game_id})
            assert all(instance._runtime_start.await_args.args[0]["capabilities"].values())
            await instance.stop()
    asyncio.run(run())


def test_pre_semantics_saved_builtin_profile_keeps_mystery_defaults(tmp_path: Path) -> None:
    instance = manager(tmp_path)
    install_builtin(instance)
    request = game_settings(tmp_path)
    request["profile"]["id"] = "paranormasight"
    instance.save_profile(request)
    loaded = manager(tmp_path)._profile_by_id("paranormasight")
    assert loaded["promptPack"] == "mystery"
    assert loaded["voiceInput"] is True
    assert loaded["capabilities"]["reasoning"] is True


def test_saved_legacy_switches_migrate_to_game_type(tmp_path: Path) -> None:
    instance = manager(tmp_path)
    request = game_settings(tmp_path)
    request["profile"]["promptPack"] = "mystery"
    game_id = instance.save_profile(request)["profileId"]
    path = VNProfileStore(tmp_path).path
    data = json.loads(path.read_text())
    data["profiles"][0]["capabilities"] = {"immediate": False, "lookahead": False, "reasoning": False}
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = manager(tmp_path)
    assert all(loaded._profile_by_id(game_id)["capabilities"].values())
    request["profile"]["id"] = game_id
    loaded.save_profile(request)
    assert "capabilities" not in json.loads(path.read_text())["profiles"][0]
    request["profile"]["capabilities"] = {"reasoning": False}
    with pytest.raises(ValueError):
        loaded.save_profile(request)


def test_steam_launch_binds_game_not_steam_and_closes_only_owned_game(tmp_path: Path) -> None:
    async def run():
        real_sleep = asyncio.sleep
        async def fast_sleep(_delay):
            await real_sleep(0)
        instance = manager(tmp_path)
        request = game_settings(tmp_path)
        request["profile"].update(launchMethod="steam", steamAppId="3345060", closeGameOnStop=True)
        game_id = instance.save_profile(request)["profileId"]
        game = Mock(spec=psutil.Process)
        game.pid = 555
        game.is_running.return_value = True
        with patch("server.vn_launch_manager._find_game_pid", return_value=None), \
             patch("server.vn_launch_manager._open_steam_game") as launch_steam, \
             patch.object(instance, "_wait_for_steam_game", new_callable=AsyncMock, return_value=game), \
             patch("server.vn_launch_manager.asyncio.sleep", side_effect=fast_sleep), \
             patch("server.vn_launch_manager._bring_process_window_to_front"), \
             patch("server.vn_launch_manager.AgentVNTextSource", Source), \
             patch.object(instance, "_spawn") as spawn:
            result = await instance.start({"profileId": game_id, "captureOnly": True})
            launch_steam.assert_called_once_with("3345060")
            spawn.assert_not_called()
            assert result["game"]["pid"] == 555
            assert result["game"]["owned"] is True
            assert instance._text_source.start.await_args.kwargs["target_pid"] == 555
            await instance.stop()
            game.terminate.assert_called_once()
    asyncio.run(run())


def test_steam_reuses_existing_game_and_timeout_never_injects(tmp_path: Path) -> None:
    async def run():
        instance = manager(tmp_path)
        request = game_settings(tmp_path)
        request["profile"].update(launchMethod="steam", steamAppId="3345060", closeGameOnStop=True)
        game_id = instance.save_profile(request)["profileId"]
        with patch("server.vn_launch_manager._find_game_pid", return_value=42), \
             patch("server.vn_launch_manager._open_steam_game") as launch_steam, \
             patch("server.vn_launch_manager.AgentVNTextSource", Source):
            result = await instance.start({"profileId": game_id})
            assert result["game"]["owned"] is False
            await instance.stop()
            launch_steam.assert_not_called()
        with patch("server.vn_launch_manager._find_game_pid", return_value=None), \
             patch("server.vn_launch_manager._open_steam_game"), \
             patch.object(instance, "_wait_for_steam_game", side_effect=TimeoutError("Steam timeout")), \
             patch("server.vn_launch_manager.AgentVNTextSource") as source:
            with pytest.raises(TimeoutError):
                await instance.start({"profileId": game_id})
            source.assert_not_called()
            assert (await instance.status())["status"] == "error"
            assert instance._game_proc is None
        with pytest.raises(TimeoutError, match="configured game"):
            await instance._wait_for_steam_game(request["profile"]["gameExe"], timeout=0)
    asyncio.run(run())


@pytest.mark.parametrize("app_id", ["", "0", "123/console", "1 -evil", "１２３"])
def test_steam_launch_rejects_non_app_ids_before_dispatch(app_id: str) -> None:
    with patch("server.vn_launch_manager.os.startfile", create=True) as dispatch:
        with pytest.raises(ValueError):
            _open_steam_game(app_id)
        dispatch.assert_not_called()


def test_steam_dispatch_and_profile_validation(tmp_path: Path) -> None:
    with patch("server.vn_launch_manager.os.name", "nt"), patch("server.vn_launch_manager.os.startfile", create=True) as dispatch:
        _open_steam_game("3345060")
        dispatch.assert_called_once_with("steam://rungameid/3345060")
        dispatch.side_effect = OSError("no registered handler")
        with pytest.raises(RuntimeError, match="Steam could not open"):
            _open_steam_game("3345060")
    instance = manager(tmp_path)
    request = game_settings(tmp_path)
    request["profile"]["launchMethod"] = "steam"
    with pytest.raises(ValueError, match="Steam app ID"):
        instance.save_profile(request)


def test_steam_wait_revalidates_process_identity(tmp_path: Path) -> None:
    async def run():
        instance = manager(tmp_path)
        executable = str(tmp_path / "game.exe")
        stale = Mock()
        stale.exe.return_value = str(tmp_path / "other.exe")
        right = Mock()
        right.exe.return_value = executable
        with patch("server.vn_launch_manager._find_game_pid", side_effect=[None, 12, 13]), \
             patch("server.vn_launch_manager.psutil.Process", side_effect=[stale, right]), \
             patch("server.vn_launch_manager.asyncio.sleep", new_callable=AsyncMock):
            assert await instance._wait_for_steam_game(executable) is right
            right.create_time.assert_called_once()
    asyncio.run(run())
