from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

from agent_host.event_reactor import AgentEventReactor
from agent_host.provider_catalog import OPENCLAW_MANIFEST
from agent_host.provider_progress import (
    progress_payload,
    split_progress_milestones,
    split_progress_stream,
    with_progress_contract,
)
from agent_host.provider_identity import with_parent_conversation_context
from agent_host.provider_types import (
    EmitProviderEvent,
    ProviderEvent,
    ProviderRunRequest,
    ProviderRunResult,
    ProviderSessionHandle,
    ProviderSteerRequest,
)
from openclaw.client import OPENCLAW_EXECUTION_SYSTEM_PROMPT, _classify_openclaw_result
from openclaw.gateway_client import OpenClawGatewayClient, OpenClawGatewayError
from config.settings import OPENCLAW_BASE_URL, OPENCLAW_TOKEN


_REQUIRED_GATEWAY_METHODS = frozenset(
    {
        "agent.wait",
        "chat.history",
        "sessions.abort",
        "sessions.create",
        "sessions.send",
    }
)
_RECOVERABLE_TRANSPORT_CODES = frozenset({"CONNECTION_CLOSED", "CONNECTION_LOST"})
_OUTCOME_UNKNOWN_CODE = "OUTCOME_UNKNOWN"
_STEER_REPLACEMENT_CONTRACT = """[Amadeus steer replacement]
The instruction below replaces the unfinished portion of the previous request in
this Session. Preserve useful observations and page state, but do not finish or
report requirements that appeared only in the superseded request. Treat the
latest instruction as authoritative.
"""


@dataclass(slots=True)
class _OpenClawRunControl:
    session: ProviderSessionHandle
    client: OpenClawGatewayClient
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    native_ready: asyncio.Event = field(default_factory=asyncio.Event)
    native_run_id: str = ""
    native_run_ids: list[str] = field(default_factory=list)
    operation_task: asyncio.Task[str] | None = None
    pending: ProviderSteerRequest | None = None
    latest_revision: int = 0
    cancelled: bool = False
    transport_recoveries: int = 0


