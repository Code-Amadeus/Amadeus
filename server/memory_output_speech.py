"""Connect the existing playback tickets to the packaged Host's output authority."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar

import numpy as np
from server import memory_speech_tickets as speech

from core import session_source_journal as journal

PUBLISHED_OUTPUT = ContextVar("published_speech_output", default=None)


class NativeSpeechRegistry(speech.OutputSpeechRegistry):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self.projected = None

    @contextmanager
    def publication(self, session_id, message_id, revision, source, *, watches=()):
        """Watch the original and previous-output versions verified at publishing."""
        watched = {}
        for coordinate, position in watches:
            sid, _message, incarnation = journal.decode(coordinate)
            versions = watched.setdefault((sid, incarnation), {})
            key = coordinate.namespace, coordinate.record_key
            versions[key] = max(versions.get(key, 0), position)
        with self.lock:
            lease = self.leases.get(source)
            if lease is None:
                lease = speech.Lease(source, not self.closed)
                lease.source_versions = watched
                self.leases[source] = lease
            elif lease.source_versions != watched:
                raise ValueError("output_read_set_changed")
        token = PUBLISHED_OUTPUT.set(((session_id, message_id, revision), lease))
        try:
            yield
        finally:
            PUBLISHED_OUTPUT.reset(token)

    def source_committed(self, generation, *, retired, changes):
        if not retired and not changes:
            return
        with self.lock:
            for lease in tuple(self.leases.values()):
                versions = lease.source_versions.get(generation)
                if versions and (retired or any(
                        (namespace, key) in versions and position > versions[(namespace, key)]
                        for namespace, key, position in changes)):
                    lease.valid = False

    def project_retirements(self):
        """Reuse the Host's installed policy before completing a mutation request."""
        host = self.host
        ready = host._exclusions_ready and host._source_policy_reason != "publication_pending"
        snapshot = host._projection_value
        previous = self.projected
        if previous is not None and previous[0] is snapshot and previous[1] == ready:
            return
        self.projected = snapshot, ready
        with self.lock:
            leases = tuple(self.leases.items())
            for source, lease in leases:
                sid, mid, incarnation = journal.decode(source)
                key = (sid, incarnation, mid)
                if (not ready or key in host._retirements
                        or key in host._exclusions or key in host._pending_sources):
                    lease.valid = False

    @staticmethod
    def selected(session_id, event):
        publication = PUBLISHED_OUTPUT.get()
        key = (session_id, event.get("message_id"), event.get("revision", 1))
        return publication[1] if publication is not None and publication[0] == key else None

    @staticmethod
    @contextmanager
    def sentence_scope(lease):
        token = speech.SPEECH_SOURCE.set(lease.source if lease is not None else None)
        try:
            yield
        finally:
            speech.SPEECH_SOURCE.reset(token)


class BoundRoleStream:
    """Keep the first fragment's exact reply lease across sibling stream callbacks."""

    def __init__(self, role, registry, session_id, event):
        self.role = role
        self.registry = registry
        self.session_id = session_id
        self.event = event
        self.lease = None

    def __getattr__(self, name):
        return getattr(self.role, name)

    @contextmanager
    def _speech(self):
        if self.lease is None:
            self.lease = self.registry.selected(self.session_id, self.event)
        with self.registry.sentence_scope(self.lease):
            yield

    async def feed(self, text):
        with self._speech():
            return await self.role.feed(text)

    async def begin_remote(self):
        boundary = getattr(self.role, "begin_remote", None)
        if boundary is not None:
            with self._speech():
                await boundary()

    async def finish(self):
        try:
            with self._speech():
                return await self.role.finish()
        finally:
            self.lease = None

    def abort(self):
        try:
            return self.role.abort()
        finally:
            self.lease = None


def delivery_type(native, registry):
    class BoundDelivery(native):
        async def _publish(self, event, **options):
            with registry.sentence_scope(registry.selected(self.session_id, event)):
                return await super()._publish(event, **options)

        def begin_stream(self, *args, **options):
            stream = super().begin_stream(*args, **options)
            if stream is not None:
                stream.role = BoundRoleStream(stream.role, registry, self.session_id, stream.event)
            return stream

    return BoundDelivery


def install(host):
    """Install before starting the memory Host or creating its Chat ingress."""
    registry = NativeSpeechRegistry(host)
    host.attach_output_speech(registry)
    return registry


def queue(registry, *, maxsize=0):
    return speech.queue_type(asyncio, registry)(maxsize=maxsize)


def player_types(player, manager, registry):
    return speech.player_type(player, registry), speech.playback_type(manager, registry, np)


def bind_pipeline(pipeline, manager, registry):
    from tts.contract import TTSRequest
    from tts.utterance_scheduler import TTSUtteranceScheduler, UtteranceSegment

    bounded = speech.scheduler_type(TTSUtteranceScheduler, UtteranceSegment, TTSRequest)

    class BoundScheduler(bounded):
        def _can_append(self, current, segment):
            # One merged PCM item can have only one revocation lifetime.
            previous = getattr(current[-1], "research_output_ticket", None)
            upcoming = getattr(segment, "research_output_ticket", None)
            return ((previous.source if previous else None) == (upcoming.source if upcoming else None)
                    and super()._can_append(current, segment))

    pipeline.configure_speech_guard(
        scheduler_factory=BoundScheduler,
        synthesis_guard=lambda native: speech.synthesis_selector(native, registry, manager, np),
    )
