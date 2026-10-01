"""Opt-in cooperative role output using the existing Host narration boundary.

The publisher checks Host Session/turn ownership and emits the line without waiting
for a renderer acknowledgement. Optional speech keeps its own enqueue receipt;
publication does not establish physical display or authorize execution.
"""
from __future__ import annotations

import inspect
import hashlib
import asyncio
import json
import re
import threading
from typing import Callable

from server.narration_delivery import NarrationRequest, NarrationSink, deliver_narration
from core.chat_history_projection import project_completed_role_history


def load_consistent_json(raw):
    """Decode one complete JSON value while rejecting conflicting duplicates."""

    def consistent_object(pairs):
        result = {}
        for key, item in pairs:
            if key in result and result[key] != item:
                raise ValueError("conflicting duplicate JSON member")
            result[key] = item
        return result

    return json.loads(raw, object_pairs_hook=consistent_object)


def normalize_coordination_root(value):
    """Keep only conversation fields when an explicit null action owns no control."""

    if (not isinstance(value, dict)
            or "action" not in value
            or "say" not in value
            or not isinstance(value["say"], str)):
        raise ValueError("invalid coordination shape")
    if value["action"] is not None and set(value) != {"action", "say"}:
        raise ValueError("action coordination root has extra fields")
    return {"action":value["action"], "say":value["say"]}


async def query_role_messages(query, messages, *, on_text=None, **kwargs):
    """Backpressure one synchronous transport onto its owning asyncio reply."""
    if on_text is None:
        return await asyncio.to_thread(query, messages, **kwargs)
    loop = asyncio.get_running_loop()
    stopped = False
    pending = set()
    lock = threading.Lock()

    def accept(text):
        with lock:
            if stopped:
                raise asyncio.CancelledError("role query cancelled")
            future = asyncio.run_coroutine_threadsafe(on_text(text), loop)
            pending.add(future)
        try:
            future.result()
        finally:
            with lock:
                pending.discard(future)

    try:
        return await asyncio.to_thread(query, messages, on_text=accept, **kwargs)
    finally:
        with lock:
            stopped = True
            for future in pending:
                future.cancel()


class CooperativeHostDelivery:
    def __init__(self, *, session_id: str, display: Callable, narration_sink: NarrationSink | None = None,
                 record_display: Callable | None = None, role_stream_factory=None,
                 partial_display=None, allows=None, finish_execution=None,
                 begin_execution_result=None):
        if not session_id:
            raise ValueError("Host Session identity is required")
        self.session_id = session_id
        self.display = display
        self.narration_sink = narration_sink
        self.record_display = record_display
        self.role_stream_factory = role_stream_factory
        self.partial_display = partial_display
        self.allows = allows
        self.finish_execution = finish_execution
        self.begin_execution_result = begin_execution_result
        self.receipts: list[dict] = []

    async def __call__(self, event: dict) -> bool:
        return await self._publish(event)

    def begin_stream(self, cause, *, gui_callback=None,
                     auip_background_capture_release=None):
        if (self.role_stream_factory is None or self.allows is None
                or (gui_callback is None and self.partial_display is None)):
            return None
        return _CooperativeReplyStream(self, cause, gui_callback=gui_callback,
            auip_background_capture_release=auip_background_capture_release)

    async def _publish(self, event, *, narration_result=None):
        cause, text = event["cause"], event["text"]
        accepted = self.display({**event, "session_id":self.session_id})
        if inspect.isawaitable(accepted):
            accepted = await accepted
        receipt = {"session_id":self.session_id, "cause":cause, "published":accepted is True}
        self.receipts.append(receipt)
        if accepted is not True:
            return False
        if self.record_display is not None:
            # The Host published the output. Persistence failure must not cause
            # another model/execution call to repeat it.
            history_text = project_completed_role_history(text)
            message_id = "cooperative-role:" + hashlib.sha256(
                (str(cause) + "\0" + text).encode("utf-8")).hexdigest()
            receipt["history_recorded"] = self.record_display(
                self.session_id, role="assistant",
                content=history_text, turn_id=cause, message_id=message_id) is True
        if self.narration_sink is not None:
            sink = (self.narration_sink if narration_result is None
                else lambda _payload: narration_result)
            voice = await deliver_narration(NarrationRequest(
                request_id=f"cooperative:{self.session_id}:{cause}", source_kind="host",
                source_id=cause, session_id=self.session_id,
                payload={"display_text":text, "voice_text_ja":text,
                    "display_language":"japanese",
                    "source":"cooperative_chat", "line_id":cause,
                    "turn_id":cause, "complete_turn":True}),
                sink)
            receipt["narration"] = voice.to_dict()
        return True


class _CooperativeReplyStream:
    def __init__(self, delivery, cause, *, gui_callback=None,
                 auip_background_capture_release=None):
        self.delivery = delivery
        self.event = {"cause":cause, "session_id":delivery.session_id}
        self.gui_callback = gui_callback
        role_kwargs = {}
        if gui_callback is not None:
            role_kwargs["gui_callback"] = gui_callback
        if auip_background_capture_release is not None:
            role_kwargs["auip_background_capture_release"] = (
                auip_background_capture_release)
        self.role = delivery.role_stream_factory(cause, **role_kwargs)
        self.releases_auip_on_first_sentence = bool(
            getattr(self.role, "releases_auip_on_first_sentence", False))
        self.closed = False

    async def _check(self):
        if self.closed or not await self.delivery.allows(self.event):
            self.abort()
            raise asyncio.CancelledError("role reply lost foreground ownership")

    async def feed(self, text):
        await self._check()
        visible = await self.role.feed(text)
        await self._check()
        if visible and self.gui_callback is None:
            await self.delivery.partial_display({**self.event, "text":visible})

    async def prepare(self):
        await self._check()
        prepare = getattr(self.role, "prepare", None)
        if prepare is not None:
            await prepare()
        await self._check()

    async def finish(self, event):
        await self._check()
        narration = await self.role.finish()
        await self._check()
        accepted = await self.delivery._publish(event, narration_result=narration)
        self.closed = True
        return accepted

    def abort(self):
        self.closed = True
        self.role.abort()


