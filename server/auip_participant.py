"""Pluggable operator lane for AUIP participant actions.

The controller may be the main model, a specialist agent, or deterministic
policy. It proposes one typed action. The AUIP runtime remains the authority
that validates stance, declaration, revision, and the application's receipt.
"""

from __future__ import annotations

import inspect
import json
import logging
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from server.auip_contract import AuipProtocolError, validate_payload
from server.auip_runtime import AuipRuntime, runtime
from server.event_bus import bus
from server.protocol import Method


logger = logging.getLogger(__name__)


class AuipParticipantController(Protocol):
    def decide(self, context: dict[str, Any]) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]:
        """Return one ``type`` + ``payload`` action proposal."""


ControllerCallable = Callable[
    [dict[str, Any]],
    Mapping[str, Any] | Awaitable[Mapping[str, Any]],
]


@dataclass(frozen=True, slots=True)
class AuipParticipantProposal:
    """One immutable, revision-bound specialist proposal without side effects."""

    proposal_id: str
    trace_id: str
    controller_id: str
    app_session_id: str
    decision_generation: int
    expected_revision: int
    action: str
    action_type: str
    payload: dict[str, Any]
    private_note: str
    created_at: float

    def authorization_dict(self) -> dict[str, Any]:
        """Project only the semantic choice owned by the role gate.

        AppSession identity, decision generation, and revision are Host
        execution authority. They remain on the immutable proposal and are
        checked before and during invocation, but exposing them to the model
        invites a second, stale mechanical verdict at this semantic boundary.
        """

        return {
            "proposal_id": self.proposal_id,
            "action": self.action,
            "type": self.action_type,
            "payload": dict(self.payload),
        }


