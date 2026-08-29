"""Handler for render mode — manages PixiJS iframe lifecycle (file:// assets)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable
from pathlib import Path
from urllib.parse import urlencode

from server.protocol import Method
from server.ws_handler import RequestHandler

logger = logging.getLogger(__name__)


class RenderHandler(RequestHandler):
    methods = [Method.RENDER_START, Method.RENDER_STOP, Method.RENDER_READY]

    def __init__(self) -> None:
        self._project_root: Path | None = None
        self._render_bridge = None
        self._state_bridge = None
        self._ensure_runtime: Callable[[], Any] | None = None
        self._stop_runtime: Callable[[], None] | None = None
        self._backend_port = 17777

    def configure(
        self,
        project_root: Path,
        render_bridge=None,
        backend_port: int = 17777,
        ensure_runtime: Callable[[], Any] | None = None,
        stop_runtime: Callable[[], None] | None = None,
        state_bridge=None,
    ) -> None:
        self._project_root = project_root
        self._render_bridge = render_bridge
        self._backend_port = backend_port
        self._ensure_runtime = ensure_runtime
        self._stop_runtime = stop_runtime
        self._state_bridge = state_bridge

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if method == Method.RENDER_START:
            return await self._start(params)
        if method == Method.RENDER_STOP:
            return await self._stop(params)
        if method == Method.RENDER_READY:
            return await self._on_render_surface_ready(params)
        return None

    async def _start(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._ensure_runtime is not None:
            self._render_bridge = self._ensure_runtime()
        if self._project_root is None:
            return {"status": "error", "error": "render handler is not configured"}
        html_path = self._project_root / "render" / "web" / "index.html"
        query = urlencode({
            "ws": f"ws://127.0.0.1:{self._backend_port}/ws",
            "v": str(int(html_path.stat().st_mtime)),
        })
        url = f"{html_path.resolve().as_uri()}?{query}"

        # Schedule a delayed replay fallback in case the render surface loads
        # before its parent event channel sends render.ready.
        asyncio.create_task(self._replay_sprites())

        return {"url": url}

    async def _replay_sprites(self) -> None:
        """Delayed replay fallback — fires after 5s in case the iframe's
        render.ready message arrives before the WS connection is up."""
        await asyncio.sleep(5)
        await self._do_replay()

    async def _on_render_surface_ready(
        self,
        _params: dict[str, Any],
    ) -> dict[str, Any]:
        """Replay registered SpriteForge state for a ready render surface."""
        logger.info("[RenderHandler] render surface ready, replaying frames")
        asyncio.create_task(self._do_replay())
        return {"status": "ok"}

    async def _do_replay(self) -> None:
        """Re-emit all registered state for newly-connected iframe clients."""
        if self._render_bridge is None:
            return
        await self._replay_bridge(self._render_bridge)
        if self._state_bridge is not None and self._state_bridge is not self._render_bridge:
            await self._replay_bridge(self._state_bridge)

    async def _replay_bridge(self, bridge) -> None:
        replay = getattr(bridge, "replay_all", None)
        if callable(replay):
            import inspect
            if inspect.iscoroutinefunction(replay):
                await replay()
            else:
                replay()
        else:
            # Fallback: old kur1or3 pipeline
            if self._project_root is None:
                return
            images_dir = self._project_root / "render" / "assets" / "images"
            if images_dir.is_dir():
                bridge.load_kur1or3_sprites(images_dir)

    async def _stop(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._stop_runtime is not None:
            self._stop_runtime()
        self._render_bridge = None
        return {"status": "stopped"}
