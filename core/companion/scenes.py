"""Scene consumers for Game Companion and Comic Companion.

The companion owns one active scene at a time.  A scene only describes which
surface the user is currently attached to and keeps a bounded amount of recent
context; it never owns ASR, TTS or the vision provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import html
import json
import os
from pathlib import Path
import threading
import time
from typing import Any


SCENE_KINDS = frozenset({"game", "comic"})


@dataclass
class SceneBinding:
    kind: str
    window_handle: str = ""
    title: str = ""
    process_name: str = ""
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_capture: str = ""
    last_capture_at: float = 0.0
    future_ref: str = ""
    turns: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "window_handle": self.window_handle,
            "title": self.title,
            "process_name": self.process_name,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "last_capture": self.last_capture,
            "last_capture_at": self.last_capture_at,
            "future_ref": self.future_ref,
            "turns": list(self.turns),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SceneBinding":
        turns_value = payload.get("turns")
        if turns_value is not None and not isinstance(turns_value, list):
            raise TypeError("scene turns must be a list")
        return cls(
            kind=str(payload.get("kind") or ""),
            window_handle=str(payload.get("window_handle") or ""),
            title=str(payload.get("title") or ""),
            process_name=str(payload.get("process_name") or ""),
            started_at=float(payload.get("started_at") or 0.0),
            updated_at=float(payload.get("updated_at") or 0.0),
            last_capture=str(payload.get("last_capture") or ""),
            last_capture_at=float(payload.get("last_capture_at") or 0.0),
            future_ref=str(payload.get("future_ref") or ""),
            turns=[
                {"user": str(item.get("user") or ""), "assistant": str(item.get("assistant") or "")}
                for item in list(turns_value or [])
                if isinstance(item, dict)
            ],
        )


class SceneStore:
    """Small persistent scene binding plus bounded recent context."""

    max_turns = 12
    # One turn is one exchange, not a transcript: the chat path already bounds
    # what it sends, so this only stops a hostile or runaway caller.
    max_turn_chars = 4000
    # The scene block shares one prompt with reading and memory context. Without
    # a total bound, a long capture plus six turns can crowd out both.
    max_block_chars = 16000

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self._lock = threading.RLock()
        self._current: SceneBinding | None = None
        self._load()

    def bind(
        self,
        kind: str,
        *,
        window_handle: str = "",
        title: str = "",
        process_name: str = "",
    ) -> SceneBinding:
        normalized_kind = str(kind or "").strip().lower()
        if normalized_kind not in SCENE_KINDS:
            raise ValueError("scene kind must be game or comic")
        with self._lock:
            self._current = SceneBinding(
                kind=normalized_kind,
                window_handle=str(window_handle or "").strip(),
                title=str(title or "").strip(),
                process_name=str(process_name or "").strip(),
            )
            self._save_locked()
            return self._current

    def close(self) -> None:
        with self._lock:
            self._current = None
            self._save_locked()

    def current(self) -> SceneBinding | None:
        with self._lock:
            return self._current

    def record_capture(self, description: str) -> SceneBinding | None:
        text = str(description or "").strip()
        if not text:
            return self.current()
        with self._lock:
            if self._current is None:
                return None
            self._current.last_capture = text[:12000]
            self._current.last_capture_at = time.time()
            self._current.updated_at = time.time()
            self._save_locked()
            return self._current

    def record_chapter(self, summary: str, *, future_ref: str = "") -> SceneBinding | None:
        text = str(summary or "").strip()
        if not text:
            return self.current()
        with self._lock:
            if self._current is None:
                return None
            self._current.last_capture = text[:12000]
            self._current.last_capture_at = time.time()
            self._current.future_ref = str(future_ref or "")[:2000]
            self._current.updated_at = time.time()
            self._save_locked()
            return self._current

    def append_turn(self, user_message: str, assistant_message: str) -> None:
        user_text = str(user_message or "").strip()[: self.max_turn_chars]
        assistant_text = str(assistant_message or "").strip()[: self.max_turn_chars]
        if not user_text and not assistant_text:
            return
        with self._lock:
            if self._current is None:
                return
            self._current.turns.append({"user": user_text, "assistant": assistant_text})
            self._current.turns = self._current.turns[-self.max_turns :]
            self._current.updated_at = time.time()
            self._save_locked()

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._current is None:
                return {"active": False, "kind": "", "title": "", "windowHandle": "", "lastCaptureAt": 0.0}
            return {
                "active": True,
                "kind": self._current.kind,
                "title": self._current.title,
                "windowHandle": self._current.window_handle,
                "processName": self._current.process_name,
                "lastCaptureAt": self._current.last_capture_at,
                "hasFuture": bool(self._current.future_ref),
                "startedAt": self._current.started_at,
            }

    def context_block(self, query: str = "") -> str:
        del query
        with self._lock:
            scene = self._current
            if scene is None:
                return ""
            turns = list(scene.turns)
            title = scene.title
            kind = scene.kind
            process_name = scene.process_name
            capture = scene.last_capture
            future_ref = scene.future_ref
        rules = (
            "只把当前场景中已经出现的内容当作事实。不要剧透尚未出现的剧情、选项结果或后续页面。"
            if kind == "game"
            else "只把当前页和已读页当作事实。后续页是未来内容，禁止引用或暗示；用户要求翻译时按阅读顺序给出原文与中文。"
        )
        lines = [
            "<scene_context>",
            f"kind={html.escape(kind)}",
            f"window_title={html.escape(title)}",
            f"process={html.escape(process_name)}",
            f"rule={html.escape(rules)}",
        ]
        if future_ref:
            lines.append("future_content=marked_but_not_loaded")
        if capture:
            lines.append(f"latest_capture={html.escape(capture)}")
        if turns:
            lines.append("recent_turns:")
            for turn in turns[-6:]:
                lines.append(
                    f"- user: {html.escape(str(turn.get('user') or ''))}\n"
                    f"  assistant: {html.escape(str(turn.get('assistant') or ''))}"
                )
        lines.append("</scene_context>")
        block = "\n".join(lines)
        if len(block) <= self.max_block_chars:
            return block
        # Cut inside the payload, never the closing tag, so the block stays a
        # well-formed element the prompt can rely on.
        closing = "\n</scene_context>"
        room = max(0, self.max_block_chars - len(closing))
        return block[:room] + closing

    def _load(self) -> None:
        """Recover the last complete snapshot, ignoring a torn or unknown one.

        An interrupted write can leave invalid JSON, or valid JSON whose scene
        cannot be adopted. Either way the next save replaces it, so loading never
        raises and never keeps a half-written binding.
        """
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        scene = payload.get("scene") if isinstance(payload, dict) else None
        if not isinstance(scene, dict):
            return
        try:
            candidate = SceneBinding.from_dict(scene)
        except (TypeError, ValueError):
            return
        if candidate.kind in SCENE_KINDS:
            self._current = candidate

    def _save_locked(self) -> None:
        """Publish the snapshot atomically so an interruption cannot lose it.

        Writing in place truncates the previous snapshot first, so a crash
        between truncate and write leaves invalid JSON and the adoption is gone.
        A temporary file plus an atomic replace keeps the previous snapshot
        readable until the new one is complete.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"scene": self._current.to_dict() if self._current else None}
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, self.path)
