"""WebSocket connection manager – bridges frontend clients to backend handlers."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Awaitable, Callable

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from server.protocol import Envelope, Method
from server.event_bus import bus

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._request_handlers: dict[str, RequestHandler] = {}

    def register_handler(self, handler: "RequestHandler") -> None:
        for method in handler.methods:
            self._request_handlers[method] = handler
        logger.info("registered handler for methods: %s", handler.methods)

    async def handle_connection(
        self,
        ws: WebSocket,
        *,
        subprotocol: str | None = None,
    ) -> None:
        if subprotocol:
            await ws.accept(subprotocol=subprotocol)
        else:
            await ws.accept()
        conn_id = uuid.uuid4().hex[:8]
        self._connections[conn_id] = ws
        logger.info("ws client connected: %s", conn_id)
        send_lock = asyncio.Lock()

        async def send_json(payload: dict[str, Any]) -> None:
            # EventBus callbacks and request responses can become ready on the
            # same loop tick.  Starlette WebSocket writes are not re-entrant;
            # one connection therefore owns one serialized outbound stream.
            async with send_lock:
                await ws.send_json(payload)

        # forward internal events to this client
        async def forward(method: str, params: dict[str, Any]) -> None:
            if conn_id in self._connections:
                try:
                    if method == Method.CHAT_INTERRUPTED:
                        logger.info(
                            "forwarding chat.interrupted to %s turn=%s text_len=%s",
                            conn_id,
                            params.get("turn_id") or "",
                            len(str(params.get("text") or "")),
                    )
                    await self._send_event(send_json, method, params)
                except WebSocketDisconnect:
                    self._connections.pop(conn_id, None)
                    logger.info("ws client disconnected during event forward: %s", conn_id)
                except Exception as exc:
                    # Starlette may raise AssertionError rather than
                    # WebSocketDisconnect when a peer closes between the
                    # membership check and send_json.  Any send failure makes
                    # this connection unusable; remove it once so later events
                    # do not produce a storm of identical tracebacks while the
                    # read loop finishes its own cleanup.
                    self._connections.pop(conn_id, None)
                    logger.info(
                        "ws client disconnected during event forward: %s (%s)",
                        conn_id,
                        type(exc).__name__,
                    )

        # subscribe to all server→client events
        event_methods = [
            Method.SESSION_CHANGED,
            Method.CHAT_TOKEN, Method.CHAT_COMPLETE, Method.CHAT_ERROR, Method.CHAT_INTERRUPTED,
            Method.CHAT_WORK_NOTE, Method.CHAT_OBSERVER_DECISION,
            Method.TTS_STATUS, Method.TTS_SENTENCE_START, Method.TTS_SENTENCE_END, Method.TTS_TURN_COMPLETE,
            Method.ASR_RECOGNIZED, Method.ASR_STATUS,
            Method.WAKE_STATUS, Method.WAKE_DETECTED,
            Method.VTS_CONNECTED, Method.VTS_DISCONNECTED, Method.VTS_MODEL_LOADED,
            Method.VAD_ENERGY,
            Method.OPENCLAW_TASK_EVENT, Method.OPENCLAW_TASK_RESULT,
            Method.VN_STATUS, Method.VN_LINE, Method.VN_REACTION,
            Method.VN_CONTEXT_UPDATED, Method.VN_SUMMARY, Method.VN_ERROR,
            Method.VN_LAUNCH_STATUS,
            Method.PROVIDER_EVENT, Method.PROVIDER_RESULT,
            Method.AUIP_ACTION_REQUESTED, Method.AUIP_UPDATED,
            Method.AUIP_LAUNCH_REQUESTED, Method.AUIP_SURFACE_CLOSE_REQUESTED,
            Method.WORK_UPDATED,
            Method.WORK_PREVIEW_UPDATED, Method.WORK_PREVIEW_OPEN_REQUESTED,
            Method.ATTENTION_UPDATED,
            Method.SYSTEM_CONFIG, Method.SYSTEM_STATUS, Method.SYSTEM_ERROR,
            Method.RENDER_EMOTION, Method.RENDER_SPEAKING, Method.RENDER_MOUTH,
            Method.RENDER_SUBTITLE, Method.RENDER_SPRITE_FRAMES, Method.RENDER_MODE,
            Method.RENDER_IDLE_ANIMATION, Method.RENDER_IDLE_FRAME_INTERVAL,
            Method.RENDER_SPRITE_CLIP_CONFIG, Method.RENDER_MOUTH_CONFIG,
            Method.RENDER_SPRITEFORGE_GRAPH, Method.RENDER_SPRITEFORGE_INTENT,
            Method.RENDER_SPRITEFORGE_RELEASE,
            Method.RENDER_HOLD_FRAME, Method.RENDER_CLEAR_HOLD,
            Method.WALLPAPER_READY, Method.WALLPAPER_EXITED,
        ]
        for m in event_methods:
            bus.on(m, forward)

        try:
            await self._read_loop(ws, conn_id, send_json)
        except Exception:
            logger.exception("ws error %s", conn_id)
        finally:
            # cleanup
            for m in event_methods:
                bus.off(m, forward)
            self._connections.pop(conn_id, None)
            logger.info("ws client disconnected: %s", conn_id)

    async def _read_loop(
        self,
        ws: WebSocket,
        conn_id: str,
        send_json: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        async for raw in ws.iter_text():
            try:
                msg: Envelope = json.loads(raw)
            except json.JSONDecodeError:
                await self._send_error(send_json, "invalid json", req_id="?")
                continue

            req_id = msg.get("id", "?")
            method = msg.get("method", "")
            params = msg.get("params", {})

            handler = self._request_handlers.get(method)
            if handler is None:
                await self._send_error(send_json, f"unknown method: {method}", req_id)
                continue

            try:
                result = await handler.handle(method, params)
                if req_id != "?":
                    await self._send_response(send_json, req_id, result)
            except Exception as e:
                logger.exception("handler error for %s", method)
                await self._send_error(send_json, str(e), req_id)

    async def _send_event(
        self,
        send_json: Callable[[dict[str, Any]], Awaitable[None]],
        method: str,
        params: dict[str, Any],
    ) -> None:
        await send_json({"type": "evt", "id": uuid.uuid4().hex[:8], "method": method, "params": params})

    async def _send_response(
        self,
        send_json: Callable[[dict[str, Any]], Awaitable[None]],
        req_id: str,
        result: dict[str, Any] | None,
    ) -> None:
        await send_json({"type": "res", "id": req_id, "method": "", "params": result or {}})

    async def _send_error(
        self,
        send_json: Callable[[dict[str, Any]], Awaitable[None]],
        message: str,
        req_id: str,
    ) -> None:
        await send_json({"type": "res", "id": req_id, "method": "", "params": {"error": message}})


# ── handler interface ──────────────────────────────────────────────────────

class RequestHandler:
    """Base for handlers that process incoming client requests."""

    methods: list[str] = []

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        raise NotImplementedError


# singleton
manager = ConnectionManager()
