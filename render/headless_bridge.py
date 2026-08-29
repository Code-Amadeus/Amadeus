"""Headless render bridge — replaces RenderEngine (PyQt5/QWebEngineView) in
headless server mode. Instead of runJavaScript(), it emits events on the
event bus, which the WS handler forwards to connected clients (including the
PixiJS iframe)."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from render.spriteforge_intent import spriteforge_intent_payload
from server.event_bus import bus
from server.protocol import Method

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

_PRESET_TO_SPRITE: dict[str, str] = {
    "smile":     "happy",
    "thinking":  "sided_thinking",
    "surprised": "sided_surprised",
}


class HeadlessRenderBridge:
    """Supported event-bus renderer replacing the archived PyQt RenderEngine."""

    def __init__(self, project_root: Path | str | None = None) -> None:
        self._project_root = Path(project_root) if project_root else _PROJECT_ROOT
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._event_queue: asyncio.Queue[tuple[Method, dict, asyncio.Future | None]] | None = None
        self._event_worker_task: asyncio.Task | None = None
        if self._loop is not None and self._loop.is_running():
            self._event_queue = asyncio.Queue()
            self._event_worker_task = self._loop.create_task(self._event_worker())
        self._registered_frames: dict[str, list[str]] = {}
        self._frame_intervals: dict[str, int] = {}
        self._clip_configs: dict[str, dict] = {}
        self._mouth_configs: dict[str, dict] = {}
        self._spriteforge_graph: dict | None = None
        self._current_mode: str = "sprite"
        self._idle_animation_enabled: bool = True
        self._current_emotion: str = ""
        self._current_speaking: bool = False
        self._current_mouth_value: float = 0.0
        self._held_frame = None

    async def _event_worker(self) -> None:
        """Serialize render events to match PyQt runJavaScript call ordering."""
        queue = self._event_queue
        if queue is None:
            return
        while True:
            method, params, done = await queue.get()
            try:
                await bus.emit(method, params)
                if done is not None and not done.done():
                    done.set_result(None)
            except Exception:
                logger.exception("render event emit failed: %s", method)
                if done is not None and not done.done():
                    done.set_exception(RuntimeError(f"render event emit failed: {method}"))
            finally:
                queue.task_done()

    def _emit(self, method: Method, params: dict) -> None:
        """Emit safely from any thread while preserving render event order."""
        loop = self._loop
        queue = self._event_queue
        if loop is not None and loop.is_running() and queue is not None:
            loop.call_soon_threadsafe(
                lambda: queue.put_nowait((method, params, None))
            )
        else:
            bus.emit_now(method, params)

    async def _emit_async(self, method: Method, params: dict) -> None:
        """Await a render event through the same ordered queue used by _emit()."""
        loop = self._loop
        queue = self._event_queue
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if loop is not None and loop.is_running() and queue is not None and current_loop is loop:
            done = loop.create_future()
            queue.put_nowait((method, params, done))
            await done
            return
        await bus.emit(method, params)

    # ── public API (mirrors RenderEngine) ──────────────────────────────

    def set_emotion(self, emotion: str) -> None:
        sprite_name = _PRESET_TO_SPRITE.get(emotion, emotion)
        self._current_emotion = sprite_name
        self._emit(Method.RENDER_EMOTION, {"emotion": sprite_name})

    def set_speaking(self, speaking: bool) -> None:
        self._current_speaking = bool(speaking)
        self._emit(Method.RENDER_SPEAKING, {"speaking": speaking})

    def set_mouth_value(self, value: float) -> None:
        self._current_mouth_value = float(max(0.0, min(1.0, value)))
        self._emit(Method.RENDER_MOUTH, {"value": self._current_mouth_value})

    def set_subtitle(self, text: str) -> None:
        self._emit(Method.RENDER_SUBTITLE, {"text": text})

    def set_mode(self, mode: str) -> None:
        self._current_mode = mode
        self._emit(Method.RENDER_MODE, {"mode": mode})

    def load_sprite_frames(self, emotion: str, frame_urls: list[str]) -> None:
        urls = [self._asset_url(u) for u in frame_urls]
        self._registered_frames[emotion] = urls
        self._emit(Method.RENDER_SPRITE_FRAMES, {"emotion": emotion, "urls": urls})

    def set_idle_animation(self, enabled: bool) -> None:
        self._idle_animation_enabled = enabled
        self._emit(Method.RENDER_IDLE_ANIMATION, {"enabled": enabled})

    def set_idle_frame_interval_ms(self, emotion: str, interval_ms: int) -> None:
        self._frame_intervals[emotion] = interval_ms
        self._emit(Method.RENDER_IDLE_FRAME_INTERVAL, {"emotion": emotion, "intervalMs": interval_ms})

    def set_sprite_clip_config(self, emotion: str, config: dict) -> None:
        self._clip_configs[emotion] = config
        self._emit(Method.RENDER_SPRITE_CLIP_CONFIG, {"emotion": emotion, "config": config})

    def load_mouth_config(self, label: str, config: dict) -> None:
        self._mouth_configs[label] = config
        self._emit(Method.RENDER_MOUTH_CONFIG, {"label": label, "config": config})

    def load_spriteforge_graph(self, payload: dict) -> None:
        self._spriteforge_graph = payload
        self._emit(Method.RENDER_SPRITEFORGE_GRAPH, payload)

    def trigger_spriteforge_intent(self, label: str) -> None:
        payload = spriteforge_intent_payload(label)
        self._emit(Method.RENDER_SPRITEFORGE_INTENT, payload)

    def hold_sprite_frame(self, which=None) -> None:
        self._held_frame = which
        self._emit(Method.RENDER_HOLD_FRAME, {"which": which})

    def clear_sprite_hold(self) -> None:
        self._held_frame = None
        self._emit(Method.RENDER_CLEAR_HOLD, {})

    def release_spriteforge(self) -> None:
        self._held_frame = None
        self._emit(Method.RENDER_SPRITEFORGE_RELEASE, {})

    async def replay_all(self) -> None:
        """Re-emit all registered state for a newly-connected iframe client.

        Uses await bus.emit() to serialize sends — prevents flooding the
        WebSocket with 37+ concurrent send_json() calls."""
        logger.info("[HeadlessBridge] replaying %d emotions, %d intervals, %d clips, %d mouth configs",
                    len(self._registered_frames), len(self._frame_intervals),
                    len(self._clip_configs), len(self._mouth_configs))
        # Frames and configs first, then the live state. This matches the old
        # PyQt pending-call behavior where assets were registered before the
        # current mode/emotion/speaking state was applied.
        for emotion, urls in self._registered_frames.items():
            await self._emit_async(Method.RENDER_SPRITE_FRAMES, {"emotion": emotion, "urls": urls})
        for emotion, interval_ms in self._frame_intervals.items():
            await self._emit_async(Method.RENDER_IDLE_FRAME_INTERVAL, {"emotion": emotion, "intervalMs": interval_ms})
        for emotion, config in self._clip_configs.items():
            await self._emit_async(Method.RENDER_SPRITE_CLIP_CONFIG, {"emotion": emotion, "config": config})
        for label, config in self._mouth_configs.items():
            await self._emit_async(Method.RENDER_MOUTH_CONFIG, {"label": label, "config": config})
        if self._spriteforge_graph is not None:
            await self._emit_async(Method.RENDER_SPRITEFORGE_GRAPH, self._spriteforge_graph)
        await self._emit_async(Method.RENDER_MODE, {"mode": self._current_mode})
        await self._emit_async(Method.RENDER_IDLE_ANIMATION, {"enabled": self._idle_animation_enabled})
        if self._current_emotion:
            await self._emit_async(Method.RENDER_EMOTION, {"emotion": self._current_emotion})
        await self._emit_async(Method.RENDER_SPEAKING, {"speaking": self._current_speaking})
        await self._emit_async(Method.RENDER_MOUTH, {"value": self._current_mouth_value})
        if self._held_frame is not None:
            await self._emit_async(Method.RENDER_HOLD_FRAME, {"which": self._held_frame})

    def load_kur1or3_sprites(self, images_dir: Path | str) -> None:
        """Scan images_dir for kurisu_*.png and register emotion frames."""
        images_dir = Path(images_dir)
        pattern = re.compile(r"kurisu_([a-z_]+?)(\d+)\.png$", re.IGNORECASE)

        emotion_frames: dict[str, list[tuple[int, str]]] = {}
        for p in sorted(images_dir.glob("kurisu_*.png")):
            m = pattern.match(p.name)
            if not m:
                continue
            emotion, frame_idx = m.group(1), int(m.group(2))
            rel = p.relative_to(self._project_root).as_posix()
            emotion_frames.setdefault(emotion, []).append((frame_idx, rel))

        for emotion, frames in emotion_frames.items():
            frames.sort(key=lambda x: x[0])
            urls = [rel for _, rel in frames]
            self.load_sprite_frames(emotion, urls)
            logger.info(f"[HeadlessBridge] registered sprite: {emotion} ({len(urls)} frames)")

    def _asset_url(self, rel_path: str) -> str:
        if rel_path.startswith("http") or rel_path.startswith("file://"):
            return rel_path
        abs_path = self._project_root / rel_path.lstrip('/')
        return abs_path.resolve().as_uri()
