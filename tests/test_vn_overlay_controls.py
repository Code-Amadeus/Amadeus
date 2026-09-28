"""Native controls and the VN page share host state, identity and failure handling."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
import websockets

from render.vn_overlay_controls import VNOverlayControls
from server.event_bus import bus
from server.handlers.vn_player_handler import VNPlayerHandler
from server.local_auth import LocalAuthPolicy
from server.protocol import Method
from server.ws_handler import ConnectionManager
from vn_player.llm_client import VNLLMClient


async def wait_state(client, predicate):
    async with asyncio.timeout(5):
        while not predicate(state := client.snapshot()):
            await asyncio.sleep(.01)
        return state


class Socket:
    def __init__(self, connection):
        self.connection = connection

    async def accept(self):
        pass

    async def send_json(self, payload):
        await self.connection.send(json.dumps(payload))

    async def iter_text(self):
        async for message in self.connection:
            yield message


def test_native_and_page_controls_share_authenticated_session_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AMADEUS_BACKEND_AUTH_MODE", "required")
    monkeypatch.setenv("AMADEUS_BACKEND_TOKEN", "test-only-token-" + "a" * 32)
    monkeypatch.setenv("AMADEUS_BACKEND_INSTANCE_NONCE", "test-only-instance")
    monkeypatch.setattr(VNLLMClient, "configured", lambda _: True)
    monkeypatch.setattr(VNLLMClient, "supports_visual", lambda _: True)

    async def run():
        import os
        auth = LocalAuthPolicy.from_environment(os.environ)
        asr, connections = {}, []
        entered, release = asyncio.Event(), asyncio.Event()
        fail_mic = False

        async def asr_control(method, params):
            if method == Method.ASR_START:
                entered.set()
                await release.wait()
                if fail_mic:
                    raise RuntimeError("test microphone unavailable")
                asr.update(active=True, source="vn_player", source_payload=params["source_payload"])
                return {"status": "listening"}
            asr.clear()
            return {"status": "stopped"}

        handler = VNPlayerHandler()
        capture = AsyncMock()
        handler.configure(tmp_path, event_emit=bus.emit, asr_control=asr_control, asr_state=lambda: asr,
                          capture_game_view=capture)
        handler._runtime._llm_enabled = True
        await handler.handle(Method.VN_START, {"session_id": "first", "prompt_pack": "base", "script_path": ""})
        manager = ConnectionManager()
        manager.register_handler(handler)

        async def connected(ws):
            assert auth.authenticate(ws.request.headers) is not None
            connections.append(ws)
            await manager.handle_connection(Socket(ws))

        async with websockets.serve(connected, "127.0.0.1", 0) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/ws"
            client = VNOverlayControls(url)
            try:
                await wait_state(client, lambda s: s["connected"])
                assert client.set_inputs("first", voice=True)
                await entered.wait()
                starting = await wait_state(client, lambda s: s["inputs"]["voice"]["starting"])
                assert starting["pending"]
                assert not client.set_inputs("first", voice=False), "do not queue repeated clicks"
                release.set()
                await wait_state(client, lambda s: not s["pending"] and s["inputs"]["voice"]["listening"])
                assert asr["source_payload"]["session_id"] == "first"

                # Another UI uses the existing API; its changes arrive as VN status events.
                async with websockets.connect(url, additional_headers={"x-amadeus-token": auth.token}) as page:
                    await page.send(json.dumps({"type": "req", "id": "page", "method": "vn.input.set",
                                                "params": {"session_id": "first", "vision_mode": "on_question"}}))
                    async with asyncio.timeout(5):
                        async for raw in page:
                            message = json.loads(raw)
                            if message.get("type") == "res" and message.get("id") == "page":
                                assert "error" not in message["params"]
                                break
                await wait_state(client, lambda s: s["inputs"]["vision"]["enabled"])
                assert client.set_inputs("first", voice=False, vision_mode="off")
                await wait_state(client, lambda s: not s["pending"] and not s["inputs"]["voice"]["enabled"]
                                 and s["inputs"]["vision"]["mode"] == "off")
                assert not handler.status()["inputs"]["vision"]["enabled"]
                capture.assert_not_awaited()  # Enabling question-time vision does not capture a frame.

                fail_mic = True
                assert client.set_inputs("first", voice=True)
                failed = await wait_state(client, lambda s: not s["pending"] and bool(s["error"]))
                assert "test microphone unavailable" in failed["error"]
                assert not failed["inputs"]["voice"]["enabled"]

                await handler.handle(Method.VN_STOP, {})
                await wait_state(client, lambda s: not s["inputs"]["session_id"])
                assert not client.set_inputs("first", voice=True)
                await handler.handle(Method.VN_START, {"session_id": "second", "prompt_pack": "base", "script_path": ""})
                await wait_state(client, lambda s: s["inputs"].get("session_id") == "second")
                assert not client.set_inputs("first", voice=True)
                assert not handler.status()["inputs"]["voice"]["enabled"]
            finally:
                await asyncio.to_thread(client.close)
                await handler.handle(Method.VN_STOP, {})
            assert not client._thread.is_alive()
            assert len(connections) == 2
    asyncio.run(run())


def test_disconnect_does_not_replay_unacknowledged_click(monkeypatch):
    monkeypatch.setenv("AMADEUS_BACKEND_AUTH_MODE", "disabled")

    async def run():
        calls, connections = [], []
        recovered = asyncio.Event()
        inputs = {"session_id": "first", "voice": {"enabled": False, "available": True},
                  "vision": {"mode": "off", "available": True}}

        async def connected(ws):
            connections.append(ws)
            async for raw in ws:
                message = json.loads(raw)
                if message["method"] == "vn.input.set":
                    calls.append(message)
                    await ws.close(1012, "test disconnect before acknowledgement")
                    return
                await ws.send(json.dumps({"type": "res", "id": message["id"], "params": {"inputs": inputs}}))
                if len(connections) == 2:
                    recovered.set()

        async with websockets.serve(connected, "127.0.0.1", 0) as server:
            client = VNOverlayControls(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/ws")
            try:
                await wait_state(client, lambda s: s["connected"])
                assert client.set_inputs("first", voice=True)
                await wait_state(client, lambda s: not s["connected"])
                assert not client.set_inputs("first", voice=True)
                await asyncio.wait_for(recovered.wait(), 5)
                state = await wait_state(client, lambda s: s["connected"])
                assert not state["pending"] and not state["inputs"]["voice"]["enabled"]
                assert len(calls) == 1
                assert state["error"], "the interrupted change remains visible until the next user request"
            finally:
                await asyncio.to_thread(client.close)
    asyncio.run(run())


@pytest.mark.parametrize("url", ["ws://example.com/ws", "wss://127.0.0.1/ws", "ws://127.0.0.1/other"])
def test_controls_never_send_desktop_credentials_to_a_remote_endpoint(url):
    with pytest.raises(ValueError, match="local backend"):
        VNOverlayControls(url)
