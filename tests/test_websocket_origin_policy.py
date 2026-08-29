from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.app import (
    _handle_websocket_connection,
    _http_request_authenticated,
    _http_request_origin_allowed,
    _websocket_origin_allowed,
)
from server.local_auth import LocalAuthPolicy


_TOKEN = "desktop-token-abcdefghijklmnopqrstuvwxyz-0123456789"
_NONCE = "instance-nonce-abcdefghijklmnop"


def _required_auth() -> LocalAuthPolicy:
    return LocalAuthPolicy.from_environment(
        {
            "AMADEUS_BACKEND_AUTH_MODE": "required",
            "AMADEUS_BACKEND_TOKEN": _TOKEN,
            "AMADEUS_BACKEND_INSTANCE_NONCE": _NONCE,
        }
    )


class _FakeWebSocket:
    def __init__(
        self,
        origin: str | None,
        user_agent: str = "",
        protocols: str = "",
    ) -> None:
        self.headers = {} if origin is None else {"origin": origin}
        if user_agent:
            self.headers["user-agent"] = user_agent
        if protocols:
            self.headers["sec-websocket-protocol"] = protocols
        self.accepted = False
        self.accepted_subprotocol = ""
        self.closed: tuple[int, str] | None = None

    async def accept(self, *, subprotocol: str | None = None) -> None:
        self.accepted = True
        self.accepted_subprotocol = str(subprotocol or "")

    async def close(self, *, code: int, reason: str) -> None:
        self.closed = (code, reason)


class _FakeManager:
    def __init__(self) -> None:
        self.calls = 0

    async def handle_connection(
        self,
        ws: _FakeWebSocket,
        *,
        subprotocol: str | None = None,
    ) -> None:
        self.calls += 1
        await ws.accept(subprotocol=subprotocol)


def test_websocket_origin_policy_is_exact() -> None:
    allowed = (
        None,
        "",
        "file://",
        "http://localhost:5173",
        "http://127.0.0.1:17777",
    )
    for origin in allowed:
        assert _websocket_origin_allowed(origin, backend_port=17777), origin

    assert _websocket_origin_allowed(
        "null",
        backend_port=17777,
        user_agent="Mozilla/5.0 Electron/36.4.0 Chrome/136.0.0.0",
    )

    blocked = (
        "https://evil.example",
        "http://evil.example",
        "null",
        "http://localhost.evil.example:5173",
        "http://127.0.0.1:5173",
        "http://localhost:17777",
        "http://127.0.0.1:17778",
        "https://localhost:5173",
        "file://C:/untrusted/page.html",
    )
    for origin in blocked:
        assert not _websocket_origin_allowed(origin, backend_port=17777), origin
    assert not _websocket_origin_allowed(
        "null",
        backend_port=17777,
        user_agent="Mozilla/5.0 Chrome/136.0.0.0",
    )

    assert _websocket_origin_allowed(
        "http://127.0.0.1:19001",
        backend_port=19001,
    )
    assert not _websocket_origin_allowed(
        "http://127.0.0.1:17777",
        backend_port=19001,
    )


def test_untrusted_origin_is_closed_before_accept() -> None:
    async def run() -> None:
        ws = _FakeWebSocket("https://evil.example")
        manager = _FakeManager()

        handled = await _handle_websocket_connection(
            ws,
            manager,
            backend_port=17777,
        )

        assert handled is False
        assert manager.calls == 0
        assert ws.accepted is False
        assert ws.closed == (1008, "untrusted websocket origin")

    asyncio.run(run())


def test_http_mutation_origin_policy_rejects_cross_site_browser() -> None:
    assert _http_request_origin_allowed({}, backend_port=17777)
    assert _http_request_origin_allowed(
        {"origin": "http://localhost:5173"},
        backend_port=17777,
    )
    assert not _http_request_origin_allowed(
        {"origin": "https://evil.example"},
        backend_port=17777,
    )
    assert not _http_request_origin_allowed(
        {"sec-fetch-site": "cross-site"},
        backend_port=17777,
    )
    assert _http_request_origin_allowed(
        {
            "origin": "null",
            "user-agent": "Mozilla/5.0 Electron/36.4.0 Chrome/136.0.0.0",
        },
        backend_port=17777,
    )


def test_http_mutation_authentication_is_separate_from_origin_policy() -> None:
    policy = _required_auth()

    assert _http_request_authenticated({"X-Amadeus-Token": _TOKEN}, policy)
    assert not _http_request_authenticated({}, policy)
    assert not _http_request_authenticated(
        {"X-Amadeus-Token": "wrong-token"},
        policy,
    )
    assert _http_request_authenticated({}, LocalAuthPolicy.disabled())


def test_owned_and_native_origins_reach_the_manager() -> None:
    async def run() -> None:
        for origin in (
            None,
            "file://",
            "http://localhost:5173",
            "http://127.0.0.1:17777",
        ):
            ws = _FakeWebSocket(origin)
            manager = _FakeManager()

            handled = await _handle_websocket_connection(
                ws,
                manager,
                backend_port=17777,
            )

            assert handled is True, origin
            assert manager.calls == 1, origin
            assert ws.accepted is True, origin
            assert ws.closed is None, origin

        packaged = _FakeWebSocket(
            "null",
            "Mozilla/5.0 Electron/36.4.0 Chrome/136.0.0.0",
        )
        packaged_manager = _FakeManager()
        assert await _handle_websocket_connection(
            packaged,
            packaged_manager,
            backend_port=17777,
        ) is True
        assert packaged_manager.calls == 1

    asyncio.run(run())


def test_owned_websocket_waits_for_runtime_readiness() -> None:
    async def run() -> None:
        ws = _FakeWebSocket("http://127.0.0.1:17777")
        manager = _FakeManager()

        handled = await _handle_websocket_connection(
            ws,
            manager,
            backend_port=17777,
            ready=lambda: False,
        )

        assert handled is False
        assert manager.calls == 0
        assert ws.accepted is False
        assert ws.closed == (1013, "backend starting")

    asyncio.run(run())


def test_desktop_websocket_requires_the_instance_credential_when_enabled() -> None:
    async def run() -> None:
        policy = _required_auth()
        allowed = _FakeWebSocket(
            "http://localhost:5173",
            protocols=f"amadeus.local.v1, amadeus.auth.{_TOKEN}",
        )
        allowed_manager = _FakeManager()
        assert await _handle_websocket_connection(
            allowed,
            allowed_manager,
            backend_port=17777,
            auth_policy=policy,
        ) is True
        assert allowed_manager.calls == 1
        assert allowed.accepted_subprotocol == "amadeus.local.v1"

        rejected = _FakeWebSocket(
            "http://localhost:5173",
            protocols="amadeus.local.v1, amadeus.auth.wrong-token",
        )
        rejected_manager = _FakeManager()
        assert await _handle_websocket_connection(
            rejected,
            rejected_manager,
            backend_port=17777,
            auth_policy=policy,
        ) is False
        assert rejected_manager.calls == 0
        assert rejected.closed == (1008, "authentication required")

    asyncio.run(run())
