"""Track output authority through queued speech, synthesis, and PCM writing."""
from contextvars import ContextVar
from dataclasses import asdict
from threading import RLock
from weakref import WeakValueDictionary


SPEECH_SOURCE = ContextVar("speech_output_source", default=None)
JOB_TICKETS = ContextVar("speech_output_tickets", default=())


class Lease:
    def __init__(self, source, valid):
        self.source = source
        self.valid = valid


class Ticket:
    def __init__(self, sentence_id, lease):
        self.sentence_id = sentence_id
        self.lease = lease
        self.closed = False
        self.source = lease.source


class OutputSpeechRegistry:
    def __init__(self):
        self.sentences = WeakValueDictionary()
        self.leases = WeakValueDictionary()
        self.lock = RLock()
        self.closed = False

    def bind_sentence(self, request, source):
        with self.lock:
            lease = self.leases.get(source)
            if lease is None:
                lease = Lease(source, False)
            ticket = Ticket(request.sentence_id, lease)
            self.sentences[request.sentence_id] = ticket
            request.metadata["research_output_ticket"] = ticket
            return ticket

    def ticket_for_sentence(self, sentence_id):
        with self.lock:
            return self.sentences.get(sentence_id)

    def current_ticket(self, ticket):
        if ticket is None:
            return True
        with self.lock:
            return not self.closed and not ticket.closed and ticket.lease is not None and ticket.lease.valid

    def release_ticket(self, ticket):
        if ticket is None:
            return
        with self.lock:
            ticket.closed = True
            ticket.lease = None
            if self.sentences.get(ticket.sentence_id) is ticket:
                self.sentences.pop(ticket.sentence_id)

    def close(self):
        with self.lock:
            self.closed = True
            for ticket in list(self.sentences.values()):
                ticket.closed = True
                ticket.lease = None
            for lease in list(self.leases.values()):
                lease.valid = False
            self.sentences.clear()
            self.leases.clear()


def queue_type(asyncio, registry):
    class BoundQueue(asyncio.Queue):
        async def put(self, request):
            source = SPEECH_SOURCE.get()
            if source is not None:
                registry.bind_sentence(request, source)
            return await super().put(request)

    return BoundQueue


def scheduler_type(native, segment_type, request_type):
    class TicketSegment(segment_type):
        __slots__ = ("research_output_ticket",)

    class BoundScheduler(native):
        def _to_segment(self, item):
            base = super()._to_segment(item)
            request = request_type.from_queue_item(item)
            segment = TicketSegment(**asdict(base))
            segment.research_output_ticket = request.metadata.get("research_output_ticket")
            return segment

    return BoundScheduler


def synthesis_selector(native, registry, playback, np):
    def select(job, **kwargs):
        name, backend = native(job, **kwargs)
        tickets = tuple(ticket for segment in job.segments
                        if (ticket := getattr(segment, "research_output_ticket", None)) is not None)

        async def guarded(text, sentence_id, is_first, **options):
            token = JOB_TICKETS.set(tickets)
            try:
                if not all(registry.current_ticket(ticket) for ticket in tickets):
                    await playback.add_to_playlist(
                        np.empty(0, dtype=np.float32), 24000, sentence_id, "",
                        playback_epoch=options.get("interrupt_epoch"))
                    return
                return await backend(text, sentence_id, is_first, **options)
            finally:
                JOB_TICKETS.reset(token)

        return name, guarded

    return select


def _tickets(registry, sentence_id):
    current = JOB_TICKETS.get()
    if current:
        return current
    ticket = registry.ticket_for_sentence(sentence_id)
    return (ticket,) if ticket else ()


def player_type(native, registry):
    class BoundPlayer(native):
        async def play_full_audio_and_signal_completion(self, audio, *args, is_current=None, **kwargs):
            tickets = getattr(audio, "research_output_tickets", ())

            def current():
                return ((is_current is None or is_current())
                        and all(registry.current_ticket(ticket) for ticket in tickets))

            try:
                return await super().play_full_audio_and_signal_completion(
                    audio, *args, is_current=current, **kwargs)
            finally:
                for ticket in tickets:
                    registry.release_ticket(ticket)

    return BoundPlayer


def playback_type(native, registry, np):
    class TicketedPCM(np.ndarray):
        def __array_finalize__(self, obj):
            self.research_output_tickets = getattr(obj, "research_output_tickets", ())

    class BoundPlayback(native):
        def _epoch_checker(self, epoch):
            current = super()._epoch_checker(epoch)
            tickets = _tickets(registry, self.current_playing_id)
            return lambda: current() and all(registry.current_ticket(ticket) for ticket in tickets)

        async def add_to_playlist(self, audio, rate, sid, text, segments=None, playback_epoch=None):
            tickets = getattr(audio, "research_output_tickets", ()) or _tickets(registry, sid)
            if not all(registry.current_ticket(ticket) for ticket in tickets):
                audio, text, segments = np.empty(0, dtype=np.float32), "", None
            audio = np.asarray(audio).view(TicketedPCM)
            audio.research_output_tickets = tickets
            return await super().add_to_playlist(audio, rate, sid, text, segments, playback_epoch)

        async def play_s1_stream(self, chunk_queue, sid, text, playback_epoch=None):
            tickets = _tickets(registry, sid)
            token = JOB_TICKETS.set(tickets)
            try:
                return await super().play_s1_stream(chunk_queue, sid, text, playback_epoch)
            finally:
                for ticket in tickets:
                    registry.release_ticket(ticket)
                JOB_TICKETS.reset(token)

    return BoundPlayback
