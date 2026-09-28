"""User-visible VN contracts, independent of a particular text source or game."""
import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import websockets

from server.handlers.vn_launch_handler import VNLaunchHandler
from server.handlers.vn_player_handler import VNPlayerHandler
from server.protocol import Method
from server.vn_profiles import inspect_game
from server.ws_handler import ConnectionManager
from vn_player.runtime import VNPlayerRuntime
from vn_player.schemas import default_response
from test_vn_profiles import manager, game_settings, Source
from test_ws_control_preemption import Socket


async def runtime_fixture(root):
    runtime = VNPlayerRuntime(root, speak_callback=AsyncMock())
    runtime._llm_enabled = runtime._immediate_llm_enabled = True
    runtime._silence_pressure_enabled = False
    await runtime.start({"session_id": "product", "prompt_pack": "base", "script_path": "",
                         "summary_llm_enabled": False, "retrospective_llm_enabled": False})
    runtime.llm.configured = lambda: True
    response = {**default_response("speak"), "importance": 0.9, "confidence": 0.9,
                "speak": {"text": "陪伴的回答", "priority": "normal"}}
    runtime._immediate_response = AsyncMock(return_value=response)
    return runtime


def test_pause_preserves_history_and_spoken_answers(tmp_path):
    async def run():
        runtime = await runtime_fixture(tmp_path)
        await runtime.set_preferences({"session_id": "product", "commentary_paused": True})
        for index in range(12):
            await runtime.ingest_line({"text": f"今天开店的第 {index} 句话。"})
        runtime._immediate_response.assert_not_awaited()
        runtime.speak_callback.assert_not_awaited()
        assert len(runtime.store.short_memory()) == 12
        assert any(item["method"] == "vn.summary" for item in runtime.activity())
        await runtime.player_intervention("ask", {"text": "发生了什么？"})
        assert [item["method"] for item in runtime.activity()][-2:] == ["vn.player.event", "vn.reaction"]
        assert runtime.activity()[-1]["payload"]["reaction"]["speak"]["text"] == "陪伴的回答"
        runtime.speak_callback.assert_awaited_once()
        snapshot = runtime.activity()
        for _ in range(250):
            await runtime._emit("vn.status", runtime.status())
        assert runtime.activity() == snapshot, "diagnostics must not evict story history"
        snapshot[-1]["payload"]["reaction"]["speak"]["text"] = "client mutation"
        assert runtime.activity()[-1]["payload"]["reaction"]["speak"]["text"] == "陪伴的回答"
        handler = VNPlayerHandler()
        handler._runtime = runtime
        restored = await handler.handle(Method.VN_STATUS, {"include_history": True})
        assert restored["activity"] == runtime.activity()
        await runtime.player_intervention("ask", {"text": "再说一句。"})
        assert runtime.speak_callback.await_count == 2
        with pytest.raises(ValueError):
            await runtime.set_preferences({"session_id": "old", "commentary_paused": False})
        await runtime.stop()
        assert (await runtime.ingest_line({"text": "late"}))["status"] == "ignored"
        await runtime.start({"session_id": "next", "prompt_pack": "base"})
        assert runtime.activity() == []
        assert runtime.status()["preferences"]["commentary_paused"] is False
    asyncio.run(run())


