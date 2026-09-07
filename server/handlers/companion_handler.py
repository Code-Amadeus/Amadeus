"""Low-priority notification speech on the existing chat TTS pipeline.

The task source owns notification/read state; this handler owns only one speech
delivery at a time. It cannot answer questions, approve actions or mark tasks read.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

from server.event_bus import bus
from server.protocol import Method
from server.ws_handler import RequestHandler

logger = logging.getLogger(__name__)


class CompanionHandler(RequestHandler):
    methods = ["companion.speak", "companion.cancel", "companion.audio-activity"]

    def __init__(self) -> None:
        self.active: dict[str, Any] | None = None
        self.job: asyncio.Task | None = None
        self.speak = None
        self.translate = None
        self.interrupt = None
        self.busy = lambda: True
        self.chat_busy = lambda: True
        self.recent_spoken: deque[str] = deque(maxlen=3)
        self.audio_activity = None

    def configure(self, *, speak, translate, interrupt, busy, chat_busy) -> None:
        self.speak, self.translate, self.interrupt = speak, translate, interrupt
        self.busy, self.chat_busy = busy, chat_busy
        bus.on(Method.TTS_SENTENCE_START, self.on_playback)
        bus.on(Method.TTS_SENTENCE_END, self.on_playback)
        bus.on(Method.TTS_STATUS, self.on_playback)

    async def handle(self, method: str, params: dict[str, Any]) -> dict:
        # Audio-session observation begins only when a Companion client asks
        # for speech or audio status, not during ordinary backend startup.
        if method in ("companion.speak", "companion.audio-activity") and self.audio_activity and self.audio_activity.job is None:
            await self.audio_activity.refresh()
            self.audio_activity.start()
        if method == "companion.audio-activity":
            return self.audio_activity.snapshot if self.audio_activity else {"blocked": False, "note": ""}
        request_id = str(params.get("request_id") or "")
        if method == "companion.cancel":
            if self.active and self.active["id"] == request_id:
                await self.cancel("cancelled", stop_audio=True)
            return {"status": "cancelled"}
        if self.active or self.busy() or self.chat_busy() or (self.audio_activity and self.audio_activity.snapshot["blocked"]):
            return {"status": "busy"}
        text = str(params.get("text") or "").strip()
        if not request_id or not text or len(text) > 16000:
            return {"error": "Invalid notification speech request"}
        if not self.speak or not self.translate:
            return {"error": "Notification speech is unavailable"}
        self.active = {"id": request_id, "started": False, "enqueued": False,
                       "last": "", "ended": set(), "done": asyncio.Event(),
                       "accepted_at": time.perf_counter(), "japanese": ""}
        logger.info("companion voice request=%s accepted", request_id)
        self.job = asyncio.create_task(self.deliver(
            self.active, text, params.get("ended") is True,
            project_name=str(params.get("project_name") or "")[:240],
            provider=str(params.get("provider") or "")[:80],
        ))
        return {"status": "accepted", "request_id": request_id}

    async def publish(self, request_id: str, status: str) -> None:
        logger.info("companion voice request=%s status=%s", request_id, status)
        await bus.emit("companion.voice", {"request_id": request_id, "status": status})

    async def deliver(self, active: dict, text: str, ended: bool, *, project_name: str, provider: str) -> None:
        async def voice_stream():
            async for piece in self.translate(text, ended=ended, project_name=project_name,
                                               provider=provider, recent_spoken=list(self.recent_spoken)):
                if self.active is not active:
                    return
                # The bridge already reserves background speech ownership. Only
                # foreground chat can preempt it while the model is generating.
                if self.chat_busy():
                    await self.cancel("preempted", stop_audio=False)
                    return
                if not active["japanese"]:
                    active["first_text_at"] = time.perf_counter()
                    logger.info("companion voice request=%s first_text_ms=%.0f", active["id"],
                                (active["first_text_at"] - active["accepted_at"]) * 1000)
                active["japanese"] += piece
                active["enqueued"] = True
                yield piece
            logger.info("companion voice request=%s generation_complete_ms=%.0f", active["id"],
                        (time.perf_counter() - active["accepted_at"]) * 1000)

        try:
            result = await self.speak({
                "source": "companion_notification", "line_id": active["id"],
                "turn_id": active["id"], "display_text": text,
                "_voice_stream": voice_stream(), "complete_turn": True,
                "speed": 1.0,
                "_narration_delivery": {"source_kind": "companion", "source_id": active["id"],
                                        "request_id": active["id"], "session_id": ""},
            })
            if self.active is not active:
                return
            if result.get("status") != "queued" or not result.get("last_sentence_id"):
                logger.warning("companion voice request=%s enqueue failed status=%s reason=%s",
                               active["id"], result.get("status"), result.get("reason", "missing_last_sentence"))
                raise RuntimeError("Notification could not enter TTS")
            active["last"] = result["last_sentence_id"]
            if active["last"] in active["ended"]:
                active["done"].set()
            await asyncio.wait_for(active["done"].wait(), timeout=max(90, len(active["japanese"]) * .8 + 60))
            if self.active is active:
                self.active = None
                await self.publish(active["id"], "finished")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("companion speech unavailable request=%s: %s (status=%s)",
                           active["id"], type(exc).__name__, getattr(exc, "status_code", None))
            if self.active is active:
                await self.cancel("unavailable", stop_audio=True)

    async def on_playback(self, method: str, params: dict) -> None:
        active = self.active
        if not active:
            return
        if method == Method.TTS_STATUS:
            if params.get("status") == "interrupted":
                await self.cancel("preempted", stop_audio=False)
            return
        from server.vn_tts_bridge import get_vn_sentence_metadata
        sentence_id = str(params.get("sentence_id") or "")
        metadata = get_vn_sentence_metadata(sentence_id) or {}
        if metadata.get("line_id") != active["id"]:
            return
        if method == Method.TTS_SENTENCE_START and not active["started"]:
            active["started"] = True
            self.recent_spoken.append(active["japanese"][:180])
            logger.info("companion voice request=%s playback latency_ms=%.0f text_and_synthesis_wait_ms=%.0f",
                        active["id"], (time.perf_counter() - active["accepted_at"]) * 1000,
                        (time.perf_counter() - active["first_text_at"]) * 1000)
            await self.publish(active["id"], "started")
        elif method == Method.TTS_SENTENCE_END:
            active["ended"].add(sentence_id)
            if active["last"] == sentence_id:
                active["done"].set()

    async def cancel(self, status: str, *, stop_audio: bool) -> None:
        active, job = self.active, self.job
        if active is None:
            return
        self.active, self.job = None, None
        if job and job is not asyncio.current_task() and not job.done():
            job.cancel()
        from server.vn_tts_bridge import cancel_pending_vn_tts
        cancel_pending_vn_tts(source="companion_notification")
        # Never send a global TTS interrupt while foreground chat owns speech.
        # Chat's existing pre-turn interrupt invokes preempt() before its own reset.
        if stop_audio and active["enqueued"] and not self.chat_busy():
            await self.interrupt()
        await self.publish(active["id"], status)

    async def preempt(self) -> None:
        await self.cancel("preempted", stop_audio=False)

    async def defer_for_external_audio(self) -> None:
        await self.cancel("deferred", stop_audio=True)
