"""Session-scoped, one-shot requests for bounded user decisions.

An attention request is the reusable host primitive for bounded user choices.
It matches the lifecycle of the existing permission interaction without
merging authorization semantics or migrating permission storage in this batch:
selecting an object never grants a capability.  Public payloads contain only
opaque option ids and presentation metadata; the continuation and canonical
entity ids remain in host memory.
"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Mapping, Sequence

from server.event_bus import bus
from server.protocol import Method


AttentionKind = Literal["selection"]
AttentionStatus = Literal["pending", "resolving", "resolved", "failed", "expired"]
AttentionContinuation = Callable[[str], Awaitable[Mapping[str, Any] | None] | Mapping[str, Any] | None]


def _compact_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


@dataclass(frozen=True, slots=True)
class AttentionOption:
    """One user-visible option with an opaque action id."""

    option_id: str
    label: str
    entity_kind: Literal["project", "work_item", "other"] = "other"
    description: str = ""
    parent_label: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.option_id,
            "label": _compact_text(self.label, 160),
            "entityKind": self.entity_kind,
        }
        if self.description:
            payload["description"] = _compact_text(self.description, 240)
        if self.parent_label:
            payload["parentLabel"] = _compact_text(self.parent_label, 160)
        if self.metadata:
            payload["metadata"] = {
                str(key): value
                for key, value in self.metadata.items()
                if str(key) in {"scope", "relation"}
                and isinstance(value, (str, int, float, bool))
            }
        return payload


@dataclass(slots=True)
class _AttentionRequest:
    request_id: str
    session_id: str
    kind: AttentionKind
    title: str
    prompt: str
    options: tuple[AttentionOption, ...]
    created_at: float
    expires_at: float
    continuation: AttentionContinuation
    dedupe_key: str = ""
    status: AttentionStatus = "pending"

    def public_dict(self) -> dict[str, Any]:
        return {
            "schemaId": "amadeus.attention.v1",
            "id": self.request_id,
            "sessionId": self.session_id,
            "kind": self.kind,
            "status": self.status,
            "title": _compact_text(self.title, 160),
            "prompt": _compact_text(self.prompt, 520),
            "options": [option.public_dict() for option in self.options],
            "createdAt": self.created_at,
            "expiresAt": self.expires_at,
        }


class AttentionRequestCoordinator:
    """Own pending decisions and invoke each continuation at most once."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._requests: dict[str, _AttentionRequest] = {}
        self._expiry_tasks: dict[str, asyncio.Task[None]] = {}

    def reset_for_tests(self) -> None:
        for task in self._expiry_tasks.values():
            task.cancel()
        self._expiry_tasks.clear()
        self._requests.clear()

    def list_pending(self, session_id: str) -> list[dict[str, Any]]:
        self._expire_synchronously()
        clean_session = str(session_id or "").strip()
        return [
            request.public_dict()
            for request in sorted(
                self._requests.values(), key=lambda item: item.created_at
            )
            if request.session_id == clean_session and request.status == "pending"
        ]

    async def create_selection(
        self,
        *,
        session_id: str,
        title: str,
        prompt: str,
        options: Sequence[AttentionOption],
        continuation: AttentionContinuation,
        ttl_s: float = 10 * 60,
        dedupe_key: str = "",
    ) -> dict[str, Any]:
        clean_session = str(session_id or "").strip()
        if not clean_session:
            raise ValueError("session_id is required")
        normalized = tuple(options)
        option_ids = [option.option_id for option in normalized]
        if len(normalized) < 2:
            raise ValueError("selection requests require at least two options")
        if any(not str(option_id or "").strip() for option_id in option_ids):
            raise ValueError("selection option ids are required")
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("selection option ids must be unique")
        if not callable(continuation):
            raise ValueError("selection continuation is required")

        clean_dedupe = str(dedupe_key or "").strip()
        if clean_dedupe:
            for request_id, request in list(self._requests.items()):
                if (
                    request.session_id == clean_session
                    and request.dedupe_key == clean_dedupe
                    and request.status == "pending"
                ):
                    self._remove(request_id, final_status="expired")

        now = float(self._clock())
        request = _AttentionRequest(
            request_id=f"attention_{uuid.uuid4().hex}",
            session_id=clean_session,
            kind="selection",
            title=str(title or "").strip(),
            prompt=str(prompt or "").strip(),
            options=normalized,
            created_at=now,
            expires_at=now + max(1.0, float(ttl_s)),
            continuation=continuation,
            dedupe_key=clean_dedupe,
        )
        self._requests[request.request_id] = request
        self._schedule_expiry(request)
        await self._publish(request.session_id, reason="attention.created")
        return request.public_dict()

    async def resolve(
        self,
        *,
        session_id: str,
        request_id: str,
        option_id: str,
    ) -> dict[str, Any]:
        self._expire_synchronously()
        clean_session = str(session_id or "").strip()
        request = self._requests.get(str(request_id or "").strip())
        if request is None:
            return {"ok": False, "error": "attention_request_not_found"}
        if request.session_id != clean_session:
            return {"ok": False, "error": "attention_session_mismatch"}
        if request.status != "pending":
            return {"ok": False, "error": "attention_request_not_pending"}
        chosen = next(
            (
                option
                for option in request.options
                if option.option_id == str(option_id or "").strip()
            ),
            None,
        )
        if chosen is None:
            return {"ok": False, "error": "attention_option_not_found"}

        # Claim before awaiting.  A duplicate click cannot invoke the original
        # operation twice even when the continuation is slow.
        request.status = "resolving"
        await self._publish(request.session_id, reason="attention.resolving")
        try:
            outcome = request.continuation(chosen.option_id)
            if inspect.isawaitable(outcome):
                outcome = await outcome
        except Exception as exc:
            request.status = "failed"
            self._cancel_expiry(request.request_id)
            # Failed continuations are terminal.  Keeping the closure in this
            # process after the card disappeared would leak user/task context
            # throughout a long voice Session without making retry possible.
            self._requests.pop(request.request_id, None)
            await self._publish(request.session_id, reason="attention.failed")
            return {
                "ok": False,
                "error": "attention_continuation_failed",
                "detail": f"{type(exc).__name__}: {exc}",
            }

        request.status = "resolved"
        self._cancel_expiry(request.request_id)
        self._requests.pop(request.request_id, None)
        await self._publish(request.session_id, reason="attention.resolved")
        return {
            "ok": True,
            "requestId": request.request_id,
            "optionId": chosen.option_id,
            "outcome": dict(outcome or {}),
            "requests": self.list_pending(request.session_id),
        }

    async def cancel_matching(self, *, session_id: str, dedupe_key: str) -> int:
        """Cancel stale pending requests superseded by a newer exact control."""

        clean_session = str(session_id or "").strip()
        clean_key = str(dedupe_key or "").strip()
        request_ids = [
            request_id
            for request_id, request in self._requests.items()
            if request.session_id == clean_session
            and request.dedupe_key == clean_key
            and request.status == "pending"
        ]
        for request_id in request_ids:
            self._remove(request_id, final_status="expired")
        if request_ids:
            await self._publish(clean_session, reason="attention.superseded")
        return len(request_ids)

    def _expire_synchronously(self) -> None:
        now = float(self._clock())
        for request_id, request in list(self._requests.items()):
            if request.status == "pending" and request.expires_at <= now:
                self._remove(request_id, final_status="expired")

    def _remove(self, request_id: str, *, final_status: AttentionStatus) -> None:
        request = self._requests.pop(request_id, None)
        if request is not None:
            request.status = final_status
        self._cancel_expiry(request_id)

    def _cancel_expiry(self, request_id: str) -> None:
        task = self._expiry_tasks.pop(request_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _schedule_expiry(self, request: _AttentionRequest) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def expire_later() -> None:
            delay = max(0.0, request.expires_at - float(self._clock()))
            await asyncio.sleep(delay)
            current = self._requests.get(request.request_id)
            if current is not request or current.status != "pending":
                return
            self._requests.pop(request.request_id, None)
            request.status = "expired"
            self._expiry_tasks.pop(request.request_id, None)
            await self._publish(request.session_id, reason="attention.expired")

        self._expiry_tasks[request.request_id] = loop.create_task(expire_later())

    async def _publish(self, session_id: str, *, reason: str) -> None:
        await bus.emit(
            Method.ATTENTION_UPDATED,
            {
                "sessionId": session_id,
                "requests": self.list_pending(session_id),
                "reason": reason,
            },
        )


attention_requests = AttentionRequestCoordinator()


def opaque_option_id() -> str:
    return f"option_{uuid.uuid4().hex}"