class OpenClawAdapter:
    provider_id = "openclaw"
    manifest = OPENCLAW_MANIFEST

    def __init__(
        self,
        *,
        gateway_client_factory: Callable[..., OpenClawGatewayClient] | None = None,
    ) -> None:
        self._gateway_client_factory = gateway_client_factory or OpenClawGatewayClient
        self._controls: dict[str, _OpenClawRunControl] = {}

    async def run(
        self,
        request: ProviderRunRequest,
        run_id: str,
        emit: EmitProviderEvent,
    ) -> ProviderRunResult:
        session = self._session_for_request(request)
        client = self._gateway_client_factory(
            base_url=OPENCLAW_BASE_URL,
            token=OPENCLAW_TOKEN,
        )
        control = _OpenClawRunControl(session=session, client=client)
        await client.connect()
        missing_methods = sorted(_REQUIRED_GATEWAY_METHODS - client.advertised_methods)
        if missing_methods:
            await client.close()
            raise OpenClawGatewayError(
                "OpenClaw Gateway lacks required session methods: "
                + ", ".join(missing_methods)
            )
        if request.session is not None:
            attached = await client.request(
                "sessions.get",
                {"key": session.session_id},
            )
            attached_messages = (
                attached.get("messages") if isinstance(attached, dict) else None
            )
            if not isinstance(attached_messages, list) or not attached_messages:
                await client.close()
                raise OpenClawGatewayError(
                    "attached OpenClaw session does not exist or has no history"
                )
        else:
            created = await client.request(
                "sessions.create",
                {
                    "key": session.session_id,
                    "agentId": "main",
                    "label": f"Amadeus {run_id}",
                },
            )
            created_key = str(
                created.get("key") if isinstance(created, dict) else ""
            ).strip()
            if created_key and created_key != session.session_id:
                await client.close()
                raise OpenClawGatewayError(
                    "OpenClaw created a different session identity"
                )

        self._controls[run_id] = control
        current_task = request.task
        current_metadata = dict(request.metadata)
        all_tool_names: set[str] = set()
        applied_revisions: list[int] = []
        current_revision = 0
        try:
            while True:
                loop = asyncio.get_running_loop()
                scheduled_emits: list[asyncio.Task[Any]] = []

                def schedule_emit(event: ProviderEvent) -> None:
                    scheduled_emits.append(loop.create_task(emit(event)))

                reactor = AgentEventReactor(
                    task=current_task,
                    source=self.provider_id,
                    run_id=run_id,
                )
                progress = _SemanticProgressEmitter(
                    provider_id=self.provider_id,
                    run_id=run_id,
                    emit=schedule_emit,
                )

                def tool_event_callback(event: dict[str, Any] | None) -> None:
                    if not event:
                        return
                    reactor.on_tool_event(event)
                    tool_name = str(event.get("type") or event.get("name") or "tool")
                    schedule_emit(
                        ProviderEvent(
                            provider=self.provider_id,
                            run_id=run_id,
                            type="tool.call",
                            payload={"tool": tool_name, "raw": event},
                        )
                    )
                    progress.feed_tool_event(event)

                def chunk_callback(text: str) -> None:
                    if not text:
                        return
                    visible = progress.feed(text)
                    if visible:
                        schedule_emit(
                            ProviderEvent(
                                provider=self.provider_id,
                                run_id=run_id,
                                type="assistant.delta",
                                payload={"text": visible},
                            )
                        )

                async def native_started_callback(_native_run_id: str) -> None:
                    if current_revision <= 0:
                        return
                    await emit(
                        ProviderEvent(
                            provider=self.provider_id,
                            run_id=run_id,
                            type="run.status",
                            payload={
                                "status": "running",
                                "stage": "steer_applied",
                                "revision": current_revision,
                                "safe_boundary": "confirmed_abort_then_same_session",
                            },
                            metadata=dict(current_metadata),
                        )
                    )

                async with control.lock:
                    control.native_run_id = ""
                    control.native_ready.clear()
                    call_task = asyncio.create_task(
                        self._run_gateway_turn(
                            control,
                            task=with_parent_conversation_context(
                                current_task,
                                metadata=current_metadata,
                                execution_provider=self.provider_id,
                            ),
                            revision=current_revision,
                            chunk_callback=chunk_callback,
                            tool_event_callback=tool_event_callback,
                            native_started_callback=native_started_callback,
                            timeout=float(current_metadata.get("timeout", 120.0)),
                            image_path=current_metadata.get("image_path"),
                            presentation_locale=current_metadata.get("presentation_locale"),
                        ),
                        name=f"openclaw-session:{run_id}",
                    )
                    control.operation_task = call_task

                boundary_interrupted = False
                outcome_unknown: OpenClawGatewayError | None = None
                try:
                    raw_result = await call_task
                except asyncio.CancelledError:
                    if asyncio.current_task() is not None and asyncio.current_task().cancelling():
                        raise
                    boundary_interrupted = True
                    raw_result = ""
                except OpenClawGatewayError as exc:
                    if exc.code != _OUTCOME_UNKNOWN_CODE:
                        raise
                    outcome_unknown = exc
                    raw_result = ""

                if not boundary_interrupted:
                    visible_tail = progress.flush()
                    if visible_tail:
                        schedule_emit(
                            ProviderEvent(
                                provider=self.provider_id,
                                run_id=run_id,
                                type="assistant.delta",
                                payload={"text": visible_tail},
                            )
                        )
                if scheduled_emits:
                    await asyncio.gather(*scheduled_emits)
                all_tool_names.update(reactor.tool_names())

                async with control.lock:
                    if control.operation_task is call_task:
                        control.operation_task = None
                    if outcome_unknown is not None:
                        # The accepted native run may still have side effects.
                        # Do not launch a queued replacement across an
                        # unconfirmed boundary.
                        control.pending = None
                        return ProviderRunResult(
                            status="orphaned",
                            error=str(outcome_unknown),
                            metadata={
                                "result_type": "transport_outcome_unknown",
                                "tool_names": sorted(all_tool_names),
                                "native_run_ids": list(control.native_run_ids),
                                "steer_revisions": applied_revisions,
                                "session_attached": request.session is not None,
                                "transport_recoveries": control.transport_recoveries,
                                "runtime_resumable": False,
                                "outcome_uncertainty": "provider_run_may_still_be_active",
                            },
                            session=session,
                        )
                    pending = control.pending
                    control.pending = None
                    if pending is not None and not control.cancelled:
                        current_task = pending.task
                        current_metadata = {
                            **dict(request.metadata),
                            **dict(pending.metadata or {}),
                        }
                        current_revision = pending.revision
                        applied_revisions.append(pending.revision)
                        continue
                    if boundary_interrupted:
                        # Runtime cancellation propagates through the parent
                        # task. A child-only cancellation without a queued
                        # replacement would otherwise manufacture completion.
                        raise asyncio.CancelledError

                visible_result, _milestones = split_progress_milestones(
                    str(raw_result or "")
                )
                reactor.on_complete(visible_result)
                result_type = _classify_openclaw_result(visible_result)
                return ProviderRunResult(
                    # Gateway lifecycle/agent.wait is the execution authority.
                    # Result prose may truthfully describe an error observed on
                    # a page; it must not retroactively turn a completed native
                    # run into a transport failure.
                    status="done",
                    result=visible_result,
                    metadata={
                        "result_type": result_type,
                        "tool_names": sorted(all_tool_names),
                        "native_run_ids": list(control.native_run_ids),
                        "steer_revisions": applied_revisions,
                        "session_attached": request.session is not None,
                        "transport_recoveries": control.transport_recoveries,
                    },
                    session=session,
                )
        finally:
            self._controls.pop(run_id, None)
            await client.close()

    async def steer(
        self,
        run_id: str,
        request: ProviderSteerRequest,
    ) -> dict[str, Any]:
        """Abort the exact native run, then continue in the same Session.

        The local OpenClaw 2026.3.x ``sessions.steer`` method starts a new run
        when the named session has already become idle. Exact abort-by-run-id
        provides the fence needed for truthful immediate steering.
        """

        control = self._controls.get(str(run_id or "").strip())
        if control is None:
            return {"accepted": False, "reason": "run_control_unavailable"}
        async with control.lock:
            if control.cancelled:
                return {"accepted": False, "reason": "run_cancelled"}
            if request.revision <= control.latest_revision:
                return {"accepted": False, "reason": "stale_revision"}
            if control.pending is not None and not control.native_run_id:
                control.pending = request
                control.latest_revision = request.revision
                return {
                    "accepted": True,
                    "safe_boundary": "confirmed_abort_then_same_session",
                    "coalesced": True,
                }
        try:
            await asyncio.wait_for(control.native_ready.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            async with control.lock:
                if control.pending is not None and not control.native_run_id:
                    control.pending = request
                    control.latest_revision = request.revision
                    return {
                        "accepted": True,
                        "safe_boundary": "confirmed_abort_then_same_session",
                        "coalesced": True,
                    }
            return {"accepted": False, "reason": "native_run_id_unavailable"}

        async with control.lock:
            if control.cancelled:
                return {"accepted": False, "reason": "run_cancelled"}
            if request.revision <= control.latest_revision:
                return {"accepted": False, "reason": "stale_revision"}
            # A replacement already owns the confirmed boundary but has not
            # started its successor request yet. Latest-wins can coalesce here
            # without sending a second native side effect.
            if control.pending is not None and not control.native_run_id:
                control.pending = request
                control.latest_revision = request.revision
                return {
                    "accepted": True,
                    "safe_boundary": "confirmed_abort_then_same_session",
                    "coalesced": True,
                }
            native_run_id = control.native_run_id
            if not native_run_id:
                return {"accepted": False, "reason": "native_run_id_unavailable"}
            outcome = await control.client.request(
                "sessions.abort",
                {
                    "key": control.session.session_id,
                    "runId": native_run_id,
                },
            )
            aborted_run_id = str(
                outcome.get("abortedRunId") if isinstance(outcome, dict) else ""
            ).strip()
            if aborted_run_id != native_run_id:
                return {
                    "accepted": False,
                    "reason": "native_run_not_active",
                }
            control.pending = request
            control.latest_revision = request.revision
            control.native_run_id = ""
            control.native_ready.clear()
            if control.operation_task is not None and not control.operation_task.done():
                control.operation_task.cancel()
            return {
                "accepted": True,
                "safe_boundary": "confirmed_abort_then_same_session",
                "native_run_id": native_run_id,
            }

    async def cancel(self, run_id: str) -> dict[str, Any]:
        control = self._controls.get(str(run_id or "").strip())
        if control is None:
            return {
                "confirmed": False,
                "cancelled": False,
                "reason": "run_control_unavailable",
            }
        async with control.lock:
            if control.pending is not None and not control.native_run_id:
                control.pending = None
                control.cancelled = True
                return {
                    "confirmed": True,
                    "cancelled": True,
                    "reason": "successor_not_started",
                }
        try:
            await asyncio.wait_for(control.native_ready.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            async with control.lock:
                if control.pending is not None and not control.native_run_id:
                    control.pending = None
                    control.cancelled = True
                    return {
                        "confirmed": True,
                        "cancelled": True,
                        "reason": "successor_not_started",
                    }
            return {
                "confirmed": False,
                "cancelled": False,
                "reason": "native_run_id_unavailable",
            }

        async with control.lock:
            native_run_id = control.native_run_id
            if not native_run_id:
                return {
                    "confirmed": False,
                    "cancelled": False,
                    "reason": "native_run_id_unavailable",
                }
            outcome = await control.client.request(
                "sessions.abort",
                {
                    "key": control.session.session_id,
                    "runId": native_run_id,
                },
            )
            aborted_run_id = str(
                outcome.get("abortedRunId") if isinstance(outcome, dict) else ""
            ).strip()
            confirmed = aborted_run_id == native_run_id
            if confirmed:
                control.cancelled = True
                control.pending = None
            return {
                "confirmed": confirmed,
                "cancelled": confirmed,
                "native_run_id": native_run_id,
                **({"reason": "native_run_not_active"} if not confirmed else {}),
            }

    @classmethod
    def _session_for_request(
        cls,
        request: ProviderRunRequest,
    ) -> ProviderSessionHandle:
        if request.session is not None:
            if request.session.provider != cls.provider_id:
                raise ValueError("OpenClaw cannot attach another provider's session")
            return request.session
        work = request.metadata.get("work") if isinstance(request.metadata.get("work"), dict) else {}
        scope = "work_item" if str(work.get("work_item_id") or "").strip() else "attempt"
        return ProviderSessionHandle(
            provider=cls.provider_id,
            session_id=f"agent:main:dashboard:amadeus-{uuid.uuid4().hex}",
            scope=scope,
        )

    async def _run_gateway_turn(
        self,
        control: _OpenClawRunControl,
        *,
        task: str,
        revision: int,
        chunk_callback,
        tool_event_callback,
        native_started_callback,
        timeout: float,
        image_path: str | None,
        presentation_locale: object = None,
    ) -> str:
        """Send one Session message and normalize its Gateway event stream."""

        idempotency_key = f"amadeus-{uuid.uuid4().hex}"
        task_contract = with_progress_contract(
            task,
            presentation_locale=presentation_locale,
        )
        if revision > 0:
            task_contract = f"{_STEER_REPLACEMENT_CONTRACT}\n{task_contract}"
        params: dict[str, Any] = {
            "key": control.session.session_id,
            "message": (
                f"{OPENCLAW_EXECUTION_SYSTEM_PROMPT}\n\n"
                f"{task_contract}"
            ),
            "timeoutMs": max(1, int(timeout * 1000)),
            "idempotencyKey": idempotency_key,
        }
        attachments = self._image_attachments(image_path)
        if attachments:
            params["attachments"] = attachments
        try:
            response = await control.client.request(
                "sessions.send",
                params,
                timeout=min(30.0, max(5.0, timeout)),
            )
        except OpenClawGatewayError as transport_error:
            if transport_error.code not in _RECOVERABLE_TRANSPORT_CODES:
                raise
            # The request crossed the socket boundary, but no acknowledgement
            # arrived.  Re-sending might execute the task twice; preserve the
            # Session and expose uncertainty instead.
            raise OpenClawGatewayError(
                "OpenClaw connection was lost while task acceptance was being "
                "confirmed. The task was not replayed.",
                code=_OUTCOME_UNKNOWN_CODE,
                details={
                    "session_id": control.session.session_id,
                    "idempotency_key": idempotency_key,
                    "transport_error": str(transport_error),
                },
            ) from transport_error
        native_run_id = str(
            response.get("runId") if isinstance(response, dict) else ""
        ).strip()
        if not native_run_id:
            raise OpenClawGatewayError("OpenClaw sessions.send returned no run id")
        control.native_run_id = native_run_id
        if native_run_id not in control.native_run_ids:
            control.native_run_ids.append(native_run_id)
        control.native_ready.set()
        await native_started_callback(native_run_id)

        wait_task = asyncio.create_task(
            control.client.request(
                "agent.wait",
                {"runId": native_run_id, "timeoutMs": max(1, int(timeout * 1000))},
                timeout=timeout + 5.0,
            ),
            name=f"openclaw-wait:{native_run_id}",
        )
        visible_text = ""
        terminal_state = ""
        deadline = time.monotonic() + timeout + 5.0
        try:
            while time.monotonic() < deadline:
                if wait_task.done() and terminal_state:
                    break
                remaining = max(0.01, deadline - time.monotonic())
                try:
                    frame = await control.client.next_event(
                        timeout=min(0.5, remaining)
                    )
                except OpenClawGatewayError as exc:
                    if exc.code == "EVENT_TIMEOUT":
                        if wait_task.done():
                            break
                        continue
                    raise
                payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
                if str(payload.get("runId") or "") != native_run_id:
                    continue
                event_name = str(frame.get("event") or "")
                if event_name == "agent":
                    stream = str(payload.get("stream") or "")
                    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                    if stream == "tool":
                        tool_event_callback(data)
                    elif stream == "lifecycle":
                        phase = str(data.get("phase") or "").strip().lower()
                        if phase in {"end", "error"}:
                            terminal_state = phase
                    continue
                if event_name != "chat":
                    continue
                state = str(payload.get("state") or "").strip().lower()
                text = self._gateway_message_text(payload.get("message"))
                if text:
                    if text.startswith(visible_text):
                        delta = text[len(visible_text) :]
                    elif visible_text.startswith(text):
                        delta = ""
                    else:
                        delta = text
                    if delta:
                        chunk_callback(delta)
                    visible_text = text
                if state in {"final", "error", "aborted"}:
                    terminal_state = state
                    if wait_task.done():
                        break

            wait_result = await wait_task
            wait_status = str(
                wait_result.get("status") if isinstance(wait_result, dict) else ""
            ).strip().lower()
            if terminal_state in {"error", "aborted"} or wait_status in {
                "error",
                "aborted",
            }:
                raise OpenClawGatewayError(
                    f"OpenClaw session turn ended as {terminal_state or wait_status}"
                )
            if not visible_text:
                history = await control.client.request(
                    "chat.history",
                    {"sessionKey": control.session.session_id, "limit": 8},
                )
                visible_text = self._latest_assistant_text(history)
            return visible_text
        except OpenClawGatewayError as transport_error:
            if transport_error.code not in _RECOVERABLE_TRANSPORT_CODES:
                raise
            try:
                recovered_text = await self._reconcile_gateway_turn(
                    control,
                    native_run_id=native_run_id,
                    deadline=deadline,
                )
            except asyncio.CancelledError:
                raise
            except Exception as recovery_error:
                raise OpenClawGatewayError(
                    "OpenClaw connection was lost after the task was accepted, "
                    "and its outcome could not be confirmed. The task was not replayed.",
                    code=_OUTCOME_UNKNOWN_CODE,
                    details={
                        "session_id": control.session.session_id,
                        "native_run_id": native_run_id,
                        "transport_error": str(transport_error),
                        "recovery_error": str(recovery_error),
                    },
                ) from recovery_error
            control.transport_recoveries += 1
            if recovered_text:
                if recovered_text.startswith(visible_text):
                    delta = recovered_text[len(visible_text) :]
                elif visible_text.startswith(recovered_text):
                    delta = ""
                else:
                    delta = recovered_text
                if delta:
                    chunk_callback(delta)
            return recovered_text
        finally:
            if not wait_task.done():
                wait_task.cancel()
                try:
                    await wait_task
                except asyncio.CancelledError:
                    pass

    async def _reconcile_gateway_turn(
        self,
        control: _OpenClawRunControl,
        *,
        native_run_id: str,
        deadline: float,
    ) -> str:
        """Recover observation of one accepted run without submitting it again."""

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OpenClawGatewayError(
                "OpenClaw task deadline elapsed before transport recovery",
                code="TIMEOUT",
            )
        async with control.lock:
            if control.cancelled:
                raise asyncio.CancelledError
            await control.client.connect()
            missing_methods = sorted(
                {"agent.wait", "chat.history"} - control.client.advertised_methods
            )
            if missing_methods:
                raise OpenClawGatewayError(
                    "OpenClaw Gateway cannot reconcile an accepted run: "
                    + ", ".join(missing_methods)
                )

        remaining = max(0.1, deadline - time.monotonic())
        wait_result = await control.client.request(
            "agent.wait",
            {
                "runId": native_run_id,
                "timeoutMs": max(1, int(remaining * 1000)),
            },
            timeout=remaining + 1.0,
        )
        wait_status = str(
            wait_result.get("status") if isinstance(wait_result, dict) else ""
        ).strip().lower()
        if wait_status in {"error", "aborted"}:
            raise OpenClawGatewayError(
                f"OpenClaw session turn ended as {wait_status}"
            )
        history = await control.client.request(
            "chat.history",
            {"sessionKey": control.session.session_id, "limit": 8},
        )
        recovered_text = self._latest_assistant_text(history)
        if not recovered_text:
            raise OpenClawGatewayError(
                "OpenClaw reported a completed run without an assistant result"
            )
        return recovered_text

    @staticmethod
    def _gateway_message_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(OpenClawAdapter._gateway_message_text(item) for item in value)
        if not isinstance(value, dict):
            return ""
        for key in ("text", "delta"):
            if isinstance(value.get(key), str):
                return str(value[key])
        return OpenClawAdapter._gateway_message_text(value.get("content"))

    @classmethod
    def _latest_assistant_text(cls, history: Any) -> str:
        messages = history.get("messages") if isinstance(history, dict) else []
        for message in reversed(messages if isinstance(messages, list) else []):
            if isinstance(message, dict) and str(message.get("role") or "") == "assistant":
                text = cls._gateway_message_text(message.get("content") or message)
                if text:
                    return text
        return ""

    @staticmethod
    def _image_attachments(image_path: str | None) -> list[dict[str, Any]]:
        path = str(image_path or "").strip()
        if not path or not os.path.isfile(path):
            return []
        extension = os.path.splitext(path)[1].lower().lstrip(".")
        mime = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "webp": "image/webp",
            "gif": "image/gif",
        }.get(extension, "image/png")
        with open(path, "rb") as handle:
            content = base64.b64encode(handle.read()).decode("ascii")
        return [
            {
                "type": "image",
                "mimeType": mime,
                "fileName": os.path.basename(path),
                "content": content,
            }
        ]


class _SemanticProgressEmitter:
    """Extract task-content progress from OpenClaw assistant text.

    OpenClaw streams ordinary assistant text. Amadeus still needs a narrower
    signal for Kurisu's observer lane: content progress, not raw tool narration.
    The shared task contract asks for three typed progress lines; this class
    also keeps a conservative heuristic fallback for natural progress sentences.
    """

    _SENTENCE_END = re.compile(r"([。！？!?]+(?:\s+|$)|[.!?]+\s+|\n+)")
    _MEANINGFUL_PATTERNS = (
        "found",
        "identified",
        "confirmed",
        "learned",
        "discovered",
        "filtered",
        "compared",
        "summarized",
        "extracted",
        "selected",
        "returned a stub",
        "returned a placeholder",
        "rate-limited",
        "rate limited",
        "results from",
        "what i learned",
        "quota",
        "blocked",
        "failed",
        "error",
        "找到",
        "发现",
        "确认",
        "筛选",
        "整理",
        "总结",
        "提取",
        "比较",
        "候选",
        "来源",
        "资料",
        "受限",
        "失败",
        "报错",
    )
    _MECHANICAL_PATTERNS = (
        "let me try",
        "i will try",
        "i'm going to",
        "fetching ",
        "opening ",
        "checking ",
        "running ",
        "让我",
        "我会尝试",
        "正在打开",
        "正在调用",
        "正在运行",
        "开始查询",
    )


    def __init__(self, *, provider_id: str, run_id: str, emit) -> None:
        self.provider_id = provider_id
        self.run_id = run_id
        self._emit = emit
        self._buffer = ""
        self._progress_line_buffer = ""
        self._last_emit_at = 0.0
        self._seen: set[str] = set()

    def feed(self, text: str) -> str:
        visible, milestones, pending = split_progress_stream(
            self._progress_line_buffer,
            text,
        )
        self._progress_line_buffer = pending
        self._emit_contract_milestones(milestones)
        self._feed_visible_text(visible)
        return visible

    def _feed_visible_text(self, text: str) -> None:
        self._buffer += str(text or "")
        while True:
            match = self._SENTENCE_END.search(self._buffer)
            if not match:
                break
            end = match.end()
            sentence = self._buffer[:end].strip()
            self._buffer = self._buffer[end:]
            self._maybe_emit(sentence)

    def flush(self) -> str:
        visible, milestones, pending = split_progress_stream(
            self._progress_line_buffer,
            "",
            final=True,
        )
        self._progress_line_buffer = pending
        self._emit_contract_milestones(milestones)
        self._feed_visible_text(visible)
        sentence = self._buffer.strip()
        self._buffer = ""
        if sentence:
            self._maybe_emit(sentence, force=True)
        return visible

    def _emit_contract_milestones(self, milestones: list[dict[str, Any]]) -> None:
        for milestone in milestones:
            self._emit_progress(
                str(milestone.get("summary") or ""),
                source="openclaw_provider_progress",
                explicit=True,
                force=True,
                milestone=str(milestone.get("milestone") or ""),
                verified=False,
            )

    def feed_tool_event(self, event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            return
        phase = str(event.get("phase") or "").lower()
        if phase != "result":
            return
        tool_name = str(event.get("name") or event.get("type") or "tool").strip().lower()
        summary = ""
        if bool(event.get("isError")):
            summary = self._tool_error_summary(tool_name, event)
        elif tool_name in {"web_fetch", "web_search"}:
            summary = self._web_tool_summary(tool_name, event)
        if summary:
            self._emit_progress(
                summary,
                source=f"openclaw_tool_result:{tool_name or 'tool'}",
                explicit=True,
                force=True,
                milestone="validation",
                verified=True,
            )

    def _maybe_emit(self, sentence: str, *, force: bool = False) -> None:
        summary = self._clean(sentence)
        if not summary:
            return
        if not self._looks_semantic(summary):
            return
        key = summary.lower()
        if key in self._seen:
            return
        now = time.monotonic()
        if not force and now - self._last_emit_at < 4.0:
            return
        self._seen.add(key)
        self._last_emit_at = now
        self._emit(
            ProviderEvent(
                provider=self.provider_id,
                run_id=self.run_id,
                type="assistant.update",
                payload={
                    "text": summary,
                    "source": "openclaw_assistant_update",
                },
            )
        )

    def _emit_progress(
        self,
        summary: str,
        *,
        source: str,
        explicit: bool,
        force: bool,
        milestone: str,
        verified: bool,
    ) -> None:
        cleaned = self._clean(summary)
        if not cleaned:
            return
        key = cleaned.lower()
        if key in self._seen:
            return
        now = time.monotonic()
        if not explicit and not force and now - self._last_emit_at < 4.0:
            return
        self._seen.add(key)
        self._last_emit_at = now
        self._emit(
            ProviderEvent(
                provider=self.provider_id,
                run_id=self.run_id,
                type="semantic.progress",
                payload=progress_payload(
                    milestone,
                    cleaned,
                    source=source,
                    explicit=explicit,
                    verified=verified,
                ),
            )
        )

    def _tool_error_summary(self, tool_name: str, event: dict[str, Any]) -> str:
        text = self._event_text(event)
        lowered = text.lower()
        if "quota" in lowered or "429" in lowered or "rate" in lowered:
            return "Search quota is limited, so the run is falling back to direct source checks."
        brief = self._brief_text(text or str(event.get("meta") or ""), 160)
        if not brief:
            return ""
        return f"{tool_name or 'tool'} returned an error that may affect this run: {brief}"

    def _web_tool_summary(self, tool_name: str, event: dict[str, Any]) -> str:
        if tool_name == "web_search":
            query = ""
            args = event.get("args") if isinstance(event.get("args"), dict) else {}
            if isinstance(args, dict):
                query = str(args.get("query") or "").strip()
            if not query:
                query = self._meta_query(event)
            if query:
                return f"Search returned candidate sources for: {query}"
            return "Search returned candidate sources for the current task."

        wrapper = self._json_wrapper(event)
        url = self._event_url(event, wrapper)
        if self._looks_like_placeholder(wrapper):
            label = self._host_label(url) or self._source_label(url, wrapper)
            return f"Checked {label}; it looked like a placeholder response, so it may not be useful evidence."
        label = self._source_label(url, wrapper)
        if label:
            return f"Checked source {label}; readable content is available for the task comparison."
        return ""

    def _json_wrapper(self, event: dict[str, Any]) -> dict[str, Any]:
        text = self._event_text(event)
        if not text:
            return {}
        try:
            data = json.loads(text)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _event_text(self, event: dict[str, Any]) -> str:
        result = event.get("result")
        if not isinstance(result, dict):
            return ""
        content = result.get("content")
        if not isinstance(content, list):
            return ""
        texts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(str(item.get("text") or ""))
        return "\n".join(texts).strip()

    def _event_url(self, event: dict[str, Any], wrapper: dict[str, Any]) -> str:
        for key in ("finalUrl", "url"):
            value = str(wrapper.get(key) or "").strip()
            if value:
                return value
        args = event.get("args") if isinstance(event.get("args"), dict) else {}
        value = str(args.get("url") or "").strip() if isinstance(args, dict) else ""
        if value:
            return value
        meta = str(event.get("meta") or "")
        match = re.search(r"https?://[^\s)]+", meta)
        return match.group(0) if match else ""

    def _source_label(self, url: str, wrapper: dict[str, Any]) -> str:
        title = self._extract_title(wrapper)
        host = self._host_label(url)
        if title and host:
            return f"{title} ({host})"
        if title:
            return title
        return host or url

    def _host_label(self, url: str) -> str:
        if not url:
            return ""
        parsed = urlparse(url)
        return (parsed.netloc or "").removeprefix("www.")

    def _extract_title(self, wrapper: dict[str, Any]) -> str:
        text = self._external_body(str(wrapper.get("text") or ""))
        match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.I | re.S)
        if match:
            return self._brief_text(re.sub(r"\s+", " ", match.group(1)).strip(), 80)
        title = self._first_content_line(text)
        if title:
            return title
        for key in ("title", "pageTitle"):
            raw_title = self._external_body(str(wrapper.get(key) or ""))
            title = self._first_content_line(raw_title) or raw_title.strip()
            if title and "external_untrusted_content" not in title.lower():
                return self._brief_text(title, 80)
        return ""

    def _first_content_line(self, text: str) -> str:
        for line in text.splitlines():
            stripped = line.strip().strip("#").strip()
            lowered = stripped.lower()
            if not stripped:
                continue
            if (
                lowered.startswith("security notice")
                or lowered.startswith("source:")
                or "external_untrusted_content" in lowered
                or lowered.startswith("- do not")
                or lowered.startswith("- respond")
                or stripped.startswith("{")
                or lowered.startswith("%pdf-")
            ):
                continue
            if 8 <= len(stripped) <= 90:
                return self._brief_text(stripped, 80)
        return ""

    def _external_body(self, text: str) -> str:
        match = re.search(
            r"Source:\s*Web Fetch\s*---\s*(.*?)(?:<<<END_EXTERNAL_UNTRUSTED_CONTENT|$)",
            text,
            flags=re.I | re.S,
        )
        if match:
            return match.group(1).strip()
        return text

    def _looks_like_placeholder(self, wrapper: dict[str, Any]) -> bool:
        text = str(wrapper.get("text") or "").lower()
        url = str(wrapper.get("url") or "").lower()
        return "duckduckgo.com" in url and (
            "just another test" in text
            or '"abstracttext": ""' in text
            or '"relatedtopics":[]' in text.replace(" ", "")
        )

    def _meta_query(self, event: dict[str, Any]) -> str:
        meta = str(event.get("meta") or "")
        match = re.search(r'for\s+"([^"]+)"', meta)
        return match.group(1).strip() if match else ""

    def _brief_text(self, text: str, limit: int) -> str:
        cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: max(0, limit - 3)].rstrip() + "..."

    def _looks_semantic(self, text: str) -> bool:
        lowered = text.lower()
        if len(text) < 36:
            return False
        if any(item in lowered for item in self._MEANINGFUL_PATTERNS):
            return True
        if any(item in lowered for item in self._MECHANICAL_PATTERNS):
            return False
        return False

    def _clean(self, text: str) -> str:
        cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
        if len(cleaned) > 260:
            cleaned = cleaned[:257].rstrip() + "..."
        return cleaned
