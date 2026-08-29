"""Host-owned scheduling for AUIP observation and Participant turns.

This coordinator owns *when* the separate Participant lane may propose an
action.  It never chooses an application action itself and never converts a
proposal into experience truth; those responsibilities remain with the
Participant controller and the AUIP runtime/app receipt respectively.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from server.auip_contract import AuipProtocolError
from server.auip_participant import (
    AuipParticipantController,
    AuipParticipantCoordinator,
    AuipParticipantProposal,
    ControllerCallable,
)
from server.auip_runtime import PENDING_ACTION_TIMEOUT_S, AuipRuntime, runtime
from server.event_bus import bus
from server.protocol import Method


logger = logging.getLogger(__name__)
RecentChat = Callable[[str], list[dict[str, str]]]
BusyProbe = Callable[[], bool]
RoleAuthorizer = Callable[
    [dict[str, Any]],
    Mapping[str, Any] | Awaitable[Mapping[str, Any]],
]


class AuipEngagementCoordinator:
    """Schedule at most one Participant decision per active AppSession."""

    def __init__(
        self,
        *,
        app_runtime: AuipRuntime | None = None,
        participant: AuipParticipantCoordinator | None = None,
        controller: AuipParticipantController | ControllerCallable | None = None,
        role_authorizer: RoleAuthorizer | None = None,
        controller_id: str = "auip-participant",
        recent_chat: RecentChat | None = None,
        is_chat_busy: BusyProbe | None = None,
        receipt_timeout_s: float | None = None,
        b2_coordinator: Any | None = None,
    ) -> None:
        self.runtime = app_runtime or runtime
        self.participant = participant or AuipParticipantCoordinator(self.runtime)
        self.controller = controller
        self.role_authorizer = role_authorizer
        self.controller_id = str(controller_id or "auip-participant")[:120]
        self.recent_chat = recent_chat or (lambda _conversation_id: [])
        self.is_chat_busy = is_chat_busy or (lambda: False)
        self.receipt_timeout_s = max(
            0.01,
            float(
                PENDING_ACTION_TIMEOUT_S + 0.1
                if receipt_timeout_s is None
                else receipt_timeout_s
            ),
        )
        self.b2_coordinator = b2_coordinator
        self._tasks: dict[str, asyncio.Task] = {}
        self._task_reasons: dict[str, str] = {}
        self._deferred_automatic_tasks: dict[str, asyncio.Task] = {}
        self._deferred_automatic_events: dict[
            str, tuple[str, str, bool, int | None]
        ] = {}
        self._receipt_tasks: dict[str, asyncio.Task] = {}
        self._seen_events: deque[str] = deque(maxlen=256)
        self._seen_event_set: set[str] = set()
        self._seen_receipts: deque[str] = deque(maxlen=256)
        self._seen_receipt_set: set[str] = set()

    def set_b2_coordinator(self, coordinator: Any | None) -> None:
        """Inject the default-off B2 owner after bootstrap dependencies exist."""

        self.b2_coordinator = coordinator

    async def set_mode(self, *, app_session_id: str, mode: str) -> dict[str, Any]:
        clean_requested = str(mode or "").strip().lower()
        if clean_requested == "delegate" and (
            self.controller is None or self.role_authorizer is None
        ):
            raise AuipProtocolError("participant_controller_unavailable")
        result = self.runtime.set_engagement_mode(
            app_session_id=app_session_id,
            mode=clean_requested,
        )
        clean = str(result.get("engagement_mode") or "observe")
        if clean != "delegate":
            self._cancel_deferred_automatic(app_session_id)
            self._cancel_decision(app_session_id)
        elif bool(result.get("changed")):
            self.request_step(
                app_session_id=app_session_id,
                instruction="Begin autonomous participation when the accepted application state makes one declared action appropriate.",
                reason="delegate_mode_entered",
            )
        return result

    def request_step(
        self,
        *,
        app_session_id: str,
        instruction: str = "",
        reason: str = "explicit_step",
        expected_revision: int | None = None,
        current_role_response: str = "",
    ) -> dict[str, Any]:
        if str(reason or "") == "explicit_step":
            self._cancel_deferred_automatic(app_session_id)
        if self.controller is None or self.role_authorizer is None:
            raise AuipProtocolError("participant_controller_unavailable")
        projection = self.runtime.get(app_session_id)
        if str(projection.get("status") or "") != "active":
            raise AuipProtocolError("session_not_active")
        # Revision zero means registration succeeded but no application state
        # has been accepted. Starting a Participant turn here can only create
        # a stale request if the app's initial publication failed locally.
        if int(projection.get("revision") or 0) <= 0:
            raise AuipProtocolError("app_state_not_ready")
        if projection.get("pending_action"):
            raise AuipProtocolError("action_already_pending")
        if (
            expected_revision is not None
            and int(projection.get("revision") or 0) != int(expected_revision)
        ):
            raise AuipProtocolError("participant_revision_changed")
        if str(projection.get("engagement_mode") or "observe") == "observe":
            projection = self.runtime.set_engagement_mode(
                app_session_id=app_session_id,
                mode="collaborate",
            )
        current = self._tasks.get(app_session_id)
        if current is not None and not current.done():
            if str(reason or "") != "explicit_step":
                return {
                    "ok": True,
                    "scheduled": False,
                    "reason": "participant_decision_in_flight",
                    **projection,
                }
            # A user/role directive may replace an autonomous proposal only
            # while it is still private model work.  ``pending_action`` was
            # checked above, so no app request or accepted fact is being
            # revoked and no second action can be emitted.
            current.cancel()
            superseded_in_flight = True
        else:
            superseded_in_flight = False
        generation = int(projection.get("decision_generation") or 0)
        task = asyncio.create_task(
            self._run_step(
                app_session_id=app_session_id,
                generation=generation,
                instruction=str(instruction or "").strip()[:800],
                reason=str(reason or "participant_step")[:80],
                expected_revision=expected_revision,
                current_role_response=str(current_role_response or "").strip()[-1600:],
            ),
            name=f"auip-participant:{app_session_id}",
        )
        self._tasks[app_session_id] = task
        self._task_reasons[app_session_id] = str(reason or "participant_step")
        task.add_done_callback(
            lambda done, sid=app_session_id: self._forget_task(sid, done)
        )
        return {
            "ok": True,
            "scheduled": True,
            "reason": reason,
            "superseded_in_flight": superseded_in_flight,
            **projection,
        }

    async def on_update(self, _method: str, payload: dict[str, Any]) -> None:
        """Schedule only accepted events authorized by the engagement policy.

        Delegate mode may react to ordinary declared semantic beats.  In
        collaborate mode the app must additionally declare that the event is
        a participant decision opportunity; a beat alone never grants a turn.
        """

        app_session_id = str(payload.get("app_session_id") or "").strip()
        receipt = payload.get("receipt") if isinstance(payload.get("receipt"), dict) else None
        if receipt is not None and app_session_id:
            self._cancel_receipt_watch(app_session_id)
            receipt_id = str(receipt.get("action_id") or "").strip()
            receipt_key = f"{app_session_id}:{receipt_id}"
            if receipt_id and receipt_key not in self._seen_receipt_set:
                self._remember_receipt(receipt_key)
        if app_session_id and str(payload.get("status") or "") != "active":
            self._cancel_receipt_watch(app_session_id)
        event = payload.get("event") if isinstance(payload.get("event"), dict) else None
        if event is None:
            return
        if not app_session_id:
            return
        event_id = str(event.get("event_id") or "").strip()
        event_key = f"{app_session_id}:{event_id}"
        if not event_id or event_key in self._seen_event_set:
            return
        self._remember_event(event_key)
        if str(event.get("actor") or "").strip().lower() == "kurisu":
            return
        if bool(event.get("terminal")):
            return
        try:
            projection = self.runtime.get(app_session_id)
            mode = str(projection.get("engagement_mode") or "")
            if mode not in {"collaborate", "delegate"}:
                return
            controller = (
                projection.get("controller")
                if isinstance(projection.get("controller"), dict)
                else {}
            )
            if str(controller.get("status") or "idle") in {
                "active",
                "stopping",
                "blocked",
            }:
                # Reactive policy already owns app-local timing. Automatic
                # Decision turns would create a competing execution loop.
                # An explicit user instruction may still request replacement.
                return
            if mode == "collaborate" and event.get("participant_opportunity") is not True:
                return
            if mode == "delegate" and not (
                event.get("participant_opportunity") is True
                or bool(event.get("beat"))
                or str(event.get("importance") or "") in {"important", "blocking"}
            ):
                return
            if self._chat_is_busy():
                # A confirmed user turn owns the character now. Do not start
                # a background proposal while the role is interpreting newer
                # user intent. Retain only the latest accepted opportunity and
                # reconsider it once Chat is idle. An explicit step, mode
                # change, or confirmed new user turn can still cancel this
                # private work before it crosses the application boundary.
                self._defer_automatic_event(
                    app_session_id=app_session_id,
                    mode=mode,
                    event_type=str(event.get("type") or "event"),
                    participant_opportunity=(
                        event.get("participant_opportunity") is True
                    ),
                    event_revision=(
                        int(event.get("revision"))
                        if isinstance(event.get("revision"), int)
                        and not isinstance(event.get("revision"), bool)
                        else None
                    ),
                )
                return
            if projection.get("pending_action"):
                return
            event_type = str(event.get("type") or "event")
            instruction = (
                f"Use the accepted participant opportunity {event_type} to choose one appropriate declared action."
                if mode == "collaborate"
                else f"React to accepted semantic event {event_type} when an action is appropriate."
            )
            self.request_step(
                app_session_id=app_session_id,
                instruction=instruction,
                reason=(
                    "collaborate_participant_opportunity"
                    if mode == "collaborate"
                    else (
                        "delegate_participant_opportunity"
                        if event.get("participant_opportunity") is True
                        else "delegate_semantic_beat"
                    )
                ),
            )
        except AuipProtocolError:
            logger.debug("AUIP automatic participant step was not eligible", exc_info=True)

    async def interrupt_for_user_turn(self, conversation_id: str) -> bool:
        """Cancel one still-private automatic decision before a Chat turn.

        Once ``pending_action`` exists, the request crossed the application
        authority boundary and cannot be revoked here. Before that boundary,
        a newer explicit user turn supersedes background model work.
        """

        projection = self.runtime.focused_projection(str(conversation_id or ""))
        if not isinstance(projection, dict):
            return False
        app_session_id = str(projection.get("app_session_id") or "")
        deferred_cancelled = self._cancel_deferred_automatic(app_session_id)
        task = self._tasks.get(app_session_id)
        reason = self._task_reasons.get(app_session_id, "")
        if (
            task is None
            or task.done()
            or projection.get("pending_action")
            or reason == "explicit_step"
        ):
            return deferred_cancelled
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        latest = self.runtime.get(app_session_id)
        if (
            str(latest.get("status") or "") == "active"
            and not latest.get("pending_action")
            and str(latest.get("operator_status") or "") == "thinking"
        ):
            latest = self.runtime.set_operator_status(
                app_session_id=app_session_id,
                status="idle",
                expected_decision_generation=int(
                    latest.get("decision_generation") or 0
                ),
            )
            await bus.emit(Method.AUIP_UPDATED, _public_update(latest))
        return True

    def _chat_is_busy(self) -> bool:
        try:
            return bool(self.is_chat_busy())
        except Exception:
            logger.exception("AUIP chat-busy probe failed")
            return True

    async def leave(self, *, app_session_id: str, reason: str = "user_left") -> dict[str, Any]:
        self._cancel_deferred_automatic(app_session_id)
        self._cancel_decision(app_session_id)
        return self.runtime.host_leave(app_session_id=app_session_id, reason=reason)

    async def wait_for_idle(self, app_session_id: str) -> None:
        session_id = str(app_session_id or "")
        while True:
            deferred = self._deferred_automatic_tasks.get(session_id)
            task = deferred or self._tasks.get(session_id)
            if task is None:
                return
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                return
            if deferred is task:
                self._forget_deferred_automatic(session_id, task)
            else:
                self._forget_task(session_id, task)

    async def close(self) -> None:
        tasks = [
            task
            for task in (
                *self._tasks.values(),
                *self._deferred_automatic_tasks.values(),
                *self._receipt_tasks.values(),
            )
            if not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._task_reasons.clear()
        self._deferred_automatic_tasks.clear()
        self._deferred_automatic_events.clear()
        self._receipt_tasks.clear()

    def _defer_automatic_event(
        self,
        *,
        app_session_id: str,
        mode: str,
        event_type: str,
        participant_opportunity: bool,
        event_revision: int | None,
    ) -> None:
        """Coalesce busy-Chat automatic opportunities into one later decision.

        The application event remains an accepted fact while the speaking role
        owns the foreground. Retaining only the latest semantic type avoids
        losing an assigned turn without replaying a backlog into several
        actions when the voice/chat lane becomes idle.
        """

        session_id = str(app_session_id or "")
        self._deferred_automatic_events[session_id] = (
            str(mode or "")[:40],
            str(event_type or "event")[:120],
            bool(participant_opportunity),
            event_revision,
        )
        current = self._deferred_automatic_tasks.get(session_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._run_deferred_automatic_event(session_id),
            name=f"auip-deferred-automatic:{session_id}",
        )
        self._deferred_automatic_tasks[session_id] = task
        task.add_done_callback(
            lambda done, sid=session_id: self._forget_deferred_automatic(sid, done)
        )

    async def _run_deferred_automatic_event(self, app_session_id: str) -> None:
        while self._chat_is_busy():
            await asyncio.sleep(0.05)
        (
            mode,
            event_type,
            participant_opportunity,
            event_revision,
        ) = self._deferred_automatic_events.pop(
            app_session_id,
            ("", "event", False, None),
        )
        try:
            projection = self.runtime.get(app_session_id)
            if (
                str(projection.get("status") or "") != "active"
                or str(projection.get("engagement_mode") or "") != mode
                or projection.get("pending_action")
            ):
                return
            self.request_step(
                app_session_id=app_session_id,
                instruction=(
                    f"Use the accepted participant opportunity {event_type} to "
                    "choose one appropriate declared action."
                    if mode == "collaborate"
                    else (
                        f"React to accepted semantic event {event_type} when an "
                        "action is appropriate."
                    )
                ),
                reason=(
                    "collaborate_participant_opportunity"
                    if mode == "collaborate"
                    else (
                        "delegate_participant_opportunity"
                        if participant_opportunity
                        else "delegate_semantic_beat"
                    )
                ),
                expected_revision=(
                    event_revision if mode == "collaborate" else None
                ),
            )
        except AuipProtocolError:
            logger.debug("deferred automatic event was no longer eligible", exc_info=True)

    def _cancel_deferred_automatic(self, app_session_id: str) -> bool:
        session_id = str(app_session_id or "")
        self._deferred_automatic_events.pop(session_id, None)
        task = self._deferred_automatic_tasks.pop(session_id, None)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def _forget_deferred_automatic(
        self,
        app_session_id: str,
        task: asyncio.Task,
    ) -> None:
        if self._deferred_automatic_tasks.get(app_session_id) is task:
            self._deferred_automatic_tasks.pop(app_session_id, None)

    async def _run_step(
        self,
        *,
        app_session_id: str,
        generation: int,
        instruction: str,
        reason: str,
        expected_revision: int | None = None,
        current_role_response: str = "",
    ) -> dict[str, Any]:
        started = time.monotonic()
        logger.info(
            "[AUIP-ENGAGEMENT] step start app_session=%s generation=%d "
            "expected_revision=%s reason=%s instruction_chars=%d",
            app_session_id,
            generation,
            expected_revision if expected_revision is not None else "",
            reason,
            len(str(instruction or "")),
        )
        # Error visibility belongs to the scheduling reason, not to any state
        # read performed inside the participant turn.  Establish it before
        # touching runtime state so a revision/generation race can still
        # publish the correct bounded outcome instead of masking the protocol
        # error with an unbound local.
        action_required = reason in {
            "collaborate_participant_opportunity",
            "delegate_participant_opportunity",
            "explicit_step",
        }
        automatic_b2 = bool(
            self.runtime.role_branch_mode == "b2"
            and self.b2_coordinator is not None
            and reason
            in {
                "collaborate_participant_opportunity",
                "delegate_participant_opportunity",
                "delegate_semantic_beat",
                "delegate_mode_entered",
            }
            and not current_role_response
        )
        if automatic_b2:
            try:
                result = await self.b2_coordinator.execute_automatic_step(
                    app_session_id=app_session_id,
                    instruction=instruction,
                    trigger=reason,
                )
                status = str(result.get("status") or "")
                if status != "unavailable":
                    if action_required and status == "blocked":
                        result_reason = str(
                            result.get("reason") or "participant_decision_unavailable"
                        )[:240]
                        await self._publish_error(
                            app_session_id,
                            generation,
                            result_reason,
                            instruction=instruction,
                            proposal_id=str(result.get("proposal_id") or ""),
                            visible=True,
                        )
                    return result
                logger.info(
                    "[AUIP-ENGAGEMENT] B2 yielded incomplete candidate space; "
                    "using full Participant lane app_session=%s reason=%s",
                    app_session_id,
                    str(result.get("reason") or ""),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("AUIP B2 automatic decision failed")
                await self._publish_error(
                    app_session_id,
                    generation,
                    type(exc).__name__,
                    instruction=instruction,
                    visible=action_required,
                )
                return {
                    "status": "blocked",
                    "error": type(exc).__name__,
                    "reason": str(exc)[:240],
                }
        try:
            thinking = self.runtime.set_operator_status(
                app_session_id=app_session_id,
                status="thinking",
                expected_decision_generation=generation,
            )
            if (
                expected_revision is not None
                and int(thinking.get("revision") or 0) != int(expected_revision)
            ):
                raise AuipProtocolError("participant_revision_changed")
            await bus.emit(Method.AUIP_UPDATED, _public_update(thinking))
            conversation_id = str(thinking.get("conversation_id") or "")
            global_context = _bounded_conversation_context(
                self.recent_chat(conversation_id),
                instruction=instruction,
                reason=reason,
                current_role_response=current_role_response,
            )
            proposal = await self.participant.propose(
                app_session_id=app_session_id,
                controller=self.controller,  # type: ignore[arg-type]
                controller_id=self.controller_id,
                global_context=global_context,
                action_required=action_required,
            )
            if proposal.action == "blocked":
                await self._publish_error(
                    app_session_id,
                    generation,
                    "participant_blocked",
                    detail=proposal.private_note,
                    instruction=instruction,
                    proposal_id=proposal.proposal_id,
                    visible=action_required,
                )
                return {
                    "status": "blocked",
                    "error": "participant_blocked",
                    "reason": proposal.private_note,
                    "proposal_id": proposal.proposal_id,
                }
            if proposal.action == "wait":
                result = self.runtime.set_operator_status(
                    app_session_id=app_session_id,
                    status="idle",
                    expected_decision_generation=generation,
                )
            else:
                proposal, global_context = await self._preflight_proposal(
                    proposal,
                    global_context=global_context,
                    action_required=action_required,
                )
                automatic_opportunity = reason in {
                    "collaborate_participant_opportunity",
                    "delegate_participant_opportunity",
                    "delegate_semantic_beat",
                    "delegate_mode_entered",
                }
                authorization = (
                    {
                        "decision": "approve",
                        "role_alignment": "not_applicable",
                        "reason": "accepted automatic participant opportunity",
                    }
                    if automatic_opportunity and not current_role_response
                    else await self._authorize_proposal(
                        proposal,
                        global_context=global_context,
                        current_role_response=current_role_response,
                    )
                )
                decision = str(authorization.get("decision") or "")
                logger.info(
                    "[AUIP-GATE] proposal=%s decision=%s reason=%r "
                    "reason_chars=%d latency_ms=%d",
                    proposal.proposal_id,
                    decision,
                    str(authorization.get("reason") or "")[:240],
                    len(str(authorization.get("reason") or "")),
                    int((time.monotonic() - started) * 1000),
                )
                if decision == "replan":
                    replan_context = _bounded_replan_context(
                        global_context,
                        proposal=proposal,
                        reason=str(authorization.get("reason") or ""),
                    )
                    proposal = await self.participant.propose(
                        app_session_id=app_session_id,
                        controller=self.controller,  # type: ignore[arg-type]
                        controller_id=self.controller_id,
                        global_context=replan_context,
                        action_required=action_required,
                    )
                    if proposal.action == "blocked":
                        await self._publish_error(
                            app_session_id,
                            generation,
                            "participant_blocked",
                            detail=proposal.private_note,
                            instruction=instruction,
                            proposal_id=proposal.proposal_id,
                            visible=action_required,
                        )
                        return {
                            "status": "blocked",
                            "error": "participant_blocked",
                            "reason": proposal.private_note,
                            "proposal_id": proposal.proposal_id,
                        }
                    if proposal.action == "wait":
                        result = self.runtime.set_operator_status(
                            app_session_id=app_session_id,
                            status="idle",
                            expected_decision_generation=generation,
                        )
                    else:
                        self.runtime.check_action_preconditions(
                            app_session_id=proposal.app_session_id,
                            type=proposal.action_type,
                            payload=proposal.payload,
                            expected_revision=proposal.expected_revision,
                            allow_controller_rebase=True,
                        )
                        authorization = await self._authorize_proposal(
                            proposal,
                            global_context=replan_context,
                            current_role_response=current_role_response,
                        )
                        logger.info(
                            "[AUIP-GATE] replan proposal=%s decision=%s reason_chars=%d",
                            proposal.proposal_id,
                            str(authorization.get("decision") or ""),
                            len(str(authorization.get("reason") or "")),
                        )
                        if str(authorization.get("decision") or "") != "approve":
                            raise AuipProtocolError(
                                "role_rejected_proposal",
                                str(authorization.get("reason") or "")[:240],
                            )
                        result = await self.participant.invoke(proposal)
                        self._watch_receipt(result)
                elif decision == "approve":
                    result = await self.participant.invoke(proposal)
                    self._watch_receipt(result)
                else:
                    raise AuipProtocolError(
                        "role_rejected_proposal",
                        str(authorization.get("reason") or "")[:240],
                    )
            await bus.emit(Method.AUIP_UPDATED, _public_update(result))
            if proposal.action == "wait":
                return {"status": "wait"}
            action = result.get("action") if isinstance(result.get("action"), dict) else {}
            return {
                "status": "authorized",
                "proposal_id": proposal.proposal_id,
                "participant_trace_id": proposal.trace_id,
                "action": dict(action),
            }
        except asyncio.CancelledError:
            raise
        except AuipProtocolError as exc:
            if exc.code == "participant_generation_changed":
                return {"status": "superseded", "error": exc.code}
            logger.warning(
                "[AUIP-ENGAGEMENT] step blocked app_session=%s generation=%d "
                "error=%s detail_chars=%d latency_ms=%d",
                app_session_id,
                generation,
                exc.code,
                len(str(exc.detail or "")),
                int((time.monotonic() - started) * 1000),
            )
            await self._publish_error(
                app_session_id,
                generation,
                exc.code,
                detail=exc.detail,
                instruction=instruction,
                visible=action_required,
            )
            return {
                "status": "blocked",
                "error": exc.code,
                "reason": str(exc.detail or "")[:240],
            }
        except Exception as exc:
            logger.exception("AUIP Participant decision failed")
            await self._publish_error(
                app_session_id,
                generation,
                type(exc).__name__,
                instruction=instruction,
                visible=action_required,
            )
            return {
                "status": "blocked",
                "error": type(exc).__name__,
                "reason": str(exc)[:240],
            }

    async def _preflight_proposal(
        self,
        proposal: AuipParticipantProposal,
        *,
        global_context: str,
        action_required: bool,
    ) -> tuple[AuipParticipantProposal, str]:
        """Check declared legality and permit one bounded corrective replan."""

        try:
            self.runtime.check_action_preconditions(
                app_session_id=proposal.app_session_id,
                type=proposal.action_type,
                payload=proposal.payload,
                expected_revision=proposal.expected_revision,
                allow_controller_rebase=True,
            )
            return proposal, global_context
        except AuipProtocolError as exc:
            if exc.code != "action_precondition_failed" or not action_required:
                raise
            replan_context = _bounded_replan_context(
                global_context,
                proposal=proposal,
                reason=(
                    "The Host-checkable declared precondition failed: "
                    f"{str(exc.detail or exc.code)[:240]}. Choose a different "
                    "declared action or payload from the same accepted state."
                ),
            )
            replacement = await self.participant.propose(
                app_session_id=proposal.app_session_id,
                controller=self.controller,  # type: ignore[arg-type]
                controller_id=self.controller_id,
                global_context=replan_context,
                action_required=action_required,
            )
            if replacement.action != "act":
                raise AuipProtocolError(
                    "participant_blocked",
                    replacement.private_note
                    or "No declared action satisfied the required precondition.",
                )
            self.runtime.check_action_preconditions(
                app_session_id=replacement.app_session_id,
                type=replacement.action_type,
                payload=replacement.payload,
                expected_revision=replacement.expected_revision,
                allow_controller_rebase=True,
            )
            return replacement, replan_context

    async def _publish_error(
        self,
        app_session_id: str,
        generation: int,
        error: str,
        *,
        detail: str = "",
        instruction: str = "",
        proposal_id: str = "",
        visible: bool = False,
    ) -> None:
        try:
            result = self.runtime.set_operator_status(
                app_session_id=app_session_id,
                status="error",
                error=error,
                error_detail=detail,
                expected_decision_generation=generation,
            )
        except AuipProtocolError:
            return
        update = _public_update(result)
        if visible:
            outcome_id = str(proposal_id or "").strip()[:160]
            if not outcome_id:
                outcome_id = f"operator_outcome_{uuid.uuid4().hex}"
            update["operator_outcome"] = {
                "status": "blocked",
                "outcome_id": outcome_id,
                "proposal_id": str(proposal_id or "").strip()[:160],
                "instruction": str(instruction or "").strip()[:800],
                "reason": _operator_failure_reason(error, detail),
            }
        await bus.emit(Method.AUIP_UPDATED, update)

    def _watch_receipt(self, result: Mapping[str, Any]) -> None:
        action = result.get("action") if isinstance(result.get("action"), dict) else None
        app_session_id = str(result.get("app_session_id") or "").strip()
        action_id = str(action.get("action_id") or "").strip() if action else ""
        if not app_session_id or not action_id:
            return
        self._cancel_receipt_watch(app_session_id)
        task = asyncio.create_task(
            self._report_receipt_timeout(app_session_id, action_id),
            name=f"auip-receipt:{app_session_id}",
        )
        self._receipt_tasks[app_session_id] = task
        task.add_done_callback(
            lambda done, sid=app_session_id: self._forget_receipt_task(sid, done)
        )

    async def _report_receipt_timeout(
        self,
        app_session_id: str,
        action_id: str,
    ) -> None:
        try:
            await asyncio.sleep(self.receipt_timeout_s)
            snapshot = self.runtime.get(app_session_id)
            expired = (
                snapshot.get("last_expired_action")
                if isinstance(snapshot.get("last_expired_action"), dict)
                else None
            )
            if (
                expired is None
                or str(expired.get("action_id") or "") != action_id
                or str(snapshot.get("operator_error") or "") != "receipt_timeout"
            ):
                return
            reason = _operator_failure_reason("receipt_timeout")
            update = _public_update(snapshot)
            update["operator_outcome"] = {
                "status": "blocked",
                "outcome_id": action_id,
                "proposal_id": str(expired.get("proposal_id") or "")[:160],
                "instruction": "",
                "reason": reason,
            }
            await bus.emit(Method.AUIP_UPDATED, update)
        except asyncio.CancelledError:
            raise
        except AuipProtocolError:
            return
        except Exception:
            logger.exception(
                "AUIP receipt timeout reporting failed app_session_id=%s",
                app_session_id,
            )

    def _cancel_receipt_watch(self, app_session_id: str) -> None:
        task = self._receipt_tasks.get(str(app_session_id or ""))
        if task is None or task.done():
            return
        task.cancel()

    def _forget_receipt_task(self, app_session_id: str, task: asyncio.Task) -> None:
        if self._receipt_tasks.get(app_session_id) is task:
            self._receipt_tasks.pop(app_session_id, None)

    def _role_authorization_context(
        self,
        proposal: AuipParticipantProposal,
        *,
        global_context: str,
        current_role_response: str = "",
    ) -> dict[str, Any]:
        """Build the AUIP branch view used by the silent main-role gate."""

        context = self.runtime.participant_context(
            proposal.app_session_id,
            global_context=global_context,
            max_chars=5200,
        )
        context["proposal"] = proposal.authorization_dict()
        context["current_role_response"] = str(current_role_response or "").strip()[
            -1600:
        ]
        context["authorization_contract"] = {
            "may_mutate_proposal": False,
            "accepted_receipt_required_for_execution_truth": True,
            "speaker_roles": {
                "current_role_response_speaker": "participant",
                "first_person": "participant",
                "second_person": "user",
                "proposal_actor": "participant",
            },
        }
        return context

    async def _authorize_proposal(
        self,
        proposal: AuipParticipantProposal,
        *,
        global_context: str,
        current_role_response: str = "",
    ) -> Mapping[str, Any]:
        authorization = self.role_authorizer(
            self._role_authorization_context(
                proposal,
                global_context=global_context,
                current_role_response=current_role_response,
            )
        )
        if inspect.isawaitable(authorization):
            authorization = await authorization
        if not isinstance(authorization, Mapping):
            raise AuipProtocolError("invalid_role_authorization")
        decision = str(authorization.get("decision") or "").strip().lower()
        if decision not in {"approve", "replan", "reject"}:
            raise AuipProtocolError("invalid_role_authorization")
        return {
            "decision": decision,
            "reason": str(authorization.get("reason") or "").strip()[:600],
        }

    def _cancel_decision(self, app_session_id: str) -> None:
        task = self._tasks.get(str(app_session_id or ""))
        if task is not None and not task.done():
            task.cancel()

    def _forget_task(self, app_session_id: str, task: asyncio.Task) -> None:
        if self._tasks.get(app_session_id) is task:
            self._tasks.pop(app_session_id, None)
            self._task_reasons.pop(app_session_id, None)

    def _remember_event(self, event_id: str) -> None:
        if len(self._seen_events) == self._seen_events.maxlen:
            old = self._seen_events.popleft()
            self._seen_event_set.discard(old)
        self._seen_events.append(event_id)
        self._seen_event_set.add(event_id)

    def _remember_receipt(self, receipt_id: str) -> None:
        if len(self._seen_receipts) == self._seen_receipts.maxlen:
            old = self._seen_receipts.popleft()
            self._seen_receipt_set.discard(old)
        self._seen_receipts.append(receipt_id)
        self._seen_receipt_set.add(receipt_id)

def _bounded_conversation_context(
    messages: list[dict[str, str]],
    *,
    instruction: str,
    reason: str,
    current_role_response: str = "",
) -> str:
    recent_chat = [
        {
            "role": str(item.get("role") or "")[:20],
            "content": str(item.get("content") or "")[:500],
        }
        for item in list(messages or [])[-6:]
        if isinstance(item, dict)
    ]
    current_response = str(current_role_response or "").strip()
    if (
        current_response
        and recent_chat
        and recent_chat[-1]["role"] == "assistant"
        and recent_chat[-1]["content"].strip() == current_response[:500]
    ):
        # A1 stages the visible turn before dispatch so ``leave`` can collapse
        # it. Keep the separately typed current response as the gate evidence
        # and avoid presenting the same commitment twice.
        recent_chat.pop()
    payload = {
        "trigger": reason,
        "instruction": instruction,
        "current_role_response": current_response[-1200:],
        "recent_chat": recent_chat,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:2400]


def _bounded_replan_context(
    global_context: str,
    *,
    proposal: AuipParticipantProposal,
    reason: str,
) -> str:
    try:
        source = json.loads(str(global_context or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        source = {}
    if not isinstance(source, dict):
        source = {}
    payload = {
        "role_replan_feedback": {
            "reason": str(reason or "").strip()[:360],
            "rejected_proposal": proposal.authorization_dict(),
        },
        "trigger": str(source.get("trigger") or "")[:80],
        "instruction": str(source.get("instruction") or "")[:600],
        "current_role_response": str(
            source.get("current_role_response") or ""
        )[-600:],
        "recent_chat": list(source.get("recent_chat") or [])[-3:],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:1200]


def _public_update(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in result.items()
        if key not in {"bridge_token", "manifest"}
    }


def _operator_failure_reason(error: str, detail: str = "") -> str:
    """Return one bounded factual reason suitable for AUIP narration."""

    clean_detail = " ".join(str(detail or "").split())[:600]
    if clean_detail:
        return clean_detail
    clean_error = str(error or "").strip()
    return {
        "participant_decision_unavailable": (
            "The participant decision service was unavailable, so no application "
            "action was requested."
        ),
        "b2_role_decision_unavailable": (
            "The application turn could not be decided after one bounded retry, "
            "so no action was requested."
        ),
        "role_authorization_unavailable": (
            "The action could not be authorized, so no application action was requested."
        ),
        "role_rejected_proposal": (
            "The proposed action conflicted with the current role or conversation policy."
        ),
        "participant_blocked": (
            "No currently declared legal action can satisfy the requested step."
        ),
        "receipt_timeout": (
            "The application did not return an action receipt before the timeout, so "
            "whether the requested action took effect is unknown."
        ),
    }.get(
        clean_error,
        f"The requested participant step was blocked ({clean_error or 'unknown reason'}).",
    )[:600]
