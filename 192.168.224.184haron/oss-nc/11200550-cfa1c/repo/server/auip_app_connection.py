"""Restricted WebSocket surface for one external cooperative AUIP app."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from server.auip_contract import MAX_MESSAGE_BYTES, AuipProtocolError
from server.auip_runtime import AuipRuntime, runtime
from server.auip_self_attach import AuipSelfAttachCoordinator
from server.event_bus import bus
from server.protocol import Method


logger = logging.getLogger(__name__)

APP_METHODS = frozenset(
    {
        Method.AUIP_ATTACH_REQUEST,
        Method.AUIP_REGISTER,
        Method.AUIP_STATE_PUBLISH,
        Method.AUIP_EVENT_PUBLISH,
        Method.AUIP_ACTION_RESULT,
        Method.AUIP_CONTROLLER_STATUS_PUBLISH,
        Method.AUIP_SESSION_CLOSE,
    }
)


class AuipAppRequestHandler:
    """Per-connection authority: one ticket becomes one AppSession."""

    def __init__(
        self,
        app_runtime: AuipRuntime | None = None,
        *,
        self_attach: AuipSelfAttachCoordinator | None = None,
    ) -> None:
        self.runtime = app_runtime or runtime
        self.self_attach = self_attach
        self.app_session_id = ""

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        data = params if isinstance(params, dict) else {}
        try:
            if method not in APP_METHODS:
                raise AuipProtocolError(
                    "unknown_method",
                    str(getattr(method, "value", method) or ""),
                )
            if method == Method.AUIP_ATTACH_REQUEST:
                if self.app_session_id:
                    raise AuipProtocolError("connection_already_registered")
                if self.self_attach is None:
                    raise AuipProtocolError("self_attach_unavailable")
                return await self.self_attach.request(
                    manifest=(
                        data.get("manifest")
                        if isinstance(data.get("manifest"), dict)
                        else {}
                    ),
                    instance_id=str(
                        data.get("instance_id") or data.get("instanceId") or ""
                    ),
                    entry_url=str(
                        data.get("entry_url") or data.get("entryUrl") or ""
                    ),
                )
            if method == Method.AUIP_REGISTER:
                if self.app_session_id:
                    raise AuipProtocolError("connection_already_registered")
                result = self.runtime.register_attached(
                    manifest=data.get("manifest")
                    if isinstance(data.get("manifest"), dict)
                    else {},
                    attach_ticket=str(
                        data.get("attach_ticket") or data.get("attachTicket") or ""
                    ),
                )
                self.app_session_id = str(result["app_session_id"])
            else:
                session_id = self._bound_session(data)
                if method == Method.AUIP_STATE_PUBLISH:
                    result = self.runtime.publish_state(
                        app_session_id=session_id,
                        bridge_token=_token(data),
                        revision=data.get("revision"),
                        state=data.get("state")
                        if isinstance(data.get("state"), dict)
                        else {},
                    )
                elif method == Method.AUIP_EVENT_PUBLISH:
                    result = self.runtime.publish_event(
                        app_session_id=session_id,
                        bridge_token=_token(data),
                        event_id=str(data.get("event_id") or data.get("eventId") or ""),
                        type=str(
                            data.get("event_type")
                            or data.get("eventType")
                            or data.get("type")
                            or ""
                        ),
                        actor=str(data.get("actor") or "app"),
                        revision=data.get("revision"),
                        payload=data.get("payload")
                        if isinstance(data.get("payload"), dict)
                        else {},
                        caused_by_action_id=str(
                            data.get("caused_by_action_id")
                            or data.get("causedByActionId")
                            or ""
                        ),
                    )
                elif method == Method.AUIP_ACTION_RESULT:
                    result = self.runtime.resolve_action(
                        app_session_id=session_id,
                        bridge_token=_token(data),
                        action_id=str(data.get("action_id") or data.get("actionId") or ""),
                        accepted=bool(data.get("accepted", False)),
                        resulting_revision=data.get(
                            "resulting_revision", data.get("resultingRevision")
                        ),
                        state=data.get("state")
                        if isinstance(data.get("state"), dict)
                        else None,
                        effects=data.get("effects")
                        if isinstance(data.get("effects"), dict)
                        else None,
                        reason=str(data.get("reason") or ""),
                    )
                elif method == Method.AUIP_CONTROLLER_STATUS_PUBLISH:
                    result = self.runtime.report_controller_status(
                        app_session_id=session_id,
                        bridge_token=_token(data),
                        lease_id=str(
                            data.get("lease_id") or data.get("leaseId") or ""
                        ),
                        generation=data.get("generation"),
                        status=str(data.get("status") or ""),
                        reason=str(data.get("reason") or ""),
                    )
                else:
                    result = self.runtime.close(
                        app_session_id=session_id,
                        bridge_token=_token(data),
                        reason=str(data.get("reason") or ""),
                    )
        except AuipProtocolError as exc:
            return {"ok": False, "error": exc.code, "detail": exc.detail}

        await bus.emit(Method.AUIP_UPDATED, _public_update(result))
        return result

    async def disconnect(self) -> None:
        if not self.app_session_id:
            return
        result = self.runtime.disconnect(self.app_session_id)
        await bus.emit(Method.AUIP_UPDATED, _public_update(result))

    def accepts_action_event(self, payload: dict[str, Any]) -> bool:
        return bool(
            self.app_session_id
            and str(payload.get("app_session_id") or "") == self.app_session_id
        )

    def _bound_session(self, data: dict[str, Any]) -> str:
        if not self.app_session_id:
            raise AuipProtocolError("connection_not_registered")
        claimed = str(data.get("app_session_id") or data.get("appSessionId") or "")
        if claimed and claimed != self.app_session_id:
            raise AuipProtocolError("connection_session_mismatch")
        return self.app_session_id


class AuipAppConnectionManager:
    """Serve the AUIP allowlist without subscribing apps to the main event bus."""

    def __init__(
        self,
        app_runtime: AuipRuntime | None = None,
        *,
        self_attach: AuipSelfAttachCoordinator | None = None,
    ) -> None:
        self.runtime = app_runtime or runtime
        self.self_attach = self_attach

    def configure_self_attach(
        self,
        coordinator: AuipSelfAttachCoordinator | None,
    ) -> None:
        self.self_attach = coordinator

    async def handle_connection(self, ws: WebSocket) -> None:
        await ws.accept()
        handler = AuipAppRequestHandler(
            self.runtime,
            self_attach=self.self_attach,
        )
        send_lock = asyncio.Lock()

        async def send(payload: dict[str, Any]) -> None:
            async with send_lock:
                await ws.send_json(payload)

        async def forward_app_event(method: str, payload: dict[str, Any]) -> None:
            if not handler.accepts_action_event(payload):
                return
            try:
                await send(
                    {
                        "type": "evt",
                        "id": uuid.uuid4().hex[:8],
                        "method": method,
                        "params": payload,
                    }
                )
            except Exception:
                logger.debug("AUIP app disconnected while forwarding an action", exc_info=True)

        bus.on(Method.AUIP_ACTION_REQUESTED, forward_app_event)
        bus.on(Method.AUIP_CONTROLLER_REVOKE_REQUESTED, forward_app_event)
        try:
            async for raw in ws.iter_text():
                req_id = "?"
                try:
                    if len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
                        raise AuipProtocolError("message_too_large")
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        raise AuipProtocolError("invalid_envelope")
                    req_id = str(message.get("id") or "?")
                    method = str(message.get("method") or "")
                    params = message.get("params")
                    if not isinstance(params, dict):
                        raise AuipProtocolError("invalid_object", "params")
                    result = await handler.handle(method, params)
                except (json.JSONDecodeError, AuipProtocolError) as exc:
                    code = exc.code if isinstance(exc, AuipProtocolError) else "invalid_json"
                    detail = exc.detail if isinstance(exc, AuipProtocolError) else ""
                    result = {"ok": False, "error": code, "detail": detail}
                await send({"type": "res", "id": req_id, "method": "", "params": result})
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("AUIP app websocket failed")
        finally:
            bus.off(Method.AUIP_ACTION_REQUESTED, forward_app_event)
            bus.off(Method.AUIP_CONTROLLER_REVOKE_REQUESTED, forward_app_event)
            try:
                await handler.disconnect()
            except Exception:
                logger.exception("failed to close disconnected AUIP AppSession")


def _token(data: dict[str, Any]) -> str:
    return str(data.get("bridge_token") or data.get("bridgeToken") or "")


def _public_update(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in result.items()
        if key not in {"bridge_token", "manifest", "attach_ticket", "entry_path"}
    }


manager = AuipAppConnectionManager()
