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

    def _published(self, path, *, generation=None, retired=False, changes=()):
        # This observer runs after the journal commit, before its caller can acknowledge it.
        speech = self.host.output_speech
        if speech is not None and generation is not None:
            speech.source_committed(generation, retired=retired, changes=changes)
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
        view = self.view(access)
        try:
            content = view.read(pin, discoverable=True)
            self.require_current(view.signatures((pin,)))
        except (FileNotFoundError, MemoryRequestError):
            return None
        return content.text

    def view(self, access):
        return SessionReadView(self, access)

    @staticmethod
    def require_current(signatures):
        try:
            for path, signature in signatures:
                if sessions._session_file_signature(path) != signature:
                    raise MemoryRequestError("conflict")
        except OSError:
            raise MemoryRequestError("conflict") from None

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


class SessionReadView:
    """One read operation over exact committed Session versions."""

    def __init__(self, delivery, access):
        self.delivery = delivery
        self.access = access
        self.files = {}

    def _source(self, source):
        sid, mid, incarnation = journal.decode(source)
        if sid not in self.files:
            path = self.delivery._path(sid)
            signature = sessions._session_file_signature(path)
            data = journal.read(path)
            live = journal.live(data)
            if live is None:
                raise MemoryRequestError("conflict")
            generation = live["generations"][-1]
            events = {}
            for event in generation["events"]:
                events.setdefault(event["source"]["record_key"], []).append(event)
            self.files[sid] = (path, signature, generation, events,
                               {row["message_id"]: row for row in data["dialog"]})
        path, signature, generation, events, rows = self.files[sid]
        scope = generation["scope"]
        if (generation["incarnation"] != incarnation or scope["user_id"] != self.access.user_id
                or scope["character_id"] != self.access.character_id
                or scope["project_id"] not in (None, self.access.project_id)):
            raise MemoryRequestError("conflict")
        return sid, mid, incarnation, generation["stream_id"], events, rows

    def pin(self, item):
        pin = self._pin(item.source)
        if pin.state_version != item.reference.target_version:
            raise MemoryRequestError("conflict")
        return pin

    def _pin(self, source):
        *_, stream, _events, _rows = self._source(source)
        pin = self.delivery.maintenance.lookup_input(self.access, stream, source, discoverable=True)
        self._row(pin, discoverable=True)
        return pin

    def _row(self, pin, *, discoverable=False):
        source = pin.source
        sid, mid, incarnation, _stream, events, rows = self._source(source)
        row = rows.get(mid)
        revisions = events.get(source.record_key)
        if row is None or not revisions or (row["revision"], row["created_at_ms"]) != (
                source.revision, pin.recorded_at_ms):
            raise MemoryRequestError("conflict")
        current = self.delivery._change(revisions[-1])
        if (current.action != "upsert" or current.source != source or not current.lookup_allowed
                or (discoverable and current.evidence_only)
                or (current.policy_revision, current.recorded_at_ms) !=
                    (pin.policy_revision, pin.recorded_at_ms)):
            raise MemoryRequestError("conflict")
        version = current.position
        for event in reversed(revisions[:-1]):
            if self.delivery._change(event) != replace(current, position=event["position"]):
                break
            version = event["position"]
        if version != pin.state_version:
            raise MemoryRequestError("conflict")
        return sid, incarnation, row

    def read(self, pin, *, max_chars=MAX_SOURCE_CHARS, max_bytes=4_000_000, discoverable=False):
        sid, incarnation, row = self._row(pin, discoverable=discoverable)
        source = pin.source
        text = project_memory_source(sid, row, expected_revision=source.revision,
                                     incarnation=incarnation)["text"]
        if len(text) > max_chars or len(text.encode("utf8")) > max_bytes:
            raise MemoryRequestError("source_budget")
        speaker = self.access.user_id if row["role"] == "user" else self.access.character_id
        return self.delivery.api.SourceContent(source, text, row["role"], speaker)

    def output_state(self, output):
        """The current displayed reply has its own lifetime within this Session."""
        sid, mid, _, _, events, rows = self._source(output)
        row = rows.get(mid)
        if row is None or row["role"] != "assistant" or row["revision"] != output.revision:
            raise MemoryRequestError("conflict")
        heads = events.get(output.record_key)
        if heads:
            head = heads[-1]
            if head["action"] != "upsert" or head["source"]["revision"] != output.revision:
                raise MemoryRequestError("conflict")
            return sid, head["position"]
        return sid, 0

    def signatures(self, pins, *, output_states=()):
        signatures = {}
        session_ids = {self._row(pin)[0] for pin in pins}
        session_ids.update(sid for sid, _ in output_states)
        for sid in session_ids:
            path, signature, *_ = self.files[sid]
            signatures[path] = signature
        return tuple(signatures.items())

    def position(self, pin):
        sid, _, incarnation, _, events, _ = self._source(pin.source)
        return (sid, incarnation), tuple(events).index(pin.source.record_key)

    def relative(self, anchor, offset):
        """Locate a committed slot without crossing an unavailable message."""
        sid, mid, _, _, events, rows = self._source(anchor.source)
        keys = tuple(events)
        at = keys.index(anchor.source.record_key)
        index = at + offset
        if not 0 <= index < len(keys):
            raise MemoryRequestError("conflict")
        entry = events[keys[index]][-1]
        source = self.delivery.api.SourceSpan(**entry["source"])
        _, target, _ = journal.decode(source)
        dialog = tuple(rows)
        # Journal ordering remembers removed slots, and the live transcript
        # also detects intervening rows written while capture was paused.
        if target not in rows or dialog.index(target) - dialog.index(mid) != offset:
            raise MemoryRequestError("conflict")
        pin = self._pin(source)
        return pin, rows[target]["role"]
