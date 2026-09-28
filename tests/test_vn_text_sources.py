"""The two external sources must feed the same existing VN line boundary."""

from __future__ import annotations

import asyncio
import ctypes
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
import psutil
import websockets

from server.vn_launch_manager import VNLaunchManager
from server.handlers.vn_player_handler import VNPlayerHandler
from server.protocol import Method
from server.vn_text_sources import AgentVNTextSource, LunaVNTextSource, _matching_agent_pids_sync
from vn_player.runtime import VNPlayerRuntime


def test_agent_default_reconnect_never_reads_clipboard_and_preserves_repetition(monkeypatch) -> None:
    clipboard = Mock(side_effect=AssertionError("VN must not open the system clipboard"))
    user32 = Mock(OpenClipboard=clipboard)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=user32, kernel32=Mock()), raising=False)

    async def run():
        received, states = [], []
        waiting, first, repeated, disconnect = (asyncio.Event() for _ in range(4))
        async def on_line(payload):
            received.append(payload)
            (first if len(received) == 1 else repeated).set()
            return {"status": "accepted"}
        async def on_status(_hook, bridge):
            states.append(bridge)
            if bridge["status"] == "waiting":
                waiting.set()
        source = AgentVNTextSource(on_line, on_status)
        # Reserve a port without listening to reproduce initial unavailability.
        reservation = socket.socket()
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
        try:
            await source.start({}, {"agentWsPort": port}, target_pid=None)
            await asyncio.wait_for(waiting.wait(), 3)
            assert received == []
            clipboard.assert_not_called()
            reservation.close()
            connections = 0
            async def stream(ws):
                nonlocal connections
                connections += 1
                await ws.send(json.dumps({"type": "copyText", "sentence": "同一句真正再次出现的台词。"}))
                if connections == 1:
                    await disconnect.wait()
                    await ws.close(code=1012, reason="test source restart")
                else:
                    await ws.wait_closed()
            async with websockets.serve(stream, "127.0.0.1", port):
                await asyncio.wait_for(first.wait(), 4)
                waiting.clear()
                disconnect.set()
                await asyncio.wait_for(waiting.wait(), 3)
                clipboard.assert_not_called()
                await asyncio.wait_for(repeated.wait(), 4)
                assert [line["text"] for line in received] == ["同一句真正再次出现的台词。"] * 2
                assert all(state["source"] == "agent_websocket" for state in states)
                assert source.status()[1]["lineCount"] == 2
                await source.stop()
        finally:
            reservation.close()
            await source.stop()
        clipboard.assert_not_called()
        assert source._ws_task is None
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["clipboard", "clip", "hybrid", "auto", "both", "unknown"])
def test_agent_rejects_unsupported_transport_instead_of_falling_back(mode):
    source = AgentVNTextSource(AsyncMock(), AsyncMock())
    with pytest.raises(ValueError, match="WebSocket only"):
        source._start_bridge({}, {"bridgeMode": mode})
    assert source._ws_task is None


def test_agent_preserves_identical_lines_even_when_message_id_repeats() -> None:
    received: list[dict] = []

    async def on_line(payload: dict) -> dict:
        received.append(payload)
        return {"status": "accepted"}

    async def run() -> None:
        source = AgentVNTextSource(on_line, AsyncMock())
        sentence = json.dumps({"text": "同一句", "speaker": "A", "script_id": "line_1"}, ensure_ascii=False)
        first = json.dumps({"type": "copyText", "id": "transport-1", "sentence": sentence}, ensure_ascii=False)
        await source._receive_agent_message(first)
        await source._receive_agent_message(first)  # Agent's id has no documented uniqueness guarantee.

    asyncio.run(run())
    assert len(received) == 2
    assert [item["text"] for item in received] == ["同一句", "同一句"]
    assert received[0]["speaker"] == "A"
    assert received[0]["script_id"] == "line_1"
    assert received[0]["metadata"]["source"] == "agent_websocket"


def test_agent_accepts_plain_text_without_optional_fields() -> None:
    on_line = AsyncMock(return_value={"status": "accepted"})

    async def run() -> None:
        source = AgentVNTextSource(on_line, AsyncMock())
        await source._receive_agent_message(json.dumps({"type": "copyText", "sentence": "普通台词"}, ensure_ascii=False))

    asyncio.run(run())
    payload = on_line.await_args.args[0]
    assert payload["text"] == "普通台词"
    assert payload["speaker"] == ""
    assert payload["script_id"] == ""


