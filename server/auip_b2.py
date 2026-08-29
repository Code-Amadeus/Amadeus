"""B2 candidate-locked handoff for bounded AUIP action turns.

Foreground user turns and Host-authorized automatic opportunities share one
action owner: the role selects an exact Host-compiled candidate and the Host
executes it at the bound revision. Declared Controller policies may rebind the
same payload to the latest telemetry revision under Runtime authority.
Presentation remains source-specific.
Foreground speech is chosen in the same call and held until an accepted
receipt; automatic choices emit no speech and yield verified consequences to
the existing sparse event-presentation lane.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from config import settings
from server.auip_action_candidates import (
    AuipActionCandidate,
    compile_auip_action_candidates,
)
from server.auip_contract import AuipProtocolError, validate_payload
from server.auip_runtime import AuipRuntime
from server.event_bus import bus
from server.protocol import Method


logger = logging.getLogger(__name__)

RoleChooser = Callable[..., Mapping[str, Any] | Awaitable[Mapping[str, Any]]]
DecisionStager = Callable[[str, Any], None]

_PRE_ACTION_REPLAN_CODES = frozenset(
    {
        "action_not_available",
        "action_precondition_failed",
        "stale_action_revision",
    }
)


class AuipB2Coordinator:
    """Own candidate selection/execution, not automatic-event presentation."""

    def __init__(
        self,
        *,
        runtime: AuipRuntime,
        control_decider: Any,
        role_chooser: RoleChooser,
        stage_decision: DecisionStager,
        open_role_chooser: RoleChooser | None = None,
        open_payload_mode: str | None = None,
        receipt_timeout_s: float | None = None,
    ) -> None:
        self.runtime = runtime
        self.control_decider = control_decider
        self.role_chooser = role_chooser
        self.open_role_chooser = open_role_chooser
        self.stage_decision = stage_decision
        self.open_payload_mode = str(
            open_payload_mode
            if open_payload_mode is not None
            else getattr(settings, "AUIP_B2_OPEN_PAYLOAD_MODE", "off")
        ).strip().lower()
        if self.open_payload_mode not in {"off", "candidate"}:
            raise ValueError("open_payload_mode must be off or candidate")
        self.receipt_timeout_s = max(
            1.0,
            float(
                settings.AUIP_ACTION_TIMEOUT_S
                if receipt_timeout_s is None
                else receipt_timeout_s
            ),
        )
        self._locks: dict[str, asyncio.Lock] = {}

    async def try_route_user_message(
        self,
        *,
        text: str,
        session_id: str,
        turn_id: str = "",
    ) -> dict[str, Any] | None:
        """Return one direct Chat branch result, or stage the decision for fallback."""

        if self.runtime.role_branch_mode != "b2":
            return None
        projection = self.runtime.focused_projection(str(session_id or ""))
        if (
            not isinstance(projection, dict)
            or str(projection.get("status") or "") != "active"
        ):
            logger.debug(
                "[AUIP-B2] foreground ineligible reason=no_active_projection"
            )
            return None
        app_session_id = str(projection.get("app_session_id") or "").strip()
        if not app_session_id or not self.runtime.role_branch_active(app_session_id):
            logger.info(
                "[AUIP-B2] foreground ineligible reason=role_branch_inactive "
                "app_session_bound=%s",
                bool(app_session_id),
            )
            return None

        prior_messages = self.runtime.recent_role_branch_messages(
            str(session_id or ""),
            limit=8,
        )
        pending = self.control_decider.capture(
            session_id=str(session_id or ""),
            user_text=str(text or ""),
            prior_messages=prior_messages or (),
            include_work_followup=False,
        )
        if pending is None:
            logger.info(
                "[AUIP-B2] foreground ineligible reason=decision_unavailable"
            )
            return None
        decision = await pending if inspect.isawaitable(pending) else pending
        if not self._owns_decision(decision):
            logger.info(
                "[AUIP-B2] foreground pass-through status=%s action=%s "
                "work_relation=%s app_session_bound=%s",
                str(getattr(decision, "status", "") or ""),
                str(getattr(decision, "action", "") or ""),
                str(getattr(decision, "work_relation", "") or ""),
                bool(str(getattr(decision, "app_session_id", "") or "")),
            )
            self.stage_decision(str(turn_id or ""), decision)
            return None

        lock = self._locks.setdefault(app_session_id, asyncio.Lock())
        async with lock:
            return await self._execute_candidate_step(
                app_session_id=app_session_id,
                conversation_id=str(session_id or ""),
                user_instruction=str(getattr(decision, "instruction", "") or text),
                source_user_text=str(text or ""),
                turn_id=str(turn_id or ""),
                decision=decision,
                trigger="explicit_step",
            )

    async def execute_automatic_step(
        self,
        *,
        app_session_id: str,
        instruction: str,
        trigger: str,
    ) -> dict[str, Any]:
        """Consume one Host-authorized automatic Participant opportunity."""

        if self.runtime.role_branch_mode != "b2":
            return {"status": "unavailable", "reason": "b2_not_active"}
        projection = self.runtime.get(app_session_id)
        conversation_id = str(projection.get("conversation_id") or "")
        lock = self._locks.setdefault(app_session_id, asyncio.Lock())
        async with lock:
            clean_trigger = str(trigger or "participant_opportunity")
            required_opportunity = clean_trigger in {
                "collaborate_participant_opportunity",
                "delegate_participant_opportunity",
            }
            result = await self._execute_candidate_step(
                app_session_id=app_session_id,
                conversation_id=conversation_id,
                # Scheduling text is Host policy, not a user instruction. The
                # accepted participant-opportunity beat is already present in
                # the bounded runtime facts.
                user_instruction="",
                source_user_text="",
                turn_id="",
                decision=None,
                trigger=clean_trigger,
                automatic=True,
                role_decision_attempts=2 if required_opportunity else 1,
            )
        return dict(result or {"status": "blocked"})

    @staticmethod
    def _owns_decision(decision: Any) -> bool:
        return bool(
            str(getattr(decision, "status", "") or "") == "ok"
            and str(getattr(decision, "action", "") or "") == "step"
            and str(getattr(decision, "work_relation", "") or "")
            in {"", "subsumed"}
            and str(getattr(decision, "app_session_id", "") or "")
        )

    async def _execute_candidate_step(
        self,
        *,
        app_session_id: str,
        conversation_id: str,
        user_instruction: str,
        source_user_text: str,
        turn_id: str,
        decision: Any | None,
        trigger: str,
        automatic: bool = False,
        role_decision_attempts: int = 1,
    ) -> dict[str, Any] | None:
        projection = self.runtime.get(app_session_id)
        if projection.get("pending_action"):
            if automatic:
                return {"status": "skipped", "reason": "action_already_pending"}
            self.stage_decision(turn_id, decision)
            return None
        if str(projection.get("engagement_mode") or "observe") == "observe":
            projection = self.runtime.set_engagement_mode(
                app_session_id=app_session_id,
                mode="collaborate",
            )
        generation = int(projection.get("decision_generation") or 0)
        thinking = self.runtime.set_operator_status(
            app_session_id=app_session_id,
            status="thinking",
            expected_decision_generation=generation,
        )
        await bus.emit(Method.AUIP_UPDATED, _public_update(thinking))

        selection_pass = 0
        while True:
            try:
                compiled = compile_auip_action_candidates(self.runtime, app_session_id)
                hybrid_decision = bool(
                    not compiled.complete
                    and self.open_payload_mode == "candidate"
                    and self.open_role_chooser is not None
                )
                if not compiled.complete and not hybrid_decision:
                    logger.info(
                        "[AUIP-B2] candidate space incomplete; yielding to full "
                        "Participant lane app_session=%s uncovered=%s",
                        app_session_id,
                        ",".join(compiled.uncovered_action_types),
                    )
                    await self._restore_idle(app_session_id)
                    if automatic:
                        return {
                            "status": "unavailable",
                            "reason": "candidate_space_incomplete",
                            "uncovered_action_types": list(
                                compiled.uncovered_action_types
                            ),
                        }
                    self.stage_decision(turn_id, decision)
                    return None
                if not compiled.candidates and not hybrid_decision:
                    raise AuipProtocolError("b2_candidates_unavailable")
                branch_messages = self.runtime.recent_role_branch_messages(
                    conversation_id,
                    limit=10,
                )
                role_result: Any = None
                chooser = (
                    self.open_role_chooser if hybrid_decision else self.role_chooser
                )
                if chooser is None:
                    raise AuipProtocolError("b2_open_role_decision_unavailable")
                role_attempt_count = max(1, int(role_decision_attempts))
                for role_attempt in range(role_attempt_count):
                    try:
                        role_kwargs: dict[str, Any] = {
                            "context": compiled.context,
                            "candidates": compiled.candidates,
                            "user_instruction": user_instruction,
                            "branch_messages": branch_messages or [],
                            "trigger": trigger,
                            "speech_required": not automatic,
                        }
                        if hybrid_decision:
                            role_kwargs["uncovered_action_types"] = (
                                compiled.uncovered_action_types
                            )
                        role_result = chooser(
                            **role_kwargs,
                        )
                        if inspect.isawaitable(role_result):
                            role_result = await role_result
                        break
                    except AuipProtocolError as exc:
                        if not (
                            automatic
                            and role_attempt + 1 < role_attempt_count
                            and exc.code
                            in {
                                "b2_role_decision_unavailable",
                                "b2_open_role_decision_unavailable",
                            }
                        ):
                            raise
                        logger.warning(
                            "[AUIP-B2] retrying required opportunity once after "
                            "transient role decision failure app_session=%s",
                            app_session_id,
                        )
                if not isinstance(role_result, Mapping):
                    raise AuipProtocolError("invalid_b2_role_decision")
                candidate = _selected_candidate(
                    role_result=role_result,
                    compiled=compiled,
                    hybrid_decision=hybrid_decision,
                )
                speech = str(role_result.get("speech") or "").strip()
                instruction_relation = str(
                    role_result.get("instruction_relation") or ""
                ).strip().lower()
                if not automatic and not speech:
                    raise AuipProtocolError("invalid_b2_role_decision")
            except Exception as exc:
                logger.warning(
                    "[AUIP-B2] pre-invoke handoff unavailable app_session=%s "
                    "error=%s detail=%r",
                    app_session_id,
                    getattr(exc, "code", type(exc).__name__),
                    str(getattr(exc, "detail", "") or "")[:240],
                )
                await self._restore_idle(app_session_id)
                if automatic:
                    return {
                        "status": "blocked",
                        "reason": getattr(exc, "code", type(exc).__name__),
                    }
                self.stage_decision(turn_id, decision)
                return None

            try:
                self.runtime.check_action_preconditions(
                    app_session_id=app_session_id,
                    type=candidate.action_type,
                    payload=dict(candidate.payload),
                    expected_revision=candidate.revision,
                    allow_controller_rebase=True,
                )
            except AuipProtocolError as exc:
                if (
                    not automatic
                    and selection_pass == 0
                    and exc.code in _PRE_ACTION_REPLAN_CODES
                ):
                    selection_pass += 1
                    logger.info(
                        "[AUIP-B2] accepted state changed before action boundary; "
                        "recompiling once app_session=%s candidate=%s reason=%s",
                        app_session_id,
                        candidate.candidate_id,
                        exc.code,
                    )
                    continue
                await self._restore_idle(app_session_id)
                if automatic:
                    return {"status": "superseded", "reason": "stale_candidate"}
                self.stage_decision(turn_id, decision)
                return None
            break
        if source_user_text:
            recorded = self.runtime.record_role_branch_turn(
                conversation_id=conversation_id,
                app_session_id=app_session_id,
                user_text=source_user_text,
                assistant_text="",
            )
            if not recorded:
                await self._restore_idle(app_session_id)
                self.stage_decision(turn_id, decision)
                return None

        proposal_id = (
            f"{'b2a' if automatic else 'b2f'}:r{candidate.revision}:"
            f"{candidate.candidate_id}"
        )[:160]
        decision_path = "b2"
        selection_source = (
            "open_schema_role" if hybrid_decision else "locked_candidate"
        )
        receipt_future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        action_id = ""

        async def capture_receipt(_method: str, payload: dict[str, Any]) -> None:
            if receipt_future.done():
                return
            receipt = (
                payload.get("receipt")
                if isinstance(payload.get("receipt"), dict)
                else None
            )
            if (
                receipt is not None
                and str(payload.get("app_session_id") or "") == app_session_id
                and action_id
                and str(receipt.get("action_id") or "") == action_id
            ):
                receipt_future.set_result(dict(receipt))

        bus.on(Method.AUIP_UPDATED, capture_receipt)
        try:
            invoked = self.runtime.invoke_action(
                app_session_id=app_session_id,
                actor="kurisu",
                type=candidate.action_type,
                payload=dict(candidate.payload),
                expected_revision=candidate.revision,
                expected_decision_generation=candidate.decision_generation,
                proposal_id=proposal_id,
                decision_context={
                    "kind": (
                        "automatic_role_choice"
                        if automatic
                        else "foreground_role_choice"
                    ),
                    "reason": str(role_result.get("choice_reason") or ""),
                    "instruction_relation": instruction_relation,
                },
                allow_controller_rebase=True,
            )
            action = invoked.get("action") if isinstance(invoked.get("action"), dict) else {}
            action_id = str(action.get("action_id") or "").strip()
            if not action_id:
                raise AuipProtocolError("invalid_action_request")
            await bus.emit(
                Method.AUIP_ACTION_REQUESTED,
                {
                    "app_session_id": app_session_id,
                    "action": dict(action),
                    "decision_path": decision_path,
                    "selection_source": selection_source,
                    "candidate_id": candidate.candidate_id,
                    "instruction_relation": instruction_relation,
                },
            )
            receipt = await asyncio.wait_for(
                receipt_future,
                timeout=self.receipt_timeout_s,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_code = str(getattr(exc, "code", type(exc).__name__) or "")
            error_detail = str(getattr(exc, "detail", "") or "")[:240]
            logger.warning(
                "[AUIP-B2] action did not reach accepted delivery app_session=%s "
                "proposal=%s error=%s detail=%r action_requested=%s",
                app_session_id,
                proposal_id,
                error_code,
                error_detail,
                bool(action_id),
            )
            await self._restore_idle(app_session_id)
            if automatic:
                return {
                    "status": "blocked",
                    "proposal_id": proposal_id,
                    "candidate_id": candidate.candidate_id,
                    "action_id": action_id,
                    "reason": error_code,
                }
            return _empty_handled_result(
                app_session_id=app_session_id,
                proposal_id=proposal_id,
                candidate_id=candidate.candidate_id,
                action_id=action_id,
                reason=error_code,
            )
        finally:
            bus.off(Method.AUIP_UPDATED, capture_receipt)

        if receipt.get("accepted") is not True:
            if automatic:
                return {
                    "status": "rejected",
                    "proposal_id": proposal_id,
                    "candidate_id": candidate.candidate_id,
                    "action_id": action_id,
                    "reason": str(receipt.get("reason") or "action_rejected"),
                }
            return _empty_handled_result(
                app_session_id=app_session_id,
                proposal_id=proposal_id,
                candidate_id=candidate.candidate_id,
                action_id=action_id,
                reason=str(receipt.get("reason") or "action_rejected"),
            )

        if automatic:
            logger.info(
                "[AUIP-B2] automatic accepted app_session=%s candidate=%s "
                "action_id=%s presentation=verified_event_lane",
                app_session_id,
                candidate.candidate_id,
                action_id,
            )
            return {
                "status": "accepted",
                "proposal_id": proposal_id,
                "candidate_id": candidate.candidate_id,
                "action_id": action_id,
                "receipt": dict(receipt),
                "presentation_owner": "verified_event_lane",
                "decision_path": decision_path,
                "selection_source": selection_source,
                "instruction_relation": instruction_relation,
            }

        delivered = False

        async def record_delivery(delivery: Mapping[str, Any]) -> None:
            nonlocal delivered
            if delivered or delivery.get("visible") is not True:
                return
            delivered = True
            self.runtime.record_delivered_narration(
                app_session_id=app_session_id,
                text=speech,
                event_id=action_id,
            )

        logger.info(
            "[AUIP-B2] accepted app_session=%s candidate=%s proposal=%s "
            "action_id=%s",
            app_session_id,
            candidate.candidate_id,
            proposal_id,
            action_id,
        )
        return {
            "handled": True,
            "display_text": speech,
            "voice_text_ja": speech,
            "speak": True,
            "save_history": False,
            "source": "auip_b2",
            "route_kind": "auip_b2_step",
            "provider": str(settings.AUIP_ACTION_PROVIDER or ""),
            "branch_id": app_session_id,
            "line_id": f"b2-{action_id}",
            "emotion": str(role_result.get("emotion") or "neutral")[:80],
            "app_session_id": app_session_id,
            "candidate_id": candidate.candidate_id,
            "proposal_id": proposal_id,
            "action_id": action_id,
            "receipt": dict(receipt),
            "decision_path": decision_path,
            "selection_source": selection_source,
            "instruction_relation": instruction_relation,
            "delivery_observer": record_delivery,
        }

    async def _restore_idle(self, app_session_id: str) -> None:
        try:
            projection = self.runtime.get(app_session_id)
            if (
                str(projection.get("status") or "") == "active"
                and projection.get("pending_action") is None
                and str(projection.get("operator_status") or "") == "thinking"
            ):
                result = self.runtime.set_operator_status(
                    app_session_id=app_session_id,
                    status="idle",
                    expected_decision_generation=int(
                        projection.get("decision_generation") or 0
                    ),
                )
                await bus.emit(Method.AUIP_UPDATED, _public_update(result))
        except AuipProtocolError:
            return


def _empty_handled_result(
    *,
    app_session_id: str,
    proposal_id: str,
    candidate_id: str,
    action_id: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "handled": True,
        "display_text": "",
        "speak": False,
        "save_history": False,
        "source": "auip_b2",
        "route_kind": "auip_b2_blocked",
        "branch_id": app_session_id,
        "app_session_id": app_session_id,
        "candidate_id": candidate_id,
        "proposal_id": proposal_id,
        "action_id": action_id,
        "reason": str(reason or "")[:240],
    }


def _selected_candidate(
    *,
    role_result: Mapping[str, Any],
    compiled: Any,
    hybrid_decision: bool,
) -> AuipActionCandidate:
    candidate_id = str(role_result.get("candidate_id") or "").strip()
    if candidate_id:
        candidate = compiled.candidates.get(candidate_id)
        if candidate is None:
            raise AuipProtocolError("b2_candidate_not_available")
        return candidate
    if not hybrid_decision:
        raise AuipProtocolError("b2_candidate_not_available")
    action_type = str(role_result.get("action_type") or "").strip().lower()
    if action_type not in set(compiled.uncovered_action_types):
        raise AuipProtocolError("b2_open_action_not_available")
    payload = validate_payload(role_result.get("payload") or {})
    encoded = json.dumps(
        [action_type, payload],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    spec = (compiled.context.get("available_actions") or {}).get(action_type)
    description = str(
        (spec if isinstance(spec, Mapping) else {}).get("description")
        or action_type
    )[:240]
    return AuipActionCandidate(
        candidate_id="open_" + hashlib.sha256(encoded).hexdigest()[:12],
        action_type=action_type,
        payload=payload,
        semantic_label=description,
        revision=int(compiled.context.get("revision") or 0),
        decision_generation=int(
            compiled.context.get("decision_generation") or 0
        ),
        source="open_schema_role",
    )


def _public_update(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in result.items()
        if key not in {"bridge_token", "manifest"}
    }
