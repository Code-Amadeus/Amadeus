from __future__ import annotations

import asyncio
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openclaw.gateway_client import OpenClawGatewayClient, gateway_websocket_url


class _FakeSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str] = asyncio.Queue()
        self.incoming.put_nowait(
            json.dumps(
                {
                    "type": "event",
                    "event": "connect.challenge",
                    "payload": {"nonce": "test"},
                }
            )
        )
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        frame = json.loads(raw)
        self.sent.append(frame)
        if frame["method"] == "connect":
            payload = {
                "protocol": 3,
                "features": {"methods": ["sessions.abort"]},
            }
        else:
            payload = {"abortedRunId": frame["params"].get("runId")}
        await self.incoming.put(
            json.dumps(
                {
                    "type": "res",
                    "id": frame["id"],
                    "ok": True,
                    "payload": payload,
                }
            )
        )

    async def recv(self) -> str:
        return await self.incoming.get()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        return await self.recv()

    async def close(self) -> None:
        self.closed = True


def test_gateway_url_preserves_authority_and_path() -> None:
    assert gateway_websocket_url("http://127.0.0.1:18789") == "ws://127.0.0.1:18789"
    assert gateway_websocket_url("https://example.test/gateway/") == (
        "wss://example.test/gateway"
    )


def test_gateway_client_correlates_requests_after_handshake() -> None:
    async def scenario() -> None:
        socket = _FakeSocket()
        connect_kwargs: dict = {}

        async def connect(*_args, **kwargs):
            connect_kwargs.update(kwargs)
            return socket

        with patch("openclaw.gateway_client.websockets.connect", new=connect):
            client = OpenClawGatewayClient(
                base_url="http://127.0.0.1:18789",
                token="test-token",
            )
            hello = await client.connect()
            assert hello["protocol"] == 3
            assert client.advertised_methods == frozenset({"sessions.abort"})
            assert connect_kwargs["ping_interval"] == 20
            assert connect_kwargs["ping_timeout"] == 60
            result = await client.request(
                "sessions.abort",
                {"key": "probe", "runId": "native-1"},
            )
            assert result == {"abortedRunId": "native-1"}
            connect_frame = socket.sent[0]
            assert connect_frame["params"]["scopes"] == [
                "operator.read",
                "operator.write",
            ]
            await client.close()

    asyncio.run(scenario())


def test_gateway_client_retires_a_stale_socket_before_reconnect() -> None:
    async def scenario() -> None:
        first = _FakeSocket()
        second = _FakeSocket()
        sockets = [first, second]

        async def connect(*_args, **_kwargs):
            return sockets.pop(0)

        with patch("openclaw.gateway_client.websockets.connect", new=connect):
            client = OpenClawGatewayClient(
                base_url="http://127.0.0.1:18789",
                token="test-token",
            )
            await client.connect()
            reader = client._reader_task
            assert reader is not None
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass
            assert client.connected is False
            assert client._socket is first

            await client.connect()
            assert first.closed is True
            assert client.connected is True
            assert client._socket is second
            await client.close()

    asyncio.run(scenario())


def _main() -> None:
    test_gateway_url_preserves_authority_and_path()
    test_gateway_client_correlates_requests_after_handshake()
    test_gateway_client_retires_a_stale_socket_before_reconnect()
    print("ok: OpenClaw Gateway transport is scoped and correlated")


if __name__ == "__main__":
    _main()
