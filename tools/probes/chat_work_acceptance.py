"""Isolated admitted-Chat → proposal → v3 Work assembly; not app bootstrap."""
from __future__ import annotations

from copy import deepcopy

from agent_host.provider_contract import select_provider
from core.chat_runtime import _bounded_delegate_source_context
from llm.prompts import finalize_system_prompt_language, get_system_prompt
from llm.stream_parser import StreamTagParser
from server.control_ledger import ControlLedgerConflict
from server.control_proposal import seal_control_proposals
from server.control_adjudication import RuntimeControlDecisionResolver
from server.provider_event_ingestion import ProviderEventIngestor
from server.provider_requirements import DelegateRequirementFacts, compile_delegate_requirements
from server.task_lookup import pre_turn_resolve, set_turn_resolution
from server.turn_admission import admission_transcript_hash
from server.work_context import augment_system_prompt_with_active_provider_context
from server.work_control import CurrentTurnSourceSpanV1, WorkEffectPayloadV3, WorkAmendPayloadV4, WorkContextPayloadV5
from tools.probes.probe_whole_turn_control import whole_turn_owner
from dataclasses import asdict
from server import compound_control as compound, control_decision

def describe(plan):
    return {"status": plan.status, "reason": plan.reason,
            "operations": [{"control": compound.operation_control_view(op),
                "targets": (None if op.action.get(control_decision.CONTROL_REFERENCE_CANDIDATES_ATTR) is None
                            else [candidate.token for candidate in op.action[control_decision.CONTROL_REFERENCE_CANDIDATES_ATTR]]),
                "source_clause": op.source_clause, "task": op.action.get("task"),
                "workspace_access": op.action.get("_host_workspace_access"),
                "payload_source": op.action.get("_host_payload_source")} for op in plan.operations],
            "decision_queries": plan.decision_queries, "candidate_queries": plan.candidate_verdict_queries,
            "clauses": [asdict(clause) for clause in plan.clauses]}




