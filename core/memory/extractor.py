"""Asynchronous memory extraction and write queue.

The model-dependent extractor is injected as a callable. This keeps the
persistence layer testable and lets Companion reuse the existing Host LLM
transport without creating a second model client.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import queue
import threading
import time
from typing import Any

from .models import MemoryRecord
from .store import MemoryStore


ExtractorResult = Iterable[MemoryRecord | Mapping[str, object]]
Extractor = Callable[[str], ExtractorResult]


def parse_extractor_payload(
    payload: object,
    *,
    default_scope: str = "user",
    default_namespace: str = "general",
    default_source: str = "conversation",
    default_source_ids: Iterable[str] = (),
) -> list[MemoryRecord]:
    """Validate the JSON shape expected from an LLM extraction call."""

    if isinstance(payload, Mapping):
        raw_items = payload.get("memories", payload.get("items", [payload]))
    elif isinstance(payload, list):
        raw_items = payload
    else:
        raise TypeError("extractor payload must be a mapping or list")
    source_ids = tuple(str(item) for item in default_source_ids if str(item))
    records: list[MemoryRecord] = []
    allowed = {"text", "kind", "scope", "namespace", "tags", "importance", "confidence"}
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError(f"unknown extracted memory fields: {', '.join(unknown)}")
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        records.append(
            MemoryRecord.create(
                text=text,
                kind=str(raw.get("kind") or "fact"),
                scope=str(raw.get("scope") or default_scope),
                namespace=str(raw.get("namespace") or default_namespace),
                tags=tuple(raw.get("tags") or ()),
                importance=int(raw.get("importance", 3)),
                confidence=float(raw.get("confidence", 0.8)),
                source=default_source,
                source_ids=source_ids,
            )
        )
    return records


class AsyncMemoryWriter:
    """Serializes extraction and writes without blocking the reply path."""

    def __init__(
        self,
        store: MemoryStore,
        extractor: Extractor,
        *,
        max_queue: int = 100,
        poll_interval: float = 0.05,
    ):
        self.store = store
        self.extractor = extractor
        self.poll_interval = max(0.01, float(poll_interval))
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=max(1, int(max_queue)))
        self._stop = threading.Event()
        self._last_error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="memory-writer", daemon=True)
        self._thread.start()

    @property
    def last_error(self) -> BaseException | None:
        return self._last_error

    def submit(self, conversation_text: str) -> bool:
        text = str(conversation_text or "").strip()
        if not text or self._stop.is_set():
            return False
        try:
            self._queue.put_nowait(text)
        except queue.Full:
            self._last_error = RuntimeError("memory write queue is full")
            return False
        return True

    def wait_idle(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while self._queue.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.poll_interval)
        return True

    def close(self, timeout: float = 5.0) -> bool:
        if self._stop.is_set():
            return self._thread.is_alive() is False
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=max(0.0, float(timeout)))
        return not self._thread.is_alive()

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=self.poll_interval)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            try:
                if item is None:
                    return
                candidates = self.extractor(item)
                self.store.remember(candidates)
            except BaseException as error:  # keep the writer alive across model errors
                self._last_error = error
            finally:
                self._queue.task_done()