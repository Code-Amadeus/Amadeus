"""WebSocket handler for VN Player launch/profile control."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from server.protocol import Method
from server.vn_launch_manager import VNLaunchManager
from server.ws_handler import RequestHandler


class VNLaunchHandler(RequestHandler):
    methods = [
        Method.VN_LAUNCH_PROFILES,
        Method.VN_LAUNCH_START,
        Method.VN_LAUNCH_STOP,
        Method.VN_LAUNCH_STATUS,
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
    ) -> None:
        self._manager = VNLaunchManager(
            project_root,
            runtime_start=runtime_start,
            runtime_stop=runtime_stop,
            runtime_status=runtime_status,
            runtime_line=runtime_line,
            before_external_launch=before_external_launch,
        )

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        manager = self._manager
        if manager is None:
            raise RuntimeError("VN launch handler is not configured")
        if method == Method.VN_LAUNCH_PROFILES:
            return manager.profiles()
        if method == Method.VN_LAUNCH_START:
            return await manager.start(params)
        if method == Method.VN_LAUNCH_STOP:
            return await manager.stop(params)
        if method == Method.VN_LAUNCH_STATUS:
            return await manager.status()
        return None
