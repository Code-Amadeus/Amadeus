"""Minimal asynchronous client for OpenClaw's documented Gateway RPC plane.

Amadeus executes tracked OpenClaw work through Gateway Sessions.  That plane
owns the native session and run identities required for continuation, steering
and confirmed cancellation.  Keeping the transport here prevents
provider/runtime code from guessing at native frames or shelling out to the
OpenClaw CLI.  The older OpenAI-compatible HTTP client remains a legacy
one-shot integration; it is not used to claim Session control.

The client deliberately implements only request/response correlation and an
event queue.  OpenClaw remains authoritative for protocol validation and
method semantics; callers must still capability-check the ``hello-ok`` result
before advertising a stronger Provider manifest.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import websockets


class OpenClawGatewayError(RuntimeError):
    """A typed Gateway transport or RPC failure."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code or "")
        self.details = dict(details or {})


def gateway_websocket_url(base_url: str) -> str:
    """Translate an HTTP Gateway base URL without changing its host/path."""

    raw = str(base_url or "").strip()
    if not raw:
        raise ValueError("OpenClaw Gateway base URL is required")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.netloc:
        raise ValueError(f"invalid OpenClaw Gateway URL: {raw!r}")
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
    path = parsed.path.rstrip("/")
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


class OpenClawGatewayClient:
    """One authenticated Gateway connection with correlated RPC requests."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        request_timeout: float = 15.0,
        event_queue_size: int = 2048,
        scopes: tuple[str, ...] = ("operator.read", "operator.write"),
    ) -> None:
        self.url = gateway_websocket_url(base_url)
        self.token = str(token or "")
        self.request_timeout = max(1.0, float(request_timeout))
        self.scopes = tuple(
            dict.fromkeys(str(scope).strip() for scope in scopes if str(scope).strip())
        )
        if not self.scopes:
            raise ValueError("at least one OpenClaw operator scope is required")
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=max(32, int(event_queue_size))
        )
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._send_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._socket: Any = None
        self._reader_task: asyncio.Task[None] | None = None
        self.hello: dict[str, Any] = {}

    async def __aenter__(self) -> "OpenClawGatewayClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()

    @property
    def connected(self) -> bool:
        return (
            self._socket is not None
            and self._reader_task is not None
            and not self._reader_task.done()
        )

    @property
    def advertised_methods(self) -> frozenset[str]:
        features = self.hello.get("features") if isinstance(self.hello, dict) else {}
        methods = features.get("methods") if isinstance(features, dict) else []
        return frozenset(str(item) for item in methods if str(item).strip())

    async def connect(self) -> dict[str, Any]:
        async with self._connect_lock:
            if self.connected:
                return dict(self.hello)
            if not self.token:
                raise OpenClawGatewayError("OpenClaw Gateway token is not configured")
            # A dead reader leaves its socket object behind.  Retire that
            # connection generation before opening another one so callers can
            # reconnect the same typed client without leaking two transports.
            if self._socket is not None or self._reader_task is not None:
                await self.close()

            try:
                socket = await asyncio.wait_for(
                    websockets.connect(
                        self.url,
                        max_size=32 * 1024 * 1024,
                        ping_interval=20,
                        # OpenClaw may briefly stop servicing Gateway frames
                        # while a native tool completes.  A 20 second pong
                        # deadline turned a successful 29 second web fetch into
                        # a false provider failure.  Application RPC deadlines
                        # remain bounded independently.
                        ping_timeout=60,
                    ),
                    timeout=self.request_timeout,
                )
                challenge = await asyncio.wait_for(socket.recv(), timeout=self.request_timeout)
                frame = self._decode_frame(challenge)
                if frame.get("type") != "event" or frame.get("event") != "connect.challenge":
                    await socket.close()
                    raise OpenClawGatewayError("Gateway did not send connect.challenge")

                request_id = uuid.uuid4().hex
                connect_frame = {
                    "type": "req",
                    "id": request_id,
                    "method": "connect",
                    "params": {
                        # 2026.3.x speaks v3; current releases speak v4.  The
                        # Gateway chooses the intersection and remains authority.
                        "minProtocol": 3,
                        "maxProtocol": 4,
                        "client": {
                            "id": "gateway-client",
                            "displayName": "Amadeus Provider Host",
                            "version": "0.1.0",
                            "platform": sys.platform,
                            "mode": "backend",
                        },
                        "role": "operator",
                        "scopes": list(self.scopes),
                        "caps": ["tool-events"],
                        "commands": [],
                        "permissions": {},
                        "auth": {"token": self.token},
                        "locale": "en-US",
                        "userAgent": "amadeus-provider-host/0.1.0",
                    },
                }
                await socket.send(json.dumps(connect_frame, ensure_ascii=False))
                while True:
                    raw = await asyncio.wait_for(socket.recv(), timeout=self.request_timeout)
                    response = self._decode_frame(raw)
                    if response.get("type") == "event":
                        self._put_event(response)
                        continue
                    if response.get("type") == "res" and response.get("id") == request_id:
                        if response.get("ok") is not True:
                            await socket.close()
                            raise self._response_error(response, method="connect")
                        payload = response.get("payload")
                        self.hello = dict(payload) if isinstance(payload, dict) else {}
                        break

                self._socket = socket
                self._reader_task = asyncio.create_task(
                    self._reader_loop(),
                    name="openclaw-gateway-reader",
                )
                return dict(self.hello)
            except OpenClawGatewayError:
                raise
            except Exception as exc:
                raise OpenClawGatewayError(
                    f"OpenClaw Gateway connection failed: {exc}",
                    code="CONNECTION_FAILED",
                ) from exc

    async def close(self) -> None:
        reader = self._reader_task
        self._reader_task = None
        socket = self._socket
        self._socket = None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass
        if socket is not None:
            try:
                await socket.close()
            except Exception:
                pass
        self._fail_pending(
            OpenClawGatewayError(
                "OpenClaw Gateway connection closed",
                code="CONNECTION_CLOSED",
            )
        )

    async def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        if not self.connected:
            await self.connect()
        socket = self._socket
        if socket is None:
            raise OpenClawGatewayError("OpenClaw Gateway is not connected")
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        frame = {
            "type": "req",
            "id": request_id,
            "method": str(method or "").strip(),
            "params": dict(params or {}),
        }
        try:
            async with self._send_lock:
                await socket.send(json.dumps(frame, ensure_ascii=False))
            wait_s = self.request_timeout if timeout is None else max(0.1, float(timeout))
            return await asyncio.wait_for(future, timeout=wait_s)
        except asyncio.TimeoutError as exc:
            raise OpenClawGatewayError(
                f"OpenClaw Gateway request timed out: {method}",
                code="TIMEOUT",
            ) from exc
        except OpenClawGatewayError:
            raise
        except Exception as exc:
            raise OpenClawGatewayError(
                f"OpenClaw Gateway request transport failed: {method}: {exc}",
                code="CONNECTION_LOST",
            ) from exc
        finally:
            self._pending.pop(request_id, None)

    async def next_event(self, *, timeout: float | None = None) -> dict[str, Any]:
        if timeout is None:
            return await self._event_queue.get()
        try:
            return await asyncio.wait_for(self._event_queue.get(), timeout=max(0.01, timeout))
        except asyncio.TimeoutError as exc:
            raise OpenClawGatewayError(
                "timed out waiting for an OpenClaw Gateway event",
                code="EVENT_TIMEOUT",
            ) from exc

    async def _reader_loop(self) -> None:
        socket = self._socket
        if socket is None:
            return
        failure: Exception | None = None
        try:
            async for raw in socket:
                frame = self._decode_frame(raw)
                if frame.get("type") == "event":
                    self._put_event(frame)
                    continue
                if frame.get("type") != "res":
                    continue
                request_id = str(frame.get("id") or "")
                future = self._pending.get(request_id)
                if future is None or future.done():
                    continue
                if frame.get("ok") is True:
                    future.set_result(frame.get("payload"))
                else:
                    future.set_exception(self._response_error(frame))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = OpenClawGatewayError(
                f"OpenClaw Gateway reader failed: {exc}",
                code="CONNECTION_LOST",
            )
        finally:
            if self._reader_task is asyncio.current_task():
                self._reader_task = None
            if failure is not None:
                self._fail_pending(failure)

    def _put_event(self, frame: dict[str, Any]) -> None:
        if self._event_queue.full():
            try:
                self._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._event_queue.put_nowait(frame)

    def _fail_pending(self, error: Exception) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    @staticmethod
    def _decode_frame(raw: Any) -> dict[str, Any]:
        try:
            decoded = json.loads(raw)
        except Exception as exc:
            raise OpenClawGatewayError("Gateway sent invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise OpenClawGatewayError("Gateway sent a non-object frame")
        return decoded

    @staticmethod
    def _response_error(frame: Mapping[str, Any], *, method: str = "") -> OpenClawGatewayError:
        error = frame.get("error") if isinstance(frame.get("error"), Mapping) else {}
        code = str(error.get("code") or "")
        message = str(error.get("message") or "Gateway request failed")
        details = error.get("details") if isinstance(error.get("details"), Mapping) else {}
        prefix = f"{method}: " if method else ""
        return OpenClawGatewayError(prefix + message, code=code, details=details)