def test_pause_revokes_a_comment_already_being_generated(tmp_path):
    async def run():
        runtime = await runtime_fixture(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        response = runtime._immediate_response.return_value
        async def pending(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return response
        runtime._immediate_response = pending
        task = asyncio.create_task(runtime.ingest_line({"text": "真实游戏文本"}))
        await entered.wait()
        await runtime.set_preferences({"session_id": "product", "commentary_paused": True})
        release.set()
        assert (await task)["reaction"]["decision"] == "silence"
        assert [item["method"] for item in runtime.activity()] == ["vn.line"]
        runtime.speak_callback.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("frequency,expected", [("quiet", 2), ("balanced", 8), ("frequent", 12)])
def test_frequency_changes_automatic_budget_but_questions_still_answer(tmp_path, frequency, expected):
    async def run():
        runtime = await runtime_fixture(tmp_path)
        await runtime.set_preferences({"session_id": "product", "commentary_frequency": frequency})
        assert sum(runtime._rate_allows_speak() for _ in range(20)) == expected
        assert (await runtime.player_intervention("ask", {"text": "请回答。"}))["reaction"]["decision"] == "speak"
    asyncio.run(run())


@pytest.mark.parametrize("source", ["agent", "luna", "replay"])
def test_text_only_sources_share_runtime_controls_and_preserve_real_repetitions(tmp_path, source):
    async def run():
        runtime = await runtime_fixture(tmp_path / "runtime")
        runtime_start = runtime.start
        async def start(params):
            result = await runtime_start({**params, "summary_llm_enabled": False, "retrospective_llm_enabled": False})
            runtime.llm.configured = lambda: True
            return result
        instance = manager(tmp_path / "profiles")
        instance._runtime_start, instance._runtime_stop = start, runtime.stop
        instance._runtime_status, instance._runtime_line = AsyncMock(side_effect=runtime.status), runtime.ingest_line
        request = game_settings(tmp_path / "profiles")
        request["profile"].update(textSource=source if source != "replay" else "agent", launchGame=False,
                                   commentaryFrequency="quiet")
        lines = ["一句真正重复的台词。", "一句真正重复的台词。", "留下\n离开"]
        async def stream(ws):
            for line in lines:
                await ws.send(json.dumps({"type": "copyText", "sentence": line}) if source == "agent" else line)
            await ws.wait_closed()
        if source == "replay":
            for line in lines:
                await runtime.ingest_line({"text": line})
        else:
            async with websockets.serve(stream, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                if source == "luna":
                    request["profile"].update(gameExe="", hookHelper="", lunaWsUrl=f"ws://127.0.0.1:{port}/api/ws/text/origin")
                    request["agentExe"] = ""
                game_id = instance.save_profile(request)["profileId"]
                with patch("server.vn_launch_manager._find_game_pid", return_value=os.getpid()), \
                     patch("server.vn_text_sources.AgentVNTextSource._launch_agent", new_callable=AsyncMock):
                    try:
                        await instance.start({"profileId": game_id, "agentWsPort": port, "bridgeMode": "websocket"})
                        async with asyncio.timeout(3):
                            while len(runtime.store.short_memory()) < len(lines):
                                await asyncio.sleep(.01)
                        assert runtime.status()["preferences"]["commentary_frequency"] == "quiet"
                    finally:
                        await instance.stop()
        expected = [lines[0], lines[1], "留下 离开"]  # Existing VN display whitespace normalization.
        assert [line["text"] for line in runtime.store.short_memory()] == expected
        assert all(not line["script_id"] and not line["speaker"] for line in runtime.store.short_memory())
        assert [item["payload"]["line"]["text"] for item in runtime.activity() if item["method"] == "vn.line"] == expected
    asyncio.run(run())


def test_same_socket_stop_cancels_steam_wait_and_cleans_owned_runtime(tmp_path):
    async def run():
        instance = manager(tmp_path)
        request = game_settings(tmp_path)
        request["profile"].update(launchMethod="steam", steamAppId="3345060")
        game_id = instance.save_profile(request)["profileId"]
        entered = asyncio.Event()
        async def wait_for_game(*_args):
            entered.set()
            await asyncio.Future()
        instance._wait_for_steam_game = wait_for_game
        handler = VNLaunchHandler()
        handler._manager = instance
        connection, socket = ConnectionManager(), Socket()
        connection.register_handler(handler)
        with patch("server.vn_launch_manager._find_game_pid", return_value=None), \
             patch("server.vn_launch_manager._open_steam_game"), \
             patch("server.vn_launch_manager.AgentVNTextSource", Source):
            reader = asyncio.create_task(connection._read_loop(socket, "vn", socket.send_json))
            socket.put("start", Method.VN_LAUNCH_START, {"profileId": game_id})
            await asyncio.wait_for(entered.wait(), 2)
            socket.put("stop", Method.VN_LAUNCH_STOP)
            assert (await socket.response("stop"))["status"] == "idle"
            assert (await socket.response("start"))["status"] == "idle"
            instance._runtime_stop.assert_awaited_once()
            assert instance._text_source is None and instance._start_task is None
            await socket.finish(reader)
    asyncio.run(run())


def test_luna_can_use_shared_game_launcher_without_agent(tmp_path):
    async def run():
        instance = manager(tmp_path)
        settings = game_settings(tmp_path)
        settings["profile"].update(textSource="luna", lunaWsUrl="ws://127.0.0.1:1/api/ws/text/origin",
                                    hookHelper="", launchMethod="steam", steamAppId="123", launchGame=True)
        settings["agentExe"] = ""
        game_id = instance.save_profile(settings)["profileId"]
        async def launch(_profile):
            instance._game_proc = SimpleNamespace(pid=123, poll=lambda: None)
        with patch("server.vn_launch_manager._find_game_pid", return_value=None), \
             patch.object(instance, "_launch_game", side_effect=launch) as launcher, \
             patch("server.vn_launch_manager.LunaVNTextSource", Source), \
             patch("server.vn_launch_manager.AgentVNTextSource", side_effect=AssertionError("Agent must not be required")):
            await instance.start({"profileId": game_id, "captureOnly": True})
            launcher.assert_awaited_once()
            assert instance._text_source.start.await_args.args[0]["launchMethod"] == "steam"
            await instance.stop()
    asyncio.run(run())


def test_steam_detection_uses_selected_install_directory_not_similar_names(tmp_path):
    steamapps = tmp_path / "steamapps"
    game = steamapps / "common/Demo/bin/game.exe"
    game.parent.mkdir(parents=True)
    game.touch()
    (steamapps / "appmanifest_123.acf").write_text('"appid" "123"\n"name" "Game Demo"\n"installdir" "Demo"')
    (steamapps / "appmanifest_456.acf").write_text('"appid" "456"\n"name" "Game"\n"installdir" "Full"')
    assert inspect_game(str(game)) == {"steamAppId": "123", "name": "Game Demo"}
    assert inspect_game("relative.exe") == {}
    (steamapps / "appmanifest_789.acf").write_text('"appid" "789"\n"installdir" "Demo"')
    assert inspect_game(str(game)) == {}, "ambiguous installations must stay a user choice"


def test_clean_install_has_no_developer_game_profile(tmp_path):
    assert manager(tmp_path).profiles()["profiles"] == []