class AcceptedWorkChatRunner:
    """Connect existing owners for a registered-Project, new-Work experiment.

    The caller owns isolation, query transport, Session/history setup and teardown.
    Other effects/refusals cannot fall through to a legacy dispatcher. This is not
    a general foreground runtime, a scope classifier, or a production rollout mode.
    """

    def __init__(self, control, executor, coordinator, query, *, fence_scope, record_turn):
        self.control, self.executor, self.coordinator = control, executor, coordinator
        self.query, self.fence_scope, self.record_turn = query, fence_scope, record_turn
        self.observations = []
        # Experimental caller-owned explicit recipient, frozen before awaits.
        # It is independent of the model's new/existing Work decision.
        self.recipient_attempt_id = ""

    async def __call__(self, text, *, turn_admission, history_snapshot, **_kwargs):
        admission = turn_admission
        recipient_attempt_id = self.recipient_attempt_id
        if admission_transcript_hash(text) != admission.transcript_hash:
            raise ControlLedgerConflict("runner text differs from admitted source")
        self.control.admit(admission, fence_scope=self.fence_scope)
        history = deepcopy(history_snapshot.dialog)
        record = {"root_id": admission.root_id, "turn_id": admission.turn_id,
                  "chat_epoch": admission.chat_epoch, "source": text, "history": history,
                  "recipient_attempt_id":recipient_attempt_id}
        self.observations.append(record)
        try:
            set_turn_resolution(None)
            record["pre_turn_lookup"] = await pre_turn_resolve(admission.session_id, text)
            prompt = finalize_system_prompt_language(augment_system_prompt_with_active_provider_context(
                get_system_prompt("with_delegate"), session_id=admission.session_id))
            response = await self.query("role", [{"role":"system", "content":prompt},
                *history, {"role":"user", "content":text}])
            record["role_reply"] = response
            _clean, actions = StreamTagParser().process_chunk(response)
            proposals = [a for a in actions if a.get("type") == "DELEGATE"]
            record["proposal_count"] = len(proposals)
            if not proposals:
                return self._no_effect(admission, text, record, "no_sealed_work_proposal")
            batch = seal_control_proposals(proposals, turn_id=admission.turn_id,
                session_id=admission.session_id, user_text=text, transport="inline_tag", prior_messages=history)
            record["proposals"] = [dict(p) for p in batch.proposals]

            async def query(messages):
                return await self.query("control", messages)

            context = RuntimeControlDecisionResolver(coordinator=self.coordinator, query=query).capture_context(batch)
            plan = await whole_turn_owner(context.messages, batch.decision_payloads(), context.candidates,
                complete=context.catalog_complete, query=query, provider_ids=context.provider_ids,
                candidate_limit=context.exhaustive_candidate_limit, proposal_controls=batch.proposals)
            record["plan_status"] = plan.status
            record["operation_count"] = len(plan.operations)
            record["plan"] = describe(plan)
            if plan.status != "ok":
                raise ControlLedgerConflict("Work planning unavailable or invalid: " + plan.reason)
            if not plan.operations:
                return self._no_effect(admission, text, record, "work_proposal_suppressed")
            if len(plan.operations) != 1:
                raise ControlLedgerConflict("outside single-new-Work experiment scope")
            op = plan.operations[0]
            attrs = op.action
            if attrs.get("intent") not in {"execute", "amend"} or attrs.get("_host_workspace_access") not in {"read", "write"}:
                raise ControlLedgerConflict("outside current-source new-Work experiment scope")
            if any(attrs.get(key) for key in ("focus", "target", "branch", "action", "fallback", "cwd")):
                raise ControlLedgerConflict("additional effect or destination outside experiment scope")
            if attrs.get("task") != op.source_clause or attrs.get("_host_control_payload_grounding"):
                raise ControlLedgerConflict("new v3 Work requires the current source payload")
            refs = attrs.get("_host_control_reference_candidates")
            target_work = None
            if attrs["intent"] == "amend":
                if attrs.get("one_off"):
                    raise ControlLedgerConflict("existing Work cannot also allocate a new Draft")
                if refs is None or len(refs) != 1 or refs[0].kind != "work_item":
                    raise ControlLedgerConflict("accepted amend needs one bound Work identity")
                target_work = self.coordinator.store.get_work_item(refs[0].entity_id)
                if target_work is None:
                    raise ControlLedgerConflict("accepted amend target is no longer available")
                project_id = target_work.project_id
            elif refs is None:
                route = self.coordinator.resolve_workspace_route({"session_id":admission.session_id,
                    "one_off":bool(attrs.get("one_off"))})
                if route.get("status") != "resolved" or not route.get("projectId"):
                    raise ControlLedgerConflict("new Work destination is unavailable")
                project_id = route["projectId"]
            elif len(refs) == 1 and refs[0].kind == "project" and not attrs.get("one_off"):
                project_id = refs[0].entity_id
            else:
                raise ControlLedgerConflict("existing, absent or ambiguous target is outside new-Work scope")
            if not project_id:
                raise ControlLedgerConflict("experiment requires a registered Project destination")
            clause = next(c for c in plan.clauses if c.text == op.source_clause)
            requirements = compile_delegate_requirements(DelegateRequirementFacts.from_delegate(
                attrs, task_requests_workspace_mutation=attrs["_host_workspace_access"] == "write",
                required_workspace_access=attrs["_host_workspace_access"]))
            selection = select_provider(requirements, self.executor.runtime.provider_manifests())
            payload_type = WorkAmendPayloadV4 if target_work is not None else WorkEffectPayloadV3
            if recipient_attempt_id:
                payload_type = WorkContextPayloadV5
            payload = payload_type(provider=selection.provider_id, task=op.source_clause,
                title=ProviderEventIngestor.task_title(op.source_clause), project_id=project_id,
                session_id=admission.session_id, utterance_id=admission.utterance_id, turn_id=admission.turn_id,
                source_user_text=text, source_user_context=_bounded_delegate_source_context(history, current_user=text),
                source_context_scope=admission.dialogue_source_scope,
                source_proof=CurrentTurnSourceSpanV1.capture(admission, text, start=clause.start, end=clause.end),
                requirements=requirements,
                **({"context_attempt_id":recipient_attempt_id} if recipient_attempt_id else {}),
                **({"work_item_id":target_work.work_item_id} if target_work is not None else {}))
            accepted = self.control.seal(admission, payload)
            record["accepted"] = accepted
            result = await self.executor.execute(accepted["effect_id"])
            record["execution"] = result
            # This isolated runner returns receipt facts, not unaccepted role promises.
            message = "Work disposition: " + str(result["status"])
            if result.get("receipt"):
                message += "/" + str(result["receipt"]["outcome"])
            self.record_turn(session_id=admission.session_id, user_text=text, assistant_text=message,
                             turn_id=admission.turn_id)
            return message
        except BaseException as exc:
            record["error"] = type(exc).__name__ + ": " + str(exc)
            raise

    def _no_effect(self, admission, text, record, reason):
        record["accepted"] = self.control.ledger.accept(admission.root_id, chat_epoch=admission.chat_epoch,
            plan_id="chat-work-none:" + admission.root_id, effects=(),
            evidence={"adapter":"admitted-chat-work-probe", "reason":reason})
        message = "No Work effect accepted."
        self.record_turn(session_id=admission.session_id, user_text=text, assistant_text=message,
                         turn_id=admission.turn_id)
        return message
