"""Advance Eidema from the durable Session journal and build its raw search cache."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import replace
import logging
from pathlib import Path

from core import session_manager as sessions
from core import session_source_journal as journal
from core.chat_history_projection import project_memory_source
from eidema.retrieval import MAX_SOURCE_CHARS
from server.memory_service import MemoryRequestError

logger = logging.getLogger(__name__)
SOURCE_PAGE = 8
MATERIALIZE_PAGE = 4


class SourceDelivery:
    def __init__(self, host):
        self.host = host
        self.api = host._api
        self.maintenance = self.api.Maintenance(host._store)
        self.retrieval = self.api.Retrieval(host._store)
        self.loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._pending = OrderedDict()
        self._failed = set()
        self._state = "starting"
        journal.configure_publication(self._published, owner=self, scope_for_session=self._scope)
        self._task = asyncio.create_task(self._run())

    @property
    def state(self):
        return "degraded" if self._state == "idle" and self._failed else self._state

    def _scope(self, session_id, character):
        return journal.scope_fields(self.host.config.user_id, character,
                                    self.host.project_for_session(session_id) or None)

    def _enqueue(self, path):
        if not self.host._closing:
            self._pending[path] = None
            self._wake.set()

    def _published(self, path):
        # The commit owner never waits for SQLite or embedding.
        self.loop.call_soon_threadsafe(self._enqueue, path)

    async def close(self):
        journal.configure_publication(owner=self)
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)

    async def _step(self, fn):
        while True:
            async with self.host._publication_lock:
                try:
                    return await self.host._db.run(fn, timeout=None)
                except MemoryRequestError as exc:
                    if exc.code != "busy":
                        raise
            await asyncio.sleep(.01)

    def _inventory(self):
        return sorted(Path(sessions._SESSION_DIR).glob("*.json"))

    @staticmethod
    def _path(session_id):
        path = Path(sessions._session_path(session_id))
        if path.resolve().parent != Path(sessions._SESSION_DIR).resolve():
            raise ValueError("source_outside_session_directory")
        return path

    def _change(self, event):
        return self.api.SourceChange(**{
            **event,
            "source": self.api.SourceSpan(**event["source"]),
            "formation_allowed": journal.allows_formation(event),
        })

    def _replay_page(self, path, affected):
        data = journal.read(self._path(path.stem))
        for generation in data.get(journal.FIELD, {}).get("generations", ()):
            scope = self.api.MemoryScope(**generation["scope"])
            events = generation["events"]
            if scope.user_id != self.host.config.user_id or not events:
                continue
            access = self.api.AccessScope(scope.user_id, scope.character_id, scope.project_id)
            affected[access] = None
            stream = generation["stream_id"]
            self.maintenance.register_stream(access, stream, scope)
            position = self.maintenance.progress(access, stream, "journal-v1").received_position
            if position > len(events):
                raise ValueError("source_journal_receiver_ahead")
            page = events[max(0, position - 1):position + SOURCE_PAGE]
            if page:
                self.maintenance.append_changes(access, stream, [self._change(event) for event in page])
            snapshot = self.host._store.source_policy(access)
            if snapshot.change_seq > self.host._policy.value["change_seq"]:
                self.host._policy.reconcile(snapshot)
            if position < len(events):
                return True
        return False

    def _current_text(self, access, pin):
        sid, mid, incarnation = journal.decode(pin.source)
        path = self._path(sid)
        before = sessions._session_file_signature(path)
        data = journal.read(path)
        live = journal.live(data)
        if live is None:
            return None
        generation = live["generations"][-1]
        scope = generation["scope"]
        if (generation["incarnation"] != incarnation or scope["user_id"] != access.user_id
                or scope["character_id"] != access.character_id
                or scope["project_id"] not in (None, access.project_id)):
            return None

        current = None
        state_version = None
        for event in reversed(generation["events"]):
            if event["source"]["record_key"] != pin.source.record_key:
                continue
            change = self._change(event)
            if current is None:
                current = change
                if (current.action != "upsert" or current.source != pin.source
                        or not current.lookup_allowed or current.evidence_only
                        or current.policy_revision != pin.policy_revision
                        or current.recorded_at_ms != pin.recorded_at_ms):
                    return None
            elif replace(current, position=change.position) != change:
                break
            state_version = change.position
        if state_version != pin.state_version:
            return None
        row = next((row for row in data["dialog"] if row["message_id"] == mid), None)
        if row is None or row["created_at_ms"] != pin.recorded_at_ms:
            return None
        projected = project_memory_source(sid, row, expected_revision=pin.source.revision,
                                          incarnation=incarnation)
        return projected["text"] if before == sessions._session_file_signature(path) else None

    def _materialize_page(self, access, after):
        # An unresolved Host-side privacy operation can precede the SDK barrier.
        if not self.host._policy.ready or self.host._policy.value["pending"]:
            return None, False
        pins, cursor = self.retrieval.unmaterialized_sources(access, limit=MATERIALIZE_PAGE, after=after)
        indexed = False
        for pin in pins:
            try:
                text = self._current_text(access, pin)
                if text and len(text) <= MAX_SOURCE_CHARS:
                    self.retrieval.materialize_source(access, pin, text)
                    indexed = True
            except (FileNotFoundError, self.api.MemoryNotFound, self.api.MemoryConflict):
                # The next journal page or a later publication supersedes this pin.
                continue
        return cursor, indexed

    async def _run(self):
        try:
            for path in await self._step(self._inventory):
                self._enqueue(path)
            while True:
                self._state = "idle"
                await self._wake.wait()
                self._wake.clear()
                self._state = "indexing"
                affected = {}
                while self._pending:
                    path, _ = self._pending.popitem(last=False)
                    try:
                        more = await self._step(lambda: self._replay_page(path, affected))
                    except Exception as exc:
                        self._failed.add(path)
                        logger.warning("[Memory] source journal delivery failed (%s)", type(exc).__name__)
                    else:
                        self._failed.discard(path)
                        self.host._sync_projection()
                        if more:
                            self._pending[path] = None
                    await asyncio.sleep(0)
                for access in affected:
                    after = None
                    while True:
                        after, indexed = await self._step(lambda: self._materialize_page(access, after))
                        if indexed:
                            self.host._schedule_index()
                        if after is None:
                            break
                        await asyncio.sleep(0)
        except Exception as exc:
            self._state = "unavailable"
            logger.warning("[Memory] source delivery unavailable (%s)", type(exc).__name__)