def test_agent_status_previews_the_line_accepted_by_vn_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VN_LLM_ENABLED", "0")
    monkeypatch.setenv("VN_IMMEDIATE_LLM_ENABLED", "0")
    script = tmp_path / "script.json"
    script.write_text(json.dumps({"lines": [{"script_id": "scene_001", "text": "桥边的可读台词", "order": 0}]}, ensure_ascii=False), encoding="utf-8")

    async def run() -> None:
        runtime = VNPlayerRuntime(tmp_path)
        await runtime.start({"session_id": "capture_test", "script_path": str(script), "lookahead_enabled": False})
        source = AgentVNTextSource(runtime.ingest_line, AsyncMock())
        sentence = json.dumps({"text": "����", "script_id": "scene_001"}, ensure_ascii=False)
        await source._receive_agent_message(json.dumps({"type": "copyText", "sentence": sentence}, ensure_ascii=False))
        _hook, bridge = source.status()
        assert bridge["lastTextPreview"] == "桥边的可读台词"
        assert bridge["lastScriptId"] == "scene_001"

    asyncio.run(run())


def test_agent_lines_cross_vn_line_handler_with_optional_id_and_repeated_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VN_LLM_ENABLED", "0")
    monkeypatch.setenv("VN_IMMEDIATE_LLM_ENABLED", "0")
    script = tmp_path / "script.json"
    script.write_text(json.dumps({"lines": [
        {"script_id": "scene_001", "text": "同一句剧情", "order": 0},
        {"script_id": "scene_002", "text": "同一句剧情", "order": 1},
    ]}, ensure_ascii=False), encoding="utf-8")

    async def run() -> None:
        handler = VNPlayerHandler()
        handler.configure(tmp_path)
        await handler.handle(Method.VN_START, {
            "session_id": "adapter_handler_validation", "script_path": str(script),
            "lookahead_enabled": False, "lookahead_llm_enabled": False,
        })
        results: list[dict] = []

        async def on_line(payload: dict) -> dict:
            result = await handler.handle(Method.VN_LINE, payload)
            assert result is not None
            results.append(result)
            return result

        source = AgentVNTextSource(on_line, AsyncMock())
        try:
            for item in [
                {"text": "【周围】"},
                {"text": "同一句剧情", "script_id": "scene_001"},
                {"text": "同一句剧情", "script_id": "scene_002"},
                {"text": "【周围】"},
            ]:
                await source._receive_agent_message(json.dumps({
                    "type": "copyText", "sentence": json.dumps(item, ensure_ascii=False),
                }, ensure_ascii=False))
            assert [result["status"] for result in results] == ["ok"] * 4
            assert [result["line"]["script_id"] for result in results] == ["", "scene_001", "scene_002", ""]
            assert source.status()[1]["lineCount"] == 4
        finally:
            await handler.handle(Method.VN_STOP, {"reason": "test"})

    asyncio.run(run())


def test_agent_adapter_launches_selected_script_and_stops_only_its_process(tmp_path: Path) -> None:
    executable = tmp_path / "agent.exe"
    script = tmp_path / "game.js"
    executable.touch()
    script.touch()
    process = Mock(pid=1234)
    process.poll.return_value = None

    async def run() -> None:
        source = AgentVNTextSource(AsyncMock(), AsyncMock())
        with patch("server.vn_text_sources._spawn", return_value=process) as spawn, \
             patch("server.vn_text_sources._terminate_proc", new_callable=AsyncMock) as terminate, \
             patch("server.vn_text_sources._matching_agent_pids", new_callable=AsyncMock, return_value=[]) as existing:
            await source.start(
                {"agentExe": str(executable), "hookHelper": str(script)},
                {"attachHook": True, "bridgeText": False}, target_pid=42,
            )
            args = spawn.call_args.args[0]
            assert args == [str(executable), "--pname=42", f"--script={script}"]
            existing.assert_awaited_once_with(executable, script)
            await source.stop()
            terminate.assert_awaited_once_with(process)

    asyncio.run(run())


def test_agent_adapter_does_not_replace_an_external_process(tmp_path: Path) -> None:
    executable = tmp_path / "agent.exe"
    script = tmp_path / "game.js"
    executable.touch()
    script.touch()

    async def run() -> None:
        source = AgentVNTextSource(AsyncMock(), AsyncMock())
        with patch("server.vn_text_sources._matching_agent_pids", new_callable=AsyncMock, return_value=[99]), \
             patch("server.vn_text_sources._spawn") as spawn:
            with pytest.raises(RuntimeError, match="already running"):
                await source.start({"agentExe": str(executable), "hookHelper": str(script)},
                                   {"attachHook": True, "bridgeText": False}, target_pid=42)
            spawn.assert_not_called()

    asyncio.run(run())


