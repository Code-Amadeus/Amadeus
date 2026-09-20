"""Trusted host-control boundary for AUIP AppSessions."""

from __future__ import annotations

import inspect
import logging
import uuid
from typing import TYPE_CHECKING, Any

from collections.abc import Callable

from server.auip_app_launcher import build_app_launch_url
from server.auip_app_source import (
    ArtifactSource,
    discover_launchable_auip_app,
    validate_launchable_app,
)
from server.auip_contract import AuipProtocolError
from server.auip_control_decision import reconcile_active_auip_control
from server.auip_engagement import AuipEngagementCoordinator
from server.auip_runtime import AuipRuntime, runtime
from server.event_bus import bus
from server.protocol import Method
from server.ws_handler import RequestHandler

if TYPE_CHECKING:
    from server.auip_launch import AuipLaunchCoordinator


logger = logging.getLogger("server")


class AuipHandler(RequestHandler):
    methods = [
        Method.AUIP_ATTACH_PREPARE,
        Method.AUIP_ACTION_INVOKE,
        Method.AUIP_STANCE_SET,
        Method.AUIP_MODE_SET,
        Method.AUIP_STEP,
        Method.AUIP_LEAVE,
        Method.AUIP_SESSION_FOCUS,
        Method.AUIP_SESSION_GET,
        Method.AUIP_LAUNCH_RESULT,
        Method.AUIP_SURFACE_CLOSE_RESULT,
    ]

    def __init__(
        self,
        app_runtime: AuipRuntime | None = None,
        *,
        artifacts: ArtifactSource | None = None,
        current_session_id: Callable[[], str] | None = None,
        app_websocket_url: str = "ws://127.0.0.1:17777/auip/ws",
        engagement: AuipEngagementCoordinator | None = None,
        launch: "AuipLaunchCoordinator | None" = None,
        preview_handoff: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.runtime = app_runtime or runtime
        self.artifacts = artifacts
        self.current_session_id = current_session_id or (lambda: "")
        self.app_websocket_url = str(app_websocket_url or "").strip()
        self.engagement = engagement
        self.launch = launch
        self.preview_handoff = preview_handoff

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        data = params if isinstance(params, dict) else {}
        try:
            if method == Method.AUIP_ATTACH_PREPARE:
                if self.artifacts is None:
                    raise AuipProtocolError("app_source_unavailable")
                artifact_id = str(data.get("artifact_id") or data.get("artifactId") or "")
                launch_request_id = str(
                    data.get("request_id") or data.get("requestId") or ""
                )
                if launch_request_id and (
                    self.launch is None
                    or not self.launch.authorize_prepare(
                        session_id=self.current_session_id(),
                        request_id=launch_request_id,
                        artifact_id=artifact_id,
                    )
                ):
                    raise AuipProtocolError("launch_request_mismatch")
                source = validate_launchable_app(
                    self.artifacts,
                    artifact_id,
                )
                app = discover_launchable_auip_app(
                    self.artifacts,
                    str(source.get("work_item_id") or ""),
                )
                if app is None or str(app.get("artifact_id") or "") != str(
                    source.get("artifact_id") or ""
                ):
                    raise AuipProtocolError("artifact_not_auip_app")
                mode = str(
                    data.get("mode") or data.get("engagement_mode") or "observe"
                ).strip().lower()
                host_surface_id = (
                    launch_request_id or f"auip_surface_{uuid.uuid4().hex}"
                )
                ticket = self.runtime.issue_attach_ticket(
                    conversation_id=self.current_session_id(),
                    artifact_ref=str(source["artifact_ref"]),
                    engagement_mode=mode,
                    host_surface_id=host_surface_id,
                )
                launch_url = build_app_launch_url(
                    entry_path=str(source["entry_path"]),
                    websocket_url=self.app_websocket_url,
                    attach_ticket=str(ticket["attach_ticket"]),
                    expires_at=float(ticket["expires_at"]),
                )
                if self.preview_handoff is not None:
                    try:
                        handoff = self.preview_handoff(
                            {**source, "host_surface_id": host_surface_id}
                        )
                        if inspect.isawaitable(handoff):
                            await handoff
                    except Exception:
                        # Preview is presentation, not AUIP authority. A broken
                        # optional callback must neither mint nor invalidate an
                        # already verified attach ticket.
                        logger.exception(
                            "failed to begin Work Preview AUIP handoff artifact=%s",
                            source.get("artifact_ref"),
                        )
                return {
                    "ok": True,
                    **source,
                    **ticket,
                    "host_surface_id": host_surface_id,
                    "launch_url": launch_url,
                }
            if method == Method.AUIP_LAUNCH_RESULT:
                if self.launch is None:
                    raise AuipProtocolError("launch_coordinator_unavailable")
                return await self.launch.record_client_result(
                    session_id=self.current_session_id(),
                    request_id=str(data.get("request_id") or data.get("requestId") or ""),
                    status=str(data.get("status") or ""),
                    detail=str(data.get("detail") or ""),
                )
            if method == Method.AUIP_SURFACE_CLOSE_RESULT:
                result = self.runtime.record_surface_close_result(
                    app_session_id=_session_id(data),
                    host_surface_id=str(
                        data.get("host_surface_id")
                        or data.get("hostSurfaceId")
                        or ""
                    ),
                    status=str(data.get("status") or ""),
                    detail=str(data.get("detail") or ""),
                )
            elif method == Method.AUIP_ACTION_INVOKE:
                result = self.runtime.invoke_action(
                    app_session_id=_session_id(data),
                    actor=str(data.get("actor") or "kurisu"),
                    type=str(data.get("action_type") or data.get("actionType") or data.get("type") or ""),
                    payload=data.get("payload") if isinstance(data.get("payload"), dict) else {},
                    expected_revision=data.get("expected_revision", data.get("expectedRevision")),
                )
                await bus.emit(
                    Method.AUIP_ACTION_REQUESTED,
                    {
                        "app_session_id": result.get("app_session_id"),
                        "action": result.get("action"),
                    },
                )
            elif method == Method.AUIP_STANCE_SET:
                stance = str(data.get("stance") or "").strip().lower()
                if stance not in {"spectator", "participant"}:
                    raise AuipProtocolError("unsupported_stance", stance)
                mode = "observe" if stance == "spectator" else "collaborate"
                result = (
                    await self.engagement.set_mode(
                        app_session_id=self._resolved_session_id(data),
                        mode=mode,
                    )
                    if self.engagement is not None
                    else self.runtime.set_stance(
                        app_session_id=self._resolved_session_id(data),
                        stance=stance,
                    )
                )
            elif method == Method.AUIP_MODE_SET:
                session_id = self._resolved_session_id(data)
                mode = str(data.get("mode") or "")
                result = (
                    await self.engagement.set_mode(app_session_id=session_id, mode=mode)
                    if self.engagement is not None
                    else self.runtime.set_engagement_mode(
                        app_session_id=session_id,
                        mode=mode,
                    )
                )
            elif method == Method.AUIP_STEP:
                if self.engagement is None:
                    raise AuipProtocolError("participant_controller_unavailable")
                result = self.engagement.request_step(
                    app_session_id=self._resolved_session_id(data),
                    instruction=str(data.get("instruction") or ""),
                    current_role_response=str(
                        data.get("current_role_response")
                        or data.get("currentRoleResponse")
                        or ""
                    ),
                    expected_revision=data.get(
                        "expected_revision",
                        data.get("expectedRevision"),
                    ),
                )
            elif method == Method.AUIP_LEAVE:
                session_id = self._resolved_session_id(data)
                result = (
                    await self.engagement.leave(
                        app_session_id=session_id,
                        reason=str(data.get("reason") or "user_left"),
                    )
                    if self.engagement is not None
                    else self.runtime.host_leave(
                        app_session_id=session_id,
                        reason=str(data.get("reason") or "user_left"),
                    )
                )
                host_surface_id = str(result.get("host_surface_id") or "")
                if host_surface_id:
                    await bus.emit(
                        Method.AUIP_SURFACE_CLOSE_REQUESTED,
                        {
                            "app_session_id": session_id,
                            "host_surface_id": host_surface_id,
                        },
                    )
            elif method == Method.AUIP_SESSION_FOCUS:
                result = self.runtime.focus(
                    conversation_id=self.current_session_id(),
                    app_session_id=_session_id(data),
                )
            elif method == Method.AUIP_SESSION_GET:
                result = {"ok": True, **self.runtime.get(_session_id(data))}
            else:
                return None
        except AuipProtocolError as exc:
            return {"ok": False, "error": exc.code, "detail": exc.detail}

        revoke = result.get("controller_revoke_request")
        if isinstance(revoke, dict):
            await bus.emit(
                Method.AUIP_CONTROLLER_REVOKE_REQUESTED,
                {
                    "app_session_id": result.get("app_session_id"),
                    "revoke": revoke,
                },
            )
        if method != Method.AUIP_SESSION_GET:
            await bus.emit(Method.AUIP_UPDATED, _public_update(result))
        return result

    async def route_control(
        self,
        attrs: dict[str, Any],
        *,
        session_id: str,
        user_text: str,
        turn_id: str,
        prepare_work=None,
    ) -> dict[str, Any] | None:
        """Apply one canonical AUIP control against Host-owned focus.

        This is the shared production/Journey seam.  Natural-language
        interpretation stays in ``AuipControlDecisionResolver``; this method
        performs no wording match and accepts no model-supplied AppSession id.
        """

        projection = self.runtime.focused_projection(session_id)
        control = reconcile_active_auip_control(attrs, projection)
        action = str(control.get("action") or "").strip().lower()
        if action in {"launch", "prepare"}:
            if self.launch is None:
                raise AuipProtocolError("launch_coordinator_unavailable")
            if action == "prepare" and not callable(prepare_work):
                raise AuipProtocolError("preparation_callback_unavailable")
            deferred_launch = bool(
                action == "launch"
                and str(control.get("after") or "").strip().lower() == "work"
            )
            expected_session_id = str(
                control.get("_host_app_session_id") or ""
            ).strip()
            if deferred_launch and expected_session_id:
                if (
                    not isinstance(projection, dict)
                    or str(projection.get("status") or "") != "active"
                    or str(projection.get("app_session_id") or "")
                    != expected_session_id
                ):
                    # Validate the frozen replacement identity before the
                    # Launch Coordinator records any deferred reservation.
                    raise AuipProtocolError("app_session_changed")
            elif (
                deferred_launch
                and isinstance(projection, dict)
                and str(projection.get("status") or "") == "active"
            ):
                raise AuipProtocolError("app_session_binding_required")
            routed = await self.launch.route_control(
                control,
                session_id=session_id,
                turn_id=turn_id,
                prepare_work=prepare_work,
            )
            if (
                action == "launch"
                and str(control.get("after") or "").strip().lower() == "work"
                and isinstance(projection, dict)
                and str(projection.get("status") or "") == "active"
                and isinstance(routed, dict)
                and routed.get("ok") is True
            ):
                await self.handle(
                    Method.AUIP_LEAVE,
                    {
                        "app_session_id": expected_session_id,
                        "reason": "replace_after_work",
                    },
                )
            return routed
        if not isinstance(projection, dict):
            raise AuipProtocolError("no_active_app_session")
        projection_status = str(projection.get("status") or "")
        # A completed AppSession may still own a visible result surface.  It is
        # no longer eligible for participation, but it remains the focused
        # lifecycle target until that surface is closed.
        eligible_statuses = {"active", "completed"} if action == "leave" else {"active"}
        if projection_status not in eligible_statuses:
            raise AuipProtocolError("no_active_app_session")
        app_session_id = str(projection.get("app_session_id") or "")
        expected_session_id = str(control.get("_host_app_session_id") or "")
        if expected_session_id and expected_session_id != app_session_id:
            raise AuipProtocolError("app_session_changed")
        if action in {"observe", "collaborate", "delegate"}:
            return await self.handle(
                Method.AUIP_MODE_SET,
                {"app_session_id": app_session_id, "mode": action},
            )
        if action == "step":
            return await self.handle(
                Method.AUIP_STEP,
                {
                    "app_session_id": app_session_id,
                    "instruction": str(control.get("instruction") or user_text),
                    "current_role_response": str(
                        control.get("_host_current_role_response") or ""
                    ),
                },
            )
        if action == "leave":
            return await self.handle(
                Method.AUIP_LEAVE,
                {"app_session_id": app_session_id, "reason": "role_control"},
            )
        if action == "none":
            return {"ok": True, "action": "none"}
        raise AuipProtocolError("unsupported_control_action", action)

    def _resolved_session_id(self, data: dict[str, Any]) -> str:
        claimed = _session_id(data)
        if claimed:
            return claimed
        projection = self.runtime.focused_projection(self.current_session_id())
        if not isinstance(projection, dict):
            raise AuipProtocolError("no_active_app_session")
        return str(projection.get("app_session_id") or "")


def _session_id(data: dict[str, Any]) -> str:
    return str(data.get("app_session_id") or data.get("appSessionId") or "")


def _public_update(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in result.items()
        if key not in {"bridge_token", "manifest"}
    }
