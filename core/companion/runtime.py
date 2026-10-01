"""Composition root for Companion memory and reading services."""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
import hashlib
import io
import os
from pathlib import Path
import threading
from typing import Any
import urllib.request

from core.memory import AsyncMemoryWriter, HostMemoryExtractor, MemoryContextProvider, MemoryStore
from core.memory.extractor import Extractor
from core.reading import ReadingChunk, ReadingEventServer, ReadingSessionStore, SpoilerGuard

from .context import CompanionContextBuilder
from .scenes import SceneBinding, SceneStore


class CompanionRuntime:
    """Owns Companion-local services without owning the Host chat loop."""

    def __init__(
        self,
        root: str | Path,
        *,
        extractor: Extractor | None = None,
        memory_limit: int = 5,
        reading_server_port: int = 17878,
    ):
        self.root = Path(root).expanduser().resolve()
        self.memory = MemoryStore.open(self.root / "memory")
        self.reading = ReadingSessionStore.open(self.root / "reading")
        self.provider = MemoryContextProvider(self.memory, limit=memory_limit)
        self.context = CompanionContextBuilder(
            self.provider,
            self.reading,
            spoiler_guard=SpoilerGuard(),
        )
        self.writer = AsyncMemoryWriter(self.memory, extractor or HostMemoryExtractor())
        self.scenes = SceneStore(self.root / "scene.json")
        self._comic_lock = threading.RLock()
        self._comic_cache: dict[str, str] = {}
        self.reading_server = ReadingEventServer(
            self.reading,
            port=reading_server_port,
            comic_handler=self._handle_comic_chapter,
        )
        self._game_context_provider = None
        self._closed = False

    def context_block(
        self,
        query: str,
        *,
        book_id: str | None = None,
        chunks: Sequence[ReadingChunk] = (),
        include_activity_memory: bool = True,
        prior_user_messages: Sequence[str] = (),
    ) -> str:
        blocks = [
            self.context.build(
                query,
                book_id=book_id,
                chunks=chunks,
                include_activity_memory=include_activity_memory,
                prior_user_messages=prior_user_messages,
            ),
            self.scenes.context_block(query),
        ]
        provider = self._game_context_provider
        if provider is not None:
            try:
                provided = str(provider() or "").strip()
            except Exception:
                provided = ""
            if provided:
                blocks.append(provided)
        return "\n\n".join(block for block in blocks if block)

    def set_game_context_provider(self, provider) -> None:
        """Attach the live VN/Galgame hook as a read-only Game Companion source."""
        self._game_context_provider = provider

    def bind_scene(self, kind: str, *, window_handle: str = "", title: str = "", process_name: str = "") -> SceneBinding:
        return self.scenes.bind(
            kind,
            window_handle=window_handle,
            title=title,
            process_name=process_name,
        )

    def close_scene(self) -> None:
        self.scenes.close()

    def scene_status(self) -> dict[str, object]:
        return self.scenes.status()

    def record_scene_capture(self, description: str) -> SceneBinding | None:
        return self.scenes.record_capture(description)

    def remember_scene_turn(self, user_message: str, assistant_message: str) -> None:
        self.scenes.append_turn(user_message, assistant_message)

    def _handle_comic_chapter(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Turn one captured comic chapter into a bounded, spoiler-safe summary."""
        images = payload.get("images")
        if not isinstance(images, list) or not images:
            raise ValueError("comic chapter requires images")
        usable = [
            item
            for item in images
            if isinstance(item, dict) and (item.get("data_url") or item.get("url"))
        ][:8]
        if not usable:
            raise ValueError("comic chapter images are empty")
        book_id = str(payload.get("book_id") or "").strip()
        chapter = str(payload.get("chapter") or "").strip() or book_id or "comic"
        next_url = str(payload.get("next_url") or "").strip()
        cache_key = self._comic_cache_key(chapter, usable)
        with self._comic_lock:
            cached = self._comic_cache.get(cache_key)
        if cached:
            self.scenes.bind("comic", title=chapter, window_handle="browser", process_name="browser")
            self.scenes.record_chapter(cached, future_ref=next_url)
            return {
                "ok": True,
                "chapter": chapter,
                "summary": cached,
                "pages_used": len(usable),
                "next_url": next_url,
                "cached": True,
            }
        image_base64 = self._comic_montage_base64(usable)
        if not image_base64:
            raise ValueError("comic chapter images could not be decoded")
        from llm.qwen_client import qwen_vision_describe

        described = qwen_vision_describe(
            image_base64,
            prompt=(
                "你正在阅读一话漫画。请按阅读顺序用中文概括本话："
                "1) 本话剧情摘要；2) 出场角色；3) 关键事件；4) 重要对白。"
                "只描述本话可见内容，不推测下一话。控制在 400 字以内。"
            ),
            model=os.environ.get("AMADEUS_COMIC_VL_MODEL", "").strip() or None,
            max_tokens=600,
            timeout=60.0,
        )
        summary = str((described or {}).get("description") or "").strip()
        if not summary:
            raise RuntimeError("vision model returned no comic chapter summary")
        self.scenes.bind("comic", title=chapter, window_handle="browser", process_name="browser")
        self.scenes.record_chapter(summary, future_ref=next_url)
        with self._comic_lock:
            self._comic_cache[cache_key] = summary
        return {
            "ok": True,
            "chapter": chapter,
            "summary": summary,
            "pages_used": len(usable),
            "next_url": next_url,
        }

    @staticmethod
    def _comic_cache_key(chapter: str, images: list[dict[str, Any]]) -> str:
        digest = hashlib.sha1()
        digest.update(str(chapter).encode("utf-8", "ignore"))
        for item in images:
            value = str(item.get("url") or item.get("data_url") or "")
            digest.update(value[:512].encode("utf-8", "ignore"))
        return digest.hexdigest()

    @staticmethod
    def _comic_image_bytes(item: dict[str, Any]) -> bytes:
        data_url = str(item.get("data_url") or "")
        if data_url.startswith("data:") and "," in data_url:
            try:
                return base64.b64decode(data_url.split(",", 1)[1])
            except Exception:
                return b""
        url = str(item.get("url") or "")
        if not url:
            return b""
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                return response.read(12 * 1024 * 1024)
        except Exception:
            return b""

    @classmethod
    def _comic_montage_base64(cls, images: list[dict[str, Any]]) -> str:
        from PIL import Image

        frames = []
        for item in images:
            raw = cls._comic_image_bytes(item)
            if not raw:
                continue
            try:
                image = Image.open(io.BytesIO(raw)).convert("RGB")
            except Exception:
                continue
            width, height = image.size
            if width <= 0 or height <= 0:
                continue
            target_width = 640
            if width != target_width:
                target_height = max(1, round(height * target_width / width))
                image = image.resize((target_width, target_height), Image.Resampling.LANCZOS)
            frames.append(image)
        if not frames:
            return ""
        while len(frames) > 1 and sum(frame.height for frame in frames) > 4000:
            frames = frames[::2]
        montage_height = max(1, sum(frame.height for frame in frames))
        montage = Image.new("RGB", (640, montage_height), "white")
        offset = 0
        for frame in frames:
            montage.paste(frame, (0, offset))
            offset += frame.height
        buffer = io.BytesIO()
        montage.save(buffer, format="JPEG", quality=75, optimize=True)
        return base64.b64encode(buffer.getvalue()).decode("ascii")

    def ingest_reading_event(self, event: Mapping[str, object]):
        return self.reading.update_from_event(event)

    def remember_turn(
        self,
        book_id: str,
        *,
        user_message: str,
        assistant_message: str,
        selected_excerpt: str = "",
        referenced_chunks: Sequence[str] = (),
    ) -> None:
        self.context.remember_turn(
            book_id,
            user_message=user_message,
            assistant_message=assistant_message,
            selected_excerpt=selected_excerpt,
            referenced_chunks=referenced_chunks,
        )
        conversation = (
            f"User: {user_message}\n"
            f"Assistant: {assistant_message}\n"
            f"Selected excerpt: {selected_excerpt}"
        )
        self.writer.submit(conversation)

    def remember_conversation(
        self,
        *,
        user_message: str,
        assistant_message: str,
        book_id: str | None = None,
        selected_excerpt: str = "",
        referenced_chunks: Sequence[str] = (),
    ) -> None:
        self.scenes.append_turn(user_message, assistant_message)
        if book_id:
            self.remember_turn(
                book_id,
                user_message=user_message,
                assistant_message=assistant_message,
                selected_excerpt=selected_excerpt,
                referenced_chunks=referenced_chunks,
            )
            return
        self.writer.submit(
            f"User: {user_message}\nAssistant: {assistant_message}"
        )

    def start_reading_server(self) -> int:
        return self.reading_server.start()

    def close(self, timeout: float = 5.0) -> None:
        if self._closed:
            return
        self._closed = True
        self.reading_server.stop()
        self.writer.close(timeout=timeout)