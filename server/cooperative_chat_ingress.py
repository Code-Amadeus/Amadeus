"""Explicit real-ChatHandler assembly for opt-in cooperative authority.

This uses Chat's real turn grant/cancellation lifecycle with one injected runner.
TurnDecision admission freezes either a zero-effect decision or one cooperative
Provider effect before native I/O. Host context checkpoints preserve confirmed
native continuity and retain unresolved runs after reconstruction. The production
manager composes accepted deliverables with Work while focused AUIP and Browser
branches retain their existing domain owners. It does not fabricate active native
reattachment when the Provider cannot prove it.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import inspect
import json
import logging
from pathlib import Path
from typing import Any, Callable

from config.settings import PENDING_TURN_GATE_TIMEOUT_S
from core import session_manager as sm
from core.turn_coordinator import TurnAuthorityError, get_turn_coordinator
from agent_host.provider_runtime import ProviderRuntime, ProviderStartAdmissionRejected
from agent_host.provider_types import ProviderPermissionResponse
from agent_host.provider_workspace import workspace_route_authority
from agent_host.work_ledger_store import (
    WorkLedgerConflict,
    WorkLedgerError,
    WorkLedgerStore,
)
from server.handlers.chat_handler import ChatHandler
from server.control_ledger import ControlLedgerConflict, ControlLedgerStore
from server.cooperative_provider_loop import (
    CooperativeProviderLoop,
    LoopConflict,
    RoleDecisionUnavailable,
)
from server.cooperative_provider_effect import CooperativeProviderEffectLedger
from server.cooperative_context_store import CooperativeContextStore
from server.turn_admission import admission_transcript_hash
from server.event_bus import bus
from server.protocol import Method
from server.ai_os_schema import canvas_payload, work_signal
from server.attention_request import (
    AttentionOption,
    AttentionRequestCoordinator,
    attention_requests,
    opaque_option_id,
)
from server.work_destination_service import WorkDestinationService
from server.provider_event_ingestion import ProviderEventIngestor
from server.work_control import (
    CurrentTurnSourceSpanV1,
    WorkAmendPayloadV4,
    WorkControl,
    WorkCooperativeContextPayloadV6,
    WorkEffectPayloadV3,
)
from server.work_effect_executor import WorkEffectDispatch, WorkEffectExecutor
from server.reference_catalog import (
    TypedReferenceCandidate,
    candidate_catalog_from_coordinator,
)
from server.reference_clarification import (
    TypedReferenceResolution,
    resolve_typed_reference,
)
from server.compound_control import (
    CompoundControlPlan,
    MAX_COMPOUND_OPERATIONS,
)
from server.control_decision import (
    CONTROL_PAYLOAD_GROUNDING_ATTR,
    CONTROL_REFERENCE_CANDIDATES_ATTR,
)
from server.focus_policy import current_focus_modifier_audit
from server.auip_control_decision import (
    AuipControlDecision,
    auip_decision_preserves_main_context,
    render_auip_role_grounding,
)
from server.interaction_branch import (
    InteractionBranchRoutingLease,
    InteractionBranchRunStopUnconfirmed,
)


def _is_planned_after_work_decision(decision) -> bool:
    return bool(isinstance(decision, AuipControlDecision)
        and decision.status == "ok" and decision.timing == "after_work"
        and decision.action in {"engage", "launch"})


def _is_independent_inactive_entry(decision) -> bool:
    return bool(isinstance(decision, AuipControlDecision)
        and decision.status == "ok" and not decision.app_session_id
        and decision.timing == "now" and decision.work_relation == "independent"
        and decision.action in {"launch", "prepare"})


def _planned_after_work_context(decision) -> dict:
    return {"action":str(decision.action), "timing":"after_work",
        "mode":str(decision.mode or "observe"),
        "app_session_id":str(decision.app_session_id or "")}


def _entry_needs_discovery(decision) -> bool:
    return bool(isinstance(decision, AuipControlDecision)
        and decision.status == "ok" and decision.action == "engage"
        and decision.timing == "now" and not decision.app_session_id
        and decision.reason == "entry_target_not_found")


def _auip_decision_context(decision) -> dict:
    read_facets = tuple(getattr(decision, "read_facets", ()) or ())
    context = {"action":"read" if read_facets else str(
            getattr(decision, "action", "") or ""),
        "app_session_id":str(getattr(decision, "app_session_id", "") or ""),
        "timing":str(getattr(decision, "timing", "") or ""),
        "instruction":str(getattr(decision, "instruction", "") or "")}
    if _entry_needs_discovery(decision):
        context.update(target=decision.target, project_ref=decision.project_ref,
            reason=decision.reason)
    ambiguity = str(getattr(decision, "ambiguity", "") or "")
    if ambiguity:
        context.update(ambiguity=ambiguity,
            role_grounding=render_auip_role_grounding(decision))
    return context


def _is_work_or_app_ambiguity(decision) -> bool:
    return bool(isinstance(decision, AuipControlDecision)
        and decision.status == "ok" and decision.ambiguity == "work_or_app")


def _suppress_ambiguous_retracts(plan):
    if (not isinstance(plan, CompoundControlPlan) or plan.status != "ok"
            or len(plan.operations) != len(plan.clauses)):
        return plan, ()
    retained = [(operation, clause) for operation, clause in zip(
        plan.operations, plan.clauses, strict=True)
        if str(operation.action.get("intent") or "") != "retract"]
    suppressed = tuple(operation for operation in plan.operations
        if str(operation.action.get("intent") or "") == "retract")
    if not suppressed:
        return plan, ()
    return replace(plan,
        operations=tuple(replace(operation, operation_index=index)
            for index, (operation, _clause) in enumerate(retained)),
        clauses=tuple(clause for _operation, clause in retained)), suppressed


def _auip_transition_presentation_outcome(action: str, outcome: dict) -> dict:
    """Select lifecycle facts without copying unrelated gameplay history."""

    common = (
        "ok", "uncertain", "error", "reason", "detail", "app_session_id",
        "status", "surface_close_status", "surface_close_detail",
    )
    selected = {key:outcome[key] for key in common if key in outcome}
    app = outcome.get("app")
    if isinstance(app, dict):
        selected["app"] = {key:app[key] for key in ("id", "title") if key in app}
    # The surface status is the closure fact. "Did not stop an external
    # process" is not evidence that a process is still running.
    if action in {"observe", "collaborate", "delegate"}:
        selected.update({key:outcome[key] for key in (
            "changed", "stance", "engagement_mode") if key in outcome})
        controller = outcome.get("controller")
        if isinstance(controller, dict):
            selected["controller"] = {key:controller[key]
                for key in ("status", "reason") if key in controller}
    return selected


def _shared_role_history(session_id: str, current_turn_id: str) -> tuple[dict, ...]:
    """Read one originating Session snapshot without consulting another Session."""

    if sm.get_current_session_id() == session_id:
        history = sm.conversation_history.snapshot()
    else:
        history, _ = sm._read_session_history(session_id)
    rows = []
    for row in history.dialog:
        role = str(row.get("role") or "")
        text = str(row.get("content") or "")
        turn_id = str(row.get("turn_id") or "")
        if role not in {"user", "assistant"} or not text:
            continue
        if turn_id and turn_id == current_turn_id:
            continue
        rows.append({"source":"user" if role == "user" else "kurisu",
            "text":text,
            "input_id" if role == "user" else "cause":turn_id})
    return tuple(rows)


class CooperativeChatIngress:
    def __init__(self, loop: CooperativeProviderLoop, *, session_id: str,
                 ledger: ControlLedgerStore, fence_scope: str,
                 handler: ChatHandler | None = None, configure_handler: bool = True,
                 install_runtime_hooks: bool = True, scope_request: Callable | None = None,
                 address_request: Callable | None = None,
                 work_request: Callable | None = None,
                 auip_request: Callable | None = None,
                 browser_request: Callable | None = None):
        self.loop, self.session_id = loop, session_id
        history, _ = sm._read_session_history(session_id)
        loop.attach_state(CooperativeContextStore(ledger, session_id),
            install_runtime_hooks=install_runtime_hooks)
        loop.attach_effect_ledger(CooperativeProviderEffectLedger(ledger))
        loop.history.extend({"source":"user" if row["role"] == "user" else "kurisu",
            "text":row["content"],
            "input_id" if row["role"] == "user" else "cause":str(row.get("turn_id") or "")}
            for row in history.dialog if row["role"] in {"user", "assistant"} and row["content"])
        loop.history_source = lambda turn_id:_shared_role_history(
            self.session_id, turn_id)
        self.receipts = loop.receipts
        self.utterance_by_turn: dict[str, str] = {}
        self.scope_request = scope_request
        self.address_request = address_request
        self.work_request = work_request
        self.auip_request = auip_request
        self.browser_request = browser_request
        self.handler = handler or ChatHandler()
        self._close_handler = configure_handler
        if configure_handler:
            # Fresh isolated assembly: neither legacy model runner nor the earlier
            # interaction-branch router is installed alongside this runner.
            self.handler.configure(stream_llm_query=self.run, pending_sentence_items=None)
            self.handler.configure_control_ingress(
                ledger, fence_scope=fence_scope, authority_mode="turn_decision",
                turn_runner=self.run)

    async def recover(self):
        """One startup observation for retained unresolved contexts, without dispatch."""
        context_ids = [child["context_id"] for child in self.loop.context_catalog()
            if not child["closed"] and child["run_status"] in {"dispatching", "queued", "running", "orphaned"}]
        outcomes = await asyncio.gather(*(self.loop.reconcile_restored_context(child_id)
            for child_id in context_ids))
        return dict(zip(context_ids, outcomes, strict=True))

    def expire_unactionable_permissions(self, store: WorkLedgerStore, *, recovery):
        """Expire callbacks that no reconstructed Runtime run can answer."""

        expired = []
        for child in self.loop.context_catalog():
            observation = recovery.get(child["context_id"], {}) if isinstance(recovery, dict) else {}
            for permission in store.list_cooperative_permission_requests(
                    self.session_id, context_id=child["context_id"], status="pending"):
                if self.loop.runtime.get_run(permission.provider_run_id) is not None:
                    continue
                current_run = permission.provider_run_id == child["run_id"]
                try:
                    resolved = store.resolve_permission_request(permission.request_id,
                        "expired", metadata={
                            "resolution":"runtime_owner_unavailable_after_recovery",
                            "provider_run_status":(
                                child["run_status"] if current_run else "superseded"),
                            "recovery_state":str(
                                (observation.get("state") or "not_required")
                                if current_run else "superseded_run"),
                        })
                except WorkLedgerConflict:
                    continue
                expired.append({"session_id":self.session_id,
                    "context_id":child["context_id"],
                    "run_id":permission.provider_run_id,
                    "permission_request_id":resolved.request_id,
                    "state":"expired", "reason":resolved.metadata["resolution"]})
        return expired

    async def run(self, text, *, turn_admission=None, visual_context=None,
                  gui_callback=None,
                  interaction_branch_routing_lease=None, **_kwargs):
        if turn_admission is None or turn_admission.session_id != self.session_id:
            raise TurnAuthorityError("cooperative Chat requires its Host-bound Session")
        with self.loop.turn_dialogue_scope(turn_admission.turn_id):
            return await self._run(text, turn_admission=turn_admission,
                visual_context=visual_context, gui_callback=gui_callback,
                interaction_branch_routing_lease=interaction_branch_routing_lease, **_kwargs)

    async def _run(self, text, *, turn_admission=None, visual_context=None,
                   gui_callback=None, interaction_branch_routing_lease=None, **_kwargs):
        admission = turn_admission
        if admission is None or admission.session_id != self.session_id:
            raise TurnAuthorityError("cooperative Chat requires its Host-bound Session")

        def require_live_turn():
            owner = get_turn_coordinator()
            state = owner.snapshot()
            if (state["active_turn_id"] != admission.turn_id
                    or state["epochs"]["chat"] != admission.chat_epoch
                    or state["session_id"] != self.session_id
                    or sm.get_current_session_id() != self.session_id
                    or owner.turn_gate(admission.turn_id) not in (
                        {"wait", "proceed"} if admission.pending else {"proceed"})):
                raise TurnAuthorityError("cooperative Chat turn no longer authorizes an action")

        require_live_turn()
        key, turn_id = admission.utterance_id, admission.turn_id
        self.utterance_by_turn[turn_id] = key
        recorded = False

        async def confirm_for_acceptance():
            nonlocal admission, recorded
            if admission.pending:
                owner = get_turn_coordinator()
                gate = owner.turn_gate(turn_id)
                if gate == "wait":
                    gate = await asyncio.to_thread(owner.wait_turn_decided, turn_id,
                        PENDING_TURN_GATE_TIMEOUT_S)
                if gate != "proceed":
                    if gate == "wait" and self.handler is not None:
                        await self.handler.discard_pending_turn(turn_id, reason="pending_acceptance_timeout")
                    raise TurnAuthorityError("pending Chat was not confirmed for acceptance")
                require_live_turn()
                admission = replace(admission, pending=False)
                if self.handler is not None:
                    await self.handler._interrupt_background_interaction()
            require_live_turn()
            if not recorded:
                if not sm.append_session_message(self.session_id, role="user", content=text, turn_id=turn_id):
                    raise TurnAuthorityError("cooperative Chat source could not be recorded")
                recorded = True
            return admission

        if not admission.pending:
            await confirm_for_acceptance()
        independent_auip = None
        auip_entry = None
        planned_after_work = None
        after_work_dispatch = None
        if self.auip_request is not None:
            requested = self.auip_request(
                self, turn_id, text, admission, confirm_for_acceptance)
            if inspect.isawaitable(requested):
                requested = await requested
            if isinstance(requested, dict) and requested.get("handled") is True:
                receipt = dict(requested.get("receipt") or {})
                receipt.update(utterance_id=key, turn_id=turn_id)
                self.receipts[key] = receipt
                return next((row["text"] for row in reversed(self.loop.history)
                    if row.get("source") == "kurisu" and row.get("cause") == turn_id), "")
            if isinstance(requested, dict) and callable(requested.get("dispatch")):
                independent_auip = requested
            if isinstance(requested, dict):
                auip_entry = requested.get("entry")
                planned_after_work = requested.get("planned_after_work")
                after_work_dispatch = requested.get("after_work_dispatch")
        async def submit():
            return await self.loop.submit(text, input_id=key, turn_id=turn_id,
                gui_callback=gui_callback,
                admission_check=require_live_turn, turn_admission=admission,
                browser_routing_scope=interaction_branch_routing_lease,
                visual_context=visual_context, acceptance_check=confirm_for_acceptance,
                auip_context=(independent_auip["context"] if independent_auip else
                    _planned_after_work_context(planned_after_work)
                    if planned_after_work is not None else None),
                auip_entry=auip_entry)

        async def resolve_work(started=None, *, app_decision=None):
            try:
                receipt = await (started if started is not None else submit())
            except Exception as exc:
                # The independent app can accept a voice turn even if
                # speculative Work parsing already failed. Keep that
                # confirmed input in the shared conversation history.
                if not recorded:
                    await confirm_for_acceptance()
                if not any(row.get("source") == "user" and row.get("input_id") == key
                        for row in self.loop.history):
                    self.loop.history.append({"source":"user", "input_id":key,
                        "turn_id":turn_id, "text":text})
                if isinstance(exc, RoleDecisionUnavailable):
                    return {"state":"not_accepted",
                        "reason":"role_decision_unavailable", "input_id":key}, ""
                raise
            if (app_decision is not None
                    and receipt.get("state") == "auip_entry_required"):
                # The deferred focused owner already consumes this app proposal.
                # Preserve the one role line as composition input, not a second action.
                receipt["state"] = "no_action"
            if (app_decision is not None
                    and receipt.get("state") == "work_plan_required"):
                receipt["auip_context"] = _auip_decision_context(app_decision)
            if (app_decision is not None and receipt.get("state") == "no_action"
                    and self.loop._effects is not None):
                self.loop._effects.accept_no_effect(
                    admission, reason="conversation_only")
            work_say = str(receipt.get("coordination_say") or "")
            receipt["_host_defer_work_started_presentation"] = True
            # submit may have confirmed a pending voice turn. Resolve with that
            # updated admission, not its speculative copy.
            work = await self._resolve_cooperative_receipt(
                receipt, turn_id, admission, require_live_turn)
            if receipt.get("_host_coordination_delivered") is True:
                work_say = ""
            return work, work_say

        async def finish_entry(receipt, decision):
            if _entry_needs_discovery(decision):
                receipt["auip_context"] = _auip_decision_context(decision)
            if _is_work_or_app_ambiguity(decision):
                receipt["auip_context"] = _auip_decision_context(decision)
                if receipt.get("state") == "task_stop_resolution_required":
                    require_live_turn()
                    self.loop._effects.accept_no_effect(
                        admission, reason="work_or_app_ambiguous")
                    result = {"state":"no_action", "reason":"work_or_app_ambiguous",
                        "input_id":admission.utterance_id,
                        "utterance_id":admission.utterance_id, "turn_id":turn_id,
                        "question":text,
                        "auip_context":dict(receipt["auip_context"])}
                    self.receipts[admission.utterance_id] = result
                    try:
                        await self.loop._express_and_deliver(
                            {"source":"host_receipt", **result}, cause=turn_id)
                    except Exception as exc:
                        self.loop.trace.append({"kind":"presentation_failed",
                            "cause":turn_id, "source":"host_receipt",
                            "error":type(exc).__name__ + ": " + str(exc)})
                    return
            if (decision is None and receipt.get("state") == "work_plan_required"
                    and auip_entry.get("work_followup") is not None):
                # An empty history may skip the initial AUIP query. A Work
                # proposal makes its existing after-Work owner eligible.
                require_live_turn()
                decision = await auip_entry["work_followup"]()
            if (receipt.get("state") == "work_plan_required"
                    and _is_planned_after_work_decision(decision)):
                receipt["_host_auip_after_work_decision"] = decision
                receipt["auip_context"] = _planned_after_work_context(decision)
                receipt["_host_existing_after_work"] = lambda prior: auip_entry["dispatch"](
                    decision, prior, allow_independent_after_work=True)
                await self._resolve_cooperative_receipt(
                    receipt, turn_id, admission, require_live_turn)
            elif _is_independent_inactive_entry(decision):
                async def accepted_role_receipt():
                    return receipt

                completion = asyncio.create_task(self._finish_independent_auip(
                    lambda:resolve_work(accepted_role_receipt(),
                        app_decision=decision),
                    turn_id, admission, require_live_turn,
                    lambda:auip_entry["dispatch"](
                        decision, {}, independent_now=True), text))
                self.loop._monitors.add(completion)
                completion.add_done_callback(self.loop._monitors.discard)
                await asyncio.shield(completion)
            elif (receipt.get("state") == "auip_entry_required"
                    or (receipt.get("state") == "no_action"
                        and auip_entry["owns"](decision))):
                await auip_entry["dispatch"](decision, receipt)
            elif (receipt.get("state") == "no_action"
                    and _is_planned_after_work_decision(decision)
                    and self.loop.work_proposals_only
                    and self.loop._effects is not None):
                await auip_entry["dispatch"](
                    decision, receipt, allow_independent_after_work=True)
            else:
                if receipt.get("state") == "no_action" and self.loop._effects is not None:
                    self.loop._effects.accept_no_effect(admission, reason="conversation_only")
                await self._resolve_cooperative_receipt(
                    receipt, turn_id, admission, require_live_turn)

        try:
            if auip_entry is not None and auip_entry.get("focused") is True:
                role_task = asyncio.create_task(submit(), name="role:" + turn_id)
                self.loop._monitors.add(role_task)
                role_task.add_done_callback(self.loop._monitors.discard)

                async def cancel_unaccepted_role():
                    role_task.cancel()
                    stored = self.loop._inputs.get(key)
                    input_task = stored[1] if stored is not None else None
                    accepted = self.loop._effects.ledger.find_admission(
                        "chat:" + self.session_id, key)
                    effect_accepted = bool(accepted and accepted.get("plan_id"))
                    drain = [role_task]
                    if (input_task is not None and not input_task.done()
                            and not effect_accepted):
                        input_task.cancel()
                        drain.append(input_task)
                    await asyncio.gather(*drain, return_exceptions=True)

                try:
                    done, _pending = await asyncio.wait(
                        {role_task, auip_entry["pending"]},
                        return_when=asyncio.FIRST_COMPLETED)
                    if role_task in done and role_task.cancelled():
                        raise asyncio.CancelledError()
                    # A failed role interpretation cannot veto independent App
                    # authority. The existing composition records that failure
                    # once the focused decision resolves; cancellation is separate.
                    decision = await auip_entry["pending"]
                except BaseException:
                    await cancel_unaccepted_role()
                    raise
                if auip_entry["owns"](decision):
                    completion = asyncio.create_task(self._finish_independent_auip(
                        lambda:resolve_work(role_task, app_decision=decision),
                        turn_id, admission, require_live_turn,
                        lambda:auip_entry["dispatch"](decision, {}), text))
                    self.loop._monitors.add(completion)
                    completion.add_done_callback(self.loop._monitors.discard)
                    await asyncio.shield(completion)
                else:
                    try:
                        receipt = await role_task
                        await finish_entry(receipt, decision)
                    except BaseException:
                        await cancel_unaccepted_role()
                        raise
            elif independent_auip is not None:
                completion = asyncio.create_task(self._finish_independent_auip(
                    resolve_work, turn_id, admission, require_live_turn, independent_auip["dispatch"], text))
                self.loop._monitors.add(completion)
                completion.add_done_callback(self.loop._monitors.discard)
                await asyncio.shield(completion)
            else:
                receipt = await submit()
                if auip_entry is not None:
                    if receipt.get("state") == "browser_required":
                        await self._resolve_cooperative_receipt(
                            receipt, turn_id, admission, require_live_turn)
                    else:
                        decision = await auip_entry["pending"]
                        await finish_entry(receipt, decision)
                else:
                    if (planned_after_work is not None
                            and receipt.get("state") == "work_plan_required"):
                        receipt["_host_auip_after_work_decision"] = planned_after_work
                        receipt["_host_existing_after_work"] = after_work_dispatch
                    if (planned_after_work is not None
                            and receipt.get("state") == "no_action"
                            and callable(after_work_dispatch)):
                        await after_work_dispatch(receipt)
                        return next((row["text"] for row in reversed(self.loop.history)
                            if row.get("source") == "kurisu"
                            and row.get("cause") == turn_id), "")
                    await self._resolve_cooperative_receipt(receipt, turn_id, admission, require_live_turn)
        finally:
            if auip_entry is not None and not auip_entry["pending"].done():
                auip_entry["pending"].cancel()
                await asyncio.gather(auip_entry["pending"], return_exceptions=True)
        return next((row["text"] for row in reversed(self.loop.history)
            if row.get("source") == "kurisu" and row.get("cause") == turn_id), "")

    async def _finish_independent_auip(self, resolve_work, turn_id, admission, require_live_turn, dispatch_app, text):
        """Neither domain's interpretation gates the other domain's acceptance."""
        work_say = ""

        work, app = await asyncio.gather(resolve_work(), dispatch_app(), return_exceptions=True)
        if isinstance(work, BaseException):
            work = {"state":"unknown", "reason":"work_dispatch_failed:" + type(work).__name__}
        else:
            work, work_say = work
        if isinstance(app, BaseException):
            app = {"receipt":{"state":"auip_unknown",
                "reason":"auip_dispatch_failed:" + type(app).__name__}}
        app_only_read = bool(
            "read_facts" in app
            and work.get("state") == "no_action"
            and str(app.get("work_relation") or "") == "subsumed")
        if app_only_read:
            receipt = {**dict(app.get("receipt") or {}),
                "utterance_id":admission.utterance_id, "turn_id":turn_id}
        else:
            receipt = {"state":"work_auip_independent", "work":dict(work),
                "auip":dict(app.get("receipt") or {}),
                "utterance_id":admission.utterance_id, "turn_id":turn_id}
        self.receipts[admission.utterance_id] = receipt
        try:
            if "read_facts" in app:
                # One presentation owner answers from the final domain facts.
                # Keep genuinely independent Work results in the compound frame.
                event = ({"source":"host_receipt", "state":"auip_read",
                    "question":text, "facts":app["read_facts"],
                    "app_session_id":receipt["app_session_id"]}
                    if app_only_read else
                    {"source":"host_receipt", "state":"work_auip_independent",
                        "question":text, "work":dict(work),
                        "app_read_facts":app["read_facts"]})
                expressed = await self.loop._decide(event)
                work_say = str(expressed.get("say") or "")
            elif (work.get("state") == "no_action"
                    and str(app.get("work_relation") or "") == "subsumed"
                    and str(app.get("display_text") or "").strip()):
                # The App owner has the sole receipt-backed answer for an
                # app-complete turn. Main still assessed Work; its coarse role
                # acknowledgement is not a second application presentation.
                work_say = ""
            elif work.get("state") not in {"work_started", "work_input_accepted", "stopped", "no_action"}:
                expressed = await self.loop._decide({"source":"host_receipt", **work})
                work_say = str(expressed.get("say") or "")
            require_live_turn()
            published = await self.loop._deliver("\n".join(line for line in (
                work_say, str(app.get("display_text") or "")) if line), cause=turn_id)
            if published and app.get("delivery_observer") is not None:
                await app["delivery_observer"]({"visible":True})
        except Exception as exc:
            self.loop.trace.append({"kind":"presentation_failed", "cause":turn_id,
                "source":"host_receipt", "error":type(exc).__name__ + ": " + str(exc)})

    async def _resolve_cooperative_receipt(self, receipt, turn_id, admission, require_live_turn):
        """Continue the existing cooperative domain receipt through its owner."""
        key = admission.utterance_id
        receipt.update(utterance_id=key, turn_id=turn_id)
        self.receipts[key] = receipt
        if (receipt.get("_host_provider_message_continuation") is True
                and receipt.get("state") in {"rejected", "unknown"}):
            try:
                await self.loop._express_and_deliver(
                    {"source":"host_receipt", "turn_id":turn_id,
                        "state":receipt["state"],
                        "reason":str(receipt.get("reason") or "")},
                    cause=turn_id)
            except Exception as exc:
                self.loop.trace.append({"kind":"presentation_failed",
                    "cause":turn_id, "source":"host_receipt",
                    "error":type(exc).__name__ + ": " + str(exc)})
        elif receipt.get("state") == "scope_change_required" and self.scope_request is not None:
            requested = self.scope_request(self, turn_id, receipt)
            if inspect.isawaitable(requested):
                requested = await requested
            receipt["attention_request_id"] = str(
                requested.get("id") if isinstance(requested, dict) else ""
            )
            try:
                await self.loop._express_and_deliver(
                    {"source":"host_receipt", "turn_id":turn_id, **receipt},
                    cause=turn_id,
                )
            except Exception as exc:
                self.loop.trace.append({"kind":"presentation_failed",
                    "cause":turn_id, "source":"host_receipt",
                    "error":type(exc).__name__ + ": " + str(exc)})
        elif (receipt.get("state") == "address_selection_required"
                and self.address_request is not None):
            requested = self.address_request(self, turn_id, receipt, admission)
            if inspect.isawaitable(requested):
                requested = await requested
            receipt["attention_request_id"] = str(
                requested.get("id") if isinstance(requested, dict) else ""
            )
            try:
                await self.loop._express_and_deliver(
                    {"source":"host_receipt", "turn_id":turn_id,
                        "state":"address_selection_required",
                        "provider":receipt.get("provider"),
                        "candidate_count":len(receipt.get("candidate_context_ids") or ()),
                        "attention_request_id":receipt["attention_request_id"]},
                    cause=turn_id,
                )
            except Exception as exc:
                self.loop.trace.append({"kind":"presentation_failed",
                    "cause":turn_id, "source":"host_receipt",
                    "error":type(exc).__name__ + ": " + str(exc)})
        elif (receipt.get("state") == "work_plan_required"
                and self.work_request is not None):
            requested = self.work_request(self, turn_id, receipt, admission,
                require_live_turn=require_live_turn)
            if inspect.isawaitable(requested):
                requested = await requested
            receipt.clear()
            receipt.update(requested if isinstance(requested, dict) else {
                "state":"rejected", "reason":"work_owner_unavailable"})
        elif (receipt.get("state") in {"work_required",
                "work_amend_resolution_required", "work_input_required",
                "work_interrupt_required", "task_stop_resolution_required", "task_address_resolution_required", "work_report_required", "work_report_batch_required",
                "work_auip_batch_required"}
                and self.work_request is not None):
            requested = self.work_request(self, turn_id, receipt, admission)
            if inspect.isawaitable(requested):
                requested = await requested
            receipt.clear()
            receipt.update(requested if isinstance(requested, dict) else {
                "state":"rejected", "reason":"work_owner_unavailable"})
        elif receipt.get("state") == "browser_required" and self.browser_request is not None:
            requested = self.browser_request(
                self, turn_id, receipt, admission, require_live_turn)
            if inspect.isawaitable(requested):
                requested = await requested
            receipt.clear()
            receipt.update(requested if isinstance(requested, dict) else {
                "state":"rejected", "reason":"browser_owner_unavailable"})
        receipt.pop("_host_provider_message_continuation", None)
        return receipt

    async def close(self):
        if self.loop._owns_runtime:
            try:
                if self._close_handler:
                    await self.handler.close()
            finally:
                await self.loop.close()
            return
        try:
            await self.begin_close()
        finally:
            await self.finish_close()

    async def begin_close(self):
        if not self._close_handler:
            await self.loop.begin_close()
            return
        try:
            await self.handler.close()
        finally:
            await self.loop.begin_close()

    async def finish_close(self):
        await self.loop.finish_close()


class CooperativeChatManager:
    """Opt-in production composition for one ChatHandler and many Sessions.

    The Handler remains the single source-admission owner. Session-local loops
    share the production Runtime, whose one app-installed preparer routes exact
    cooperative slots here and every other request to the existing Work intake.
    """

    def __init__(self, handler: ChatHandler, *, ledger: ControlLedgerStore,
                 fence_scope: str, provider: str, runtime: ProviderRuntime,
                 context_requirements: dict, allocate: Callable,
                 query: Callable, persona: str = "", publish_factory: Callable | None = None,
                 role_provider_selector: Callable[[str], Any] | None = None,
                 permission_policy: str = "",
                 permission_store: WorkLedgerStore | None = None,
                 destination: WorkDestinationService | None = None,
                 work_planner: Callable | None = None,
                 attention: AttentionRequestCoordinator = attention_requests):
        if not isinstance(handler, ChatHandler):
            raise TypeError("cooperative Chat requires the production ChatHandler")
        if not provider or provider not in context_requirements:
            raise ValueError("cooperative Chat requires explicit Provider requirements")
        self.handler = handler
        self.ledger = ledger
        self.fence_scope = fence_scope
        self.provider = provider
        self.runtime = runtime
        self.context_requirements = dict(context_requirements)
        self.allocate = allocate
        self.query = query
        self.persona = persona
        self.publish_factory = publish_factory
        self.role_provider_selector = role_provider_selector
        self.permission_policy = str(permission_policy or "").strip().lower()
        if self.permission_policy not in {"", "deny", "ask"}:
            raise ValueError("unsupported cooperative permission policy")
        if self.permission_policy in {"deny", "ask"} and not isinstance(
                permission_store, WorkLedgerStore):
            raise ValueError("cooperative permission policy requires durable storage")
        self.permission_store = permission_store
        self.destination = destination
        if work_planner is not None and not callable(work_planner):
            raise TypeError("cooperative Work planner must be callable")
        self.work_planner = work_planner
        self.attention = attention
        self.ingresses: dict[str, CooperativeChatIngress] = {}
        self.recovery_receipts: dict[str, dict] = {}
        self.permission_receipts: list[dict] = []
        self._session_lock = asyncio.Lock()
        self._installed = False
        self._closed = False
        self._close_started = False
        self._close_finished = False
        self._closing_ingresses = ()
        self.work_control: WorkControl | None = None
        self.work_executor: WorkEffectExecutor | None = None
        self.work_input: Callable | None = None
        self.work_report: Callable | None = None
        self.work_focus: Callable | None = None
        self.auip_decider = None
        self.auip_router: Callable | None = None
        self.auip_cancel_deferred: Callable | None = None
        self.auip_step: Callable | None = None
        self.auip_entry_context: Callable | None = None
        self.browser_owner = None
        self._work_dispatches: dict[str, WorkEffectDispatch] = {}
        self._work_tasks: set[asyncio.Task] = set()
        self._activation_guard = handler.invalidate_session_context

    def install(self, *, pending_sentence_items=None, on_turn_finished=None,
                assistant_voice_sink=None, presentation_interrupt=None,
                background_interaction_interrupt=None) -> None:
        """Install the entire cohort while the production Handler is quiescent."""
        if self._installed or self._closed:
            raise TurnAuthorityError("cooperative Chat manager is not installable")
        if sm._activation_guard is not None:
            raise TurnAuthorityError("Session activation already has another fence owner")
        self.handler.configure(stream_llm_query=self.run,
            pending_sentence_items=pending_sentence_items, on_turn_finished=on_turn_finished,
            interaction_branch_router=None, assistant_voice_sink=assistant_voice_sink,
            presentation_interrupt=presentation_interrupt,
            background_interaction_interrupt=background_interaction_interrupt,
            abort_sink=self.abort_turn, permission_sink=self.resolve_permission)
        self.handler.configure_control_ingress(self.ledger, fence_scope=self.fence_scope,
            authority_mode="turn_decision", turn_runner=self.run,
            admission_preparer=self.prepare_session, allows_pending=True)
        sm.configure_activation_guard(self._activation_guard)
        if self.permission_policy in {"deny", "ask"}:
            bus.on(Method.PROVIDER_EVENT, self._handle_provider_event)
        if self.permission_policy == "ask":
            bus.on(Method.SESSION_CHANGED, self._handle_permission_session_changed)
        self._installed = True

    async def _ingress_for(self, session_id: str) -> CooperativeChatIngress:
        existing = self.ingresses.get(session_id)
        if existing is not None:
            return existing
        async with self._session_lock:
            existing = self.ingresses.get(session_id)
            if existing is not None:
                return existing
            if self._closed:
                raise TurnAuthorityError("cooperative Chat manager is closed")
            publisher = self.publish_factory(session_id) if self.publish_factory else None
            def resolve_initial_destination(provider_id, requirements):
                if self.destination is None:
                    return None
                manifest = self.runtime.get_manifest(provider_id)
                ownership = (requirements.workspace_ownership
                    or (manifest.capabilities.workspace_ownership
                        if manifest is not None else "none"))
                if (requirements.workspace_access == "none"
                        or workspace_route_authority(ownership) != "host"):
                    return None
                active = self.destination.store.get_session_work_context(session_id)
                if (active is not None
                        and active.metadata.get("explicit_context_binding") is True):
                    item = self.destination.store.get_work_item(
                        active.active_work_item_id)
                    if item is None or item.state == "archived":
                        raise LoopConflict("selected Work destination is unavailable")
                    # A workspace-less Work is still a valid conversation subject;
                    # it supplies no local receiving directory. Keep the Session's
                    # independent Project/default destination for later local work.
                    if item.workspace_mode != "none":
                        if not Path(item.workspace_path).is_dir():
                            raise LoopConflict("selected Work destination is unavailable")
                        return self.work_conversation_destination(item, provider_id, requirements)
                current = self.work_for_recipient(session_id, "")
                if (current is not None and active is not None
                        and active.active_work_item_id == current["work_item_id"]):
                    item = self.destination.store.get_work_item(current["work_item_id"])
                    if item is not None and item.workspace_mode != "none":
                        return self.work_conversation_destination(item, provider_id, requirements)
                binding = self.destination.store.get_conversation_binding(session_id)
                if binding is None:
                    return None
                project = self.destination.available_project(binding.project_id)
                route = {"status":"resolved", "source":"cooperative_session_project",
                    "projectId":project.project_id, "workItemId":"",
                    "cwd":project.canonical_path}
                workspace = project.canonical_path
                if binding.anchor_work_item_id:
                    item = self.destination.store.get_work_item(binding.anchor_work_item_id)
                    if (item is None or item.state == "archived"
                            or item.workspace_mode == "none"
                            or not Path(item.workspace_path).is_dir()
                            or item.project_id != project.project_id):
                        raise LoopConflict("selected Work destination is unavailable")
                    workspace = item.workspace_path
                    route.update(source="cooperative_session_work_item",
                        workItemId=item.work_item_id, cwd=item.workspace_path)
                return {"requirements":replace(requirements, workspace_access="read"),
                    "workspace":workspace, "workspace_route":route}

            def initial_destination(provider_id, requirements):
                # This is a fact projection for interpretation, not admission
                # to the selected directory. Its consuming first-send owner
                # revalidates the route and refuses an unavailable destination.
                try:
                    return resolve_initial_destination(provider_id, requirements)
                except (LoopConflict, WorkLedgerConflict) as exc:
                    return {"requirements":replace(requirements, workspace_access="read"),
                        "workspace":"", "workspace_route":{
                            "status":"invalid", "reason":str(exc)}}

            def validate_context_destination(child):
                route = child.workspace_route
                source = str(route.get("source") or "")
                if source not in {"cooperative_session_project",
                        "cooperative_session_work_item",
                        "cooperative_project_selection",
                        "cooperative_project_write_selection",
                        "cooperative_session_work_item_write"}:
                    return True
                if self.destination is None:
                    return False
                if source in {"cooperative_session_work_item",
                        "cooperative_session_work_item_write"}:
                    item = self.destination.store.get_work_item(
                        str(route.get("workItemId") or ""))
                    if (item is None or item.state == "archived"
                            or item.workspace_mode == "none"
                            or item.project_id != str(route.get("projectId") or "")):
                        return False
                    expected_workspace = item.workspace_path
                    if route.get("destinationKind") == "draft":
                        if not self.destination.is_unkept_draft(item.workspace_path):
                            return False
                    else:
                        project = self.destination.available_project(item.project_id)
                        if project.project_id != item.project_id:
                            return False
                else:
                    project = self.destination.available_project(
                        str(route.get("projectId") or ""))
                    expected_workspace = project.canonical_path
                return Path(expected_workspace).resolve() == Path(child.workspace).resolve()

            loop = CooperativeProviderLoop(self.runtime, self.query, self.allocate,
                provider=self.provider, context_requirements={provider:replace(requirements,
                    workspace_access="read") if requirements.workspace_access == "write" else requirements
                    for provider, requirements in self.context_requirements.items()},
                persona=self.persona, publish=publisher, owns_runtime=False,
                work_proposals_only=self.work_planner is not None,
                initial_destination=initial_destination,
                context_destination_validator=validate_context_destination,
                workspace_leases=self.permission_store,
                active_work=lambda context_id:self.active_work_for_recipient(
                    session_id, context_id),
                recipient_work=lambda context_id:self.work_for_recipient(session_id, context_id),
                role_app_context=lambda app_session_id="", transition_action="":
                    self.role_app_context_for_session(session_id,
                        app_session_id=app_session_id,
                        transition_action=transition_action),
                browser_context=lambda scope:self.browser_context_for_session(
                    session_id, scope))
            ingress = CooperativeChatIngress(loop, session_id=session_id, ledger=self.ledger,
                fence_scope=self.fence_scope, handler=self.handler, configure_handler=False,
                install_runtime_hooks=False, scope_request=self.request_scope_change,
                address_request=self.request_addressed_context,
                work_request=self.handle_work_action,
                auip_request=self.handle_auip_action,
                browser_request=self.handle_browser_action)
            loop.task_contexts = lambda:self.task_context_candidates(ingress, for_prompt=True)
            try:
                recovery = await ingress.recover()
                self.recovery_receipts[session_id] = recovery
                self._release_terminal_writer_leases(session_id, ingress.loop)
                for child in ingress.loop.children.values():
                    if child.run_status in {"done", "error", "cancelled"}:
                        ingress.loop._release_writer_lease(child, status="released",
                            metadata={"startup_terminal_context":child.run_status})
                if self.permission_store is not None:
                    self.permission_receipts.extend(
                        ingress.expire_unactionable_permissions(
                            self.permission_store, recovery=recovery
                        )
                    )
            except BaseException:
                await ingress.close()
                raise
            self.ingresses[session_id] = ingress
            return ingress

    def _release_terminal_writer_leases(self, session_id, loop) -> None:
        if self.permission_store is None:
            return
        for lease in self.permission_store.list_writer_leases(active_only=True):
            if lease.owner_kind != "cooperative_run" or lease.session_id != session_id:
                continue
            try:
                effect = self.ledger.get_effect(lease.provider_effect_id)
                payload = json.loads(effect["payload_json"])
            except (ControlLedgerConflict, TypeError, ValueError):
                continue
            if (effect["kind"] != "provider" or effect["state"] != "terminal"
                    or payload.get("session_id") != session_id
                    or payload.get("context_id") != lease.context_id
                    or Path(str(payload.get("workspace") or "")).resolve()
                    != Path(lease.workspace_path).resolve()
                    or (lease.provider_run_id
                        and effect["external_id"] != lease.provider_run_id)):
                continue
            released = self.permission_store.release_cooperative_writer_lease(
                lease.provider_effect_id,
                metadata={"startup_terminal_effect":True})
            if released is not None:
                loop.trace.append({"kind":"writer_lease_released",
                    "effect_id":lease.provider_effect_id,
                    "lease_id":lease.lease_id, "status":released.status,
                    "recovery":True})

    async def run(self, text, *, turn_admission=None, provider=None, **kwargs):
        if not self._installed or self._closed:
            raise TurnAuthorityError("cooperative Chat manager is unavailable")
        admission = turn_admission
        session_id = str(getattr(admission, "session_id", "") or "")
        if not session_id:
            raise TurnAuthorityError("cooperative Chat requires an admitted Session")
        if self.role_provider_selector is not None and provider:
            selected = self.role_provider_selector(str(provider))
            if inspect.isawaitable(selected):
                await selected
        ingress = await self._ingress_for(session_id)
        return await ingress.run(text, turn_admission=admission, **kwargs)

    async def prepare_session(self, admission) -> None:
        """Restore the receiving context before this source is durably admitted."""
        session_id = str(getattr(admission, "session_id", "") or "")
        if not session_id:
            raise TurnAuthorityError("cooperative Chat requires an admitted Session")
        ingress = await self._ingress_for(session_id)
        async with ingress.loop._foreground:
            summary = ingress.loop.context_facts(ingress.loop.bound_context_id)
            if summary is not None and summary.get("requirements", {}).get("workspace_access") == "write":
                child = ingress.loop.get_context(ingress.loop.bound_context_id)
                ingress.loop.prepare_conversation_contract(child)

    async def abort_turn(self, turn_id: str, session_id: str) -> dict:
        ingress = self.ingresses.get(session_id)
        utterance_id = ingress.utterance_by_turn.get(turn_id) if ingress is not None else None
        receipt = ingress.receipts.get(utterance_id) if ingress is not None and utterance_id else None
        if isinstance(receipt, dict) and isinstance(receipt.get("work"), dict):
            receipt = receipt.get("work")
        if not isinstance(receipt, dict) or not receipt.get("run_id"):
            return {"state":"not_active", "reason":"cooperative_run_not_found"}
        if receipt.get("state") == "work_started":
            return await self._stop_active_work(
                session_id, str(receipt.get("child_id") or ""), receipt)
        if not receipt.get("child_id"):
            return {"state":"not_active", "reason":"cooperative_run_not_found"}
        return await ingress.loop._apply(
            {"op":"interrupt", "recipient":receipt["child_id"]}, "",
            expected_run_id=receipt["run_id"],
        )

    async def request_scope_change(self, ingress, turn_id: str, receipt: dict) -> dict:
        """Offer one Host-owned, one-shot cooperative binding choice."""

        loop = ingress.loop
        expected_context_id = loop._binding.child_id
        expected_token = loop._binding.token
        choices = {}
        options = []
        for child in loop.context_catalog():
            if child["closed"]:
                continue
            option_id = opaque_option_id()
            choices[option_id] = ("existing", child["context_id"])
            workspace = Path(child["workspace"]).name if child["workspace"] else "no local workspace"
            options.append(AttentionOption(option_id=option_id,
                label=("Stay in " if child["context_id"] == expected_context_id else "Switch to ")
                    + workspace,
                description=f"{child['provider']} · {child['label']}",
                metadata={"scope":"cooperative_context",
                    "relation":"current" if child["context_id"] == expected_context_id else "existing"}))
        if not expected_context_id:
            option_id = opaque_option_id()
            choices[option_id] = ("stay", "")
            options.append(AttentionOption(option_id=option_id,
                label="Stay in the current chat", description="Do not create an execution context",
                metadata={"scope":"cooperative_context", "relation":"current"}))
        for provider_id in sorted(loop.context_requirements):
            new_option_id = opaque_option_id()
            choices[new_option_id] = ("new", provider_id)
            options.append(AttentionOption(option_id=new_option_id,
                label=("New isolated workspace" if provider_id == loop.provider
                    else f"New {provider_id} context"),
                description=f"Create a separate {provider_id} receiving context",
                metadata={"scope":"cooperative_context", "relation":"new"}))
        primary_requirements = loop.context_requirements[loop.provider]
        destination = getattr(self, "destination", None)
        manifest = loop.runtime.get_manifest(loop.provider)
        primary_ownership = (primary_requirements.workspace_ownership
            or (manifest.capabilities.workspace_ownership
                if manifest is not None else "none"))
        if (destination is not None
                and primary_requirements.workspace_access in {"read", "write"}
                and workspace_route_authority(primary_ownership) == "host"):
            project_context = destination.workspace_routing_context(limit=8)
            if project_context.get("candidatesComplete") is True:
                for project in project_context.get("candidates") or []:
                    project_id = str(project.get("projectId") or "")
                    if not project_id:
                        continue
                    option_id = opaque_option_id()
                    choices[option_id] = ("project", project_id)
                    name = str(project.get("projectName") or "Project")
                    options.append(AttentionOption(option_id=option_id,
                        label=f"Read-only project {name}",
                        description=str(project.get("workspacePath") or ""),
                        metadata={"scope":"cooperative_context", "relation":"new"}))
            active = destination.store.get_session_work_context(ingress.session_id)
            if (active is not None
                    and active.metadata.get("explicit_context_binding") is True):
                item = destination.store.get_work_item(active.active_work_item_id)
                if (item is not None and item.state != "archived"
                        and item.workspace_mode != "none"
                        and Path(item.workspace_path).is_dir()):
                    item_option_id = opaque_option_id()
                    choices[item_option_id] = ("work_read", item.work_item_id)
                    is_draft = destination.is_unkept_draft(item.workspace_path)
                    kind_label = "draft" if is_draft else "work"
                    options.append(AttentionOption(option_id=item_option_id,
                        label=f"Read-only selected {kind_label} {item.title}",
                        description=item.workspace_path,
                        metadata={"scope":"cooperative_context", "relation":"new"}))

        async def continue_once(option_id: str):
            choice = choices.get(option_id)
            if choice is None:
                raise LoopConflict("scope option is not part of this request")
            if sm.get_current_session_id() != ingress.session_id:
                raise LoopConflict("scope request Session is no longer current")
            async with loop._foreground:
                if (loop._binding.child_id, loop._binding.token) != (
                        expected_context_id, expected_token):
                    raise LoopConflict("scope request binding changed")
                kind, target_id = choice
                if kind == "existing" and target_id != expected_context_id:
                    loop.bind_context(target_id)
                elif kind == "new":
                    child = loop._create_context("Amadeus workspace", target_id)
                    loop.bind_context(child.child_id)
                elif kind == "project":
                    if destination is None:
                        raise LoopConflict("Project destination owner is unavailable")
                    project = destination.available_project(target_id)
                    base = loop.context_requirements[loop.provider]
                    child = loop._create_context(
                        project.name or Path(project.canonical_path).name,
                        loop.provider,
                        requirements=replace(base, workspace_access="read"),
                        workspace=project.canonical_path,
                        workspace_route={"status":"resolved",
                            "source":"cooperative_project_selection",
                            "projectId":project.project_id, "workItemId":"",
                            "cwd":project.canonical_path},
                    )
                    loop.bind_context(child.child_id)
                elif kind == "work_read":
                    if destination is None:
                        raise LoopConflict("Work destination owner is unavailable")
                    active = destination.store.get_session_work_context(ingress.session_id)
                    if (active is None or active.active_work_item_id != target_id
                            or active.metadata.get("explicit_context_binding") is not True):
                        raise LoopConflict("selected Work destination changed")
                    item = destination.store.get_work_item(target_id)
                    if (item is None or item.state == "archived"
                            or item.workspace_mode == "none"
                            or not Path(item.workspace_path).is_dir()):
                        raise LoopConflict("selected Work destination is unavailable")
                    is_draft = destination.is_unkept_draft(item.workspace_path)
                    if not is_draft:
                        project = destination.available_project(item.project_id)
                        if project.project_id != item.project_id:
                            raise LoopConflict("selected Work Project changed")
                    base = loop.context_requirements[loop.provider]
                    child = loop._create_context(
                        item.title, loop.provider,
                        requirements=replace(base, workspace_access="read"),
                        workspace=item.workspace_path,
                        workspace_route={"status":"resolved",
                            "source":"cooperative_session_work_item",
                            "destinationKind":"draft" if is_draft else "work_item",
                            "projectId":item.project_id,
                            "workItemId":item.work_item_id,
                            "cwd":item.workspace_path},
                    )
                    loop.bind_context(child.child_id)
                selected = loop.get_context(loop.bound_context_id)
                outcome = {"state":"scope_bound", "turn_id":turn_id,
                    "context":loop._context_facts(selected) if selected is not None else None}
                presentation = {"source":"host_receipt", "state":"scope_bound",
                    "turn_id":turn_id,
                    "context":({"provider":selected.provider,
                        "workspace":selected.workspace or None}
                        if selected is not None else None)}
                presentation_cause = (
                    f"scope_bound:{turn_id}:{selected.child_id}"
                    if selected is not None else f"scope_bound:{turn_id}:chat"
                )
                loop.history.append(dict(presentation))
                loop.trace.append({"kind":"scope_bound_fact",
                    "cause":presentation_cause,
                    "provider":selected.provider if selected is not None else ""})
            try:
                await loop._express_and_deliver(
                    presentation, cause=presentation_cause
                )
            except Exception as exc:
                loop.trace.append({"kind":"presentation_failed",
                    "cause":presentation_cause, "source":"host_receipt",
                    "error":type(exc).__name__ + ": " + str(exc)})
            return outcome

        target = " ".join(str(receipt.get("target") or "").split())[:160]
        prompt = "Switch future messages without closing or rewriting earlier contexts."
        if target:
            prompt = f"Requested target: {target}. " + prompt
        return await self.attention.create_selection(session_id=ingress.session_id,
            title="Choose the receiving context",
            prompt=prompt,
            options=options, continuation=continue_once,
            dedupe_key="cooperative_scope_change")

    async def request_addressed_context(self, ingress, turn_id: str, receipt: dict,
                                        admission) -> dict:
        """Resolve one ambiguous retained recipient without changing default scope."""

        loop = ingress.loop
        expected_context_id = str(receipt.get("source_binding_context_id") or "")
        expected_token = str(receipt.get("source_binding_token") or "")
        candidate_ids = tuple(str(value) for value in
            (receipt.get("candidate_context_ids") or ()))
        candidates = [row for row in loop.context_catalog()
            if row["context_id"] in candidate_ids and not row["closed"]]
        if len(candidates) < 2:
            raise LoopConflict("address selection no longer has multiple candidates")
        choices = {}
        options = []
        for child in candidates:
            option_id = opaque_option_id()
            choices[option_id] = child["context_id"]
            workspace = Path(child["workspace"]).name if child["workspace"] else "no local workspace"
            options.append(AttentionOption(option_id=option_id,
                label=f"Send to {child['label']} · {workspace}",
                description=child["provider"],
                metadata={"scope":"cooperative_context", "relation":"existing"}))

        async def continue_once(option_id: str):
            target_id = choices.get(option_id)
            if target_id is None:
                raise LoopConflict("address option is not part of this request")
            if sm.get_current_session_id() != ingress.session_id:
                raise LoopConflict("address request Session is no longer current")
            async with loop._foreground:
                if (loop._binding.child_id, loop._binding.token) != (
                        expected_context_id, expected_token):
                    raise LoopConflict("address request binding changed")
                target = loop.get_context(target_id)
                if (target is None or target.closed
                        or target.child_id not in candidate_ids):
                    raise LoopConflict("address request target changed")
                actual = await loop._apply({"op":"send",
                    "recipient":target.child_id,
                    "source_binding_context_id":expected_context_id},
                    str(receipt.get("text") or ""),
                    parent_context=str(receipt.get("parent_context") or ""),
                    input_id=str(receipt.get("input_id") or ""),
                    turn_id=turn_id, binding_token=expected_token,
                    turn_admission=admission, foreground_owned=True)
                actual.update(input_id=str(receipt.get("input_id") or ""),
                    utterance_id=str(receipt.get("input_id") or ""),
                    turn_id=turn_id)
                ingress.receipts[str(receipt.get("input_id") or "")] = actual
                return {"state":"addressed", "receipt":actual,
                    "provider":target.provider}

        return await self.attention.create_selection(session_id=ingress.session_id,
            title="Choose the reply context",
            prompt="Send this message without changing the default receiving context.",
            options=options, continuation=continue_once,
            dedupe_key="cooperative_addressed_context")

    def task_context_candidates(self, ingress, *, for_prompt=False):
        """Address Work objects and independent Provider conversations once each."""
        candidates, complete, reason, targets = self.task_stop_candidates(ingress,
            include_work=getattr(self.work_executor, "coordinator", None) is not None,
            work_item_limit=5 if for_prompt else 200)
        represented = {candidate.entity_id for candidate in candidates if candidate.kind == "work_item"}
        kept = []
        for candidate in candidates:
            target = targets[candidate.token]
            child = ingress.loop.get_context(target.get("child_id", ""))
            if target["kind"] == "provider" and child and child.work_item_id in represented:
                continue
            kept.append(candidate)
        return tuple(kept), complete, reason, {candidate.token:targets[candidate.token] for candidate in kept}

    async def resolve_task_address(self, ingress, turn_id, receipt, admission):
        """Resolve a task reference, then reuse the existing addressed-send boundary."""
        loop, frozen = ingress.loop, dict(receipt)
        candidates, complete, reason, targets = self.task_context_candidates(ingress)
        provider = str(frozen.get("provider") or "")
        candidates = tuple(candidate for candidate in candidates
            if not provider or targets[candidate.token]["kind"] == "work"
            or targets[candidate.token]["provider"] == provider)
        # A role-proposed token proves catalog membership, not that the user
        # addressed that task. Resolve the admitted request, as task stop does.
        phrase = str(frozen.get("text") or "")
        resolution = self._known_task_reference(phrase, candidates, complete=complete)
        if resolution is None:
            history = loop.prior_messages(turn_id)
            resolution = await resolve_typed_reference(phrase, candidates,
                complete=complete, query=lambda messages:self.query([
                    {**messages[0], "content":messages[0]["content"] +
                        "\n今回はメッセージの送信先タスクの解決です。今回の追加指示を受け取る対象だけを選び、"
                        "そのまま維持するよう述べられた別タスクは選ばないでください。"
                        "既定の接続先であることだけを対象の根拠にしてはいけません。"},
                    *messages[1:]]), history=history)
        return await self._continue_resolved_task_address(
            ingress, turn_id, frozen, admission, resolution,
            catalog=(candidates, complete, reason, targets))

    async def _send_resolved_task_address(self, ingress, turn_id, frozen,
                                          admission, candidate, target,
                                          admission_check=None):
        """Deliver one revalidated task candidate through the existing owners."""
        loop = ingress.loop
        provider = str(frozen.get("provider") or "")
        work_input_receipt = None
        async with loop._foreground:
            if admission_check is not None:
                admission_check()
            if sm.get_current_session_id() != ingress.session_id:
                raise LoopConflict("task address Session changed")
            if (loop._binding.child_id, loop._binding.token) != (
                    frozen["source_binding_context_id"], frozen["source_binding_token"]):
                raise LoopConflict("task address source binding changed")
            text = str(frozen.get("text") or "")
            CurrentTurnSourceSpanV1.capture(admission, text, start=0, end=len(text))
            current, _, _, current_targets = self.task_context_candidates(ingress)
            current_candidate = next((item for item in current
                if item.token == candidate.token), None)
            if (current_candidate is None
                    or any((current_targets.get(candidate.token) or {}).get(key)
                        != target.get(key) for key in (
                            "kind", "child_id", "provider"))):
                raise LoopConflict("task address identity changed")
            if target["kind"] == "work":
                work_id = target["work_item_id"]
                active = self.active_work_for_recipient(ingress.session_id,
                    frozen["source_binding_context_id"], work_item_id=work_id)
                if active is not None:
                    work_input_receipt = {**frozen, **active,
                        "child_id":frozen["source_binding_context_id"]}
                    child = None
                else:
                    child = self.work_conversation_context(ingress, work_id,
                        provider or self.provider)
            else:
                child = loop.get_context(target["child_id"])
            if work_input_receipt is not None:
                actual = {}
            elif child is None or child.closed or (target["kind"] == "provider"
                    and child.provider != target["provider"]):
                loop._effects.accept_no_effect(admission,
                    reason="addressed_context_unavailable")
                actual = {"state":"rejected", "reason":"addressed_context_unavailable"}
            elif self.runtime.get_manifest(child.provider) is None:
                loop._effects.accept_no_effect(admission, reason="provider_unavailable")
                actual = {"state":"rejected", "reason":"provider_unavailable"}
            else:
                active = loop.active_work(child.child_id) if loop.active_work is not None else None
                if active is not None:
                    loop._effects.accept_no_effect(admission, reason="addressed_task_busy")
                    actual = {"state":"rejected", "reason":"addressed_task_busy",
                        "child_id":child.child_id}
                else:
                    actual = await loop._apply({"op":"send", "recipient":child.child_id,
                        **({"continuation_effect_id":candidate.entity_id}
                            if target["kind"] == "provider" else {}),
                        "source_binding_context_id":frozen["source_binding_context_id"]}, text,
                        parent_context=str(frozen.get("parent_context") or ""),
                        input_id=admission.utterance_id, turn_id=turn_id,
                        binding_token=frozen["source_binding_token"], turn_admission=admission,
                        foreground_owned=True, admission_check=admission_check)
            if work_input_receipt is None:
                actual.update(input_id=admission.utterance_id,
                    utterance_id=admission.utterance_id, turn_id=turn_id)
                ingress.receipts[admission.utterance_id] = actual
        if work_input_receipt is not None:
            # Durable input delivery and its role expression must not hold
            # the foreground semantic lock.
            return await self._handle_work_input(
                ingress, turn_id, work_input_receipt, admission)
        if actual["state"] in {"rejected", "unknown"}:
            await loop._express_and_deliver(
                {"source":"host_receipt", **actual}, cause=turn_id)
        else:
            await loop._deliver(str(frozen.get("coordination_say") or ""),
                cause=turn_id)
        return actual

    async def _continue_resolved_task_address(self, ingress, turn_id, receipt,
                                              admission, resolution, *, catalog,
                                              admission_check=None):
        """Revalidate a typed resolution, then enter the shared delivery owner."""
        if not isinstance(resolution, TypedReferenceResolution):
            raise TypeError("task address resolution must be typed")
        loop, frozen = ingress.loop, dict(receipt)
        candidates, complete, _reason, targets = catalog
        provider = str(frozen.get("provider") or "")
        candidates = tuple(candidate for candidate in candidates
            if not provider or targets[candidate.token]["kind"] == "work"
            or targets[candidate.token]["provider"] == provider)
        known = {candidate.token:candidate for candidate in candidates}
        status = resolution.status
        selected = tuple(known[candidate.token] for candidate in resolution.candidates
            if candidate.token in known)
        if not complete:
            status = "incomplete"
        elif (status in {"unique", "ambiguous"}
                and len(selected) != len(resolution.candidates)):
            status = "none"

        if status == "unique":
            candidate = selected[0]
            return await self._send_resolved_task_address(
                ingress, turn_id, frozen, admission, candidate,
                targets[candidate.token], admission_check=admission_check)
        if status == "ambiguous":
            choices, options = {}, []
            for candidate in selected:
                option_id = opaque_option_id()
                choices[option_id] = candidate
                options.append(AttentionOption(option_id=option_id, label=candidate.label,
                    description=str(targets[candidate.token].get("provider")
                        or targets[candidate.token].get("execution_provider") or "")))
            async def continue_once(option_id):
                if option_id not in choices:
                    raise LoopConflict("task address option changed")
                candidate = choices[option_id]
                return await self._send_resolved_task_address(
                    ingress, turn_id, frozen, admission, candidate,
                    targets[candidate.token], admission_check=admission_check)
            request = await self.attention.create_selection(session_id=ingress.session_id,
                title="Choose the task to continue", prompt="Which task should receive this message?",
                options=options, continuation=continue_once, dedupe_key="cooperative_addressed_context")
            result = {"state":"task_address_selection_required", "attention_request_id":request["id"]}
        else:
            reason = "task_address_target_" + status
            loop._effects.accept_no_effect(admission, reason=reason)
            result = {"state":"rejected", "reason":reason}
        result.update(input_id=admission.utterance_id, utterance_id=admission.utterance_id, turn_id=turn_id)
        ingress.receipts[admission.utterance_id] = result
        await loop._express_and_deliver({"source":"host_receipt", **result}, cause=turn_id)
        return result

    def configure_work(self, control: WorkControl, executor: WorkEffectExecutor, *,
                       input_request: Callable | None = None,
                       report_request: Callable | None = None,
                       focus_request: Callable | None = None) -> None:
        if self.work_control is not None and self.work_control is not control:
            raise TurnAuthorityError("cooperative Work owner is already configured")
        if not isinstance(control, WorkControl) or not isinstance(executor, WorkEffectExecutor):
            raise TypeError("cooperative Work requires WorkControl and WorkEffectExecutor")
        self.work_control = control
        self.work_executor = executor
        self.work_input = input_request
        self.work_report = report_request
        if focus_request is not None and not callable(focus_request):
            raise TypeError("cooperative Work focus owner must be callable")
        self.work_focus = focus_request

    def configure_auip(self, decider, router: Callable, *,
                       cancel_deferred: Callable | None = None,
                       step_request: Callable | None = None,
                       entry_context: Callable | None = None) -> None:
        """Install the existing source-local AUIP decision and domain router."""

        if not callable(getattr(decider, "capture", None)):
            raise TypeError("cooperative AUIP requires the existing decision resolver")
        if not callable(router):
            raise TypeError("cooperative AUIP requires the existing domain router")
        if cancel_deferred is not None and not callable(cancel_deferred):
            raise TypeError("cooperative AUIP deferred cancellation must be callable")
        if entry_context is not None and not callable(entry_context):
            raise TypeError("cooperative AUIP entry context must be callable")
        self.auip_decider = decider
        self.auip_router = router
        self.auip_cancel_deferred = cancel_deferred
        self.auip_step = step_request
        self.auip_entry_context = entry_context

    def configure_browser(self, owner) -> None:
        """Install the existing InteractionBranch domain owner."""

        if not all(callable(getattr(owner, name, None)) for name in (
                "capture_routing_lease", "resolve_routing_lease",
                "start_from_turn", "continue_from_delegate",
                "close_from_routing_lease")):
            raise TypeError("cooperative Browser requires InteractionBranch ownership")
        self.browser_owner = owner

    def browser_context_for_session(self, session_id: str, raw_scope) -> dict | None:
        if self.browser_owner is None or not isinstance(raw_scope, dict):
            return None
        if str(raw_scope.get("state") or "") != "bound":
            return None
        lease = InteractionBranchRoutingLease.from_mapping(raw_scope)
        if lease is None or lease.parent_session_id != str(session_id or ""):
            return None
        branch = self.browser_owner.resolve_routing_lease(lease)
        if branch is None:
            return None
        return {"model":{"status":branch.status,
                "title":str(branch.title or "")[:160],
                "url":str(branch.url or "")[:800],
                "page_summary":str(branch.page_summary or "")[:500],
                "pending_goal":str(branch.pending_goal or "")[:500],
                "can_continue":True, "can_close":True}}

    async def handle_browser_action(self, ingress, turn_id: str, receipt: dict,
                                    admission, require_live_turn: Callable) -> dict:
        """Consume one exact captured Browser lease without continue-to-new fallback."""

        scope = receipt.get("routing_scope")
        lease = InteractionBranchRoutingLease.from_mapping(scope)
        intent = str(receipt.get("intent") or "")
        require_live_turn()
        ingress.loop._effects.accept_no_effect(admission,
            reason="browser_" + (intent or "invalid"))
        state = "rejected"
        reason = "browser_routing_lease_unavailable"
        run = {}
        if (self.browser_owner is not None and intent == "open"
                and isinstance(scope, dict)):
            try:
                domain_receipt = await self.browser_owner.start_from_turn(
                    session_id=ingress.session_id,
                    source_user_text=str(receipt.get("text") or ""),
                    target_url=str(receipt.get("target") or ""),
                    turn_id=turn_id, routing_scope=scope,
                    admission_check=require_live_turn)
                if domain_receipt is None:
                    state, reason = "rejected", "browser_entry_scope_stale"
                else:
                    run = dict(domain_receipt.run)
                    reason = domain_receipt.reason
                    state = "accepted" if domain_receipt.accepted else "unknown"
            except Exception as exc:
                state, reason = "unknown", "browser_start_failed:" + type(exc).__name__
        elif (self.browser_owner is not None and lease is not None
                and lease.parent_session_id == ingress.session_id):
            try:
                if intent == "close":
                    accepted = await self.browser_owner.close_from_routing_lease(
                        lease, reason="cooperative_user_close",
                        admission_check=require_live_turn)
                    state = "closed" if accepted else "rejected"
                    reason = "closed" if accepted else "browser_routing_lease_stale"
                elif intent == "continue":
                    domain_receipt = await self.browser_owner.continue_from_delegate(
                        session_id=ingress.session_id,
                        task=str(receipt.get("text") or ""),
                        source_user_text=str(receipt.get("text") or ""),
                        turn_id=turn_id, routing_lease=lease,
                        admission_check=require_live_turn)
                    if domain_receipt is None:
                        state, reason = "rejected", "browser_routing_lease_stale"
                    else:
                        run = dict(domain_receipt.run)
                        reason = domain_receipt.reason
                        if domain_receipt.accepted:
                            state = "accepted"
                        elif domain_receipt.execution_started is False:
                            state = "rejected"
                        else:
                            state = "unknown"
            except InteractionBranchRunStopUnconfirmed as exc:
                state, reason = "unknown", "run_stop_unconfirmed:" + exc.reason
        result = {"state":"browser_" + state, "reason":reason,
            "intent":intent, "input_id":admission.utterance_id,
            "utterance_id":admission.utterance_id, "turn_id":turn_id,
            "run_id":str(run.get("run_id") or "")}
        ingress.receipts[admission.utterance_id] = result
        if state in {"accepted", "closed"}:
            await ingress.loop._deliver(
                str(receipt.get("coordination_say") or ""), cause=turn_id)
        else:
            try:
                await ingress.loop._express_and_deliver({"source":"host_receipt",
                    "state":result["state"], "reason":reason,
                    "intent":intent}, cause=turn_id)
            except Exception as exc:
                ingress.loop.trace.append({"kind":"presentation_failed",
                    "cause":turn_id, "source":"host_receipt",
                    "error":type(exc).__name__ + ": " + str(exc)})
        return result

    async def handle_auip_action(self, ingress, turn_id: str, text: str,
                                 admission, require_live_turn: Callable) -> dict | None:
        """Route focused control; schedule every inactive entry behind the role."""

        if self.auip_decider is None or self.auip_router is None:
            return None
        binding = ingress.loop._binding
        history = ingress.loop.prior_messages(turn_id)

        def record_decision(decision, *, work_followup=False, source=""):
            observation = {"kind":"auip_entry_decision", "turn_id":turn_id,
                "work_followup":work_followup,
                "status":getattr(decision, "status", "absent"),
                "action":getattr(decision, "action", "none"),
                "timing":getattr(decision, "timing", ""),
                "work_relation":getattr(decision, "work_relation", ""),
                "reason":getattr(decision, "reason", ""),
                "raw":getattr(decision, "raw_reply", "")}
            if source:
                observation["source"] = source
            ingress.loop.trace.append(observation)
            logging.getLogger(__name__).info("[COOPERATIVE-AUIP-ENTRY] %s",
                json.dumps(observation, ensure_ascii=False, separators=(",", ":")))
            return decision

        pending = self.auip_decider.capture(session_id=ingress.session_id,
            user_text=text, prior_messages=history,
            active_required=True, result_entry=self.work_planner is not None)
        if pending is None:
            context = getattr(self, "auip_entry_context", None)
            if context is None:
                return None
            prompt = await asyncio.to_thread(context, ingress.session_id)
            release = asyncio.Event()

            async def capture_entry(*, include_work_followup=False):
                # Historical artifacts are capability candidates. Their query
                # starts behind the role request, outside semantic input locks.
                await release.wait()
                captured = await asyncio.to_thread(self.auip_decider.capture,
                    session_id=ingress.session_id, user_text=text, prior_messages=history,
                    include_work_followup=include_work_followup,
                    result_entry=self.work_planner is not None)
                decision = await captured if inspect.isawaitable(captured) else captured
                return record_decision(decision, work_followup=include_work_followup)

            task = asyncio.create_task(capture_entry(), name="auip-entry:" + turn_id)
            ingress.loop._monitors.add(task)
            task.add_done_callback(ingress.loop._monitors.discard)

            async def dispatch_entry(decision, receipt, *,
                                     allow_independent_after_work=False,
                                     independent_now=False):
                return await self._apply_inactive_auip_entry(ingress, turn_id, text, admission,
                    require_live_turn, binding, decision, receipt, history,
                    allow_independent_after_work=allow_independent_after_work,
                    independent_now=independent_now)

            return {"entry":{"pending":task, "release":release, "prompt":str(prompt or ""),
                "work_followup":lambda: capture_entry(include_work_followup=True),
                "owns":self._owns_inactive_auip_entry, "dispatch":dispatch_entry}}
        async def dispatch_focused(decision, *, independent):
            return await self._dispatch_focused_auip_decision(
                ingress, turn_id, text, admission, require_live_turn,
                decision, independent=independent)

        if self.work_planner is not None:
            async def capture_focused():
                decision = await pending if inspect.isawaitable(pending) else pending
                return record_decision(decision, source="focused_active")

            task = asyncio.create_task(
                capture_focused(), name="auip-focused:" + turn_id)
            ingress.loop._monitors.add(task)
            task.add_done_callback(ingress.loop._monitors.discard)
            release = asyncio.Event()

            async def dispatch_deferred(decision, receipt, *,
                                        allow_independent_after_work=False):
                if _is_planned_after_work_decision(decision):
                    return await self._apply_inactive_auip_entry(
                        ingress, turn_id, text, admission, require_live_turn,
                        binding, decision, receipt, history,
                        allow_independent_after_work=allow_independent_after_work)
                return await dispatch_focused(decision, independent=True)

            return {"entry":{"pending":task, "release":release, "prompt":"",
                "owns":self._owns_focused_auip,
                "dispatch":dispatch_deferred, "focused":True}}

        decision = await pending if inspect.isawaitable(pending) else pending
        record_decision(decision, source="focused_active")
        work_relation = str(getattr(decision, "work_relation", "") or "")

        owns_turn = self._owns_focused_auip(decision)
        if not owns_turn:
            return None
        # The specialized Work owner must see the original turn even when the
        # app interpreter believes its own action satisfies the whole request.
        independent = auip_decision_preserves_main_context(decision)
        async def dispatch():
            return await dispatch_focused(decision, independent=independent)
        if independent:
            return {"handled":False, "dispatch":dispatch,
                "context":{**({"work_relation":work_relation}
                    if self.work_planner is None and work_relation else {}),
                    **_auip_decision_context(decision)}}
        return await dispatch()

    async def resolve_after_work_targets(self, ingress, text, history):
        """Resolve the requested Work, then freeze its current Operation handoff."""
        if self.work_executor is None:
            return (), "work_target_owner_unavailable"
        coordinator = self.work_executor.coordinator
        candidates, complete, reason = candidate_catalog_from_coordinator(coordinator, ingress.session_id)
        work = tuple(item for item in candidates if item.kind == "work_item")
        rows = {item.entity_id:coordinator.bound_work_item_status_row(ingress.session_id, item.entity_id)
            for item in work}
        if not complete or any(row is None for row in rows.values()):
            return (), "work_target_catalog_incomplete:" + str(reason)
        resolved = await resolve_typed_reference(text, work, complete=True, query=self.query, history=history)
        if resolved.status not in {"unique", "ambiguous"}:
            return (), "work_target_" + resolved.status
        targets = []
        for candidate in resolved.candidates:
            before = rows[candidate.entity_id]
            current = coordinator.bound_work_item_status_row(ingress.session_id, candidate.entity_id)
            if current is None or any(current.get(key) != before.get(key)
                    for key in ("work_item_id", "attempt_id", "operation_id")):
                return (), "work_target_changed"
            targets.append({key:before[key] for key in ("work_item_id", "attempt_id", "operation_id")})
        return tuple(targets), ""

    @staticmethod
    def _owns_focused_auip(decision):
        action = str(getattr(decision, "action", "") or "")
        # Domain ownership is distinct from permission/capability to execute.
        # A blocked app operation must not become a reply to an unrelated Work.
        return bool(getattr(decision, "status", "") in {"ok", "blocked"}
            and str(getattr(decision, "app_session_id", "") or "")
            and (action in {"observe", "collaborate", "delegate", "step", "leave"}
                or (action == "none" and tuple(
                    getattr(decision, "read_facets", ()) or ()))))

    async def _dispatch_focused_auip_decision(self, ingress, turn_id, text,
            admission, require_live_turn, decision, *, independent):
        """Continue one resolved focused decision through its existing owner."""
        work_relation = str(getattr(decision, "work_relation", "") or "")

        def record_user():
            ingress.loop.history.append({"source":"user",
                "input_id":admission.utterance_id,
                "turn_id":turn_id, "text":text})

        async def finish(receipt, event=None, display_text=None,
                         delivery_observer=None):
            # The domain outcome is already known. Optional role expression must
            # not erase its receipt or make the accepted action look unexecuted.
            receipt["display_text"] = ""
            cause = turn_id
            if not independent:
                ingress.receipts[admission.utterance_id] = receipt
            else:
                # Keep domain evidence even if the sibling Work handoff fails.
                ingress.loop.trace.append({"kind":"auip_receipt", "cause":cause,
                    **receipt})
                if receipt.get("action") == "read":
                    return {"handled":False, "receipt":receipt, "display_text":"",
                        "read_facts":event["facts"], "work_relation":work_relation}
            try:
                if display_text is None:
                    expressed = await ingress.loop._decide(event)
                    display_text = str(expressed.get("say") or "")
                if independent:
                    return {"handled":False, "display_text":display_text,
                        "receipt":receipt, "delivery_observer":delivery_observer,
                        "work_relation":work_relation}
                published = await ingress.loop._deliver(display_text, cause=cause)
                if published and delivery_observer is not None:
                    await delivery_observer({"visible":True})
                receipt["display_text"] = next((row["text"] for row in reversed(
                    ingress.loop.history) if row.get("source") == "kurisu"
                    and row.get("cause") == cause), "")
            except Exception as exc:
                ingress.loop.trace.append({"kind":"presentation_failed",
                    "cause":turn_id, "source":"host_receipt",
                    "state":receipt["state"],
                    "error":type(exc).__name__ + ": " + str(exc)})
            return {"handled":True, "display_text":receipt["display_text"],
                "receipt":receipt}

        return await self._apply_focused_auip(
            ingress, turn_id, text, admission, require_live_turn, decision,
            independent=independent, record_user=record_user, finish=finish)

    @staticmethod
    def _owns_inactive_auip_entry(decision):
        return bool(getattr(decision, "status", "") == "ok"
            and not getattr(decision, "app_session_id", "")
            and getattr(decision, "action", "") in {"engage", "launch", "prepare"}
            and getattr(decision, "timing", "now") in {"now", "after_work"}
            and getattr(decision, "work_relation", "") != "independent")

    async def _apply_inactive_auip_entry(self, ingress, turn_id, text, admission,
            require_live_turn, binding, decision, prior_receipt, history, *,
            allow_independent_after_work=False, independent_now=False):
        confirmed = require_live_turn()
        if inspect.isawaitable(confirmed):
            admission = await confirmed
        ingress.loop._require_binding(binding)
        CurrentTurnSourceSpanV1.capture(admission, text, start=0, end=len(text))
        if self.work_planner is not None and _entry_needs_discovery(decision):
            # A missing managed-app entry is an unresolved destination, not a
            # refusal of the user's goal. The existing professional Work owner
            # decides whether the exact source calls for independent discovery.
            child = ingress.loop.get_context(binding.child_id)
            parent_context = "\n".join(("User" if row["role"] == "user" else "Main Chat")
                + ": " + json.dumps(row["content"], ensure_ascii=False)
                for row in history[-6:])
            return await self.handle_work_action(ingress, turn_id, {
                "state":"work_plan_required", "text":text, "source_user_text":text,
                "input_id":admission.utterance_id, "parent_context":parent_context,
                "child_id":binding.child_id, "source_binding_token":binding.token,
                "context_revision":child.revision if child is not None else -1,
                "auip_context":_auip_decision_context(decision),
            }, admission)
        targets, target_error = (), ""
        owns_after_work = (_is_planned_after_work_decision(decision)
            and (allow_independent_after_work
                or self._owns_inactive_auip_entry(decision)))
        if owns_after_work:
            targets, target_error = await self.resolve_after_work_targets(ingress, text, history)
            confirmed = require_live_turn()
            if inspect.isawaitable(confirmed):
                admission = await confirmed
            ingress.loop._require_binding(binding)
        if not (independent_now or (self._owns_inactive_auip_entry(decision)
                and getattr(decision, "action", "") == "prepare")):
            ingress.loop._effects.accept_no_effect(admission, reason="auip_entry")

        async def prepare_work(candidate, mode):
            child = ingress.loop.get_context(binding.child_id)
            context = "\n".join(("User" if row["role"] == "user" else "Main Chat")
                + ": " + json.dumps(row["content"], ensure_ascii=False) for row in history[-6:])
            return await self.handle_work_action(ingress, turn_id, {
                "state":"work_required", "text":text, "parent_context":context,
                "child_id":binding.child_id, "source_binding_token":binding.token,
                "context_revision":child.revision if child is not None else -1,
                "work_item_id":candidate.work_item_id,
                "plan_evidence":{"auip_preparation_work_item_id":candidate.work_item_id,
                    "auip_preparation_mode":mode},
                "_host_defer_work_started_presentation":True,
            }, admission)
        # Once admitted, finishing the domain handoff is independent from the
        # foreground reply. A later Chat turn only supersedes its presentation.
        task = asyncio.create_task(self._finish_inactive_auip_entry(
            ingress, turn_id, text, admission, decision, prior_receipt, require_live_turn,
            prepare_work, targets, target_error,
            allow_independent_after_work=allow_independent_after_work,
            independent_now=independent_now))
        ingress.loop._monitors.add(task)
        task.add_done_callback(ingress.loop._monitors.discard)
        return await asyncio.shield(task)

    async def _finish_inactive_auip_entry(self, ingress, turn_id, text, admission,
            decision, prior_receipt, require_live_turn, prepare_work, targets,
            target_error, *, allow_independent_after_work=False,
            independent_now=False):
        state = "auip_rejected"
        planned_after_work = (_is_planned_after_work_decision(decision)
            and allow_independent_after_work)
        after_work = (getattr(decision, "timing", "") == "after_work"
            and (self._owns_inactive_auip_entry(decision) or planned_after_work))
        if (independent_now and getattr(decision, "action", "") == "prepare"):
            outcome = {"ok":False,
                "error":"independent_prepare_requires_compound_work_authority"}
        elif ((self._owns_inactive_auip_entry(decision)
                and getattr(decision, "action", "") in {"launch", "prepare"})
                or planned_after_work or independent_now):
            attrs = decision.control_attrs()
            if planned_after_work and not attrs:
                attrs = {"action":"launch", "target":"delivery",
                    "mode":decision.mode, "after":"work"}
            if after_work:
                attrs = dict(attrs or {})
                attrs["_host_work_binding"] = "active"
                attrs["_host_active_work_attempt_ids"] = tuple(row["attempt_id"] for row in targets)
                if len(targets) == 1:
                    attrs["_host_work_item_id"] = targets[0]["work_item_id"]
            try:
                if after_work and not targets:
                    outcome = {"ok":False, "error":target_error}
                else:
                    outcome = self.auip_router(attrs, session_id=ingress.session_id,
                        user_text=text, turn_id=turn_id,
                        **({"prepare_work":prepare_work} if decision.action == "prepare" else {}))
                    outcome = await outcome if inspect.isawaitable(outcome) else outcome
                    outcome = dict(outcome or {})
                if outcome.get("ok") is True:
                    state = ("auip_after_work_deferred" if after_work
                        and outcome.get("deferred") is True and not outcome.get("attention")
                        else "auip_entry_pending")
                elif outcome.get("uncertain") is True:
                    state = "auip_unknown"
            except Exception as exc:
                state = "auip_unknown"
                outcome = {"ok":False, "uncertain":True, "control":attrs,
                    "error":"entry_dispatch_failed:" + type(exc).__name__}
        else:
            outcome = {"ok":False, "error":getattr(decision, "reason", "") or "app_entry_not_confirmed"}
        receipt = {"state":state,
            "action":"launch_after_work" if after_work else getattr(decision, "action", "launch"),
            "outcome":outcome, **(targets[0] if len(targets) == 1 else {}),
            "display_text":"",
            "question":text, "input_id":admission.utterance_id,
            "utterance_id":admission.utterance_id, "turn_id":turn_id}
        if isinstance(outcome.get("work"), dict):
            receipt["work"] = outcome.pop("work")
        if independent_now:
            try:
                if outcome.get("requested") is True:
                    display_text = ""
                else:
                    expressed = await ingress.loop._decide(
                        {"source":"host_receipt", **receipt})
                    display_text = str(expressed.get("say") or "")
            except Exception as exc:
                ingress.loop.trace.append({"kind":"presentation_failed",
                    "cause":turn_id, "source":"host_receipt",
                    "error":type(exc).__name__ + ": " + str(exc)})
                display_text = ""
            return {"receipt":receipt, "display_text":display_text,
                "work_relation":"independent"}
        ingress.receipts[admission.utterance_id] = receipt
        try:
            if (outcome.get("requested") is True
                    or state == "auip_after_work_deferred"):
                # Reuse the ordinary role's accepted entry acknowledgement.
                # A successful reservation needs no second expression query.
                display_text = (None if prior_receipt.get("state") == "no_action" else
                    str(prior_receipt.get("coordination_say") or ""))
            else:
                expressed = await ingress.loop._decide({"source":"host_receipt", **receipt})
                display_text = str(expressed.get("say") or "")
            visible = require_live_turn()
            if inspect.isawaitable(visible):
                await visible
            if display_text is not None:
                await ingress.loop._deliver(display_text, cause=turn_id)
        except Exception as exc:
            ingress.loop.trace.append({"kind":"presentation_failed", "cause":turn_id,
                "source":"host_receipt", "error":type(exc).__name__ + ": " + str(exc)})
        receipt["display_text"] = next((row["text"] for row in reversed(ingress.loop.history)
            if row.get("source") == "kurisu" and row.get("cause") == turn_id), "")
        return receipt

    async def _apply_focused_auip(self, ingress, turn_id, text, admission,
            require_live_turn, decision, *, independent, record_user, finish):
        """Apply the already frozen focused-app decision through its existing router."""
        action = str(getattr(decision, "action", "") or "")
        read_facets = tuple(getattr(decision, "read_facets", ()) or ())
        app_session_id = str(getattr(decision, "app_session_id", "") or "")
        confirmed = require_live_turn()
        if inspect.isawaitable(confirmed):
            admission = await confirmed
        if not independent:
            ingress.loop._effects.accept_no_effect(admission,
                reason="auip_rejected" if getattr(decision, "status", "") != "ok"
                    else "auip_read" if read_facets else "auip_" + action)
            record_user()
        display_text = None
        role_scope = ({"role_scope":"この応答ではアプリ側の要求だけを扱います。独立したWorkは別の担当が説明します。"}
            if independent else {})
        if getattr(decision, "status", "") != "ok":
            receipt = {"state":"auip_rejected", "action":action,
                "input_id":admission.utterance_id, "app_session_id":app_session_id,
                "outcome":{"ok":False,
                    "reason":str(getattr(decision, "reason", "") or "app_control_unavailable"),
                    "available_modes":list(getattr(decision, "available_modes", ()) or ())}}
            return await finish(receipt, {"source":"host_receipt", **receipt, **role_scope})
        step_request = self.auip_step
        if action == "step" and step_request is not None:
            result = await step_request(decision=decision, text=text,
                session_id=ingress.session_id, turn_id=turn_id,
                acceptance_check=require_live_turn)
            if result is not None:
                outcome = {key:value for key,value in result.items() if key != "delivery_observer"}
                accepted = (outcome.get("receipt") or {}).get("accepted") is True
                receipt = {"state":"auip_applied" if accepted else "auip_rejected",
                    "action":"step", "input_id":admission.utterance_id,
                    "app_session_id":app_session_id, "outcome":outcome}
                return await finish(receipt,
                    {"source":"host_receipt", **receipt, **role_scope},
                    display_text=str(outcome.get("display_text") or "") if accepted else None,
                    delivery_observer=result.get("delivery_observer") if accepted else None)
        if read_facets:
            facts = self.auip_decider.render_read_only_answer(
                decision, language="ja")
            outcome = {"ok":bool(facts), "action":"read"}
            event = {"source":"host_receipt", "state":"auip_read",
                "question":text, "facts":facts,
                "app_session_id":app_session_id, **role_scope}
        else:
            attrs = decision.control_attrs()
            prospective = ""
            if action == "step":
                expressed = await ingress.loop._decide({"source":"host_receipt",
                    "state":"auip_step_pending", "instruction":text,
                    **role_scope})
                prospective = str(expressed.get("say") or "")
                if not prospective:
                    raise TurnAuthorityError(
                        "AUIP step requires one visible prospective role commitment")
                attrs = {**attrs, "_host_current_role_response":prospective}
                confirmed = require_live_turn()
                if inspect.isawaitable(confirmed):
                    admission = await confirmed
            routed = self.auip_router(attrs, session_id=ingress.session_id,
                user_text=text, turn_id=turn_id)
            outcome = await routed if inspect.isawaitable(routed) else routed
            outcome = dict(outcome or {})
            if action == "step" and outcome.get("ok") is True:
                display_text = prospective
            else:
                event = {"source":"host_receipt",
                    "state":"auip_applied" if outcome.get("ok") is True else "auip_rejected",
                    "action":action,
                    **({"question":text, "outcome":(
                        _auip_transition_presentation_outcome(action, outcome)
                        if action in {"observe", "collaborate", "delegate", "leave"}
                        else dict(outcome))}
                        if action != "step" else {}),
                    **role_scope}
        receipt = {"state":"auip_read" if read_facets
                else "auip_applied" if outcome.get("ok") is True else "auip_rejected",
            "action":"read" if read_facets else action,
            "input_id":admission.utterance_id,
            "app_session_id":app_session_id,
            "outcome":outcome}
        return await finish(receipt, event if display_text is None else None,
            display_text=display_text)

    @staticmethod
    def _work_owner_key(session_id: str, context_id: str) -> str:
        clean_context_id = str(context_id or "").strip()
        return clean_context_id or "session:" + str(session_id or "").strip()

    def active_work_for_recipient(self, session_id: str,
                                  context_id: str, *, work_item_id: str = "") -> dict | None:
        """Read the recipient hint or validate one explicitly addressed Work."""

        clean_session_id = str(session_id or "").strip()
        clean_context_id = str(context_id or "").strip()
        explicitly_addressed = bool(work_item_id)
        if clean_context_id and not work_item_id:
            ingress = self.ingresses.get(clean_session_id)
            child = ingress.loop.get_context(clean_context_id) if ingress else None
            if child is not None and child.work_item_id:
                work_item_id = child.work_item_id
                explicitly_addressed = True
        owner_key = self._work_owner_key(clean_session_id, clean_context_id)
        dispatch = self._work_dispatches.get(owner_key)
        if dispatch is not None and (not work_item_id or dispatch.binding["work_item_id"] == work_item_id):
            record = self.runtime.get_run(dispatch.binding["provider_run_id"])
            if record is not None and record.status in {"queued", "running"}:
                return {"effect_id":dispatch.effect_id,
                    "work_item_id":dispatch.binding["work_item_id"],
                    "attempt_id":dispatch.binding["attempt_id"],
                    "run_id":record.run_id, "status":record.status,
                    "runtime_attached":True}
        if self.work_control is None or self.work_executor is None:
            return None
        if not clean_context_id and not work_item_id:
            current = self.work_for_recipient(clean_session_id, "")
            if current is not None:
                # Finishing the current subject cannot silently re-address
                # conversation to another task merely because it still runs.
                work_item_id = current["work_item_id"]
        matches = []
        for attempt in self.work_executor.coordinator.store.list_unresolved_provider_attempts():
            if work_item_id and attempt.work_item_id != work_item_id:
                continue
            predecessor = self.work_executor.coordinator.store.get_recovery_predecessor(attempt)
            # A Host Retry continues its accepted Operation; the original
            # Control effect must not be rebound to pretend it started again.
            owner_attempt = predecessor or attempt
            attachment = (owner_attempt.metadata.get("provider_session_attach")
                if isinstance(owner_attempt.metadata.get("provider_session_attach"), dict)
                else {})
            if explicitly_addressed:
                if not clean_session_id or str(attempt.metadata.get("session_id") or "") != clean_session_id:
                    continue
            elif clean_context_id:
                if attachment.get("cooperative_context_id") != clean_context_id:
                    continue
            elif (not clean_session_id
                    or str(attempt.metadata.get("session_id") or "") != clean_session_id
                    or attachment.get("cooperative_context_id")):
                continue
            if not owner_attempt.origin_effect_id:
                continue
            try:
                binding = self.work_control.binding(owner_attempt.origin_effect_id)
            except (ControlLedgerConflict, WorkLedgerConflict):
                continue
            if (binding is None or binding["attempt_id"] != owner_attempt.attempt_id
                    or binding["work_item_id"] != attempt.work_item_id
                    or binding["provider_run_id"] != owner_attempt.provider_run_id):
                continue
            matches.append((attempt, binding, owner_attempt.origin_effect_id))
        if len(matches) > 1:
            return {"effect_id":"", "work_item_id":"", "attempt_id":"",
                "run_id":"", "status":"unknown", "runtime_attached":False,
                "reason":"work_owner_ambiguous"}
        if not matches:
            return None
        attempt, binding, effect_id = matches[0]
        record = self.runtime.get_run(attempt.provider_run_id)
        attached = record is not None and record.status in {"queued", "running"}
        return {"effect_id":effect_id,
            "work_item_id":binding["work_item_id"],
            "attempt_id":attempt.attempt_id,
            "run_id":attempt.provider_run_id,
            "status":record.status if attached else attempt.execution_status,
            "runtime_attached":attached,
            "reason":"" if attached else "work_runtime_owner_unavailable"}

    def active_work_for_context(self, context_id: str) -> dict | None:
        """Compatibility view for callers that already hold an exact context id."""

        return self.active_work_for_recipient("", context_id)

    def work_for_recipient(self, session_id: str, context_id: str) -> dict | None:
        """Read the last Work accepted at this receiving address, including terminal Work.

        This is conversation context, never an instruction to amend automatically.
        It comes from accepted effect lineage, not UI focus or Provider prose.
        """
        if self.work_control is None or self.work_executor is None:
            return None
        with self.ledger._lock:
            row = self.ledger._db.execute("""SELECT e.effect_id FROM control_effect_outbox e
                JOIN control_admissions a ON a.root_id=e.root_id
                JOIN run_attempts r ON r.origin_effect_id=e.effect_id
                WHERE a.source_scope=? AND e.kind='work'
                AND COALESCE(json_extract(e.payload_json,'$.cooperative_context_id'),'')=?
                ORDER BY r.created_at DESC, r.rowid DESC LIMIT 1""",
                ("chat:" + session_id, str(context_id or ""))).fetchone()
        coordinator = self.work_executor.coordinator
        store = coordinator.store
        if row is None:
            ingress = self.ingresses.get(session_id)
            child = ingress.loop.get_context(context_id) if ingress and context_id else None
            item = store.get_work_item(child.work_item_id) if child and child.work_item_id else None
        else:
            binding = self.work_control.binding(row["effect_id"])
            if binding is None:
                return None
            item = store.get_work_item(binding["work_item_id"])
        if item is None or item.state == "archived":
            return None
        attempts = store.list_attempts(item.work_item_id)
        attempt = attempts[-1] if attempts else None
        if attempt is None:
            return None
        export_plan = next((prior.metadata["export_plan"]
            for prior in reversed(attempts)
            if isinstance(prior.metadata.get("export_plan"), dict)), None)
        assessment = store.latest_completion(item.work_item_id)
        if assessment is not None and assessment.attempt_id != attempt.attempt_id:
            assessment = None
        input_requirements = coordinator.read_model.input_requirements(
            item.work_item_id, attempt_id=attempt.attempt_id)
        projected: dict[str, object] = {"work_item_id":item.work_item_id, "title":item.title, "goal":item.goal,
            "provider":attempt.provider,
            "execution_status":attempt.execution_status, "work_state":item.state,
            "completeness":assessment.completeness if assessment is not None else "unknown",
            "attention":assessment.attention if assessment is not None else "unknown",
            "workspace":item.workspace_path,
            "export_target":"desktop" if isinstance(export_plan, dict)
                and export_plan.get("kind") == "desktop" else ""}
        if input_requirements:
            projected["input_requirements"] = input_requirements
        return projected

    @staticmethod
    def role_app_context_for_session(
        session_id: str,
        *,
        app_session_id: str = "",
        transition_action: str = "",
    ) -> str:
        """Use the shared role projections independently of action proposals."""
        from server.auip_runtime import runtime

        expected = str(app_session_id or "").strip()
        # Rendering is synchronous and the Runtime uses an RLock. Keep the
        # identity check and both existing projections in one focus snapshot,
        # so a receipt for App A can never acquire App B's current facts.
        with runtime._lock:
            focused = runtime.focused_projection(session_id)
            focused_id = (
                str(focused.get("app_session_id") or "")
                if isinstance(focused, dict)
                else ""
            )
            if expected and focused_id != expected:
                return ""
            action = str(transition_action or "").strip().lower()
            if action in {"observe", "collaborate", "delegate", "leave"}:
                current = _auip_transition_presentation_outcome(
                    action, focused if isinstance(focused, dict) else {})
                return ("[Current AUIP transition state]\n"
                    + json.dumps(current, ensure_ascii=False, separators=(",", ":"))
                    + "\n[/Current AUIP transition state]")
            return "\n\n".join(value for value in (
                runtime.render_main_chat_context(session_id, language="ja",
                    include_control_contract=False),
                runtime.render_main_chat_briefing(
                    session_id, app_session_id=focused_id),
            ) if value)

    def work_conversation_destination(self, item, provider_id, requirements) -> dict:
        """Resolve an interaction address without changing Work or Project state."""
        from server.provider_session_binding import resolve_provider_session_attachment

        if item.state == "archived" or item.workspace_mode == "none" or not Path(item.workspace_path).is_dir():
            raise LoopConflict("selected Work destination is unavailable")
        is_draft = self.destination.is_unkept_draft(item.workspace_path)
        if not is_draft:
            self.destination.available_project(item.project_id)
        attempts = self.destination.store.list_attempts(item.work_item_id)
        manifest = self.runtime.get_manifest(provider_id)
        attachment = resolve_provider_session_attachment(has_existing_item=True,
            previous_attempt=attempts[-1] if attempts else None, continuation="conversation",
            provider_capabilities=manifest.capabilities.to_dict() if manifest else {},
            request_provider=provider_id)
        return {"requirements":replace(requirements, workspace_access="read"),
            "workspace":item.workspace_path, "native_session":attachment.session,
            "workspace_route":{"status":"resolved", "source":"cooperative_session_work_item",
                "destinationKind":"draft" if is_draft else "work_item",
                "projectId":item.project_id, "workItemId":item.work_item_id,
                "cwd":item.workspace_path}}

    def work_conversation_context(self, ingress, work_item_id, provider_id):
        """Lazily restore a Work-associated conversation; no Work mutation occurs."""
        loop = ingress.loop
        item = self.destination.store.get_work_item(work_item_id)
        requirements = self.context_requirements.get(provider_id)
        if item is None or requirements is None:
            raise LoopConflict("Work conversation destination is unavailable")
        destination = self.work_conversation_destination(item, provider_id, requirements)
        for row in loop.context_catalog():
            if (not row["closed"] and row["work_item_id"] == work_item_id
                    and row["provider"] == provider_id):
                child = loop.get_context(row["context_id"])
                if (child is not None and child.requirements.workspace_access == "read"
                        and Path(child.workspace).resolve() == Path(item.workspace_path).resolve()):
                    return child
        child = loop._create_context(item.title, provider_id,
            requirements=destination["requirements"], workspace=destination["workspace"],
            workspace_route=destination["workspace_route"])
        loop.bind_work_item(work_item_id, context_id=child.child_id)
        if destination["native_session"] is not None:
            loop._save_child(child, native_session=destination["native_session"])
        return child

    def work_candidates_for_context(self, session_id: str, context_id: str
                                    ) -> tuple[tuple[TypedReferenceCandidate, ...], bool, str]:
        """Freeze visible Work targets; their execution owner checks eligibility."""

        ingress = self.ingresses.get(str(session_id or ""))
        clean_context_id = str(context_id or "")
        child = (ingress.loop.get_context(clean_context_id)
            if ingress and clean_context_id else None)
        if (ingress is None or self.work_executor is None
                or (clean_context_id and child is None)):
            return (), False, "work_target_owner_unavailable"
        candidates, complete, reason = candidate_catalog_from_coordinator(
            self.work_executor.coordinator, session_id)
        eligible = []
        for candidate in candidates:
            if candidate.kind != "work_item":
                continue
            item = self.work_executor.coordinator.store.get_work_item(
                candidate.entity_id)
            if item is None or item.state == "archived":
                continue
            eligible.append(candidate)
        return tuple(eligible), bool(complete), str(reason or "")

    def resolve_work_recipient(self, payload, *, cursor=None, workspace_path="",
                               require_current_binding=False):
        ingress = self.ingresses.get(str(getattr(payload, "session_id", "")))
        if ingress is None or self.destination is None:
            raise WorkLedgerConflict("cooperative Work recipient Session is unavailable")
        if cursor is None:
            self.destination.available_project(str(getattr(payload, "project_id", "")))
        return ingress.loop._state.work_recipient(payload, cursor=cursor,
            workspace_path=workspace_path,
            require_current_binding=require_current_binding)

    async def handle_work_action(self, ingress, turn_id: str, receipt: dict,
                                 admission, *, require_live_turn=None) -> dict:
        state = str(receipt.get("state") or "")
        if state == "work_plan_required":
            if self.work_planner is None:
                return await self._reject_work_request(
                    ingress, turn_id, receipt, admission, "work_planner_unavailable")
            frozen = dict(receipt)
            provider_message = isinstance(frozen.get("provider_message_action"), dict)
            source_input = ingress.loop._inputs.get(admission.utterance_id)
            planner_receipt = dict(frozen)
            plan = self.work_planner(ingress, turn_id, planner_receipt, admission)
            plan_task = asyncio.ensure_future(plan) if inspect.isawaitable(plan) else None
            coordination_delivered = frozen.get("_host_coordination_delivered") is True

            async def publish_coordination():
                if (coordination_delivered
                        or not frozen.get("coordination_say")):
                    return coordination_delivered
                try:
                    if callable(require_live_turn):
                        require_live_turn()
                    return bool(await ingress.loop._deliver(
                        str(frozen["coordination_say"]), cause=turn_id))
                except Exception as exc:
                    ingress.loop.trace.append({"kind":"presentation_failed",
                        "cause":turn_id, "source":"work_coordination",
                        "error":type(exc).__name__ + ": " + str(exc)})
                    return False

            delivery_task = asyncio.create_task(publish_coordination())
            try:
                if plan_task is not None:
                    plan, coordination_delivered = await asyncio.gather(
                        plan_task, delivery_task)
                else:
                    coordination_delivered = await delivery_task
            except BaseException:
                for task in (plan_task, delivery_task):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(*(task for task in (plan_task, delivery_task)
                    if task is not None), return_exceptions=True)
                raise
            auip_context = frozen.get("auip_context")
            suppressed_retracts = ()
            if (isinstance(auip_context, dict)
                    and auip_context.get("ambiguity") == "work_or_app"):
                plan, suppressed_retracts = _suppress_ambiguous_retracts(plan)
                if suppressed_retracts:
                    # The AUIP owner proved that this turn did not identify
                    # whether Work or the active application should stop. Keep
                    # all independent Work clauses, but prevent either stop
                    # interpretation from reaching an execution owner.
                    frozen["_host_defer_work_started_presentation"] = True
            if callable(require_live_turn):
                require_live_turn()
            planned_message = (isinstance(plan, CompoundControlPlan)
                and plan.status == "ok" and any(
                    operation.action.get("intent") == "message"
                    for operation in plan.operations))
            if frozen.get("coordination_say") and not (provider_message or planned_message):
                frozen.pop("coordination_say", None)
            frozen["_host_coordination_delivered"] = coordination_delivered
            if isinstance(frozen.get("_host_auip_after_work_decision"),
                    AuipControlDecision):
                existing_entry = frozen.get("_host_existing_after_work")
                if (isinstance(plan, CompoundControlPlan) and plan.status == "ok"
                        and not plan.operations and not plan.clauses and callable(existing_entry)):
                    # No new Work does not cancel the independently understood
                    # request to open an existing Work's result. Its owner makes
                    # the one no-effect acceptance and resolves the original target.
                    result = existing_entry(frozen)
                    if inspect.isawaitable(result):
                        result = await result
                else:
                    result = await self.resolve_planned_work_after_work(
                        ingress, turn_id, frozen, admission, plan,
                        require_live_turn=require_live_turn)
            elif planned_message:
                live_check = require_live_turn or (lambda:None)

                async def continue_and_resolve():
                    live_check()
                    return await self.handle_planned_work_action(
                        ingress, turn_id, frozen, admission, plan,
                        require_live_turn=live_check)

                if not suppressed_retracts:
                    return await ingress.loop.handoff_provider_message_task(
                        admission.utterance_id, str(frozen.get("text") or ""),
                        turn_id, continue_and_resolve, expected_input=source_input)
                result = await ingress.loop.handoff_provider_message_task(
                    admission.utterance_id, str(frozen.get("text") or ""), turn_id,
                    continue_and_resolve, expected_input=source_input)
            else:
                result = await self.handle_planned_work_action(
                    ingress, turn_id, frozen, admission, plan)
            if suppressed_retracts:
                result = {**result, "auip_context":dict(auip_context)}
                ingress.receipts[admission.utterance_id] = result
                try:
                    await ingress.loop._express_and_deliver({"source":"host_receipt",
                        **result, "question":str(frozen.get("source_user_text")
                            or frozen.get("text") or "")}, cause=turn_id)
                except Exception as exc:
                    ingress.loop.trace.append({"kind":"presentation_failed",
                        "cause":turn_id, "source":"host_receipt",
                        "error":type(exc).__name__ + ": " + str(exc)})
            result["_host_coordination_delivered"] = coordination_delivered
            return result
        if state == "task_stop_resolution_required":
            return await self.resolve_task_stop(ingress, turn_id, receipt, admission)
        if state == "task_address_resolution_required":
            return await self.resolve_task_address(ingress, turn_id, receipt, admission)
        if state == "work_report_required":
            return await self.resolve_work_report(ingress, turn_id, receipt, admission)
        if state == "work_report_batch_required":
            return await self.resolve_work_report_batch(
                ingress, turn_id, receipt, admission)
        if state == "work_auip_batch_required":
            return await self.resolve_work_auip_batch(
                ingress, turn_id, receipt, admission)
        if state == "work_amend_resolution_required":
            return await self.resolve_work_amend_target(
                ingress, turn_id, receipt, admission)
        if state == "work_input_required":
            return await self._handle_work_input(ingress, turn_id, receipt, admission)
        if state == "work_interrupt_required":
            return await self._handle_work_interrupt(ingress, turn_id, receipt)
        if self.work_control is None or self.work_executor is None:
            return await self._reject_work_request(
                ingress, turn_id, receipt, admission, "work_owner_unavailable")
        loop = ingress.loop
        async with loop._foreground:
            sealed = self._seal_work_request(ingress, turn_id, receipt, admission)
        if isinstance(sealed, dict):
            result = await self._reject_work_request(
                ingress, turn_id, receipt, admission, sealed["reason"])
            result.update(sealed)
            return result
        accepted, recipient_context_id, work_provider = sealed
        return await self._dispatch_accepted_work(ingress, turn_id, receipt,
            admission, accepted["effect_id"], recipient_context_id, work_provider)

    async def _dispatch_accepted_work(self, ingress, turn_id: str, receipt: dict,
                                      admission, effect_id: str,
                                      recipient_context_id: str,
                                      work_provider: str, *, present=True) -> dict:
        """Dispatch and track one already-accepted Work effect."""

        loop = ingress.loop
        dispatch = await self.work_executor.dispatch(effect_id)
        if dispatch.record is None:
            rejected = dispatch.status == "rejected"
            terminal = dispatch.status == "terminal"
            detail = (dispatch.receipt or {}).get("details", {})
            result = {"state":"rejected" if rejected else "terminal" if terminal else "unknown",
                "reason":str(detail.get("reason") or (
                    "work_already_terminal" if terminal else "work_runtime_owner_unavailable")),
                "error":str(detail.get("error") or ""),
                "outcome":str((dispatch.receipt or {}).get("outcome") or ""),
                "input_id":admission.utterance_id, "utterance_id":admission.utterance_id,
                "turn_id":turn_id, "effect_id":effect_id,
                "work_item_id":dispatch.binding["work_item_id"],
                "attempt_id":dispatch.binding["attempt_id"]}
            ingress.receipts[admission.utterance_id] = result
            if present and receipt.get("_host_defer_work_started_presentation") is not True:
                await loop._express_and_deliver({"source":"host_receipt", **result}, cause=turn_id)
            return result
        owner_key = self._work_owner_key(ingress.session_id, recipient_context_id)
        self._work_dispatches[owner_key] = dispatch
        result = {"state":"work_started", "input_id":admission.utterance_id,
            "utterance_id":admission.utterance_id, "turn_id":turn_id,
            "child_id":recipient_context_id, "effect_id":effect_id,
            "work_item_id":dispatch.binding["work_item_id"],
            "attempt_id":dispatch.binding["attempt_id"],
            "run_id":dispatch.binding["provider_run_id"]}
        ingress.receipts[admission.utterance_id] = result
        task = asyncio.create_task(self._finish_work(
            owner_key, dispatch, session_id=ingress.session_id),
            name=f"cooperative-work:{dispatch.effect_id}")
        self._work_tasks.add(task)
        task.add_done_callback(self._work_tasks.discard)
        if present and receipt.get("_host_defer_work_started_presentation") is not True:
            try:
                if receipt.get("_host_coordination_delivered") is True:
                    pass
                elif "coordination_say" in receipt:
                    await loop._deliver(str(receipt.get("coordination_say") or ""), cause=turn_id)
                else:
                    # Work/report batches and a completed target choice retain
                    # their existing Host-receipt expression owner.
                    await loop._express_and_deliver({"source":"host_receipt",
                        "state":"work_started", "provider":work_provider}, cause=turn_id)
            except Exception as exc:
                loop.trace.append({"kind":"presentation_failed", "cause":turn_id,
                    "source":"host_receipt",
                    "error":type(exc).__name__ + ": " + str(exc)})
        return result

    def _prepare_work_request(self, ingress, turn_id, receipt, admission, *,
            planned_provider: str | None = None,
            planned_workspace_access: str | None = None):
        """Prepare one typed Work payload without accepting its admission."""
        loop = ingress.loop
        context_id = str(receipt.get("child_id") or "")
        token = str(receipt.get("source_binding_token") or "")
        work_provider = planned_provider if planned_provider is not None else self.provider
        if (loop._binding.child_id, loop._binding.token) != (context_id, token):
            return {"state":"rejected", "reason":"work_source_binding_changed",
                "input_id":str(receipt.get("input_id") or ""), "turn_id":turn_id}
        text = str(receipt.get("text") or "")
        source_user_text = str(receipt.get("source_user_text") or text)
        display_title = (str(receipt.get("display_title") or "")
            if planned_provider is not None else "")
        source_start = receipt.get("source_start", 0)
        source_end = receipt.get("source_end", len(source_user_text))
        source_proof = CurrentTurnSourceSpanV1.capture(admission,
            source_user_text, start=source_start, end=source_end)
        work_item_id = str(receipt.get("work_item_id") or "")
        export_target = str(receipt.get("external_export_target") or "")
        if work_item_id and not export_target:
            attempts = self.work_executor.coordinator.store.list_attempts(work_item_id)
            if any(isinstance(attempt.metadata.get("export_plan"), dict)
                    and attempt.metadata["export_plan"].get("kind") == "desktop"
                    for attempt in attempts):
                export_target = "desktop"
        child = loop.get_context(context_id) if context_id else None
        if context_id and (child is None or child.revision != receipt.get("context_revision")):
            return {"state":"rejected", "reason":"work_context_changed",
                "input_id":str(receipt.get("input_id") or ""), "turn_id":turn_id}
        target = (self.work_executor.coordinator.store.get_work_item(work_item_id)
            if work_item_id else None)
        explicit_project_id = str(receipt.get("project_id") or "")
        context_project_id = str(child.workspace_route.get("projectId") or "") if child else ""
        if (context_project_id and self.destination is not None
                and self.destination.is_unkept_draft(child.workspace)):
            context_project_id = ""
        # Source binding authorizes this turn; only a matching execution
        # workspace can reuse its native context. Other existing Work keeps
        # its ordinary amendment owner and original workspace.
        use_context = (child is not None and receipt.get("one_off") is not True
            and bool(context_project_id) and child.native_session is not None
            and child.provider in self.context_requirements
            and replace(child.requirements, workspace_access="read") == replace(
                self.context_requirements[child.provider], workspace_access="read")
            and (planned_provider is None or child.provider == planned_provider)
            and (not explicit_project_id or explicit_project_id == context_project_id)
            and (not work_item_id or (target is not None
            and target.project_id == str(child.workspace_route.get("projectId") or "")
            and Path(target.workspace_path).resolve() == Path(child.workspace).resolve())))
        # The detailed legacy proposal names this receiving directory by
        # default. Without a valid native/destination contract it cannot be
        # silently redirected to a different Draft. Professional independent
        # Work already supplies its explicit placement.
        if (child is not None and not use_context and planned_provider is None
                and not work_item_id and not export_target and not explicit_project_id
                and receipt.get("one_off") is not True):
            return {"state":"rejected", "reason":"work_context_not_settled",
                "input_id":str(receipt.get("input_id") or ""), "turn_id":turn_id}
        if (child is not None and target is not None
                and Path(target.workspace_path).resolve() == Path(child.workspace).resolve()
                and child.run_status not in {"done", "error", "cancelled", "idle"}):
            return {"state":"rejected", "reason":"work_context_not_settled",
                "input_id":str(receipt.get("input_id") or ""), "turn_id":turn_id}
        recipient_context_id = context_id if use_context else ""
        work_provider = child.provider if use_context else work_provider
        if self.runtime.get_manifest(work_provider) is None:
            return {"state":"rejected", "reason":"provider_unavailable", "provider":work_provider,
                "input_id":str(receipt.get("input_id") or ""), "turn_id":turn_id}
        requirements = self.context_requirements[work_provider]
        requested_access = planned_workspace_access or "write"
        access_rank = {"none":0, "read":1, "write":2}
        if access_rank[requirements.workspace_access] < access_rank[requested_access]:
            return {"state":"rejected", "reason":"work_provider_not_writable",
                "input_id":str(receipt.get("input_id") or ""), "turn_id":turn_id}
        requirements = replace(requirements, workspace_access=requested_access)
        if use_context:
            record = self.runtime.get_run(child.run_id) if child.run_id else None
            if (child.closed or not child.workspace
                    or child.run_status not in {"done", "error", "cancelled"}
                    or (record is not None and record.status not in {"done", "error", "cancelled"})):
                return {"state":"rejected", "reason":"work_context_not_settled",
                    "input_id":str(receipt.get("input_id") or ""), "turn_id":turn_id}
        if (recipient_context_id or work_item_id) and self.active_work_for_recipient(
                ingress.session_id, recipient_context_id,
                **({"work_item_id":work_item_id} if not use_context else {})) is not None:
            return {"state":"rejected", "reason":"work_recipient_busy",
                "input_id":str(receipt.get("input_id") or ""),
                "turn_id":turn_id}
        if use_context:
            work_provider = child.provider
            project_id = str(child.workspace_route.get("projectId") or "")
            if not project_id:
                return {"state":"rejected", "reason":"work_project_unavailable",
                    "input_id":str(receipt.get("input_id") or ""),
                    "turn_id":turn_id}
            payload = WorkCooperativeContextPayloadV6(
                provider=child.provider, task=text,
                title=ProviderEventIngestor.work_item_title(
                    text, "" if work_item_id else display_title),
                project_id=project_id, session_id=ingress.session_id,
                utterance_id=admission.utterance_id, turn_id=turn_id,
                source_user_text=source_user_text,
                source_user_context=str(receipt.get("parent_context") or ""),
                source_context_scope=admission.dialogue_source_scope,
                source_proof=source_proof, requirements=requirements,
                cooperative_context_id=child.child_id,
                cooperative_binding_token=token,
                cooperative_context_revision=child.revision,
                work_item_id=work_item_id,
                external_export_target=export_target)
        else:
            if self.destination is None:
                return {"state":"rejected",
                    "reason":"work_destination_owner_unavailable",
                    "input_id":str(receipt.get("input_id") or ""),
                    "turn_id":turn_id}
            if work_item_id:
                item = self.work_executor.coordinator.store.get_work_item(
                    work_item_id)
                project_id = str(item.project_id if item is not None else "")
            else:
                destination_project_id = explicit_project_id or (
                    context_project_id if receipt.get("one_off") is not True else "")
                route = self.destination.resolve_workspace_route({
                    "session_id":ingress.session_id,
                    **({"project_id":destination_project_id} if destination_project_id else {}),
                    **({"one_off":True} if receipt.get("one_off") is True else {})})
                if (receipt.get("one_off") is True
                        and route.get("source") != "scratch_default"):
                    return {"state":"rejected",
                        "reason":"work_one_off_destination_unavailable",
                        "input_id":str(receipt.get("input_id") or ""),
                        "turn_id":turn_id}
                if (explicit_project_id
                        and route.get("source") != "intent_project"):
                    return {"state":"rejected",
                        "reason":"work_project_destination_unavailable",
                        "input_id":str(receipt.get("input_id") or ""),
                        "turn_id":turn_id}
                project_id = str(route.get("projectId") or "")
                if route.get("status") != "resolved":
                    project_id = ""
            if not project_id:
                return {"state":"rejected", "reason":"work_destination_unavailable",
                    "input_id":str(receipt.get("input_id") or ""),
                    "turn_id":turn_id}
            base = dict(provider=work_provider, task=text,
                title=ProviderEventIngestor.work_item_title(
                    text, "" if work_item_id else display_title),
                project_id=project_id, session_id=ingress.session_id,
                utterance_id=admission.utterance_id, turn_id=turn_id,
                source_user_text=source_user_text,
                source_user_context=str(receipt.get("parent_context") or ""),
                source_context_scope=admission.dialogue_source_scope,
                source_proof=source_proof, requirements=requirements,
                external_export_target=export_target)
            payload = (WorkAmendPayloadV4(**base, work_item_id=work_item_id)
                if work_item_id else WorkEffectPayloadV3(**base))
        return payload, recipient_context_id, work_provider

    def _seal_work_request(self, ingress, turn_id, receipt, admission):
        """Compile and accept the compatibility single-Work path."""
        prepared = self._prepare_work_request(
            ingress, turn_id, receipt, admission)
        if isinstance(prepared, dict):
            ingress.loop._effects.accept_no_effect(
                admission, reason=str(prepared.get("reason") or "work_rejected"))
            return prepared
        payload, recipient_context_id, work_provider = prepared
        accepted = self.work_control.seal(admission, payload,
            plan_evidence=(receipt.get("plan_evidence")
                if isinstance(receipt.get("plan_evidence"), dict) else None))
        return accepted, recipient_context_id, work_provider

    async def resolve_planned_work_after_work(self, ingress, turn_id: str,
            receipt: dict, admission, plan: CompoundControlPlan, *,
            require_live_turn=None, _selections=None, _expected_catalog=None) -> dict:
        """Join one after-Work decision to the plan's unique Work mutation."""

        decision = receipt.get("_host_auip_after_work_decision")
        operations = plan.operations if isinstance(plan, CompoundControlPlan) else ()
        if not _is_planned_after_work_decision(decision):
            return await self._reject_work_auip_batch(
                ingress, turn_id, receipt, admission,
                "planned_after_work_decision_invalid")
        if not isinstance(plan, CompoundControlPlan) or plan.status != "ok":
            logging.getLogger(__name__).warning("[WORK-PLAN-REJECTED] turn=%s status=%s reason=%s",
                turn_id, getattr(plan, "status", "invalid"), getattr(plan, "reason", "invalid plan"))
            return await self._reject_work_auip_batch(
                ingress, turn_id, receipt, admission, "planned_work_planning_failed")
        if len(operations) != len(plan.clauses):
            return await self._reject_work_auip_batch(
                ingress, turn_id, receipt, admission, "planned_work_source_invalid")
        if not operations:
            return await self._reject_work_auip_batch(
                ingress, turn_id, receipt, admission,
                "planned_after_work_requires_one_mutation")
        if callable(require_live_turn):
            live = require_live_turn()
            if inspect.isawaitable(live):
                await live
        selections = dict(_selections or {})
        async with ingress.loop._foreground:
            compiled = self._compile_planned_work_plan(
                ingress, turn_id, receipt, admission, plan, selections)
            if (_expected_catalog is not None
                    and compiled.get("status") != "error"
                    and compiled.get("catalog") != _expected_catalog):
                raise LoopConflict("planned Work target catalog changed")
        if compiled.get("status") == "attention":
            async def resume(next_selections, expected_catalog):
                return await self.resolve_planned_work_after_work(
                    ingress, turn_id, receipt, admission, plan,
                    require_live_turn=None,
                    _selections=next_selections,
                    _expected_catalog=expected_catalog)

            return await self._request_planned_plan_selection(
                ingress, turn_id, receipt, admission, plan, compiled,
                selections, resume=resume)
        if compiled.get("status") == "error":
            return await self._reject_work_auip_batch(
                ingress, turn_id, receipt, admission,
                str(compiled.get("reason") or "planned_after_work_invalid"))
        mutations = tuple(row for row in compiled.get("operations") or ()
            if row.get("kind") in {"write", "input"})
        if len(mutations) != 1:
            return await self._reject_work_auip_batch(
                ingress, turn_id, receipt, admission,
                "planned_after_work_requires_one_mutation")
        mutation = mutations[0]
        planned_input = None
        if mutation["kind"] == "write":
            payload = mutation["payload"]
            payload_operation = str(payload.to_payload().get("operation") or "")
            existing_work_item_id = str(getattr(payload, "work_item_id", "") or "")
        else:
            payload_operation = "amend"
            planned_input = {**mutation["active"],
                "input_id":mutation["receipt"]["_host_operation_input_id"]}
            existing_work_item_id = str(planned_input["work_item_id"])
        if payload_operation not in {"execute", "amend"}:
            return await self._reject_work_auip_batch(
                ingress, turn_id, receipt, admission,
                "planned_after_work_operation_invalid")
        source_text = str(receipt.get("source_user_text")
            or receipt.get("text") or "")
        work_clause = plan.clauses[int(mutation["index"])]
        batch_receipt = {**receipt, "text":source_text,
            "batch_actions":[{"op":"work", "intent":payload_operation,
                "source":work_clause.text, "source_start":work_clause.start,
                "source_end":work_clause.end},
                {"op":"auip_after_work", "mode":decision.mode,
                    "source":source_text, "source_start":0,
                    "source_end":len(source_text)}]}
        return await self.resolve_work_auip_batch(
            ingress, turn_id, batch_receipt, admission,
            planned_plan=plan, after_work_decision=decision,
            planned_selections=selections,
            expected_catalog=compiled.get("catalog"),
            planned_work_item_id=existing_work_item_id,
            planned_work_index=int(mutation["index"]), planned_input=planned_input)

    async def resolve_work_auip_batch(self, ingress, turn_id: str,
                                      receipt: dict, admission, *,
                                      planned_plan: CompoundControlPlan | None = None,
                                      after_work_decision: AuipControlDecision | None = None,
                                      planned_selections=None,
                                      expected_catalog=None,
                                      planned_work_item_id="",
                                      planned_work_index=0,
                                      planned_input=None) -> dict:
        """Apply one Work mutation after reserving its AUIP result handoff."""

        loop = ingress.loop
        actions = receipt.get("batch_actions")
        if (self.work_control is None or self.work_executor is None
                or self.auip_router is None
                or self.auip_cancel_deferred is None
                or not isinstance(actions, list)):
            return await self._reject_work_auip_batch(
                ingress, turn_id, receipt, admission,
                "work_auip_batch_owner_unavailable")
        work_action = next((row for row in actions
            if isinstance(row, dict) and row.get("op") == "work"), None)
        auip_action = next((row for row in actions
            if isinstance(row, dict) and row.get("op") == "auip_after_work"), None)
        supported_work_intents = ({"execute", "amend"}
            if planned_plan is not None else {"execute"})
        if (work_action is None or auip_action is None
                or work_action.get("intent") not in supported_work_intents):
            return await self._reject_work_auip_batch(
                ingress, turn_id, receipt, admission,
                "work_auip_batch_invalid")
        context_id = str(receipt.get("child_id") or "")
        token = str(receipt.get("source_binding_token") or "")
        child = loop.get_context(context_id) if context_id else None
        if ((loop._binding.child_id, loop._binding.token) != (context_id, token)
                or (child is not None
                    and child.revision != receipt.get("context_revision"))):
            return await self._reject_work_auip_batch(
                ingress, turn_id, receipt, admission,
                "work_auip_batch_binding_changed")

        attrs = (after_work_decision.control_attrs()
            if isinstance(after_work_decision, AuipControlDecision) else None)
        if isinstance(after_work_decision, AuipControlDecision) and not attrs:
            attrs = {"action":"launch", "target":"delivery",
                "mode":after_work_decision.mode, "after":"work"}
        attrs = dict(attrs or {"action":"launch", "target":"delivery",
            "mode":str(auip_action.get("mode") or ""), "after":"work"})
        attrs.pop("_host_active_work_attempt_ids", None)
        attrs["_host_work_binding"] = "turn"
        if planned_plan is not None and planned_work_item_id:
            attrs["_host_work_item_id"] = str(planned_work_item_id)
        if planned_input is not None:
            attrs["_host_work_binding"] = "active"
            attrs["_host_active_work_attempt_ids"] = (planned_input["attempt_id"],)
            attrs["_host_work_input_id"] = planned_input["input_id"]
        if (attrs.get("action") not in {"launch", "engage"} or attrs.get("target") != "delivery"
                or attrs.get("after") != "work"
                or attrs.get("mode") not in {"observe", "collaborate", "delegate"}):
            return await self._reject_work_auip_batch(
                ingress, turn_id, receipt, admission,
                "work_auip_batch_invalid")
        routed = self.auip_router(attrs, session_id=ingress.session_id,
            user_text=str(receipt.get("text") or ""), turn_id=turn_id)
        outcome = await routed if inspect.isawaitable(routed) else routed
        outcome = dict(outcome or {})
        if outcome.get("ok") is not True or outcome.get("deferred") is not True:
            return await self._reject_work_auip_batch(
                ingress, turn_id, receipt, admission,
                "work_auip_batch_reservation_rejected")

        source_text = str(receipt.get("text") or "")
        evidence_actions = [{"index":index, "op":action["op"],
            **({"intent":str(action.get("intent") or "")} if action["op"] == "work" else
                {"mode":str(action.get("mode") or "")}),
            "source_start":action["source_start"],
            "source_end":action["source_end"],
            "source_sha256":hashlib.sha256(
                str(action["source"]).encode("utf-8")).hexdigest()}
            for index, action in enumerate(actions)]
        work_receipt = {"state":"work_required", "intent":"execute",
            "external_export_target":str(work_action.get("external_export_target") or ""),
            "child_id":context_id, "text":str(work_action["source"]),
            "source_user_text":source_text,
            "source_start":work_action["source_start"],
            "source_end":work_action["source_end"],
            "parent_context":str(receipt.get("parent_context") or ""),
            "source_binding_token":token,
            "context_revision":receipt.get("context_revision", -1),
            "work_item_id":"",
            "plan_evidence":{"cooperative_batch":{"version":2,
                "kind":"work_then_auip_after_work",
                "actions":evidence_actions}},
            "input_id":admission.utterance_id,
            "_host_defer_work_started_presentation":True}
        try:
            work_result = (await self.handle_planned_work_action(
                ingress, turn_id, {**receipt, **work_receipt}, admission,
                planned_plan, _selections=planned_selections,
                _expected_catalog=expected_catalog) if planned_plan is not None else
                await self.handle_work_action(
                    ingress, turn_id, work_receipt, admission))
        except BaseException:
            self.auip_cancel_deferred(
                session_id=ingress.session_id, turn_id=turn_id)
            raise
        work_leaf = (work_result.get("operations", [])[planned_work_index]
            if work_result.get("state") == "planned_work_batch_applied"
            and len(work_result.get("operations") or ()) > planned_work_index
            else work_result)
        expected_work_state = ("work_input_accepted" if planned_input is not None
            else "work_started")
        if work_leaf.get("state") != expected_work_state:
            # A missing Runtime observation can still have an exact accepted
            # Work binding. Keep its reservation for the existing completion
            # owner; only verified Work/artifact facts can later launch it.
            if work_leaf.get("state") != "unknown":
                self.auip_cancel_deferred(
                    session_id=ingress.session_id, turn_id=turn_id)
            try:
                await loop._express_and_deliver(
                    {"source":"host_receipt", **work_result}, cause=turn_id)
            except Exception as exc:
                loop.trace.append({"kind":"presentation_failed",
                    "cause":turn_id, "source":"host_receipt",
                    "error":type(exc).__name__ + ": " + str(exc)})
            return work_result
        try:
            if (receipt.get("_host_coordination_delivered") is not True
                    and receipt.get("coordination_say")):
                await loop._deliver(str(receipt["coordination_say"]),
                    cause=turn_id)
        except Exception as exc:
            loop.trace.append({"kind":"presentation_failed",
                "cause":turn_id, "source":"coordination",
                "error":type(exc).__name__ + ": " + str(exc)})
        result = {"state":"work_auip_batch_started",
            "input_id":admission.utterance_id,
            "utterance_id":admission.utterance_id, "turn_id":turn_id,
            "work":dict(work_result), "auip":outcome}
        ingress.receipts[admission.utterance_id] = result
        if (planned_plan is not None and len(planned_plan.operations) > 1
                and receipt.get("_host_defer_work_started_presentation") is not True):
            try:
                expressed = await loop._decide({"source":"host_receipt", **result})
                await loop._deliver(str(expressed.get("say") or ""), cause=turn_id)
            except Exception as exc:
                loop.trace.append({"kind":"presentation_failed", "cause":turn_id,
                    "source":"host_receipt",
                    "error":type(exc).__name__ + ": " + str(exc)})
        return result

    async def _reject_work_auip_batch(self, ingress, turn_id: str,
                                      receipt: dict, admission,
                                      reason: str) -> dict:
        ingress.loop._effects.accept_no_effect(admission, reason=reason)
        result = {"state":"rejected", "reason":reason,
            "input_id":admission.utterance_id,
            "utterance_id":admission.utterance_id, "turn_id":turn_id,
            "child_id":str(receipt.get("child_id") or "")}
        ingress.receipts[admission.utterance_id] = result
        try:
            await ingress.loop._express_and_deliver({"source":"host_receipt",
                "state":"rejected", "reason":reason}, cause=turn_id)
        except Exception as exc:
            ingress.loop.trace.append({"kind":"presentation_failed",
                "cause":turn_id, "source":"host_receipt",
                "error":type(exc).__name__ + ": " + str(exc)})
        return result

    async def _resolve_work_reference(self, phrase, candidates, history, *, complete=True):
        """Resolve a Work phrase, with exact known references as a fast path."""
        normalized = " ".join(str(phrase).casefold().split())
        exact = [candidate for candidate in candidates
            if str(phrase) in {candidate.token, candidate.entity_id}]
        if not exact and complete:
            exact = [candidate for candidate in candidates
                if normalized in {" ".join(str(value).casefold().split())
                    for value in (candidate.label, *candidate.aliases)
                    if str(value).strip()}]
        if len(exact) == 1:
            return TypedReferenceResolution(status="unique",
                candidates=(exact[0],), reason="exact_alias")
        return await resolve_typed_reference(phrase, candidates,
            complete=complete, query=self.query, history=history)

    async def _read_work_report(self, session_id, source_text, work_item_id, *, nonblocking=False,
                                publish=None):
        return await self._read_ledger_report(session_id, source_text,
            "work_item", work_item_id,
            nonblocking=nonblocking, publish=publish)

    async def _read_ledger_report(self, session_id, source_text, subject, entity_id, *,
                                  nonblocking=False, publish=None):
        if subject not in {"project", "work_item"}:
            raise ValueError("Ledger report target must be a Project or WorkItem")
        attrs = {"intent":"report", "subject":subject,
            "lookup_session_id":session_id,
            **({"_host_nonblocking_report":True} if nonblocking else {})}
        attrs["workspace_ref" if subject == "work_item" else "project_id"] = entity_id
        result = self.work_report(source_text, attrs,
            **({"publish":publish} if publish is not None else {}))
        return await result if inspect.isawaitable(result) else result

    async def _read_selected_ledger_report(self, ingress, turn_id: str, source_text: str,
            admission, candidate, coordinator, *, defer_presentation=False) -> dict:
        current, is_complete, _ = candidate_catalog_from_coordinator(
            coordinator, ingress.session_id)
        if (not is_complete or not any(row.kind == candidate.kind
                and row.entity_id == candidate.entity_id for row in current)
                or (candidate.kind == "work_item" and
                    coordinator.bound_work_item_status_row(
                        ingress.session_id, candidate.entity_id) is None)):
            raise LoopConflict("Ledger report target is no longer available")
        result = {"state":"work_reported", "input_id":admission.utterance_id,
            "utterance_id":admission.utterance_id, "turn_id":turn_id,
            **({"report_work_item_id":candidate.entity_id}
                if candidate.kind == "work_item" else
                {"report_project_id":candidate.entity_id})}
        ingress.receipts[admission.utterance_id] = result
        answers = []

        async def publish(answer):
            if defer_presentation:
                answers.append(str(answer))
                return False
            return await ingress.loop._deliver(answer, cause=turn_id)

        result["report_result"] = str(await self._read_ledger_report(
            ingress.session_id, source_text, candidate.kind, candidate.entity_id,
            publish=publish) or "")
        if defer_presentation and answers:
            result["report_result"] = "\n".join(answers)
        return result

    async def resolve_work_report(self, ingress, turn_id, receipt, admission):
        """Resolve a read subject using the same complete Ledger catalog as batch."""
        source_text = str(receipt.get("text") or "")
        if self.work_executor is None or self.work_report is None:
            return await self._reject_work_request(
                ingress, turn_id, receipt, admission, "work_report_owner_unavailable")
        coordinator = self.work_executor.coordinator
        candidates, complete, reason = candidate_catalog_from_coordinator(
            coordinator, ingress.session_id)
        if not complete:
            return await self._reject_work_request(
                ingress, turn_id, receipt, admission, "work_report_catalog_incomplete:" + str(reason))
        eligible = tuple(candidate for candidate in candidates if candidate.kind == "work_item"
            and coordinator.bound_work_item_status_row(ingress.session_id, candidate.entity_id) is not None)
        history = ingress.loop.prior_messages(turn_id)
        resolution = await self._resolve_work_reference(
            source_text, eligible, history)

        if resolution.status == "unique":
            ingress.loop._effects.accept_no_effect(admission, reason="work_report")
            return await self._read_selected_ledger_report(
                ingress, turn_id, source_text, admission, resolution.candidate, coordinator)
        if resolution.status != "ambiguous":
            return await self._reject_work_request(
                ingress, turn_id, receipt, admission, "work_report_target_" + resolution.status)
        from server.reference_clarification import create_reference_selection

        async def resume(plan):
            return await self._read_selected_ledger_report(
                ingress, turn_id, source_text, admission, plan.candidate, coordinator)

        ingress.loop._effects.accept_no_effect(admission, reason="work_report_selection_required")
        request = await create_reference_selection(session_id=ingress.session_id,
            task_text=source_text, attrs={"intent":"report"},
            candidates=resolution.candidates, resume=resume, coordinator=self.attention)
        result = {"state":"work_report_selection_required", "input_id":admission.utterance_id,
            "utterance_id":admission.utterance_id, "turn_id":turn_id,
            "attention_request_id":str(request.get("id") or "")}
        ingress.receipts[admission.utterance_id] = result
        return result

    async def resolve_work_report_batch(self, ingress, turn_id: str,
                                        receipt: dict, admission) -> dict:
        """Accept one Work effect plus one independent canonical Ledger read."""

        loop = ingress.loop
        actions = receipt.get("batch_actions")
        if (self.work_control is None or self.work_executor is None
                or self.work_report is None or not isinstance(actions, list)):
            return await self._reject_work_request(
                ingress, turn_id, receipt, admission,
                "work_report_batch_owner_unavailable")
        context_id = str(receipt.get("child_id") or "")
        if (loop._binding.child_id, loop._binding.token) != (
                context_id, str(receipt.get("source_binding_token") or "")):
            return await self._reject_work_request(
                ingress, turn_id, receipt, admission,
                "work_report_batch_binding_changed")
        work_action = next((row for row in actions
            if isinstance(row, dict) and row.get("op") == "work"), None)
        report_action = next((row for row in actions
            if isinstance(row, dict) and row.get("op") == "report"), None)
        if work_action is None or report_action is None:
            return await self._reject_work_request(
                ingress, turn_id, receipt, admission,
                "work_report_batch_invalid")

        coordinator = self.work_executor.coordinator
        catalog = candidate_catalog_from_coordinator(
            coordinator, ingress.session_id)
        candidates, complete, reason = catalog
        if not complete:
            return await self._reject_work_request(
                ingress, turn_id, receipt, admission,
                "work_report_batch_catalog_incomplete:" + str(reason or "unknown"))
        work_candidates, work_complete, work_reason = (
            self.work_candidates_for_context(ingress.session_id, context_id))
        if not work_complete:
            return await self._reject_work_request(
                ingress, turn_id, receipt, admission,
                "work_report_batch_work_catalog_incomplete:"
                + str(work_reason or "unknown"))
        report_candidates = tuple(candidate for candidate in candidates
            if candidate.kind == "work_item"
            and coordinator.bound_work_item_status_row(
                ingress.session_id, candidate.entity_id) is not None)
        history = loop.prior_messages(turn_id)

        work_target = None
        if work_action.get("intent") == "amend":
            # The role's target is a proposal. Resolve the exact admitted Work
            # clause so a known alias cannot replace the object the user named.
            resolution = await self._resolve_work_reference(
                str(work_action.get("source") or ""), work_candidates, history,
                complete=work_complete)
            if resolution.status != "unique":
                return await self._reject_work_request(
                    ingress, turn_id, receipt, admission,
                    "work_report_batch_work_target_" + resolution.status)
            work_target = resolution.candidate
        report_resolution = await self._resolve_work_reference(
            str(report_action.get("source") or ""), report_candidates, history)
        if report_resolution.status != "unique":
            return await self._reject_work_request(
                ingress, turn_id, receipt, admission,
                "work_report_batch_report_target_" + report_resolution.status)
        report_target = report_resolution.candidate
        if (candidate_catalog_from_coordinator(coordinator, ingress.session_id)
                != catalog
                or self.work_candidates_for_context(ingress.session_id, context_id)
                != (work_candidates, work_complete, work_reason)
                or (loop._binding.child_id, loop._binding.token) != (
                    context_id, str(receipt.get("source_binding_token") or ""))):
            raise LoopConflict("Work/report batch target catalog changed")
        report_row = coordinator.bound_work_item_status_row(
            ingress.session_id, report_target.entity_id)
        if report_row is None:
            return await self._reject_work_request(
                ingress, turn_id, receipt, admission,
                "work_report_batch_report_target_unavailable")

        source_text = str(receipt.get("text") or "")
        evidence_actions = []
        for index, action in enumerate(actions):
            source = str(action.get("source") or "")
            evidence_actions.append({"index":index, "op":action["op"],
                **({"intent":str(action.get("intent") or "")}
                    if action["op"] == "work" else {}),
                "work_item_id":(report_target.entity_id
                    if action["op"] == "report" else
                    work_target.entity_id if work_target is not None else ""),
                "source_start":action["source_start"],
                "source_end":action["source_end"],
                "source_sha256":hashlib.sha256(
                    source.encode("utf-8")).hexdigest()})
        batch_evidence = {"cooperative_batch":{"version":1,
            "kind":"work_and_report", "actions":evidence_actions}}
        work_receipt = {"state":"work_required",
            "external_export_target":str(work_action.get("external_export_target") or ""),
            "intent":str(work_action.get("intent") or ""),
            "child_id":context_id, "text":str(work_action["source"]),
            "source_user_text":source_text,
            "source_start":work_action["source_start"],
            "source_end":work_action["source_end"],
            "parent_context":str(receipt.get("parent_context") or ""),
            "source_binding_token":str(
                receipt.get("source_binding_token") or ""),
            "context_revision":receipt.get("context_revision", -1),
            "work_item_id":work_target.entity_id if work_target is not None else "",
            "plan_evidence":batch_evidence,
            "input_id":admission.utterance_id}
        work_result = await self.handle_work_action(
            ingress, turn_id, work_receipt, admission)
        if work_result.get("state") != "work_started":
            return work_result
        report_result = await self._read_work_report(
            ingress.session_id, str(report_action["source"]), report_target.entity_id,
            nonblocking=True)
        result = {"state":"work_report_batch_started",
            "input_id":admission.utterance_id,
            "utterance_id":admission.utterance_id, "turn_id":turn_id,
            "work":dict(work_result),
            "report_work_item_id":report_target.entity_id,
            "report_result":str(report_result or "")}
        ingress.receipts[admission.utterance_id] = result
        return result

    async def _reject_work_request(self, ingress, turn_id: str,
                                        receipt: dict, admission,
                                        reason: str) -> dict:
        ingress.loop._effects.accept_no_effect(admission, reason=reason)
        result = {"state":"rejected", "reason":reason,
            "input_id":admission.utterance_id,
            "utterance_id":admission.utterance_id, "turn_id":turn_id,
            "child_id":str(receipt.get("child_id") or "")}
        ingress.receipts[admission.utterance_id] = result
        if receipt.get("_host_defer_work_started_presentation") is True:
            return result
        try:
            await ingress.loop._express_and_deliver({"source":"host_receipt",
                "state":"rejected", "reason":reason}, cause=turn_id)
        except Exception as exc:
            ingress.loop.trace.append({"kind":"presentation_failed",
                "cause":turn_id, "source":"host_receipt",
                "error":type(exc).__name__ + ": " + str(exc)})
        return result

    @staticmethod
    def _planned_operation_input_id(admission, operation_index: int, count: int) -> str:
        if count == 1:
            return admission.utterance_id
        digest = hashlib.sha256(
            f"{admission.root_id}\0{operation_index}".encode("utf-8")
        ).hexdigest()
        return "planned-input-" + digest

    def _planned_work_catalog(self, session_id, plan):
        coordinator = self.work_executor.coordinator
        catalog = candidate_catalog_from_coordinator(coordinator, session_id)
        current_ids = {row.entity_id for row in catalog[0] if row.kind == "work_item"}
        projects = set()
        for operation in plan.operations:
            references = operation.action.get(CONTROL_REFERENCE_CANDIDATES_ATTR)
            if not isinstance(references, tuple):
                continue
            projects.update(reference.parent_project_id for reference in references
                if isinstance(reference, TypedReferenceCandidate) and reference.kind == "work_item"
                and reference.entity_id not in current_ids and reference.scope == "project")
        return (candidate_catalog_from_coordinator(coordinator, session_id,
            indexed_project_ids=tuple(sorted(projects))) if projects else catalog)

    def _compile_planned_work_plan(self, ingress, turn_id: str, receipt: dict,
                                   admission, plan: CompoundControlPlan, selections):
        """Validate every operation and prepare all domain inputs before acceptance."""

        if not isinstance(plan, CompoundControlPlan) or plan.status != "ok":
            return {"status":"error", "reason":"planned_work_shape_unsupported"}
        if (len(plan.operations) != len(plan.clauses)
                or len(plan.operations) > MAX_COMPOUND_OPERATIONS):
            return {"status":"error", "reason":"planned_work_shape_unsupported"}
        source_text = str(receipt.get("source_user_text") or receipt.get("text") or "")
        if not plan.operations:
            if plan.clauses:
                return {"status":"error", "reason":"planned_work_shape_unsupported"}
            return {"status":"ready", "operations":(), "payloads":(),
                "evidence":{"compound_work_plan":{"version":1, "operations":[]}}}
        if any(left.end > right.start
                for left, right in zip(plan.clauses, plan.clauses[1:])):
            return {"status":"error", "reason":"planned_work_source_invalid"}
        if self.work_control is None or self.work_executor is None:
            return {"status":"error", "reason":"work_owner_unavailable"}
        context_id = str(receipt.get("child_id") or "")
        token = str(receipt.get("source_binding_token") or "")
        loop = ingress.loop
        if sm.get_current_session_id() != ingress.session_id:
            return {"status":"error", "reason":"planned_work_session_changed"}
        if (loop._binding.child_id, loop._binding.token) != (context_id, token):
            return {"status":"error", "reason":"planned_work_binding_changed"}
        if context_id:
            child = loop.get_context(context_id)
            if child is None or child.revision != receipt.get("context_revision"):
                return {"status":"error", "reason":"planned_work_binding_changed"}
        catalog = self._planned_work_catalog(ingress.session_id, plan)
        current, complete, catalog_reason = catalog
        if not complete:
            return {"status":"error", "reason":
                "planned_work_catalog_incomplete:" + str(catalog_reason or "unknown")}

        prepared = []
        payloads = []
        evidence_rows = []
        mutated_targets = set()
        focus_operation = None
        unsupported = ("cwd", "branch", "action", "fallback",
            "url", "query", "text", "external_export_target")
        selected_by_index = dict(selections or {})
        for index, (operation, source_clause) in enumerate(
                zip(plan.operations, plan.clauses)):
            clause = str(operation.source_clause or "")
            start = source_text.find(clause)
            if (operation.operation_index != index or not clause or start < 0
                    or source_text.find(clause, start + 1) >= 0
                    or source_clause.text != clause or source_clause.start != start
                    or source_clause.end != start + len(clause)):
                return {"status":"error", "reason":"planned_work_source_invalid"}
            action = dict(operation.action)
            display_title = str(action.pop("_host_display_title", "") or "")
            intent = str(action.get("intent") or "")
            focus_audit = (current_focus_modifier_audit(action)
                if action.get("focus") and intent != "focus" else None)
            one_off_value = action.get("one_off")
            one_off = one_off_value is True
            focus_modifier = str(action.get("focus") or "")
            target = str(action.get("target") or "").strip().lower()
            export_authority = str(
                action.get("_host_external_target_authorized") or "")
            export_target = ("desktop" if target in {"desktop", "user_desktop"}
                and export_authority == "desktop" else "")
            workspace_access = str(action.get("_host_workspace_access") or "")
            planned_provider = str(action.get("provider") or "")
            if (intent not in {"execute", "amend", "report", "retract", "focus", "message"}
                    or planned_provider not in self.context_requirements
                    or action.get("force_provider") not in (None, "", "user")
                    or (action.get("task") not in (None, "") if intent == "focus"
                        else str(action.get("task") or clause) != clause)
                    or (workspace_access not in {"none", "read", "write"}
                        if intent in {"execute", "amend"}
                        else workspace_access not in {"", "none"})
                    or one_off_value not in (None, False, True)
                    or (one_off is True and intent != "execute")
                    or focus_modifier not in {"", "set", "clear"}
                    or (focus_modifier and intent != "focus" and (
                        focus_audit is None or not focus_audit.allowed
                        or focus_audit.requested != focus_modifier
                        or str(action.get("_host_source_user_text") or "") != source_text))
                    or bool(target or export_authority) != bool(export_target)
                    or (export_target and (intent not in {"execute", "amend"}
                        or workspace_access != "write"))
                    or any(action.get(key) not in (None, "", False)
                        for key in unsupported)
                    or receipt.get("external_export_target")):
                return {"status":"error",
                    "reason":"planned_work_semantics_unsupported"}
            if intent == "message" and (focus_modifier
                    or CONTROL_PAYLOAD_GROUNDING_ATTR in action):
                return {"status":"error", "reason":"planned_work_semantics_unsupported"}
            references = action.get(CONTROL_REFERENCE_CANDIDATES_ATTR)
            workspace_ref = str(action.get("workspace_ref") or "")
            project_id = str(action.get("project_id") or action.get("projectId") or "")
            selected = None
            global_project_report = False
            clear_focus = False
            if references is None:
                subject = str(action.get("subject") or "")
                global_project_report = (
                    intent == "report" and subject == "project"
                    and not workspace_ref and not project_id)
                clear_focus = (intent == "focus" and focus_modifier == "clear"
                    and not subject and not workspace_ref and not project_id)
                if (not global_project_report and not clear_focus
                        and (intent not in {"execute", "message"} or subject
                            or workspace_ref or project_id)):
                    return {"status":"error",
                        "reason":"planned_work_target_unsupported"}
            else:
                if (not isinstance(references, tuple)
                        or any(not isinstance(row, TypedReferenceCandidate)
                            or row.kind not in {"project", "work_item"}
                            for row in references)
                        or len({row.token for row in references}) != len(references)):
                    return {"status":"error",
                        "reason":"planned_work_target_unsupported"}
                if not references:
                    return {"status":"error",
                        "reason":"planned_work_target_unavailable"}
                kinds = {row.kind for row in references}
                expected_subject = next(iter(kinds)) if len(kinds) == 1 else "open"
                if str(action.get("subject") or "") != expected_subject:
                    return {"status":"error",
                        "reason":"planned_work_target_unsupported"}
                resolved = []
                for reference in references:
                    matches = [candidate for candidate in current
                        if candidate.kind == reference.kind
                        and candidate.entity_id == reference.entity_id]
                    if len(matches) != 1:
                        return {"status":"error",
                            "reason":"planned_work_target_unavailable"}
                    resolved.append(matches[0])
                chosen = selected_by_index.get(index)
                if chosen is None and len(resolved) > 1:
                    return {"status":"attention", "operation_index":index,
                        "candidates":tuple(resolved), "catalog":catalog,
                        "selections":selected_by_index}
                selected = chosen or resolved[0]
                if not any(row.kind == selected.kind
                        and row.entity_id == selected.entity_id for row in resolved):
                    return {"status":"error",
                        "reason":"planned_work_target_unavailable"}
            try:
                CurrentTurnSourceSpanV1.capture(
                    admission, source_text, start=start, end=start + len(clause))
            except (TypeError, ValueError):
                return {"status":"error", "reason":"planned_work_source_invalid"}
            planned_receipt = {**receipt, "intent":intent, "text":clause,
                "source_user_text":source_text, "source_start":start,
                "source_end":start + len(clause), "one_off":one_off is True,
                **({"display_title":display_title} if display_title else {}),
                "_host_defer_work_started_presentation":(
                    receipt.get("_host_defer_work_started_presentation") is True
                    or len(plan.operations) > 1),
                "_host_operation_input_id":self._planned_operation_input_id(
                    admission, index, len(plan.operations))}
            descriptor = {"index":index, "intent":intent, "source":clause,
                "receipt":planned_receipt, "candidate":selected}
            focus_attrs = None
            if intent != "focus" and focus_modifier:
                if focus_modifier == "set":
                    if selected is None or selected.kind != "project":
                        return {"status":"error",
                            "reason":"planned_work_target_unsupported"}
                    focus_attrs = {"intent":"focus",
                        "project_id":selected.entity_id}
                else:
                    focus_attrs = {"intent":"focus"}
            if clear_focus:
                focus_attrs = {"intent":"focus"}
                descriptor["kind"] = "focus"
            elif intent == "focus":
                if (selected is None or selected.kind != "project"
                        or workspace_ref or export_target
                        or focus_modifier not in {"", "set"}
                        or (project_id and project_id != selected.entity_id)):
                    return {"status":"error",
                        "reason":"planned_work_target_unsupported"}
                focus_attrs = {"intent":"focus", "project_id":selected.entity_id}
                descriptor["kind"] = "focus"
            elif global_project_report:
                if self.work_report is None:
                    return {"status":"error",
                        "reason":"work_report_owner_unavailable"}
                descriptor["kind"] = "project_report"
            elif intent == "message":
                if (project_id or (selected is not None and (
                        selected.kind != "work_item"
                        or workspace_ref not in {"", selected.entity_id}))):
                    return {"status":"error", "reason":"planned_work_target_unsupported"}
                active = (self.active_work_for_recipient(
                    ingress.session_id, context_id, work_item_id=selected.entity_id)
                    if selected is not None else None)
                if active is not None:
                    if self.work_input is None:
                        return {"status":"error", "reason":"work_input_owner_unavailable"}
                    planned_receipt.update(active, state="work_input_required")
                    descriptor.update(kind="input", active=dict(active))
                else:
                    # Provider conversations have one accepted recipient per
                    # source turn. Keep their existing owner and admission;
                    # never preaccept a no-effect Work plan before its effect.
                    if len(plan.operations) != 1:
                        return {"status":"error",
                            "reason":"planned_work_semantics_unsupported"}
                    descriptor.update(kind="message", provider=planned_provider,
                        message_catalog=self.task_context_candidates(ingress))
            elif selected is None:
                planned_receipt["external_export_target"] = export_target
                planned_receipt.update(state="work_required", work_item_id="")
                value = self._prepare_work_request(
                    ingress, turn_id, planned_receipt, admission,
                    planned_provider=planned_provider,
                    planned_workspace_access=workspace_access)
                if isinstance(value, dict):
                    return {"status":"error", "reason":value["reason"]}
                payload, recipient, provider = value
                descriptor.update(kind="write", payload=payload,
                    recipient_context_id=recipient, work_provider=provider)
                payloads.append(payload)
            elif selected.kind == "project":
                if (workspace_ref or (project_id and project_id != selected.entity_id)
                        or intent not in {"execute", "amend", "report"}):
                    return {"status":"error",
                        "reason":"planned_work_target_unsupported"}
                if intent == "report":
                    if self.work_report is None:
                        return {"status":"error",
                            "reason":"work_report_owner_unavailable"}
                    descriptor["kind"] = "report"
                else:
                    planned_receipt["external_export_target"] = export_target
                    if self.destination is None:
                        return {"status":"error",
                            "reason":"work_destination_owner_unavailable"}
                    try:
                        project = self.destination.available_project(selected.entity_id)
                    except (WorkLedgerError, OSError):
                        return {"status":"error",
                            "reason":"planned_work_target_unavailable"}
                    planned_receipt.update(state="work_required", work_item_id="",
                        one_off=False, project_id=project.project_id)
                    value = self._prepare_work_request(
                        ingress, turn_id, planned_receipt, admission,
                        planned_provider=planned_provider,
                        planned_workspace_access=workspace_access)
                    if isinstance(value, dict):
                        return {"status":"error", "reason":value["reason"]}
                    payload, recipient, provider = value
                    descriptor.update(kind="write", payload=payload,
                        recipient_context_id=recipient, work_provider=provider)
                    payloads.append(payload)
            else:
                if (project_id
                        or (workspace_ref and workspace_ref != selected.entity_id)
                        or intent == "execute" or focus_modifier == "set"):
                    return {"status":"error",
                        "reason":"planned_work_target_unsupported"}
                target_key = selected.token
                if intent in {"amend", "retract"}:
                    if target_key in mutated_targets:
                        return {"status":"error",
                            "reason":"planned_work_duplicate_mutation"}
                    mutated_targets.add(target_key)
                if intent == "report":
                    if (self.work_report is None
                            or self.work_executor.coordinator.bound_work_item_status_row(
                                ingress.session_id, selected.entity_id) is None):
                        return {"status":"error",
                            "reason":"planned_work_target_unavailable"}
                    descriptor["kind"] = "report"
                elif intent == "retract":
                    candidates, stop_complete, stop_reason, targets = (
                        self.task_stop_candidates(ingress, include_provider=False))
                    stop_candidate = next((row for row in candidates
                        if row.kind == selected.kind
                        and row.entity_id == selected.entity_id), None)
                    if (not stop_complete or stop_candidate is None
                            or stop_candidate.token not in targets):
                        return {"status":"error", "reason":
                            "planned_work_catalog_incomplete:" + str(stop_reason or "unknown")
                            if not stop_complete else "planned_work_target_unavailable"}
                    descriptor.update(kind="stop", candidate=stop_candidate,
                        stop_target=dict(targets[stop_candidate.token]))
                elif intent == "amend":
                    planned_receipt["external_export_target"] = export_target
                    active = self.active_work_for_recipient(
                        ingress.session_id, context_id,
                        work_item_id=selected.entity_id) or {}
                    if active.get("work_item_id") == selected.entity_id:
                        if self.work_input is None:
                            return {"status":"error", "reason":"work_input_owner_unavailable"}
                        attempt = self.work_executor.coordinator.store.get_attempt(
                            active["attempt_id"])
                        if attempt is None or attempt.provider != planned_provider:
                            return {"status":"error",
                                "reason":"planned_work_active_provider_mismatch"}
                        planned_receipt.update(active, state="work_input_required")
                        descriptor.update(kind="input", active=dict(active))
                    else:
                        planned_receipt.update(state="work_required",
                            work_item_id=selected.entity_id)
                        value = self._prepare_work_request(
                            ingress, turn_id, planned_receipt, admission,
                            planned_provider=planned_provider,
                            planned_workspace_access=workspace_access)
                        if isinstance(value, dict):
                            return {"status":"error", "reason":value["reason"]}
                        payload, recipient, provider = value
                        descriptor.update(kind="write", payload=payload,
                            recipient_context_id=recipient, work_provider=provider)
                        payloads.append(payload)
                else:
                    return {"status":"error",
                        "reason":"planned_work_target_unsupported"}
            if focus_attrs is not None:
                if self.work_focus is None:
                    return {"status":"error", "reason":"work_focus_owner_unavailable"}
                if focus_operation is not None:
                    return {"status":"error", "reason":"planned_work_duplicate_focus"}
                focus_operation = index
                descriptor["focus_attrs"] = focus_attrs
            evidence_rows.append({"index":index, "intent":intent,
                "target":("focus:draft" if clear_focus else
                    "project:*" if global_project_report else
                    selected.token if selected is not None else ""),
                **({"focus":"set", "focus_project_id":
                    str(focus_attrs.get("project_id") or "")}
                    if focus_attrs and focus_attrs.get("project_id") else
                    {"focus":"clear"} if focus_attrs else {}),
                **({"external_export_target":"desktop"} if export_target else {}),
                **({"input_id":planned_receipt["_host_operation_input_id"],
                    "run_id":planned_receipt["run_id"]} if descriptor["kind"] == "input" else {}),
                "source_start":start, "source_end":start + len(clause),
                "source_sha256":hashlib.sha256(clause.encode("utf-8")).hexdigest()})
            prepared.append(descriptor)
        supplied_evidence = (dict(receipt.get("plan_evidence"))
            if isinstance(receipt.get("plan_evidence"), dict) else {})
        if "compound_work_plan" in supplied_evidence:
            return {"status":"error", "reason":"planned_work_evidence_invalid"}
        return {"status":"ready", "operations":tuple(prepared),
            "payloads":tuple(payloads), "catalog":catalog,
            "evidence":{**supplied_evidence,
                "compound_work_plan":{"version":1,
                    "operations":evidence_rows}}}

    async def _present_preaccepted_planned_rejection(self, ingress, turn_id: str,
                                                      receipt: dict, admission,
                                                      reason: str) -> dict:
        result = {"state":"rejected", "reason":reason,
            "input_id":admission.utterance_id,
            "utterance_id":admission.utterance_id, "turn_id":turn_id,
            "child_id":str(receipt.get("child_id") or "")}
        ingress.receipts[admission.utterance_id] = result
        if receipt.get("_host_defer_work_started_presentation") is not True:
            try:
                await ingress.loop._express_and_deliver(
                    {"source":"host_receipt", **result}, cause=turn_id)
            except Exception as exc:
                ingress.loop.trace.append({"kind":"presentation_failed",
                    "cause":turn_id, "source":"host_receipt",
                    "error":type(exc).__name__ + ": " + str(exc)})
        return result

    async def _request_planned_plan_selection(self, ingress, turn_id: str,
            receipt: dict, admission, plan, compiled, selections, *, resume=None) -> dict:
        frozen = dict(receipt)
        choices = {opaque_option_id():candidate
            for candidate in compiled["candidates"]}
        operation_index = int(compiled["operation_index"])
        expected_catalog = compiled["catalog"]
        expected_binding = (str(frozen.get("child_id") or ""),
            str(frozen.get("source_binding_token") or ""))
        expected_revision = frozen.get("context_revision")

        async def continue_once(option_id):
            selected = choices.get(option_id)
            if selected is None:
                raise LoopConflict("planned Work option is not part of this request")
            if sm.get_current_session_id() != ingress.session_id:
                raise LoopConflict("planned Work Session is no longer current")
            if (ingress.loop._binding.child_id,
                    ingress.loop._binding.token) != expected_binding:
                raise LoopConflict("planned Work binding changed")
            if expected_binding[0]:
                child = ingress.loop.get_context(expected_binding[0])
                if child is None or child.revision != expected_revision:
                    raise LoopConflict("planned Work context changed")
            if self._planned_work_catalog(ingress.session_id, plan) != expected_catalog:
                raise LoopConflict("planned Work target catalog changed")
            next_selections = {**selections, operation_index:selected}
            if resume is not None:
                return await resume(next_selections, expected_catalog)
            return await self.handle_planned_work_action(
                ingress, turn_id, frozen, admission, plan,
                _selections=next_selections, _expected_catalog=expected_catalog,
                _after_selection=True)

        request = await self.attention.create_selection(
            session_id=ingress.session_id, title="Choose the Work target",
            prompt="The planned operation retains more than one possible Host target.",
            options=[AttentionOption(option_id=option_id,
                label=("Project" if candidate.kind == "project" else "Work")
                    + " · " + (candidate.label or candidate.entity_id),
                description=(candidate.parent_project_label
                    if candidate.kind == "work_item" else "Persistent Project"),
                metadata={"scope":candidate.kind})
                for option_id, candidate in choices.items()],
            continuation=continue_once,
            dedupe_key="cooperative_planned_work_target")
        result = {"state":"planned_work_selection_required",
            "input_id":admission.utterance_id,
            "utterance_id":admission.utterance_id, "turn_id":turn_id,
            "child_id":str(receipt.get("child_id") or ""),
            "attention_request_id":str(request.get("id") or "")}
        ingress.receipts[admission.utterance_id] = result
        return result

    async def _dispatch_prepared_work_stop(self, ingress, descriptor):
        candidate = descriptor["candidate"]
        candidates, complete, _reason, targets = self.task_stop_candidates(
            ingress, include_provider=False)
        current = next((row for row in candidates if row.kind == candidate.kind
            and row.entity_id == candidate.entity_id), None)
        if (not complete or current is None or current.token not in targets
                or targets[current.token].get("work_item_id")
                    != descriptor["stop_target"].get("work_item_id")):
            return {"state":"unknown", "reason":"work_execution_changed",
                **descriptor["stop_target"]}
        return await self._stop_named_work(
            ingress.session_id, dict(targets[current.token]))

    async def _dispatch_prepared_focus(self, ingress, turn_id: str,
                                       descriptor) -> dict:
        if sm.get_current_session_id() != ingress.session_id:
            return {"state":"rejected", "reason":"planned_work_session_changed",
                "ok":False}
        outcome = self.work_focus(dict(descriptor["focus_attrs"]),
            session_id=ingress.session_id)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        outcome = dict(outcome or {})
        return {"state":"work_focus_changed" if outcome.get("ok") is True
                else "rejected",
            "reason":"" if outcome.get("ok") is True
                else str(outcome.get("message") or "work_focus_rejected"),
            **outcome}

    async def _dispatch_compiled_work_plan(self, ingress, turn_id: str, receipt: dict,
                                           admission, compiled, accepted) -> dict:
        operations = compiled["operations"]
        if not operations:
            result = {"state":"no_action", "input_id":admission.utterance_id,
                "utterance_id":admission.utterance_id, "turn_id":turn_id,
                "child_id":str(receipt.get("child_id") or "")}
            ingress.receipts[admission.utterance_id] = result
            if (receipt.get("_host_defer_work_started_presentation") is not True
                    and receipt.get("_host_coordination_delivered") is not True
                    and receipt.get("coordination_say")):
                await ingress.loop._deliver(
                    str(receipt["coordination_say"]), cause=turn_id)
            return result
        effect_ids = iter(accepted.get("effect_ids") or ())
        results = []
        source_binding = (str(receipt.get("child_id") or ""),
            str(receipt.get("source_binding_token") or ""))
        focus_changed = False
        for descriptor in operations:
            kind = descriptor["kind"]
            operation_receipt = descriptor["receipt"]
            focus_result = (await self._dispatch_prepared_focus(
                ingress, turn_id, descriptor)
                if descriptor.get("focus_attrs") is not None else None)
            focus_changed = (focus_changed
                or bool(focus_result and focus_result.get("ok") is True))
            if kind == "write":
                result = await self._dispatch_accepted_work(
                    ingress, turn_id, operation_receipt, admission,
                    next(effect_ids), descriptor["recipient_context_id"],
                    descriptor["work_provider"], present=len(operations) == 1)
            elif kind == "input":
                result = await self._handle_work_input(
                    ingress, turn_id, operation_receipt, admission,
                    preaccepted=(accepted.get("local") or {}).get(str(descriptor["index"])))
            elif kind == "report":
                result = await self._read_selected_ledger_report(
                    ingress, turn_id, descriptor["source"], admission,
                    descriptor["candidate"], self.work_executor.coordinator,
                    defer_presentation=operation_receipt.get(
                        "_host_defer_work_started_presentation") is True)
            elif kind == "project_report":
                answers = []

                async def publish(answer):
                    if operation_receipt.get(
                            "_host_defer_work_started_presentation") is True:
                        answers.append(str(answer))
                        return False
                    return await ingress.loop._deliver(answer, cause=turn_id)

                report_result = await self._read_ledger_report(
                    ingress.session_id, descriptor["source"], "project", "",
                    publish=publish)
                result = {"state":"work_reported",
                    "input_id":admission.utterance_id,
                    "utterance_id":admission.utterance_id, "turn_id":turn_id,
                    "report_project_id":"",
                    "report_result":"\n".join(answers) if answers else
                        str(report_result or "")}
            elif kind == "focus":
                result = focus_result
            else:
                stopped = await self._dispatch_prepared_work_stop(ingress, descriptor)
                if (len(operations) == 1 and operation_receipt.get(
                        "_host_defer_work_started_presentation") is not True):
                    result = await self._present_task_stop(
                        ingress, turn_id, operation_receipt, admission,
                        {**stopped, "target":descriptor["candidate"].label})
                else:
                    result = {**stopped, "target":descriptor["candidate"].label,
                        "input_id":admission.utterance_id,
                        "utterance_id":admission.utterance_id, "turn_id":turn_id,
                        "action":"interrupt",
                        "question":str(receipt.get("source_user_text")
                            or receipt.get("text") or "")}
            if focus_result is not None and kind != "focus":
                result = {**result, "focus":focus_result}
            results.append(result)
        if (focus_changed and (ingress.loop._binding.child_id,
                ingress.loop._binding.token) == source_binding):
            ingress.loop.bind_context("")
        if len(results) == 1:
            result = results[0]
        else:
            result = {"state":"planned_work_batch_applied",
                "input_id":admission.utterance_id,
                "utterance_id":admission.utterance_id, "turn_id":turn_id,
                "child_id":str(receipt.get("child_id") or ""),
                "operations":results}
            ingress.receipts[admission.utterance_id] = result
            if receipt.get("_host_defer_work_started_presentation") is not True:
                try:
                    await ingress.loop._express_and_deliver(
                        {"source":"host_receipt", **result}, cause=turn_id)
                except Exception as exc:
                    ingress.loop.trace.append({"kind":"presentation_failed",
                        "cause":turn_id, "source":"host_receipt",
                        "error":type(exc).__name__ + ": " + str(exc)})
        ingress.receipts[admission.utterance_id] = result
        if (len(results) == 1 and operations[0]["kind"] == "focus"
                and receipt.get("_host_defer_work_started_presentation") is not True):
            try:
                await ingress.loop._express_and_deliver(
                    {"source":"host_receipt", **result}, cause=turn_id)
            except Exception as exc:
                ingress.loop.trace.append({"kind":"presentation_failed",
                    "cause":turn_id, "source":"host_receipt",
                    "error":type(exc).__name__ + ": " + str(exc)})
        return result

    async def handle_planned_work_action(self, ingress, turn_id: str,
                                         receipt: dict, admission,
                                         plan: CompoundControlPlan, *,
                                         _selections=None, _expected_catalog=None,
                                         _after_selection=False,
                                         require_live_turn=None) -> dict:
        """Compile one complete resolved plan, accept it once, then dispatch owners."""

        loop = ingress.loop
        selections = dict(_selections or {})
        if self.work_executor is not None:
            await self.work_executor.coordinator.drain_provider_facts()
        message = None
        async with loop._foreground:
            if require_live_turn is not None:
                require_live_turn()
            compiled = self._compile_planned_work_plan(
                ingress, turn_id, receipt, admission, plan, selections)
            if (_expected_catalog is not None and compiled.get("status") != "error"
                    and compiled.get("catalog") != _expected_catalog):
                raise LoopConflict("planned Work target catalog changed")
            if compiled.get("status") == "attention":
                pass
            elif compiled.get("status") == "error":
                reason = str(compiled.get("reason") or "planned_work_rejected")
                loop._effects.accept_no_effect(admission, reason=reason)
            elif (len(compiled["operations"]) == 1
                    and compiled["operations"][0]["kind"] == "message"):
                message = compiled["operations"][0]
            else:
                evidence = compiled["evidence"]
                inputs = tuple(row for row in compiled["operations"] if row["kind"] == "input")
                def accept_inputs(cursor):
                    return {str(row["index"]):self._accept_work_input_row(
                        cursor, row["receipt"], admission) for row in inputs}
                try:
                    if compiled["payloads"]:
                        accepted = self.work_control.seal_many(
                            admission, compiled["payloads"], plan_evidence=evidence,
                            local_apply=accept_inputs if inputs else None)
                    else:
                        accepted = loop._effects.accept_no_effect(
                            admission, reason="planned_work_controls",
                            plan_evidence=evidence, local_apply=accept_inputs if inputs else None)
                except (ControlLedgerConflict, WorkLedgerError, TypeError, ValueError):
                    reason = "planned_work_acceptance_rejected"
                    loop._effects.accept_no_effect(admission, reason=reason,
                        plan_evidence=evidence)
                    compiled = {"status":"error", "reason":reason}
        if compiled.get("status") == "attention":
            return await self._request_planned_plan_selection(
                ingress, turn_id, receipt, admission, plan, compiled, selections)
        if compiled.get("status") == "error":
            return await self._present_preaccepted_planned_rejection(
                ingress, turn_id, receipt, admission, compiled["reason"])
        if message is not None:
            return await self._dispatch_planned_provider_message(
                ingress, turn_id, receipt, admission, message,
                require_live_turn=require_live_turn)
        dispatch_receipt = dict(receipt)
        if _after_selection:
            dispatch_receipt.pop("coordination_say", None)
            dispatch_receipt.pop("_host_defer_work_started_presentation", None)
        return await self._dispatch_compiled_work_plan(
            ingress, turn_id, dispatch_receipt, admission, compiled, accepted)

    async def _dispatch_planned_provider_message(self, ingress, turn_id, receipt,
            admission, message, *, require_live_turn=None):
        """Send a classified conversation through the original message owner."""
        frozen = dict(receipt)
        # A single conversation retains the entire admitted user turn. Its
        # clause identifies the requested act; it is not rewritten payload.
        frozen["text"] = str(receipt.get("source_user_text") or receipt.get("text") or "")
        if frozen.get("_host_coordination_delivered") is True:
            frozen["coordination_say"] = ""
        selected = message["candidate"]
        if selected is not None:
            frozen.update(source_binding_context_id=receipt.get("child_id", ""),
                provider=message["provider"])
            return await self._continue_resolved_task_address(
                ingress, turn_id, frozen, admission,
                TypedReferenceResolution(status="unique", candidates=(selected,)),
                catalog=message["message_catalog"],
                admission_check=require_live_turn)
        if not isinstance(frozen.get("provider_message_action"), dict):
            child = ingress.loop.get_context(str(frozen.get("child_id") or ""))
            current_provider = child.provider if child is not None else ingress.loop.provider
            frozen["provider_message_action"] = (
                {"op":"send"} if current_provider == message["provider"] else
                {"op":"send_to", "provider":message["provider"]})
        result = await ingress.loop.continue_provider_message(
            frozen, admission, admission_check=require_live_turn)
        result.update(input_id=admission.utterance_id,
            utterance_id=admission.utterance_id, turn_id=turn_id,
            _host_coordination_delivered=receipt.get("_host_coordination_delivered") is True,
            _host_provider_message_continuation=True)
        return await ingress._resolve_cooperative_receipt(
            result, turn_id, admission, require_live_turn or (lambda:None))

    async def _dispatch_selected_work_amend(self, ingress, turn_id: str,
            source_receipt: dict, admission, selected: TypedReferenceCandidate, *,
            catalog, expected_binding, expected_revision, active_work,
            after_selection=False) -> dict:
        candidates, complete, reason = catalog
        context_id = str(source_receipt.get("child_id") or "")
        current = self.work_candidates_for_context(ingress.session_id, context_id)
        if current != (candidates, complete, reason):
            raise LoopConflict("Work amendment target catalog changed")
        selected_receipt = {**source_receipt, "state":"work_required",
            "work_item_id":selected.entity_id}
        if after_selection:
            selected_receipt.pop("coordination_say", None)
            selected_receipt.pop("_host_defer_work_started_presentation", None)
        selected_active = (active_work if active_work.get("work_item_id") == selected.entity_id else
            self.active_work_for_recipient(ingress.session_id, context_id,
                work_item_id=selected.entity_id) or {})
        if (selected_active.get("work_item_id") == selected.entity_id
                and not source_receipt.get("plan_evidence")):
            loop = ingress.loop
            async with loop._foreground:
                if self.work_candidates_for_context(
                        ingress.session_id, context_id) != (candidates, complete, reason):
                    raise LoopConflict("Work amendment target catalog changed")
                if (loop._binding.child_id, loop._binding.token) != expected_binding:
                    raise LoopConflict("Work amendment binding changed")
                if context_id:
                    child = loop.get_context(context_id)
                    if child is None or child.revision != expected_revision:
                        raise LoopConflict("Work amendment context changed")
                text = str(source_receipt.get("text") or "")
                source_text = str(source_receipt.get("source_user_text") or text)
                CurrentTurnSourceSpanV1.capture(admission, source_text,
                    start=source_receipt.get("source_start", 0),
                    end=source_receipt.get("source_end", len(source_text)))
                selected_receipt.update(selected_active, state="work_input_required")
        return await self.handle_work_action(
            ingress, turn_id, selected_receipt, admission)

    async def resolve_work_amend_target(self, ingress, turn_id: str,
                                        receipt: dict, admission) -> dict:
        """Resolve the admitted Work request outside the role's semantic proposal."""

        source_receipt = dict(receipt)
        context_id = str(receipt.get("child_id") or "")
        candidates, complete, reason = self.work_candidates_for_context(
            ingress.session_id, context_id)
        expected_binding = (context_id,
            str(receipt.get("source_binding_token") or ""))
        expected_revision = receipt.get("context_revision")
        active_reader = getattr(ingress.loop, "active_work", None)
        active_work = dict(active_reader(context_id) or {}) if active_reader else {}
        history = ingress.loop.prior_messages(turn_id)
        # The role-proposed target proves at most that an object exists. Identity
        # comes from the admitted Work request (or its exact source clause).
        resolution = await self._resolve_work_reference(
            str(source_receipt.get("text") or ""), candidates, history,
            complete=complete)

        if resolution.status == "unique":
            return await self._dispatch_selected_work_amend(
                ingress, turn_id, source_receipt, admission, resolution.candidate,
                catalog=(candidates, complete, reason), expected_binding=expected_binding,
                expected_revision=expected_revision, active_work=active_work)
        if resolution.status != "ambiguous":
            ingress.loop._effects.accept_no_effect(admission,
                reason="work_amend_target_" + resolution.status)
            result = {"state":"rejected",
                "reason":"work_amend_target_" + resolution.status,
                "input_id":admission.utterance_id,
                "utterance_id":admission.utterance_id, "turn_id":turn_id,
                "child_id":context_id}
            ingress.receipts[admission.utterance_id] = result
            if receipt.get("_host_defer_work_started_presentation") is True:
                return result
            try:
                await ingress.loop._express_and_deliver({"source":"host_receipt",
                    "state":"rejected", "reason":result["reason"]}, cause=turn_id)
            except Exception as exc:
                ingress.loop.trace.append({"kind":"presentation_failed",
                    "cause":turn_id, "source":"host_receipt",
                    "error":type(exc).__name__ + ": " + str(exc)})
            return result

        choices = {opaque_option_id():candidate
            for candidate in resolution.candidates}

        async def continue_once(option_id: str):
            selected = choices.get(option_id)
            if selected is None:
                raise LoopConflict("Work amendment option is not part of this request")
            if sm.get_current_session_id() != ingress.session_id:
                raise LoopConflict("Work amendment Session is no longer current")
            loop = ingress.loop
            if (loop._binding.child_id, loop._binding.token) != expected_binding:
                raise LoopConflict("Work amendment binding changed")
            if context_id:
                child = loop.get_context(context_id)
                if child is None or child.revision != expected_revision:
                    raise LoopConflict("Work amendment context changed")
            if self.work_candidates_for_context(
                    ingress.session_id, context_id) != (candidates, complete, reason):
                raise LoopConflict("Work amendment target catalog changed")
            result = await self._dispatch_selected_work_amend(
                ingress, turn_id, source_receipt, admission, selected,
                catalog=(candidates, complete, reason), expected_binding=expected_binding,
                expected_revision=expected_revision, active_work=active_work,
                after_selection=True)
            ingress.receipts[admission.utterance_id] = result
            return result

        options = []
        labels = set()
        for index, (option_id, candidate) in enumerate(choices.items(), start=1):
            label = candidate.label or "Untitled WorkItem"
            alias = next((str(value) for value in candidate.aliases
                if str(value) and str(value) not in label), "")
            if alias:
                label = f"{label} · {alias}"
            if label in labels:
                label = f"{label} · choice {index}"
            labels.add(label)
            options.append(AttentionOption(option_id=option_id, label=label,
                description="Modify this existing deliverable",
                metadata={"scope":"work_item", "relation":"amend"}))
        request = await self.attention.create_selection(
            session_id=ingress.session_id,
            title="Choose the deliverable to modify",
            prompt="The request names more than one possible existing deliverable.",
            options=options, continuation=continue_once,
            dedupe_key="cooperative_work_amend")
        result = {"state":"work_amend_selection_required",
            "input_id":admission.utterance_id, "utterance_id":admission.utterance_id,
            "turn_id":turn_id, "child_id":context_id,
            "attention_request_id":str(request.get("id") or "")}
        ingress.receipts[admission.utterance_id] = result
        if receipt.get("_host_defer_work_started_presentation") is True:
            return result
        try:
            await ingress.loop._express_and_deliver({"source":"host_receipt",
                "state":"work_amend_selection_required",
                "candidate_count":len(options)}, cause=turn_id)
        except Exception as exc:
            ingress.loop.trace.append({"kind":"presentation_failed",
                "cause":turn_id, "source":"host_receipt",
                "error":type(exc).__name__ + ": " + str(exc)})
        return result

    @staticmethod
    def _work_input_values(receipt, admission):
        return {"input_id":str(receipt.get("_host_operation_input_id") or admission.utterance_id),
            "work_item_id":receipt["work_item_id"], "run_id":receipt["run_id"],
            "text":str(receipt.get("text") or "")}

    def _accept_work_input_row(self, cursor, receipt, admission):
        return self.work_executor.coordinator.accept_work_input(cursor,
            values=self._work_input_values(receipt, admission),
            amendment=receipt.get("intent") == "amend", admission=admission)

    async def _handle_work_input(self, ingress, turn_id: str, receipt: dict,
                                 admission, *, preaccepted=None) -> dict:
        active = ({key:receipt[key] for key in ("effect_id", "work_item_id", "attempt_id",
            "run_id", "status", "runtime_attached", "reason") if key in receipt}
            if preaccepted is not None else self.active_work_for_recipient(
            ingress.session_id, str(receipt.get("child_id") or ""),
            work_item_id=str(receipt.get("work_item_id") or "")))
        if active is None or any(active.get(key) != receipt.get(key)
                for key in ("effect_id", "work_item_id", "attempt_id", "run_id")):
            return {"state":"rejected", "reason":"work_run_not_active",
                "input_id":admission.utterance_id, "turn_id":turn_id}
        if active.get("runtime_attached") is not True:
            result = {"state":"unknown",
                "reason":str(active.get("reason") or "work_runtime_owner_unavailable"),
                "input_id":admission.utterance_id,
                "utterance_id":admission.utterance_id, "turn_id":turn_id,
                "child_id":str(receipt.get("child_id") or ""), **active}
            ingress.receipts[admission.utterance_id] = result
            if receipt.get("_host_defer_work_started_presentation") is not True:
                if receipt.get("_host_coordination_delivered") is True:
                    await ingress.loop._express_and_deliver(
                        {"source":"host_receipt", **result}, cause=turn_id)
                else:
                    await ingress.loop._deliver(str(receipt.get("coordination_say") or ""),
                        cause=turn_id)
            return result
        if self.work_input is None:
            return {"state":"rejected", "reason":"work_input_owner_unavailable",
                "input_id":admission.utterance_id, "turn_id":turn_id}
        values = self._work_input_values(receipt, admission)
        if preaccepted is None:
            coordinator = self.work_executor.coordinator
            await coordinator.drain_provider_facts()
            accepted_source = ingress.loop._effects.accept_no_effect(admission, reason="work_input",
                plan_evidence={"work_input":{key:value for key, value in values.items() if key != "text"}},
                local_apply=lambda cursor:{"input":self._accept_work_input_row(cursor, receipt, admission)})
            preaccepted = (accepted_source.get("local") or {}).get("input")
            if preaccepted is None:
                # An accepted-source replay can only read its original input;
                # it must never reconstruct a missing receipt or resend unknown.
                original = coordinator.store.get_provider_input(values["input_id"])
                if original is None:
                    raise LoopConflict("accepted Work input receipt is unavailable")
                preaccepted = original, False
        accepted = self.work_input(values, accepted=preaccepted)
        if inspect.isawaitable(accepted):
            accepted = await accepted
        result = {"state":"work_input_accepted",
            "input_id":admission.utterance_id, "utterance_id":admission.utterance_id,
            "turn_id":turn_id, "child_id":str(receipt.get("child_id") or ""),
            **active,
            "input":dict(accepted.get("input") or {})
                if isinstance(accepted, dict) else {}}
        ingress.receipts[admission.utterance_id] = result
        if (receipt.get("_host_defer_work_started_presentation") is not True
                and receipt.get("_host_coordination_delivered") is not True):
            await ingress.loop._deliver(str(receipt.get("coordination_say") or ""),
                cause=turn_id)
        return result

    def task_stop_candidates(self, ingress, *, include_work=True, include_provider=True,
                             work_item_limit=200):
        """Project existing Work targets and accepted runs, never context labels."""
        candidates, targets = [], {}
        complete, reason = True, ""
        if include_work and self.work_executor is not None:
            work_candidates, complete, reason = candidate_catalog_from_coordinator(
                self.work_executor.coordinator, ingress.session_id, work_item_limit=work_item_limit)
            store = self.work_executor.coordinator.store
            for candidate in work_candidates:
                if candidate.kind != "work_item":
                    continue
                attempts = store.list_attempts(candidate.entity_id)
                latest = attempts[-1] if attempts else None
                candidates.append(candidate)
                targets[candidate.token] = {"kind":"work", "work_item_id":candidate.entity_id,
                    "execution_provider":latest.provider if latest else "",
                    "attempt_id":latest.attempt_id if latest else "",
                    "run_id":latest.provider_run_id if latest else "",
                    "effect_id":latest.origin_effect_id if latest else "",
                    "status":latest.execution_status if latest else "idle",
                    "session_id":str(latest.metadata.get("session_id") or "") if latest else ""}
        if not include_provider:
            return tuple(candidates), bool(complete), str(reason or ""), targets
        contexts = {row["context_id"]:row for row in ingress.loop.context_catalog()}
        with self.ledger._lock:
            rows = self.ledger._db.execute("""SELECT e.*, a.transcript_hash,
                    r.receipt_json FROM control_effect_outbox e
                JOIN control_admissions a ON a.root_id=e.root_id
                LEFT JOIN control_effect_receipts r ON r.effect_id=e.effect_id
                WHERE a.source_scope=? AND e.kind='provider'
                    AND json_extract(e.payload_json,'$.operation')='start'
                ORDER BY e.rowid""", ("chat:" + ingress.session_id,)).fetchall()
        session_history = None
        provider_tasks = {}
        effects = CooperativeProviderEffectLedger(self.ledger)
        for row in rows:
            payload = json.loads(row["payload_json"])
            if payload.get("session_id") != ingress.session_id:
                complete, reason = False, "task_source_scope_unavailable"
                continue
            run_id = str(row["external_id"] or "")
            record = self.runtime.get_run(run_id) if run_id else None
            source = record.task if record is not None else next((str(item.get("text") or "")
                for item in ingress.loop.history if item.get("source") == "user"
                and item.get("input_id") in {payload.get("source_utterance_id"), payload.get("turn_id")}), "")
            if not source or admission_transcript_hash(source) != row["transcript_hash"]:
                if session_history is None:
                    session_history = sm._read_session_history(ingress.session_id)[0].dialog
                source = next((str(item.get("content") or "") for item in session_history
                    if item.get("role") == "user" and item.get("turn_id") == payload.get("turn_id")
                    and admission_transcript_hash(str(item.get("content") or "")) == row["transcript_hash"]), "")
            if not source or admission_transcript_hash(source) != row["transcript_hash"]:
                complete, reason = False, "task_source_history_unavailable"
                continue
            terminal = json.loads(row["receipt_json"] or "{}")
            details = terminal.get("details") or {}
            child_id = str(payload.get("context_id") or "")
            try:
                root_id = effects.task_root(row["effect_id"], session_id=ingress.session_id,
                    context_id=child_id, provider=str(payload.get("provider") or ""))
            except ControlLedgerConflict:
                complete, reason = False, "task_lineage_unavailable"
                continue
            context = contexts.get(child_id)
            status = (record.status if record is not None else
                context["run_status"] if context is not None and context["run_id"] == run_id else
                str(details.get("provider_status") or details.get("status") or "unknown"))
            if run_id.startswith("not-started:"):
                status, run_id = "not_started", ""
            prior = provider_tasks.pop(root_id, None)
            if prior is None and root_id != row["effect_id"]:
                complete, reason = False, "task_source_history_unavailable"
                continue
            candidate = (TypedReferenceCandidate(kind="execution", entity_id=root_id,
                label=source[:160], aliases=(source,), delegated_goal=source,
                scope="session_draft", execution=status) if prior is None else
                replace(prior, aliases=tuple(dict.fromkeys((*prior.aliases, source))), execution=status))
            provider_tasks[root_id] = candidate
            targets[candidate.token] = {"kind":"provider", "child_id":child_id,
                "run_id":run_id, "effect_id":row["effect_id"], "status":status,
                "provider":str(payload.get("provider") or "")}
        candidates.extend(replace(candidate, recency_rank=index) for index, candidate
            in enumerate(reversed(tuple(provider_tasks.values())), start=1))
        return tuple(candidates), bool(complete), str(reason or ""), targets

    @staticmethod
    def _known_task_reference(phrase, candidates, *, complete):
        exact = [candidate for candidate in candidates if phrase == candidate.token]
        if not exact and complete:
            exact = [candidate for candidate in candidates if phrase.casefold() in {
                value.casefold() for value in (candidate.label, *candidate.aliases)}]
        if len(exact) == 1:
            return TypedReferenceResolution(status="unique", candidates=tuple(exact), reason="exact_alias")
        if phrase.startswith(("execution:", "work_item:")):
            return TypedReferenceResolution(status="none", reason="unknown_token")
        return None

    async def _present_task_stop(self, ingress, turn_id: str, frozen: dict,
                                 admission, result: dict) -> dict:
        loop = ingress.loop
        result.update(input_id=admission.utterance_id,
            utterance_id=admission.utterance_id, turn_id=turn_id,
            action="interrupt", question=str(
                frozen.get("source_user_text") or frozen.get("text") or ""))
        ingress.receipts[admission.utterance_id] = result
        if frozen.get("_host_defer_work_started_presentation") is True:
            return result
        try:
            if (result.get("state") == "stopped" and frozen.get("coordination_say")
                    and frozen.get("_host_coordination_delivered") is not True):
                await loop._deliver(frozen["coordination_say"], cause=turn_id)
            else:
                await loop._express_and_deliver(
                    {"source":"host_receipt", **result}, cause=turn_id)
        except Exception as exc:
            loop.trace.append({"kind":"presentation_failed", "cause":turn_id,
                "source":"host_receipt", "error":type(exc).__name__ + ": " + str(exc)})
        return result

    async def _stop_selected_task(self, ingress, turn_id: str, frozen: dict,
                                  admission, candidate, targets) -> dict:
        loop = ingress.loop
        async with loop._foreground:
            if sm.get_current_session_id() != ingress.session_id:
                raise LoopConflict("task stop Session changed")
            if (loop._binding.child_id, loop._binding.token) != (
                    str(frozen.get("child_id") or ""),
                    str(frozen.get("source_binding_token") or "")):
                raise LoopConflict("task stop source binding changed")
            text = str(frozen.get("text") or "")
            source_text = str(frozen.get("source_user_text") or text)
            CurrentTurnSourceSpanV1.capture(admission, source_text,
                start=frozen.get("source_start", 0),
                end=frozen.get("source_end", len(source_text)))
            target = dict(targets[candidate.token])
            if target["kind"] == "provider":
                target["status"] = self._provider_stop_status(
                    ingress.session_id, target)
            if target["kind"] == "provider" and target["status"] in {"queued", "running"}:
                child = loop.get_context(target["child_id"])
                if (child is None or child.closed or child.run_id != target["run_id"]
                        or self.runtime.get_run(target["run_id"]) is None):
                    loop._effects.accept_no_effect(admission,
                        reason="task_execution_changed")
                    result = {"state":"unknown", "reason":"task_execution_changed", **target}
                else:
                    result = await loop._apply({"op":"interrupt", "recipient":child.child_id,
                        "source_binding_context_id":str(frozen.get("child_id") or "")}, text,
                        input_id=admission.utterance_id, turn_id=turn_id,
                        binding_token=str(frozen.get("source_binding_token") or ""),
                        turn_admission=admission, foreground_owned=True,
                        expected_run_id=target["run_id"])
            else:
                loop._effects.accept_no_effect(admission, reason="task_stop")
                if target["kind"] == "work":
                    result = await self._stop_named_work(ingress.session_id, target)
                else:
                    result = {**target, "state":"not_active" if target["status"] in {
                        "done", "error", "cancelled", "not_started", "idle"} else "unknown"}
        return await self._present_task_stop(ingress, turn_id, frozen, admission,
            {**result, "target":candidate.label})

    async def resolve_task_stop(self, ingress, turn_id, receipt, admission):
        frozen = dict(receipt)
        loop = ingress.loop
        catalog = self.task_stop_candidates(ingress,
            include_provider=frozen.get("target_kind") != "work")
        candidates, complete, reason, targets = catalog
        # Resolve the admitted stop request, not the role's paraphrased target.
        # A valid catalog token proposed by the role proves membership only:
        # it must not turn an absent named task into cancellation of current Work.
        phrase = str(frozen.get("text") or "")
        resolution = self._known_task_reference(phrase, candidates, complete=complete)
        if resolution is None:
            history = loop.prior_messages(turn_id)
            async def query_stop(messages):
                # The same reference query resolves cancellation authority, not
                # every entity mentioned alongside a stop request.
                policy = (
                    "\n今回は停止対象の解決です。停止を求められたタスクだけを選んでください。"
                    "継続を求められたタスクや、そのタスクも含む分割不能な実行は選べません。"
                    "候補の元の依頼が実行の同一性を示します。後の役割応答だけで別タスクに置き換えず、"
                    "停止対象が候補にない場合は空の集合を返してください。")
                return await self.query([{**messages[0], "content":messages[0]["content"] + policy},
                    *messages[1:]])
            resolution = await resolve_typed_reference(phrase, candidates,
                complete=complete, query=query_stop, history=history)

        if resolution.status == "unique":
            return await self._stop_selected_task(
                ingress, turn_id, frozen, admission, resolution.candidate, targets)
        if resolution.status != "ambiguous":
            loop._effects.accept_no_effect(admission, reason="task_stop_target_" + resolution.status)
            return await self._present_task_stop(ingress, turn_id, frozen, admission,
                {"state":"rejected", "reason":"task_stop_target_" + resolution.status})
        choices = {opaque_option_id():candidate for candidate in resolution.candidates}

        async def continue_once(option_id):
            if option_id not in choices:
                raise LoopConflict("task stop option is not part of this request")
            return await self._stop_selected_task(
                ingress, turn_id, frozen, admission, choices[option_id], targets)

        request = await self.attention.create_selection(session_id=ingress.session_id,
            title="Choose the task to stop", prompt="Which of these tasks should stop?",
            options=[AttentionOption(option_id=key, label=candidate.label,
                description=candidate.execution) for key, candidate in choices.items()],
            continuation=continue_once, dedupe_key="cooperative_task_stop")
        return await self._present_task_stop(ingress, turn_id, frozen, admission,
            {"state":"task_stop_selection_required", "attention_request_id":request["id"]})

    def _provider_stop_status(self, session_id, target):
        """Re-read only the selected immutable start identity and its current outcome."""
        with self.ledger._lock:
            row = self.ledger._db.execute("""SELECT e.*, a.source_scope, r.receipt_json
                FROM control_effect_outbox e JOIN control_admissions a ON a.root_id=e.root_id
                LEFT JOIN control_effect_receipts r ON r.effect_id=e.effect_id
                WHERE e.effect_id=?""", (target["effect_id"],)).fetchone()
        if row is None or row["kind"] != "provider" or row["source_scope"] != "chat:" + session_id:
            return "unknown"
        payload = json.loads(row["payload_json"])
        if any(payload.get(key) != value for key, value in {
                "operation":"start", "session_id":session_id,
                "context_id":target["child_id"], "provider":target["provider"]}.items()):
            return "unknown"
        if not target["run_id"] and str(row["external_id"] or "").startswith("not-started:"):
            return "not_started"
        if row["external_id"] != target["run_id"]:
            return "unknown"
        details = json.loads(row["receipt_json"] or "{}").get("details") or {}
        terminal = details.get("provider_status") or details.get("status")
        if terminal in {"done", "error", "cancelled"}:
            return terminal
        record = self.runtime.get_run(target["run_id"])
        return record.status if record is not None and record.provider == target["provider"] else "unknown"

    async def _stop_named_work(self, session_id, target):
        store = self.work_executor.coordinator.store
        selected = store.get_attempt(target["attempt_id"]) if target["attempt_id"] else None
        if selected is None:
            return {"state":"not_active", "reason":"work_run_not_active", **target}
        if (selected.work_item_id, selected.provider_run_id, selected.origin_effect_id) != (
                target["work_item_id"], target["run_id"], target["effect_id"]):
            return {"state":"unknown", "reason":"work_execution_changed", **target}
        target = {**target, "status":selected.execution_status}
        if str(selected.metadata.get("session_id") or "") != session_id:
            return {"state":"rejected", "reason":"work_stop_source_scope_mismatch", **target}
        latest = store.latest_attempt(target["work_item_id"])
        if latest is not None and latest.attempt_id != selected.attempt_id:
            predecessor = store.get_recovery_predecessor(latest)
            if predecessor is not None and predecessor.attempt_id == selected.attempt_id:
                if str(latest.metadata.get("session_id") or "") != session_id:
                    return {"state":"rejected", "reason":"work_stop_source_scope_mismatch", **target}
                selected = latest
                target.update(attempt_id=latest.attempt_id, run_id=latest.provider_run_id,
                    effect_id=latest.origin_effect_id, status=latest.execution_status)
        if selected.execution_status in {"succeeded", "failed", "cancelled"}:
            if self.work_executor.coordinator.cancel_pending_provider_recovery(selected.attempt_id):
                return {"state":"stopped", "reason":"work_continuation_cancelled", **target}
            return {"state":"not_active", **target}
        if latest is None:
            return {"state":"not_active", "reason":"work_run_not_active", **target}
        if (latest.attempt_id, latest.provider_run_id) != (target["attempt_id"], target["run_id"]):
            return {"state":"unknown", "reason":"work_execution_changed", **target}
        record = self.runtime.get_run(target["run_id"])
        if record is None or record.status == "orphaned":
            return {"state":"unknown", "reason":"work_runtime_owner_unavailable", **target}
        if record.status in {"done", "error", "cancelled"}:
            if self.work_executor.coordinator.cancel_pending_provider_recovery(selected.attempt_id):
                return {"state":"stopped", "reason":"work_continuation_cancelled", **target}
            return {"state":"not_active", **target}
        return await self._cancel_work_run(target)

    async def _handle_work_interrupt(self, ingress, turn_id: str,
                                     receipt: dict) -> dict:
        context_id = str(receipt.get("child_id") or "")
        result = await self._stop_active_work(
            ingress.session_id, context_id, receipt)
        result.update({"input_id":str(receipt.get("input_id") or ""),
            "turn_id":turn_id, "child_id":context_id})
        ingress.receipts[result["input_id"]] = result
        await ingress.loop._deliver(str(receipt.get("coordination_say") or ""),
            cause=turn_id)
        return result

    async def _stop_active_work(self, session_id: str, context_id: str,
                                expected: dict) -> dict:
        active = self.active_work_for_recipient(session_id, context_id,
            work_item_id=str(expected.get("work_item_id") or ""))
        if active is None or any(active.get(key) != expected.get(key)
                for key in ("effect_id", "work_item_id", "attempt_id", "run_id")):
            if all(expected.get(key) for key in ("work_item_id", "attempt_id", "run_id")):
                return await self._stop_named_work(session_id, {"effect_id":"", **expected})
            return {"state":"not_active", "reason":"work_run_not_active"}
        if active.get("runtime_attached") is not True:
            return {"state":"unknown",
                "reason":str(active.get("reason") or "work_runtime_owner_unavailable"),
                **active, "provider_status":str(active.get("status") or "unknown")}
        return await self._cancel_work_run(active)

    async def _cancel_work_run(self, active):
        outcome = await self.runtime.cancel(active["run_id"])
        record = self.runtime.get_run(active["run_id"])
        provider_status = record.status if record is not None else "unknown"
        terminal = provider_status in {"done", "error", "cancelled"}
        return {"state":"stopped" if terminal else "unknown",
            "reason":str(outcome.get("reason") or "") if isinstance(outcome, dict) else "",
            **active,
            "status":provider_status, "provider_status":provider_status}

    async def _finish_work(self, owner_key: str, dispatch: WorkEffectDispatch,
                           *, session_id: str) -> None:
        try:
            result = await self.work_executor.finish(dispatch)
        except Exception as exc:
            result = {"status":"error",
                "error":type(exc).__name__ + ": " + str(exc)}
        ingress = self.ingresses.get(session_id)
        if ingress is None:
            ingress = next((candidate for candidate in self._closing_ingresses
                if candidate.session_id == session_id), None)
        if ingress is not None:
            ingress.loop.trace.append({"kind":"work_finished",
                "effect_id":dispatch.effect_id, "result":result})
        if self._work_dispatches.get(owner_key) is dispatch:
            self._work_dispatches.pop(owner_key, None)

    def prepare_runtime_request(self, request, run_id, intake_authority=None):
        """Route one cooperative slot through the Runtime's single Host intake owner."""
        context_id = str(request.metadata.get("cooperative_context_id") or "")
        owners = [ingress.loop for ingress in self.ingresses.values()
            if ingress.loop.has_context(context_id)]
        if len(owners) != 1:
            raise ProviderStartAdmissionRejected("cooperative_context_unavailable")
        loop = owners[0]
        return loop.prepare_runtime_run(request, run_id, intake_authority)

    async def _handle_provider_event(self, _method: str, params: dict) -> None:
        event_type = str(params.get("type") or "").lower()
        if self.permission_policy not in {"deny", "ask"} or event_type not in {
            "permission.requested", "permission.required", "permission.expired",
        }:
            return
        metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
        context_id = str(metadata.get("cooperative_context_id") or "")
        run_id = str(params.get("run_id") or "")
        provider = str(params.get("provider") or "").strip().lower()
        owners = [(session_id, ingress, ingress.loop.get_context(context_id))
            for session_id, ingress in self.ingresses.items()
            if ingress.loop.has_context(context_id)]
        if len(owners) != 1:
            return
        session_id, _ingress, child = owners[0]
        if (child is None or child.provider != provider or child.run_id != run_id
                or metadata.get("session_id") != session_id):
            return
        payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
        if event_type == "permission.expired":
            provider_request_id = str(
                payload.get("request_id") or payload.get("requestId") or ""
            ).strip()[:240]
            matches = [permission for permission in
                self.permission_store.list_cooperative_permission_requests(
                    session_id, context_id=context_id,
                    provider_run_id=run_id, status="pending")
                if permission.metadata.get("provider_request_id") == provider_request_id]
            if len(matches) != 1:
                return
            try:
                resolved = self.permission_store.resolve_permission_request(
                    matches[0].request_id, "expired",
                    metadata={"resolution":"provider_expired",
                        "reason":str(payload.get("reason") or "")})
            except WorkLedgerConflict:
                return
            self.permission_receipts.append({"session_id":session_id,
                "context_id":context_id, "run_id":run_id,
                "provider_request_id":provider_request_id,
                "permission_request_id":resolved.request_id,
                "state":"expired", "accepted":True, "reason":"provider_expired"})
            await self._emit_cooperative_permission_canvas(resolved, visible=False)
            await self._present_current_cooperative_permission()
            return
        source = next((payload[key] for key in
            ("permissionRequest", "permission_request", "request", "permission")
            if isinstance(payload.get(key), dict)), payload)
        request_id = str(
            source.get("request_id") or source.get("requestId") or ""
        ).strip()[:240]
        if not request_id:
            return
        scope = source.get("scope")
        candidates = (list(scope.values()) if isinstance(scope, dict)
            else scope if isinstance(scope, list) else [] if scope in (None, "")
            else [scope])
        scope_paths = []
        for value in candidates:
            if isinstance(value, dict):
                value = value.get("path") or value.get("target") or value.get("value")
            clean = str(value or "").strip()
            if clean:
                scope_paths.append(clean[:2048])
            if len(scope_paths) == 32:
                break
        provider_options = source.get("options")
        provider_options = (list(provider_options) if isinstance(provider_options, list)
            else [provider_options] if provider_options not in (None, "") else [])
        normalized_options = []
        for value in provider_options[:8]:
            option = str(value.get("kind") if isinstance(value, dict) else value
                or "").strip().lower()
            option = {"allow":"allow_once", "approve_once":"allow_once",
                "reject":"deny"}.get(option, option)
            if option in {"allow_once", "deny"} and option not in normalized_options:
                normalized_options.append(option)
        if "deny" not in normalized_options:
            normalized_options.append("deny")
        ask_user = (self.permission_policy == "ask"
            and child.requirements.workspace_access != "read")
        permission = self.permission_store.create_cooperative_permission_request(
            session_id=session_id, context_id=context_id, provider_run_id=run_id,
            capability=str(source.get("capability") or "tool.execute")[:120],
            action=str(source.get("action") or "invoke_tool")[:120],
            scope_paths=scope_paths,
            reason=str(source.get("reason") or "Explicit user approval is required.")[:1000],
            reversibility=str(source.get("reversibility") or "unknown")[:240],
            options=normalized_options if ask_user else ["deny"],
            idempotency_key=f"provider:{provider}:{run_id}:{request_id}",
            metadata={"kind":"provider_permission", "provider":provider,
                "provider_request_id":request_id,
                "provider_options":[str(value)[:80] for value in provider_options[:8]],
                "resolution_policy":"user_prompt" if ask_user else "automatic_deny"})
        if permission.status != "pending":
            self.permission_receipts.append({"session_id":session_id,
                "context_id":context_id, "run_id":run_id,
                "provider_request_id":request_id,
                "permission_request_id":permission.request_id,
                "accepted":permission.status == "denied", "replayed":True,
                "reason":"already_" + permission.status})
            return
        if ask_user:
            self.permission_receipts.append({"session_id":session_id,
                "context_id":context_id, "run_id":run_id,
                "provider_request_id":request_id,
                "permission_request_id":permission.request_id,
                "state":"pending", "accepted":False, "reason":"user_decision_required"})
            if sm.get_current_session_id() == session_id:
                await self._emit_cooperative_permission_canvas(permission, visible=True)
            return
        outcome = await self.runtime.resolve_permission(run_id, ProviderPermissionResponse(
            request_id=request_id, allow=False, automatic=True,
            reason=("cooperative_read_only_context"
                if child.requirements.workspace_access == "read"
                else "cooperative_permission_policy_deny")))
        if outcome.get("accepted") is True:
            permission = self.permission_store.resolve_permission_request(
                permission.request_id, "denied",
                metadata={"resolution":"policy_denied",
                    "resolved_automatically":True})
        else:
            permission = self.permission_store.resolve_permission_request(
                permission.request_id, "expired",
                metadata={"resolution":"provider_permission_unavailable",
                    "reason":str(outcome.get("reason") or "runtime_rejected")})
        self.permission_receipts.append({"session_id":session_id, "context_id":context_id,
            "run_id":run_id, "provider_request_id":request_id,
            "permission_request_id":permission.request_id,
            "capability":str(source.get("capability") or ""),
            "action":str(source.get("action") or ""),
            "accepted":outcome.get("accepted") is True,
            "reason":str(outcome.get("reason") or "")})

    async def _emit_cooperative_permission_canvas(
        self,
        permission,
        *,
        visible: bool,
    ) -> None:
        """Project one Host-owned cooperative permission onto the shared Slice card."""

        provider_request_id = str(
            permission.metadata.get("provider_request_id") or ""
        )
        request = {
            "id": permission.request_id,
            "ownerKind": "cooperative_run",
            "sessionId": permission.session_id,
            "runId": permission.provider_run_id,
            "providerRequestId": provider_request_id,
            "capability": permission.capability,
            "action": permission.action,
            "scope": list(permission.scope_paths),
            "reason": permission.reason,
            "reversibility": permission.reversibility,
            "status": permission.status,
            "options": list(permission.options),
        }
        signal = work_signal(label="permission", text=permission.reason,
            detail="; ".join(permission.scope_paths[:2]), kind="permission",
            importance="blocking", ref=permission.request_id)
        identity = {"owner_kind":"cooperative_run",
            "permission_request_id":permission.request_id,
            "session_id":permission.session_id,
            "cooperative_context_id":permission.context_id,
            "provider_run_id":permission.provider_run_id}
        canvas = (canvas_payload(mode="permission", phase="Checkpoint",
            title="Provider permission required", lead=permission.reason,
            signals=[signal], size_preset="compact", open=True,
            metadata=identity) if visible else {"metadata": identity})
        canvas["permissionVisible"] = visible
        canvas["permissionRequest"] = request
        await bus.emit(Method.WALLPAPER_CANVAS, canvas)

    async def _handle_permission_session_changed(
        self,
        _method: str,
        _params: dict,
    ) -> None:
        """Show only the canonical pending permission for the foreground Session."""

        if self.permission_store is None:
            return
        current_session_id = str(sm.get_current_session_id() or "")
        for session_id in tuple(self.ingresses):
            pending = self.permission_store.list_cooperative_permission_requests(
                session_id, status="pending")
            if session_id != current_session_id:
                for permission in pending:
                    await self._emit_cooperative_permission_canvas(
                        permission, visible=False)
        await self._present_current_cooperative_permission()

    async def _present_current_cooperative_permission(self) -> None:
        if self.permission_store is None:
            return
        session_id = str(sm.get_current_session_id() or "")
        if not session_id or session_id not in self.ingresses:
            return
        pending = self.permission_store.list_cooperative_permission_requests(
            session_id, status="pending")
        if pending:
            await self._emit_cooperative_permission_canvas(
                pending[-1], visible=True)

    async def resolve_permission(self, params: dict) -> dict:
        """Resolve one exact active cooperative permission without model routing."""

        if self.permission_policy != "ask" or self.permission_store is None:
            return {"ok":False, "error":"cooperative_permission_interaction_unavailable"}
        session_id = str(params.get("session_id") or "").strip()
        run_id = str(params.get("run_id") or "").strip()
        provider_request_id = str(params.get("provider_request_id") or "").strip()
        allow = params.get("allow")
        if not session_id or not run_id or not provider_request_id:
            return {"ok":False, "error":"cooperative_permission_identity_incomplete"}
        if not isinstance(allow, bool):
            return {"ok":False, "error":"cooperative_permission_decision_invalid"}
        if sm.get_current_session_id() != session_id:
            return {"ok":False, "error":"cooperative_permission_session_not_current"}
        ingress = self.ingresses.get(session_id)
        if ingress is None:
            return {"ok":False, "error":"cooperative_permission_session_unavailable"}
        matches = [permission for permission in
            self.permission_store.list_cooperative_permission_requests(
                session_id, provider_run_id=run_id, status="pending")
            if permission.metadata.get("provider_request_id") == provider_request_id]
        if len(matches) != 1:
            return {"ok":False, "error":"cooperative_permission_not_pending"}
        permission = matches[0]
        permission_request_id = str(
            params.get("permission_request_id") or ""
        ).strip()
        if (permission_request_id
                and permission.request_id != permission_request_id):
            return {"ok":False,
                "error":"cooperative_permission_request_mismatch"}
        child = ingress.loop.get_context(permission.context_id)
        record = self.runtime.get_run(run_id)
        metadata = record.metadata if record is not None else {}
        if (child is None or child.closed or child.run_id != run_id
                or record is None or record.status not in {"queued", "running"}
                or metadata.get("session_id") != session_id
                or metadata.get("cooperative_context_id") != child.child_id):
            return {"ok":False, "error":"cooperative_permission_run_not_current"}
        if allow and child.requirements.workspace_access == "read":
            return {"ok":False, "error":"cooperative_permission_read_only_context"}
        if allow and "allow_once" not in permission.options:
            return {"ok":False, "error":"cooperative_permission_option_not_allowed"}
        outcome = await self.runtime.resolve_permission(run_id,
            ProviderPermissionResponse(request_id=provider_request_id, allow=allow))
        if outcome.get("accepted") is not True:
            return {"ok":False,
                "error":str(outcome.get("reason") or "provider_permission_rejected")}
        resolved = self.permission_store.resolve_permission_request(
            permission.request_id, "allowed" if allow else "denied",
            metadata={"resolution":"user_allowed" if allow else "user_denied"})
        self.permission_receipts.append({"session_id":session_id,
            "context_id":child.child_id, "run_id":run_id,
            "provider_request_id":provider_request_id,
            "permission_request_id":resolved.request_id,
            "state":resolved.status, "accepted":True, "reason":"user_decision"})
        await self._emit_cooperative_permission_canvas(resolved, visible=False)
        await self._present_current_cooperative_permission()
        return {"ok":True, "permission":resolved.to_dict()}

    async def checkpoint_native_session(self, run_id, session) -> None:
        record = self.runtime.get_run(run_id)
        context_id = str(record.metadata.get("cooperative_context_id") or "") if record else ""
        if not context_id:
            # The shared Runtime's established Work path had no awaited native
            # checkpoint. Installing this cooperative multiplexer must not turn
            # unrelated Work sessions into cooperative contexts or new gates.
            return
        owners = [ingress.loop for ingress in self.ingresses.values()
            if ingress.loop.has_context(context_id)]
        if len(owners) != 1:
            raise ProviderStartAdmissionRejected("cooperative_context_unavailable")
        await owners[0]._checkpoint_native_session(run_id, session)

    async def close(self) -> None:
        try:
            await self.begin_close()
        finally:
            await self.finish_close()

    async def begin_close(self) -> None:
        if self._close_started:
            return
        self._close_started = True
        self._closed = True
        if self.permission_policy in {"deny", "ask"} and self._installed:
            bus.off(Method.PROVIDER_EVENT, self._handle_provider_event)
        if self.permission_policy == "ask" and self._installed:
            bus.off(Method.SESSION_CHANGED, self._handle_permission_session_changed)
        if sm._activation_guard == self._activation_guard:
            sm.configure_activation_guard(None)
        ingresses, self.ingresses = tuple(self.ingresses.values()), {}
        self._closing_ingresses = ingresses
        outcomes = await asyncio.gather(*(ingress.begin_close() for ingress in ingresses),
            return_exceptions=True)
        failure = next((outcome for outcome in outcomes if isinstance(outcome, BaseException)), None)
        if failure is not None:
            raise failure

    async def finish_close(self) -> None:
        if self._close_finished:
            return
        if not self._close_started:
            await self.begin_close()
        outcomes = await asyncio.gather(*(ingress.finish_close()
            for ingress in self._closing_ingresses), return_exceptions=True)
        if self._work_tasks:
            outcomes = [*outcomes, *(await asyncio.gather(
                *tuple(self._work_tasks), return_exceptions=True))]
        self._close_finished = True
        failure = next((outcome for outcome in outcomes if isinstance(outcome, BaseException)), None)
        if failure is not None:
            raise failure
