"""One VN utterance: authorize once, submit safe segments, retain only submitted text."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import logging

from llm.sentence_splitter import split_stream_buffer_for_first_sentence
from llm.stream_parser import StreamTagParser

logger = logging.getLogger(__name__)


def take_speech_segment(text: str, *, first: bool, complete: bool,
                        early_cut: int, language: str) -> tuple[str, str]:
    """Use the ordinary voice path's first-sentence safe-boundary helper."""
    for end, char in enumerate(text, 1):
        prefix = text[:end]
        length = len(prefix.strip())
        if (char in "。！？!?\n" or (language == "英文" and char == ".")) and length >= (6 if first else 14):
            return prefix, text[end:]
        if char in "、，,；;：:" and length >= (18 if first else 14):
            return prefix, text[end:]
        if first and early_cut > 0 and length >= early_cut:
            head, tail, reason = split_stream_buffer_for_first_sentence(prefix, early_cut, language)
            if head and (tail or reason != "japanese_wait_boundary"):
                return head, tail + text[end:]
    return (text, "") if complete else ("", text)


class VNSpeechStream:
    """Coalesce cumulative prefixes while one worker preserves delivery order.

    ``authorize`` returns None only while the existing repeat check needs more
    text. ``active`` is rechecked for every segment. Every callback still receives
    an ordinary complete speak payload; no async object crosses the sink API.
    """

    def __init__(self, *, authorize, active, submit, normalize, previous=None,
                 lock=None, early_cut=11, language="日文"):
        self.authorize, self.active, self.submit = authorize, active, submit
        self.normalize, self.previous = normalize, previous
        self.lock = lock
        self.early_cut, self.language = early_cut, language
        self.delivery = {}
        self.observed = False
        self.sent_text = ""
        self.interruption = ""
        self._latest = None
        self._changed = asyncio.Event()
        self._finished = False
        self._task = None
        self._segment = 0

    async def update(self, header: dict, text_complete: bool = True) -> None:
        self.observed = True
        self._latest = (deepcopy(header), text_complete)
        self._changed.set()
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def finish(self) -> None:
        self._finished = True
        self._changed.set()
        if self._task is not None:
            await self._task

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        locked = False
        if self.previous is not None:
            await self.previous.wait()
        try:
            while True:
                await self._changed.wait()
                self._changed.clear()
                if not self.active():
                    self.interruption = "revoked"
                    return
                header, complete = self._latest
                raw_text = header["speak"]["text"]
                # With a cumulative prefix, an unfinished EMO tag stays buffered
                # by the same parser used for ordinary voice. Actions are data
                # here; only the confirmed playback metadata reaches the sink.
                cleaned, _ = StreamTagParser(stop_after_control=False).process_chunk(self.normalize(raw_text))
                cleaned = cleaned.lstrip()
                if not cleaned.startswith(self.sent_text):
                    raise ValueError("Stream revised submitted speech")
                rest = cleaned[len(self.sent_text):]
                while rest:
                    chunk, remaining = take_speech_segment(
                        rest, first=self._segment == 0, complete=complete,
                        early_cut=self.early_cut, language=self.language,
                    )
                    if not chunk:
                        break
                    if not self.delivery:
                        if self.lock is not None:
                            await self.lock.acquire()
                            locked = True
                        if not self.active():
                            self.interruption = "revoked"
                            return
                        approved = self.authorize(header, complete)
                        if approved is None:
                            if locked:
                                self.lock.release()
                                locked = False
                            break
                        self.delivery = deepcopy(approved)
                        if approved.get("decision") != "speak" or not approved.get("speak"):
                            return
                    if not self.active():
                        self.interruption = "revoked"
                        return
                    payload = {**self.delivery["speak"], "text": chunk,
                               "vn_speech_segment": self._segment + 1}
                    receipt = await self.submit(payload)
                    if isinstance(receipt, dict) and receipt.get("status") != "queued":
                        self.interruption = str(receipt.get("reason") or receipt.get("status") or "tts_unavailable")
                        return
                    self._segment += 1
                    self.sent_text += chunk
                    self.delivery["speak"]["text"] = self.sent_text.strip()
                    rest = remaining
                if complete or (self._finished and not self._changed.is_set()):
                    # Never flush a partial word/string after a broken stream.
                    if not complete and rest.strip():
                        self.interruption = "incomplete_text"
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.interruption = type(exc).__name__
            logger.warning("VN speech stream interrupted: %s", exc)
        finally:
            if locked:
                self.lock.release()