class AuipParticipantCoordinator:
    def __init__(self, app_runtime: AuipRuntime | None = None) -> None:
        self.runtime = app_runtime or runtime
        self._debug_traces: deque[dict[str, Any]] = deque(maxlen=128)

    async def propose(
        self,
        *,
        app_session_id: str,
        controller: AuipParticipantController | ControllerCallable,
        controller_id: str,
        global_context: str = "",
        action_required: bool = False,
    ) -> AuipParticipantProposal:
        started = time.monotonic()
        context = self.runtime.participant_context(
            app_session_id,
            global_context=global_context,
        )
        # This is scheduling policy, not application state. A collaborate
        # opportunity is a bounded turn assigned to the Participant; ordinary
        # delegate beats may still be observed without acting. Keeping the
        # distinction explicit lets the controller schema enforce it instead
        # of hoping a prompt will choose the right meaning for ``wait``.
        context["action_required"] = bool(action_required)
        decide = getattr(controller, "decide", controller)
        if not callable(decide):
            raise AuipProtocolError("invalid_participant_controller")
        proposal = decide(context)
        if inspect.isawaitable(proposal):
            proposal = await proposal
        if not isinstance(proposal, Mapping):
            raise AuipProtocolError("invalid_action_proposal")
        action_type = str(proposal.get("type") or "").strip().lower()
        payload = validate_payload(proposal.get("payload") or {})
        trace_id = f"participant_{uuid.uuid4().hex}"
        proposal_id = f"proposal_{uuid.uuid4().hex}"
        trace = {
            "trace_id": trace_id,
            "controller_id": str(controller_id or "participant").strip()[:120],
            "app_session_id": app_session_id,
            "revision": context["revision"],
            "action_type": action_type,
            "action_required": bool(action_required),
            "private_note": str(proposal.get("private_note") or "").strip()[:600],
            "created_at": time.time(),
        }
        self._debug_traces.append(trace)
        proposal_action = str(proposal.get("action") or "act").strip().lower()
        payload_log = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )[:480]
        logger.info(
            "[AUIP-PARTICIPANT] proposal app_session=%s controller=%s "
            "generation=%s revision=%s required=%s action=%s type=%s "
            "payload=%s note_chars=%d latency_ms=%d",
            app_session_id,
            str(controller_id or "participant")[:120],
            context.get("decision_generation"),
            context.get("revision"),
            bool(action_required),
            proposal_action,
            action_type,
            payload_log,
            len(str(proposal.get("private_note") or "")),
            int((time.monotonic() - started) * 1000),
        )
        if proposal_action == "blocked" or (
            proposal_action == "wait" and bool(action_required)
        ):
            reason = str(proposal.get("private_note") or "").strip()[:600]
            if not reason:
                reason = "No declared action can satisfy the required participant step."
            return AuipParticipantProposal(
                proposal_id=proposal_id,
                trace_id=trace_id,
                controller_id=str(controller_id or "participant").strip()[:120],
                app_session_id=app_session_id,
                decision_generation=int(context["decision_generation"]),
                expected_revision=int(context["revision"]),
                action="blocked",
                action_type="",
                payload={},
                private_note=reason,
                created_at=trace["created_at"],
            )
        if proposal_action == "wait":
            return AuipParticipantProposal(
                proposal_id=proposal_id,
                trace_id=trace_id,
                controller_id=str(controller_id or "participant").strip()[:120],
                app_session_id=app_session_id,
                decision_generation=int(context["decision_generation"]),
                expected_revision=int(context["revision"]),
                action="wait",
                action_type="",
                payload={},
                private_note=str(proposal.get("private_note") or "").strip()[:600],
                created_at=trace["created_at"],
            )
        if proposal_action != "act" or not action_type:
            logger.warning(
                "[AUIP-PARTICIPANT] invalid proposal app_session=%s action=%s type=%s",
                app_session_id,
                proposal_action,
                action_type,
            )
            raise AuipProtocolError("invalid_action_proposal")
        choice_options = context.get("available_choice_options")
        choice_action_types = {
            str(value or "").strip().lower()
            for value in context.get("choice_action_types") or []
            if str(value or "").strip()
        }
        if action_type in choice_action_types and isinstance(
            choice_options, list
        ) and not any(
            isinstance(option, Mapping)
            and str(option.get("action") or "").strip().lower() == action_type
            and option.get("payload") == payload
            for option in choice_options
        ):
            logger.warning(
                "[AUIP-PARTICIPANT] proposal absent from available choices "
                "app_session=%s type=%s",
                app_session_id,
                action_type,
            )
            return AuipParticipantProposal(
                proposal_id=proposal_id,
                trace_id=trace_id,
                controller_id=str(controller_id or "participant").strip()[:120],
                app_session_id=app_session_id,
                decision_generation=int(context["decision_generation"]),
                expected_revision=int(context["revision"]),
                action="blocked",
                action_type="",
                payload={},
                private_note=(
                    "The proposed action and payload are not one of the "
                    "application's currently available choice options."
                ),
                created_at=trace["created_at"],
            )
        return AuipParticipantProposal(
            proposal_id=proposal_id,
            trace_id=trace_id,
            controller_id=str(controller_id or "participant").strip()[:120],
            app_session_id=app_session_id,
            decision_generation=int(context["decision_generation"]),
            expected_revision=int(context["revision"]),
            action="act",
            action_type=action_type,
            payload=payload,
            private_note=str(proposal.get("private_note") or "").strip()[:600],
            created_at=trace["created_at"],
        )

    async def invoke(self, proposal: AuipParticipantProposal) -> dict[str, Any]:
        """Invoke the exact proposal after the separate role gate approves it."""

        if not isinstance(proposal, AuipParticipantProposal) or proposal.action != "act":
            raise AuipProtocolError("invalid_action_proposal")
        logger.info(
            "[AUIP-PARTICIPANT] invoke proposal=%s app_session=%s generation=%d "
            "revision=%d type=%s",
            proposal.proposal_id,
            proposal.app_session_id,
            proposal.decision_generation,
            proposal.expected_revision,
            proposal.action_type,
        )
        result = self.runtime.invoke_action(
            app_session_id=proposal.app_session_id,
            actor="kurisu",
            type=proposal.action_type,
            payload=proposal.payload,
            expected_revision=proposal.expected_revision,
            expected_decision_generation=proposal.decision_generation,
            proposal_id=proposal.proposal_id,
        )
        await bus.emit(
            Method.AUIP_ACTION_REQUESTED,
            {
                "app_session_id": result.get("app_session_id"),
                "action": result.get("action"),
            },
        )
        action = result.get("action") if isinstance(result.get("action"), dict) else {}
        logger.info(
            "[AUIP-PARTICIPANT] requested proposal=%s action_id=%s app_session=%s",
            proposal.proposal_id,
            str(action.get("action_id") or ""),
            proposal.app_session_id,
        )
        return {
            **result,
            "participant_trace_id": proposal.trace_id,
            "participant_proposal_id": proposal.proposal_id,
        }

    def debug_trace(self, trace_id: str) -> dict[str, Any] | None:
        target = str(trace_id or "").strip()
        for item in reversed(self._debug_traces):
            if item.get("trace_id") == target:
                return dict(item)
        return None
