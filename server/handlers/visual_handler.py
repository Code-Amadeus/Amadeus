"""Authenticated desktop controls for visual assets and renderer diagnostics."""
from __future__ import annotations

from typing import Any, Callable

from render.visual_profile import VisualProfileStore, inspect_model
from server.character_presentation import coordinator
from server.event_bus import bus
from server.protocol import Method
from server.ws_handler import RequestHandler


class VisualHandler(RequestHandler):
    methods = [Method.VISUAL_GET, Method.VISUAL_INSPECT, Method.VISUAL_SAVE,
               Method.VISUAL_RELOAD, Method.VISUAL_PREVIEW, Method.VISUAL_STATUS]

    def __init__(self) -> None:
        self.store: VisualProfileStore | None = None
        self._surfaces: dict[str, dict] = {}
        self._apply: Callable[[], Any] | None = None
        self._surface_active: Callable[[str], bool] | None = None

    def configure(self, store: VisualProfileStore, apply: Callable[[], Any],
                  surface_active: Callable[[str], bool] | None = None) -> None:
        self.store = store
        self._apply = apply
        self._surface_active = surface_active
        coordinator.set_payload_resolver(store.intent_payload)
        coordinator.reproject()

    def snapshot(self) -> dict:
        if self.store is None:
            raise RuntimeError("visual handler is not configured")
        with coordinator.selection_lock:
            return {**self.store.snapshot(), "surfaces": dict(self._surfaces)}

    def report_status(self, params: dict) -> bool:
        """A surface observation can report readiness; it cannot choose a profile."""
        if self.store is None or params.get("surface") not in {"render", "wallpaper"}:
            return False
        with coordinator.selection_lock:
            if (params.get("state") != "unloaded" and self._surface_active is not None
                    and not self._surface_active(params["surface"])):
                return False
            config = self.store.config
            expected_id = config["selected_profile_id"]
            if (params.get("profile_id", "") != expected_id
                    or params.get("runtime_id") != self.store.runtime_id
                    or params.get("revision") != self.store.revision
                    or params.get("state") not in {"loading", "ready", "error", "unloaded"}):
                return False
            observation = {key: params[key] for key in
                           ("surface", "profile_id", "runtime_id", "revision", "state", "error", "diagnostic")
                           if key in params}
            self._surfaces[params["surface"]] = observation
        bus.emit_now(Method.VISUAL_UPDATED, self.snapshot())
        return True

    def surface_closed(self, surface: str) -> None:
        """The owning Host lifecycle knows closure even if its page cannot report it."""
        if self.store is None or surface not in {"render", "wallpaper"}:
            return
        with coordinator.selection_lock:
            self._surfaces[surface] = {
                "surface": surface, "profile_id": self.store.config["selected_profile_id"],
                "runtime_id": self.store.runtime_id, "revision": self.store.revision,
                "state": "unloaded",
            }
        bus.emit_now(Method.VISUAL_UPDATED, self.snapshot())

    async def _apply_selection(self) -> dict:
        if self._apply:
            result = self._apply()
            if hasattr(result, "__await__"):
                await result
        await coordinator.replay_current()
        result = self.snapshot()
        await bus.emit(Method.VISUAL_UPDATED, result)
        return result

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if self.store is None:
            raise RuntimeError("visual handler is not configured")
        if method == Method.VISUAL_GET:
            return self.snapshot()
        if method == Method.VISUAL_INSPECT:
            profile, capabilities = self.store.inspect(str(params.get("model_path") or ""))
            return {"profile": profile, "capabilities": capabilities}
        if method == Method.VISUAL_SAVE:
            with coordinator.selection_lock:
                previous = self.store.backend
                self.store.save(params.get("config"))
                if previous != self.store.backend:
                    coordinator.reproject()
                self._surfaces.clear()
            return await self._apply_selection()
        if method == Method.VISUAL_RELOAD:
            with coordinator.selection_lock:
                self.store.reload()
                self._surfaces.clear()
            return await self._apply_selection()
        if method == Method.VISUAL_PREVIEW:
            profile = params.get("profile") or self.store.selected_profile()
            if not profile:
                raise ValueError("select a model for preview")
            runtime = self.store.runtime_config(str(params.get("surface") or "render"), profile=profile,
                                                core_path=params.get("core_path"), preview=True)
            capabilities, _ = inspect_model(profile["model_path"])
            url = (self.store.project_root / "render/web/visual_preview.html").resolve().as_uri()
            return {"url": url, "config": runtime, "capabilities": capabilities}
        if method == Method.VISUAL_STATUS:
            return {"accepted": self.report_status(params)}
        return None
