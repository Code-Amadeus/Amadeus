"""WebSocket handler for VN Player launch/profile control."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from server.protocol import Method
from server.local_auth import LocalAuthPolicy
from server.vn_launch_manager import VNLaunchManager
from server.vn_profiles import inspect_game
from server.ws_handler import RequestHandler


class VNLaunchHandler(RequestHandler):
    methods = [
        Method.VN_LAUNCH_PROFILES,
        Method.VN_LAUNCH_PROFILE_SAVE,
        Method.VN_LAUNCH_START,
        Method.VN_LAUNCH_STOP,
        Method.VN_LAUNCH_STATUS,
        Method.VN_LAUNCH_CAPTURE,
        Method.VN_LAUNCH_OVERLAY,
        Method.VN_LAUNCH_INSPECT,
        Method.VN_OCR_WINDOWS,
        Method.VN_OCR_PREVIEW,
    ]

    def __init__(self) -> None:
        self._manager: VNLaunchManager | None = None

    def configure(
        self,
        project_root: Path,
        *,
        runtime_start,
        runtime_stop,
        runtime_status,
        runtime_line,
        before_external_launch=None,
        runtime_overlay=None,
        backend_url: str = "",
        auth_policy: LocalAuthPolicy | None = None,
    ) -> None:
        self._manager = VNLaunchManager(
            project_root,
            runtime_start=runtime_start,
            runtime_stop=runtime_stop,
            runtime_status=runtime_status,
            runtime_line=runtime_line,
            before_external_launch=before_external_launch,
            runtime_overlay=runtime_overlay,
            backend_url=backend_url,
            auth_policy=auth_policy,
        )

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        manager = self._manager
        if manager is None:
            raise RuntimeError("VN launch handler is not configured")
        if method == Method.VN_LAUNCH_PROFILES:
            return manager.profiles()
        if method == Method.VN_LAUNCH_INSPECT:
            return inspect_game(str(params.get("gameExe") or ""))
        if method == Method.VN_LAUNCH_PROFILE_SAVE:
            return manager.save_profile(params)
        if method == Method.VN_LAUNCH_START:
            return await manager.start(params)
        if method == Method.VN_LAUNCH_STOP:
            return await manager.stop(params)
        if method == Method.VN_LAUNCH_STATUS:
            return await manager.status()
        if method == Method.VN_LAUNCH_CAPTURE:
            return await manager.capture()
        if method == Method.VN_LAUNCH_OVERLAY:
            return await manager.set_overlay(params)
        if method == Method.VN_OCR_WINDOWS:
            from server.vn_text_sources import list_macos_windows
            return {"windows": await list_macos_windows()}
        if method == Method.VN_OCR_PREVIEW:
            from server.vn_text_sources import capture_macos_window_preview
            window_id = int(params.get("windowId") or params.get("window_id") or 0)
            window_name = str(params.get("windowName") or params.get("window_name") or "")
            app_name = str(params.get("appName") or params.get("app_name") or "")
            pid = int(params.get("pid") or 0)
            return await capture_macos_window_preview(
                window_id=window_id or None,
                window_name=window_name or "",
                app_name=app_name or "",
                pid=pid or None,
            )
        return None
