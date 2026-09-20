"""Handler for wallpaper mode.

Wallpaper owns its scene graph and character resource bootstrap. The chat
render iframe and wallpaper only share realtime signals such as speaking,
mouth amplitude, SpriteForge intents, and semantic wallpaper activities.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from collections.abc import Callable
from pathlib import Path

from config.settings import WAKE_AUTO_START_WITH_WALLPAPER, WAKE_ENABLED
from server.canvas_presentation import project_canvas_presentation
from server.event_bus import bus
from server.protocol import Method
from server.ws_handler import RequestHandler

logger = logging.getLogger(__name__)


class WallpaperHandler(RequestHandler):
    methods = [
        Method.WALLPAPER_START,
        Method.WALLPAPER_STOP,
        Method.WALLPAPER_ACTIVITY,
        Method.WALLPAPER_CANVAS,
    ]

    def __init__(self) -> None:
        self._wallpaper_host = None
        self._project_root: Path | None = None
        self._render_bridge = None
        self._wallpaper_animator = None
        self._subscribed = False
        self._wake_start_fn: Callable[[], Any] | None = None
        self._wake_stop_fn: Callable[[], Any] | None = None
        self._canvas_action_fn: Callable[[dict[str, Any]], Any] | None = None
        self._chat_send_fn: Callable[[str, str], Any] | None = None
        self._ensure_chat_session_fn: Callable[[], Any] | None = None
        self._canvas_projector: Callable[[dict[str, Any]], dict[str, Any]] | None = None
        self._attention_snapshot: Callable[[], list[dict[str, Any]]] | None = None
        self._last_canvas_payload: dict[str, Any] | None = None

    def configure(
        self,
        project_root: Path,
        render_bridge=None,
        wake_start_fn: Callable[[], Any] | None = None,
        wake_stop_fn: Callable[[], Any] | None = None,
        canvas_action_fn: Callable[[dict[str, Any]], Any] | None = None,
        chat_send_fn: Callable[[str, str], Any] | None = None,
        ensure_chat_session_fn: Callable[[], Any] | None = None,
        canvas_projector: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        attention_snapshot: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> None:
        self._project_root = project_root
        self._render_bridge = render_bridge
        self._wake_start_fn = wake_start_fn
        self._wake_stop_fn = wake_stop_fn
        self._canvas_action_fn = canvas_action_fn
        self._chat_send_fn = chat_send_fn
        self._ensure_chat_session_fn = ensure_chat_session_fn
        self._canvas_projector = canvas_projector
        self._attention_snapshot = attention_snapshot
        if not self._subscribed:
            for method in self._render_event_methods():
                bus.on(method, self._forward_render_event)
            bus.on(Method.WALLPAPER_ACTIVITY, self._forward_activity_event)
            bus.on(Method.WALLPAPER_CANVAS, self._forward_canvas_event)
            bus.on(Method.ASR_STATUS, self._forward_asr_event)
            bus.on(Method.ATTENTION_UPDATED, self._forward_attention_event)
            bus.on(Method.SESSION_CHANGED, self._forward_attention_event)
            self._subscribed = True

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if method == Method.WALLPAPER_START:
            return await self._start(params)
        if method == Method.WALLPAPER_STOP:
            return await self._stop(params)
        if method == Method.WALLPAPER_ACTIVITY:
            return await self._set_activity(params)
        if method == Method.WALLPAPER_CANVAS:
            return await self._set_canvas(params)
        return None

    async def _start(self, params: dict[str, Any]) -> dict[str, Any]:
        """Launch the web wallpaper bridge.

        The external wallpaper page (Wallpaper Engine/Lively/browser) consumes
        bridge events and runs render/web/renderer.js locally, so visual graph
        timing remains frontend-owned.
        """
        if self._wallpaper_host is not None:
            return self._status("already_running")

        try:
            from wallpaper.wallpaper_engine_bridge import WallpaperEngineBridgeHost
            from render.spriteforge_animator import SpriteForgeAnimator

            requested_slice_host = str(
                params.get("slice_host") or params.get("sliceHost") or "wallpaper"
            ).strip().lower()
            slice_host = "electron" if requested_slice_host == "electron" else "wallpaper"
            self._wallpaper_host = WallpaperEngineBridgeHost(slice_host=slice_host)
            self._install_canvas_action_handler(self._wallpaper_host)
            self._install_chat_submit_handler(self._wallpaper_host)
            self._wallpaper_host.start()
            from server import presentation_runtime

            self._wallpaper_host.set_canvas_presentation(
                presentation_runtime.get_config()
            )
            # Restore the durable selected WorkItem projection immediately;
            # bridge replay alone only remembers the previous process' final
            # in-memory canvas.
            self._apply_canvas({})
            self._apply_attention_snapshot()
            self._wallpaper_animator = SpriteForgeAnimator(self._wallpaper_host)
            self._wallpaper_animator.start()
            payload = self._status("started")
            await bus.emit(Method.WALLPAPER_READY, payload)
            if WAKE_ENABLED and WAKE_AUTO_START_WITH_WALLPAPER and self._wake_start_fn:
                try:
                    result = self._wake_start_fn()
                    if hasattr(result, "__await__"):
                        await result
                except Exception:
                    logger.exception("wake auto-start failed")
            return payload
        except Exception as e:
            logger.exception("wallpaper start failed")
            self._stop_wallpaper_animator()
            self._wallpaper_host = None
            return {"status": "error", "error": str(e)}

    def _install_canvas_action_handler(self, host) -> None:
        if not hasattr(host, "set_canvas_action_handler"):
            return
        loop = asyncio.get_running_loop()

        def _handler(payload: dict) -> dict:
            future = asyncio.run_coroutine_threadsafe(self._route_canvas_action(payload or {}), loop)
            return future.result(timeout=10)

        host.set_canvas_action_handler(_handler)

    async def _route_canvas_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        canvas_action = self._canvas_action_fn
        if canvas_action is None:
            return {"ok": False, "error": "canvas_action_router_unavailable"}
        result = canvas_action(payload)
        if hasattr(result, "__await__"):
            result = await result
        if isinstance(result, dict):
            return result
        return {"ok": True, "result": result}

    def _install_chat_submit_handler(self, host) -> None:
        if not hasattr(host, "set_chat_submit_handler"):
            return
        loop = asyncio.get_running_loop()

        def _handler(payload: dict) -> dict:
            future = asyncio.run_coroutine_threadsafe(
                self._route_chat_submit(payload or {}), loop
            )
            return future.result(timeout=10)

        host.set_chat_submit_handler(_handler)

    async def _route_chat_submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        text = str(payload.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "empty_message"}
        send_chat = self._chat_send_fn
        if send_chat is None:
            return {"ok": False, "error": "wallpaper_chat_unavailable"}
        ensure_session = self._ensure_chat_session_fn
        if ensure_session is None:
            return {"ok": False, "error": "wallpaper_session_unavailable"}
        session = ensure_session()
        if hasattr(session, "__await__"):
            session = await session
        if not isinstance(session, dict) or session.get("ok") is not True:
            return {"ok": False, "error": "wallpaper_session_unavailable"}
        session_id = str(session.get("current_session_id") or "").strip()
        if not session_id:
            return {"ok": False, "error": "wallpaper_session_unavailable"}
        result = send_chat(text, session_id)
        if hasattr(result, "__await__"):
            result = await result
        if isinstance(result, dict):
            # ChatHandler returns {status: "ok", turn_id: ...}; bridge action
            # endpoints use an explicit ok flag for their HTTP status.
            if result.get("status") == "ok" and result.get("ok") is not True:
                return {"ok": True, **result}
            return result
        return {"ok": True, "result": result}

    async def _stop(self, params: dict[str, Any]) -> dict[str, Any]:
        self._stop_wallpaper_animator()
        if self._wallpaper_host:
            try:
                self._wallpaper_host.stop()
            except Exception:
                logger.exception("wallpaper stop failed")
            self._wallpaper_host = None
        if WAKE_ENABLED and WAKE_AUTO_START_WITH_WALLPAPER and self._wake_stop_fn:
            try:
                result = self._wake_stop_fn()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                logger.exception("wake auto-stop failed")
        await bus.emit(Method.WALLPAPER_EXITED, {"status": "stopped"})
        return {"status": "stopped"}

    async def _set_activity(self, params: dict[str, Any]) -> dict[str, Any]:
        activity = str(params.get("activity") or "").strip().lower()
        ok = self._apply_activity(activity)
        return {"status": "ok" if ok else "error", "activity": activity}

    async def _forward_activity_event(self, _method: str, params: dict[str, Any]) -> None:
        activity = str(params.get("activity") or "").strip().lower()
        self._apply_activity(activity)

    def _apply_activity(self, activity: str) -> bool:
        host = self._wallpaper_host
        if host is not None:
            try:
                logger.info("wallpaper activity set: %s", activity or "idle")
                if hasattr(host, "set_activity"):
                    host.set_activity(activity)
                else:
                    host.set_work_mode(activity == "work")
            except Exception:
                logger.exception("failed to set wallpaper activity: %s", activity)
                return False
        return True

    async def _set_canvas(self, params: dict[str, Any]) -> dict[str, Any]:
        ok = self._apply_canvas(params or {})
        return {"status": "ok" if ok else "error"}

    async def _forward_canvas_event(self, _method: str, params: dict[str, Any]) -> None:
        self._apply_canvas(params or {})

    async def _forward_asr_event(self, _method: str, params: dict[str, Any]) -> None:
        self._apply_asr_status(params or {})

    async def _forward_attention_event(
        self, _method: str, _params: dict[str, Any]
    ) -> None:
        self._apply_attention_snapshot()

    def _apply_attention_snapshot(self) -> bool:
        snapshot = self._attention_snapshot
        if snapshot is None:
            return True
        try:
            requests = [
                dict(item)
                for item in list(snapshot() or [])[:3]
                if isinstance(item, dict)
            ]
        except Exception:
            logger.exception("failed to read current Attention snapshot")
            return False
        host = self._wallpaper_host
        if host is None:
            return True
        if str(getattr(host, "slice_host", "wallpaper")) != "electron":
            return True
        setter = getattr(host, "set_attention", None)
        if not callable(setter):
            logger.error("Electron Slice host has no set_attention transport")
            return False
        try:
            setter(
                {
                    "schemaId": "amadeus.attention.slice.v1",
                    "requests": requests,
                }
            )
            if requests:
                logger.info(
                    "[ATTENTION-PRESENTATION] dispatched request=%s surface=electron_slice",
                    requests[0].get("id", ""),
                )
        except Exception:
            logger.exception("failed to project Attention to Electron Slice")
            return False
        return True

    def _apply_canvas(self, payload: dict[str, Any]) -> bool:
        projected = dict(payload or {})
        projector = self._canvas_projector
        if projector is not None:
            try:
                candidate = projector(projected)
                if isinstance(candidate, dict):
                    projected = candidate
            except Exception:
                logger.exception("failed to project wallpaper canvas through work ledger")
        self._last_canvas_payload = dict(projected)
        host = self._wallpaper_host
        if host is None:
            return True
        try:
            from server import presentation_runtime

            host.set_canvas(
                project_canvas_presentation(
                    projected,
                    locale=presentation_runtime.get_presentation_locale(),
                )
            )
        except Exception:
            logger.exception("failed to set wallpaper canvas")
            return False
        return True

    def _apply_asr_status(self, payload: dict[str, Any]) -> bool:
        host = self._wallpaper_host
        if host is None:
            return True
        try:
            if hasattr(host, "set_asr_status"):
                host.set_asr_status(payload or {})
        except Exception:
            logger.exception("failed to set wallpaper ASR status")
            return False
        return True

    def _status(self, status: str) -> dict[str, Any]:
        host = self._wallpaper_host
        return {
            "status": status,
            "url": getattr(host, "url", ""),
            "lively_url": getattr(host, "lively_url", ""),
            "assetPort": getattr(host, "asset_port", -1),
            "bridgePort": getattr(host, "bridge_port", -1),
            "assetVersion": getattr(host, "asset_version", ""),
            "sliceHost": getattr(host, "slice_host", "wallpaper"),
            "sliceBounds": getattr(host, "slice_bounds", {}),
            "canvasBounds": getattr(host, "canvas_bounds", {}),
            "keyboardInputToggleBounds": getattr(host, "keyboard_input_toggle_bounds", {}),
            "keyboardComposerBounds": getattr(host, "keyboard_composer_bounds", {}),
        }

    def bridge_info(self) -> dict[str, Any]:
        host = self._wallpaper_host
        if host is None:
            # Lively may keep the page loaded while wallpaper mode is manually
            # disabled. Expose only the static client revision so that page can
            # refresh stale JS without implicitly starting the bridge.
            from wallpaper.wallpaper_engine_bridge import _wallpaper_asset_revision

            return {
                "running": False,
                "assetVersion": _wallpaper_asset_revision(),
            }
        return {
            "running": True,
            "assetPort": getattr(host, "asset_port", -1),
            "bridgePort": getattr(host, "bridge_port", -1),
            "bridgeToken": getattr(host, "action_token", ""),
            "assetVersion": getattr(host, "asset_version", ""),
            "sliceHost": getattr(host, "slice_host", "wallpaper"),
            "sliceBounds": getattr(host, "slice_bounds", {}),
            "canvasBounds": getattr(host, "canvas_bounds", {}),
            "keyboardInputToggleBounds": getattr(host, "keyboard_input_toggle_bounds", {}),
            "keyboardComposerBounds": getattr(host, "keyboard_composer_bounds", {}),
            "url": getattr(host, "url", ""),
            "lively_url": getattr(host, "lively_url", ""),
        }

    def set_subtitle(self, text: str) -> None:
        host = self._wallpaper_host
        if host is None:
            return
        try:
            host.set_subtitle(str(text or ""))
        except Exception:
            logger.exception("failed to update wallpaper subtitle")

    def set_canvas_presentation(self, profile: dict[str, Any]) -> None:
        host = self._wallpaper_host
        if host is None:
            return
        try:
            host.set_canvas_presentation(dict(profile or {}))
            if self._last_canvas_payload is not None:
                host.set_canvas(
                    project_canvas_presentation(
                        self._last_canvas_payload,
                        locale=(profile or {}).get("presentation_locale"),
                    )
                )
        except Exception:
            logger.exception("failed to update canvas presentation")

    def _stop_wallpaper_animator(self) -> None:
        animator = self._wallpaper_animator
        self._wallpaper_animator = None
        if animator is None:
            return
        try:
            animator.stop()
        except Exception:
            logger.exception("wallpaper animator stop failed")

    async def _forward_render_event(self, method: str, params: dict[str, Any]) -> None:
        host = self._wallpaper_host
        if host is None:
            return
        try:
            self._apply_render_event(host, method, params or {})
        except Exception:
            logger.exception("failed to forward render event to wallpaper: %s", method)

    @staticmethod
    def _render_event_methods() -> list[Method]:
        return [
            Method.RENDER_SPEAKING,
            Method.RENDER_MOUTH,
            Method.RENDER_SUBTITLE,
            Method.RENDER_MODE,
            Method.RENDER_SPRITEFORGE_INTENT,
            Method.RENDER_SPRITEFORGE_RELEASE,
        ]

    @staticmethod
    def _apply_render_event(host, method: str, params: dict[str, Any]) -> None:
        if method == Method.RENDER_SPRITE_FRAMES:
            host.load_sprite_frames(str(params.get("emotion") or ""), list(params.get("urls") or []))
        elif method == Method.RENDER_IDLE_FRAME_INTERVAL:
            host.set_idle_frame_interval_ms(str(params.get("emotion") or ""), int(params.get("intervalMs") or 42))
        elif method == Method.RENDER_SPRITE_CLIP_CONFIG:
            host.set_sprite_clip_config(str(params.get("emotion") or ""), dict(params.get("config") or {}))
        elif method == Method.RENDER_MOUTH_CONFIG:
            host.load_mouth_config(str(params.get("label") or ""), dict(params.get("config") or {}))
        elif method == Method.RENDER_SPRITEFORGE_GRAPH:
            host.load_spriteforge_graph(params)
        elif method == Method.RENDER_MODE:
            host.set_mode(str(params.get("mode") or "sprite"))
        elif method == Method.RENDER_IDLE_ANIMATION:
            host.set_idle_animation(bool(params.get("enabled", True)))
        elif method == Method.RENDER_EMOTION:
            host.set_emotion(str(params.get("emotion") or ""))
        elif method == Method.RENDER_SPEAKING:
            host.set_speaking(bool(params.get("speaking", False)))
        elif method == Method.RENDER_MOUTH:
            host.set_mouth_value(float(params.get("value") or 0.0))
        elif method == Method.RENDER_SUBTITLE:
            host.set_subtitle(str(params.get("text") or ""))
        elif method == Method.RENDER_SPRITEFORGE_INTENT:
            host.trigger_spriteforge_intent(
                str(params.get("label") or ""),
                params,
            )
        elif method == Method.RENDER_SPRITEFORGE_RELEASE:
            release = getattr(host, "release_spriteforge", None)
            if callable(release):
                release(params)
            else:
                host.clear_sprite_hold()
        elif method == Method.RENDER_HOLD_FRAME:
            host.hold_sprite_frame(params.get("which"))
        elif method == Method.RENDER_CLEAR_HOLD:
            host.clear_sprite_hold()