class ConversationSayDecoder:
    """Decode say after an explicit leading conversation or coarse Work proposal.

    Detailed actions and other property order stay on completed delivery. This
    decoder exposes presentation text, never execution permission.
    """

    _prefix = re.compile(
        r'\s*\{\s*"action"\s*:\s*(?:(?P<none>null)|\{\s*"op"\s*:\s*"work"\s*\})'
        r'\s*,\s*"say"\s*:\s*"')

    def __init__(self, *, allow_work_proposal=False):
        self.raw = ""
        self.offset = 0
        self.started = False
        self.ended = False
        self.text = ""
        self.high_surrogate = ""
        self.action = None
        self.allow_work_proposal = bool(allow_work_proposal)

    def feed(self, delta):
        self.raw += delta
        if not self.started:
            match = self._prefix.match(self.raw)
            if match is None:
                return ""
            if match.group("none") is None and not self.allow_work_proposal:
                return ""
            self.started = True
            self.offset = match.end()
            self.action = None if match.group("none") else {"op":"work"}
        output = []
        while not self.ended and self.offset < len(self.raw):
            char = self.raw[self.offset]
            if char == '"':
                self.ended = True
                self.offset += 1
                break
            size = 1
            if char == "\\":
                if self.offset + 1 >= len(self.raw):
                    break
                size = 6 if self.raw[self.offset + 1] == "u" else 2
                if self.offset + size > len(self.raw):
                    break
                char = json.loads('"' + self.raw[self.offset:self.offset + size] + '"')
            elif ord(char) < 32:
                raise ValueError("invalid JSON string control character")
            self.offset += size
            if self.high_surrogate:
                if not 0xDC00 <= ord(char) <= 0xDFFF:
                    raise ValueError("unpaired JSON surrogate")
                char = chr(0x10000 + ((ord(self.high_surrogate) - 0xD800) << 10) + ord(char) - 0xDC00)
                self.high_surrogate = ""
            elif 0xD800 <= ord(char) <= 0xDBFF:
                self.high_surrogate = char
                continue
            elif 0xDC00 <= ord(char) <= 0xDFFF:
                raise ValueError("unpaired JSON surrogate")
            output.append(char)
        delta = "".join(output)
        self.text += delta
        return delta

    def finish(self, value):
        if not self.started:
            return
        try:
            if not self.ended or self.high_surrogate:
                raise ValueError

            complete = normalize_coordination_root(load_consistent_json(self.raw))
            streamed = {"action":self.action, "say":self.text}
            if (complete != streamed
                    or value != streamed):
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "streamed conversation JSON changed or was incomplete") from exc


class RoleDelegateReady(Exception):
    """A complete inline handoff ends the role request, not Host validation."""

    def __init__(self, raw):
        self.raw = raw
        super().__init__("role reached its DELEGATE handoff")


class DelegateRoleDecoder(ConversationSayDecoder):
    """Use the shared inline parser; retain JSON decoding for existing callers/history.

    The tag carries the same cooperative action fields. It never invokes the
    default Chat dispatcher: the existing cooperative validation/Host owns them.
    """

    def __init__(self):
        super().__init__(allow_work_proposal=True)
        from llm.stream_parser import StreamTagParser

        self.parser = StreamTagParser()
        self.inline = None
        self.handoff = False
        self._leading = ""

    def feed(self, delta):
        if self.inline is None:
            self._leading += delta
            if not self._leading.strip():
                return ""
            self.inline = not self._leading.lstrip().startswith("{")
            delta, self._leading = self._leading, ""
        if not self.inline:
            return super().feed(delta)
        self.raw += delta
        self.started = True
        _clean, _actions, parts = self.parser.process_chunk_parts(delta)
        output = []
        for kind, value in parts:
            if kind == "text":
                output.append(value)
            elif value["type"] == "DELEGATE":
                self.action = dict(value["attrs"])
                if not self.action.get("op"):
                    raise ValueError("cooperative DELEGATE requires an explicit op")
                self.handoff = True
            elif value["type"] == "EMO":
                output.append(value["raw"])
            else:
                raise ValueError("unsupported cooperative role control tag")
        text = "".join(output)
        self.text += text
        return text

    def value(self):
        if not self.inline:
            return normalize_coordination_root(load_consistent_json(self.raw))
        if self.parser._in_tag:
            raise ValueError("incomplete role tag")
        return {"say": self.text, "action": self.action}

    def finish(self, value):
        if not self.inline:
            return super().finish(value)
        # _decide validates the completed tag and may transfer an AUIP proposal
        # to its existing source-local owner. That is not a mutation of speech.
        if value["say"] != self.value()["say"]:
            raise ValueError("streamed role decision changed")