def test_matching_agent_process_requires_exact_executable_script_and_switch(tmp_path: Path) -> None:
    executable = tmp_path / "agent.exe"
    script = tmp_path / "script.js"

    def process(pid, exe, args):
        item = Mock(pid=pid)
        item.info = {"name": "agent.exe"}
        item.exe.return_value = str(exe)
        item.cmdline.return_value = args
        return item

    candidates = [
        process(1, executable, [str(executable), "--pname=42", f"--script={script}"]),
        process(2, tmp_path / "other" / "agent.exe", [str(executable), "--pname=42", f"--script={script}"]),
        process(3, executable, [str(executable), "--pname=42", "--script=other.js"]),
        process(4, executable, [str(executable), str(script)]),
    ]
    wrong_name = process(5, executable, [str(executable), "--pname=42", f"--script={script}"])
    wrong_name.info["name"] = "other.exe"
    candidates.append(wrong_name)
    with patch("server.vn_text_sources.psutil.process_iter", return_value=candidates):
        assert _matching_agent_pids_sync(executable, script) == [1]


def test_matching_agent_process_inspection_failure_is_not_treated_as_no_match(tmp_path: Path) -> None:
    candidate = Mock(pid=1)
    candidate.info = {"name": "agent.exe"}
    candidate.exe.side_effect = psutil.AccessDenied(pid=1)
    with patch("server.vn_text_sources.psutil.process_iter", return_value=[candidate]):
        with pytest.raises(psutil.AccessDenied):
            _matching_agent_pids_sync(tmp_path / "agent.exe", tmp_path / "script.js")


def test_luna_original_text_is_forwarded_as_plain_vn_lines_even_when_repeated() -> None:
    received: list[dict] = []
    two_lines = asyncio.Event()

    async def on_line(payload: dict) -> dict:
        received.append(payload)
        if len(received) == 2:
            two_lines.set()
        return {"status": "accepted"}

    async def run() -> None:
        async def send_original_text(ws) -> None:
            await ws.send("同一句")
            await ws.send("同一句")
            await ws.wait_closed()

        server = await websockets.serve(send_original_text, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        source = LunaVNTextSource(on_line, AsyncMock())
        try:
            await source.start({}, {"lunaWsUrl": f"ws://127.0.0.1:{port}/api/ws/text/origin"}, target_pid=None)
            await asyncio.wait_for(two_lines.wait(), timeout=2)
            await source.stop()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())
    assert [item["text"] for item in received] == ["同一句", "同一句"]
    assert all(item["metadata"] == {"source": "luna_original_text"} for item in received)
    assert all("script_id" not in item for item in received)


def test_launcher_selects_luna_without_agent_launch(tmp_path: Path) -> None:
    class FakeSource:
        def __init__(self, _on_line, _on_status):
            self.start = AsyncMock()
            self.stop = AsyncMock()

        def status(self):
            return ({"status": "external_running", "owned": False}, {"status": "running", "source": "luna_original_text", "lineCount": 0})

    async def run() -> None:
        manager = VNLaunchManager(
            tmp_path, runtime_start=AsyncMock(return_value={"status": "active"}),
            runtime_stop=AsyncMock(return_value={"status": "stopped"}),
            runtime_status=AsyncMock(return_value={"status": "active"}), runtime_line=AsyncMock(),
        )
        with patch("server.vn_launch_manager.LunaVNTextSource", FakeSource), \
             patch("server.vn_launch_manager.AgentVNTextSource") as agent_factory:
            profile_id = manager.save_profile({"profile": {"name": "Luna", "textSource": "luna",
                "lunaWsUrl": "ws://127.0.0.1:2333/api/ws/text/origin", "launchOverlay": False}})["profileId"]
            started = await manager.start({
                "profileId": profile_id,
                "launchOverlay": False,
            })
            assert started["textSource"] == "luna"
            assert started["hook"]["owned"] is False
            agent_factory.assert_not_called()
            await manager.stop()

    asyncio.run(run())


def test_luna_requires_a_websocket_url() -> None:
    async def run() -> None:
        source = LunaVNTextSource(AsyncMock(), AsyncMock())
        with pytest.raises(ValueError, match="WebSocket URL"):
            await source.start({}, {}, target_pid=None)

    asyncio.run(run())
