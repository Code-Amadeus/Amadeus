"""Branch-aware Browser provider adapter.

ProviderRuntime remains the single execution spine. This adapter is registered
as the normal ``browser`` provider and wraps the Playwright-backed BrowserAdapter
only when a run asks for branch semantics.

- ProviderRuntime owns run ids, cancellation, event bus, canvas updates, and
  ProviderRunResult records.
- BrowserAdapter owns Playwright sessions and low-level page actions.
- ProviderBranchStore owns raw DOM/tool trace storage and branch merge metadata.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_host.adapters.browser import BrowserAdapter
from agent_host.browser_engine import BrowserExecutionEngine
from agent_host.browser_interaction_policy import BrowserBranchPolicyDecision, BrowserInteractionPolicy
from agent_host.browser_outcome import (
    observed_submit_expected_state as _observed_submit_expected_state,
    structured_action_target as _structured_action_target,
)
from agent_host.provider_catalog import BROWSER_MANIFEST
from agent_host.provider_outcome import ProviderOutcomeEvidence
from agent_host.provider_identity import with_main_role_reference
from agent_host.provider_types import (
    EmitProviderEvent,
    ProviderEvent,
    ProviderRunRequest,
    ProviderRunResult,
    ProviderSteerRequest,
)
from server.provider_branch import ProviderBranchStore


BranchPlanner = Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]]
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _BrowserSteerControl:
    latest_revision: int = 0
    applied_revision: int = 0
    pending: ProviderSteerRequest | None = None
    accepting: bool = True

    def offer(self, request: ProviderSteerRequest) -> dict[str, Any]:
        if not self.accepting:
            return {"accepted": False, "reason": "steering_window_closed"}
        if request.revision <= self.latest_revision:
            return {"accepted": False, "reason": "stale_revision"}
        self.latest_revision = request.revision
        self.pending = request
        return {
            "accepted": True,
            "revision": request.revision,
            "safe_boundary": "next_atomic_boundary",
        }

    def take_latest(self) -> ProviderSteerRequest | None:
        pending = self.pending
        if pending is None or pending.revision <= self.applied_revision:
            return None
        self.pending = None
        self.applied_revision = pending.revision
        return pending

    def has_newer(self, revision: int) -> bool:
        return bool(self.pending is not None and self.pending.revision > revision)


class BrowserBranchAdapter:
    provider_id = "browser"
    # The branch wrapper preserves the base provider's public capabilities.
    manifest = BrowserAdapter.manifest

    def __init__(
        self,
        *,
        base_adapter: BrowserExecutionEngine | None = None,
        store: ProviderBranchStore | None = None,
        policy: BrowserInteractionPolicy | None = None,
        branch_planner: BranchPlanner | None = None,
    ) -> None:
        self.base = base_adapter or BrowserAdapter()
        self.store = store or ProviderBranchStore(Path("runtime") / "provider_branches")
        self.policy = policy or BrowserInteractionPolicy()
        self.branch_planner = branch_planner or _default_branch_planner
        self._steer_controls: dict[str, _BrowserSteerControl] = {}

    async def run(
        self,
        request: ProviderRunRequest,
        run_id: str,
        emit: EmitProviderEvent,
    ) -> ProviderRunResult:
        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        initial_revision = max(0, int(metadata.get("branch_instruction_revision") or 0))
        control = _BrowserSteerControl(
            latest_revision=initial_revision,
            applied_revision=initial_revision,
        )
        self._steer_controls[run_id] = control
        try:
            policy_decision = self.policy.decide(request)
            if not policy_decision.use_branch:
                direct_result = await self.base.run(request, run_id, emit)
                if not control.has_newer(initial_revision):
                    control.accepting = False
                    return direct_result
                browser_session_id = str(
                    direct_result.metadata.get("browser", {}).get("browser_session_id")
                    or metadata.get("browser_session_id")
                    or metadata.get("browserSessionId")
                    or ""
                ).strip()
                if direct_result.status != "done" or not browser_session_id:
                    control.accepting = False
                    direct_result.metadata["steering"] = {
                        "state": "deferred",
                        "revision": control.latest_revision,
                        "reason": "direct_action_did_not_leave_a_live_session",
                    }
                    return direct_result
                # A direct action is one atomic operation. If the user changes
                # course while it is running, promote the same provider run to
                # a DOM branch afterwards rather than starting a second run on
                # the same Playwright session or dropping the correction.
                promoted_metadata = {
                    **dict(metadata),
                    "provider_branch": True,
                    "browser_session_id": browser_session_id,
                    "browser_action": "observe",
                    "browser_mode": "observe",
                    "branch_promoted_from_direct": True,
                }
                promoted_request = ProviderRunRequest(
                    provider=request.provider,
                    task=request.task,
                    cwd=request.cwd,
                    mode="observe",
                    metadata=promoted_metadata,
                    requirements=request.requirements,
                    ownership=request.ownership,
                )
                policy_decision = self.policy.decide(promoted_request)
                return await self._run_branch(
                    promoted_request,
                    run_id,
                    emit,
                    policy_decision=policy_decision,
                    steer_control=control,
                )
            return await self._run_branch(
                request,
                run_id,
                emit,
                policy_decision=policy_decision,
                steer_control=control,
            )
        finally:
            control.accepting = False
            self._steer_controls.pop(run_id, None)

    async def steer(
        self,
        run_id: str,
        request: ProviderSteerRequest,
    ) -> dict[str, Any]:
        control = self._steer_controls.get(str(run_id or ""))
        if control is None:
            return {"accepted": False, "reason": "run_not_steerable"}
        return control.offer(request)

    async def cancel(self, run_id: str) -> None:
        await self.base.cancel(run_id)

    async def shutdown(self) -> None:
        await self.base.shutdown()

    async def _run_branch(
        self,
        request: ProviderRunRequest,
        run_id: str,
        emit: EmitProviderEvent,
        *,
        policy_decision: BrowserBranchPolicyDecision,
        steer_control: _BrowserSteerControl | None = None,
    ) -> ProviderRunResult:
        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        if steer_control is None:
            initial_revision = max(
                0,
                int(metadata.get("branch_instruction_revision") or 0),
            )
            steer_control = _BrowserSteerControl(
                latest_revision=initial_revision,
                applied_revision=initial_revision,
            )
        session_id = str(metadata.get("session_id") or metadata.get("chat_session_id") or "")
        source_goal = str(metadata.get("source_user_text") or request.task).strip()
        branch = self.store.create_branch(
            parent_session_id=session_id,
            provider=self.provider_id,
            goal=source_goal,
            branch_id=str(metadata.get("branch_id") or run_id),
        )
        branch.add_message(
            role="user",
            content=source_goal,
            visibility="visible",
            source="main_chat_intervention",
        )
        branch.add_message(
            role="system",
            content=json.dumps(policy_decision.to_metadata(), ensure_ascii=False, indent=2),
            visibility="hidden",
            source="browser_interaction_policy",
            metadata={"content_type": "application/json"},
        )

        events: list[dict[str, Any]] = []

        async def branch_emit(event: ProviderEvent) -> None:
            events.append(event.to_dict())
            await emit(event)

        first_request = policy_decision.initial_request
        first_result = await self.base.run(first_request, run_id, branch_emit)
        if first_result.status != "done":
            branch.add_risk({"level": "error", "note": first_result.error or first_result.result})
            self._persist_events(branch, events, label="initial_provider_trace")
            merge = branch.close(
                final_report=f"Browser branch failed to start: {first_result.error or first_result.result}",
                compact_digest="Browser branch failed before it could inspect the page.",
                status="error",
            )
            merge["policy"] = policy_decision.to_metadata()
            return self._merge_result(first_result, merge)

        browser_session_id = str(
            first_result.metadata.get("browser", {}).get("browser_session_id")
            or metadata.get("browser_session_id")
            or metadata.get("browserSessionId")
            or ""
        )
        page_state = await self._capture_hidden_page_state(
            branch,
            browser_session_id,
            label="initial_dom_snapshot",
            include_dom=policy_decision.capture_hidden_dom,
        )
        active_request = request
        active_metadata = dict(metadata)
        active_revision = steer_control.applied_revision
        applied_revisions: list[int] = []
        decision: dict[str, Any] = {}
        actions: list[dict[str, Any]] = []
        final_result: ProviderRunResult = first_result
        expected_state: dict[str, str] = {}
        deferred_actions: list[dict[str, Any]] = []
        latest_state: dict[str, Any] = {}
        snapshot_index = 0
        executed_action_count = 0
        awaiting_goal_confirmation = False

        while True:
            pending_steer = steer_control.take_latest()
            if pending_steer is not None:
                active_revision = pending_steer.revision
                applied_revisions.append(active_revision)
                # The superseding instruction starts from the live page, not
                # from the previous plan's terminal status.
                final_result = first_result
                active_request = self._request_with_steer(
                    request,
                    pending_steer,
                    base_metadata=metadata,
                )
                active_metadata = dict(active_request.metadata)
                branch.add_message(
                    role="user",
                    content=str(
                        active_metadata.get("branch_user_message")
                        or pending_steer.task
                    ),
                    visibility="visible",
                    source="mid_run_steer",
                    metadata={
                        "revision": active_revision,
                        "turn_id": str(active_metadata.get("turn_id") or ""),
                    },
                )
                await branch_emit(
                    ProviderEvent(
                        provider=self.provider_id,
                        run_id=run_id,
                        type="run.status",
                        payload={
                            "status": "running",
                            "stage": "steer_applied",
                            "revision": active_revision,
                            "safe_boundary": "after_atomic_action",
                        },
                        metadata=dict(active_metadata),
                    )
                )
                page_state = await self._capture_hidden_page_state(
                    branch,
                    browser_session_id,
                    label=f"steer_{active_revision}_dom_snapshot",
                    include_dom=policy_decision.capture_hidden_dom,
                )
                branch_control = str(
                    active_metadata.get("branch_control") or ""
                ).strip().lower()
                if branch_control in {"close", "supersede", "stop_plan"}:
                    latest_state = await self.base.inspect_session(
                        browser_session_id,
                        include_dom=False,
                    )
                    decision = {
                        "actions": [],
                        "final_report": (
                            "Stopped the remaining browser plan on: "
                            f"{latest_state.get('title') or latest_state.get('url')}."
                        ),
                        "compact_digest": (
                            "The remaining browser plan was superseded at an atomic "
                            "action boundary; the browser session remains available."
                        ),
                    }
                    actions = []
                    expected_state = {}
                    deferred_actions = []
                    branch.add_risk(
                        {
                            "level": "info",
                            "note": (
                                "The semantic browser branch closed; remaining actions "
                                "were stopped after the current atomic action."
                            ),
                            "instruction_revision": active_revision,
                            "branch_control": branch_control,
                        }
                    )
                    steer_control.accepting = False
                    break

            planner_context = self._planner_context(
                request=active_request,
                metadata=active_metadata,
                session_id=session_id,
                run_id=run_id,
                branch_id=branch.branch_id,
                browser_session_id=browser_session_id,
                page_state=page_state,
                events=events,
                policy_decision=policy_decision,
                instruction_revision=active_revision,
            )
            planned = await _maybe_await(self.branch_planner(planner_context))
            decision = dict(planned) if isinstance(planned, dict) else {}

            # Planning is not an external side effect. If a newer instruction
            # arrived while the model was thinking, discard the stale plan
            # before publishing its prose or issuing its first browser action.
            if steer_control.has_newer(active_revision):
                branch.add_message(
                    role="system",
                    content=json.dumps(decision, ensure_ascii=False, indent=2),
                    visibility="hidden",
                    source="superseded_browser_branch_decision",
                    metadata={
                        "content_type": "application/json",
                        "instruction_revision": active_revision,
                    },
                )
                continue

            assistant_message = str(decision.get("assistant_message") or "").strip()
            if assistant_message:
                branch.add_message(
                    role="assistant",
                    content=assistant_message,
                    visibility="visible",
                    source="browser_branch_llm",
                    metadata={"instruction_revision": active_revision},
                )
            branch.add_message(
                role="system",
                content=json.dumps(decision, ensure_ascii=False, indent=2),
                visibility="hidden",
                source="browser_branch_decision",
                metadata={
                    "content_type": "application/json",
                    "instruction_revision": active_revision,
                },
            )

            remaining_budget = max(
                0,
                policy_decision.max_actions - executed_action_count,
            )
            actions = [
                dict(item)
                for item in list(decision.get("actions") or [])[:remaining_budget]
                if isinstance(item, dict)
            ]
            if not actions and awaiting_goal_confirmation:
                if not bool(decision.get("goal_satisfied")):
                    expected_state = {}
                awaiting_goal_confirmation = False
            if actions:
                expected_state = {}
                awaiting_goal_confirmation = False
            deferred_actions = []
            superseded = False
            replan_after_page_change = False
            for index, action in enumerate(actions, start=1):
                if steer_control.has_newer(active_revision):
                    superseded = True
                    deferred_actions = [dict(item) for item in actions[index - 1 :]]
                    break
                page_state_before_action = page_state
                action_expected_state = _structured_action_target(action, page_state)
                # Only the terminal action may ground the terminal report.
                expected_state = action_expected_state
                recorded_action = {
                    **dict(action),
                    "browser_session_id": browser_session_id,
                    "instruction_revision": active_revision,
                }
                if action_expected_state:
                    recorded_action["expected_state"] = action_expected_state
                branch.add_action(recorded_action)
                action_request = ProviderRunRequest(
                    provider=self.provider_id,
                    task=str(action.get("task") or active_request.task),
                    cwd=active_request.cwd,
                    mode=str(action.get("action") or "observe"),
                    metadata=self._action_metadata(
                        action,
                        active_metadata,
                        browser_session_id,
                    ),
                )
                final_result = await self.base.run(action_request, run_id, branch_emit)
                if final_result.status != "done":
                    branch.add_risk(
                        {
                            "level": "error",
                            "note": final_result.error or final_result.result,
                            "action": dict(action),
                            "instruction_revision": active_revision,
                        }
                    )
                    break
                executed_action_count += 1
                if str(action.get("action") or "").strip().lower() == "back":
                    back_url = str(
                        final_result.metadata.get("browser", {}).get("current_url") or ""
                    ).strip()
                    if back_url:
                        expected_state = {"url": back_url}
                        branch.actions[-1]["expected_state"] = dict(expected_state)
                browser_session_id = str(
                    final_result.metadata.get("browser", {}).get("browser_session_id")
                    or browser_session_id
                )
                snapshot_index += 1
                page_state = await self._capture_hidden_page_state(
                    branch,
                    browser_session_id,
                    label=f"post_action_{snapshot_index}_dom_snapshot",
                    include_dom=policy_decision.capture_hidden_dom,
                )
                observed_submit_state = _observed_submit_expected_state(
                    action,
                    previous_state=page_state_before_action,
                    current_state=page_state,
                )
                if observed_submit_state and not action_expected_state:
                    # A submitted search has no href to predict before the
                    # action.  Certify it only when the host observes a page
                    # transition whose URL/title carries the submitted value.
                    # Otherwise the terminal report remains deliberately
                    # unverifiable and the Observer may not call it complete.
                    expected_state = observed_submit_state
                    branch.actions[-1]["expected_state"] = dict(expected_state)
                remaining = [dict(item) for item in actions[index:]]
                if steer_control.has_newer(active_revision):
                    superseded = True
                    deferred_actions = remaining
                    break
                if remaining and not _remaining_actions_match_page(
                    remaining,
                    previous_state=page_state_before_action,
                    current_state=page_state,
                ):
                    deferred_actions = remaining
                    branch.add_risk(
                        {
                            "level": "info",
                            "note": (
                                "The page changed after a planned action; remaining actions were "
                                "deferred until the new page can be observed and replanned."
                            ),
                            "deferred_action_count": len(deferred_actions),
                            "instruction_revision": active_revision,
                        }
                    )
                    replan_after_page_change = True
                    break

            if superseded:
                branch.add_risk(
                    {
                        "level": "info",
                        "note": (
                            "A newer user instruction superseded the remaining browser plan at "
                            "an atomic action boundary."
                        ),
                        "superseded_revision": active_revision,
                        "deferred_action_count": len(deferred_actions),
                    }
                )
                expected_state = {}
                continue

            if (
                final_result.status == "done"
                and actions
                and executed_action_count < policy_decision.max_actions
                and (
                    replan_after_page_change
                    or str(actions[-1].get("action") or "").strip().lower()
                    == "open"
                )
            ):
                # Navigation mutates the evidence available to the planner.
                # Observe and plan again within the same bounded action budget
                # instead of treating a search/results page as the user's
                # terminal goal.  A no-action decision on the next iteration
                # closes the loop; a stale remainder is discarded, not replayed.
                if replan_after_page_change:
                    deferred_actions = []
                awaiting_goal_confirmation = True
                continue

            latest_state = await self.base.inspect_session(
                browser_session_id,
                include_dom=False,
            )
            if steer_control.has_newer(active_revision):
                expected_state = {}
                continue

            # No await is allowed between sealing this window and producing the
            # merge. A later user instruction is then rejected as a steer and
            # becomes a normal next-turn run instead of racing the terminal
            # truth snapshot.
            steer_control.accepting = False
            break

        self._persist_events(branch, events, label="provider_event_trace")
        if final_result.status != "done":
            detail = _short_text(final_result.error or final_result.result or "unknown browser error", 220)
            final_report = f"ブラウザ操作で止まったわ: {detail}"
            compact_digest = f"Browser branch action failed after {len(branch.actions)} recorded action(s): {detail}"
        elif deferred_actions:
            final_report = (
                "Browser branch stopped after the page changed. Current page: "
                f"{latest_state.get('title') or latest_state.get('url')}."
            )
            compact_digest = (
                f"Browser branch executed {len(branch.actions)} action(s) and deferred "
                f"{len(deferred_actions)} stale-plan action(s) after the page changed."
            )
            # A partial multi-action prediction must not certify its own prose.
            # The interaction coordinator will narrate only terminal host facts.
            expected_state = {}
        else:
            final_report = str(decision.get("final_report") or "").strip()
            if not final_report:
                final_report = f"Browser branch finished on: {latest_state.get('title') or latest_state.get('url')}."
                # This fallback is composed exclusively from the terminal
                # inspect_session result, so the report and expected state
                # share the same host-observed source.
                expected_state = {
                    "url": str(latest_state.get("url") or ""),
                    "title": str(latest_state.get("title") or ""),
                }
            compact_digest = str(decision.get("compact_digest") or "").strip()
            if not compact_digest:
                compact_digest = (
                    f"Browser branch completed {len(branch.actions)} action(s) on "
                    f"{latest_state.get('title') or latest_state.get('url')}."
                )
        merge = branch.close(
            final_report=final_report,
            compact_digest=compact_digest,
            status="done" if final_result.status == "done" else final_result.status,
            next_state={
                "browser_session_id": browser_session_id,
                "current_url": latest_state.get("url") or "",
                "page_title": latest_state.get("title") or "",
                "expected_state": {
                    key: value for key, value in expected_state.items() if value
                },
                "requires_observe_for_next_action": True,
                "instruction_revision": active_revision,
                "applied_steer_revisions": list(applied_revisions),
            },
        )
        merge["policy"] = policy_decision.to_metadata()
        operation_id = _semantic_operation(active_request)
        operation = BROWSER_MANIFEST.capabilities.operation(operation_id)
        if operation is not None and operation.outcome_facet:
            merge["operation"] = operation_id
            merge["outcome_evidence"] = ProviderOutcomeEvidence(
                facet=operation.outcome_facet,
                operation=operation_id,
                expected={key: value for key, value in expected_state.items() if value},
                observed={
                    "url": str(latest_state.get("url") or ""),
                    "title": str(latest_state.get("title") or ""),
                    "text": str(latest_state.get("text") or "")[:2000],
                },
            ).to_dict()
        merged = self._merge_result(final_result, merge)
        merged.metadata["steering"] = {
            "state": "applied" if applied_revisions else "not_used",
            "revision": active_revision,
            "applied_revisions": list(applied_revisions),
            "safe_boundary": "after_atomic_action",
        }
        return merged

    @staticmethod
    def _request_with_steer(
        original: ProviderRunRequest,
        steer: ProviderSteerRequest,
        *,
        base_metadata: dict[str, Any],
    ) -> ProviderRunRequest:
        return ProviderRunRequest(
            provider=original.provider,
            task=steer.task,
            cwd=original.cwd,
            mode=original.mode,
            metadata={
                **dict(base_metadata),
                **dict(steer.metadata),
                "branch_instruction_revision": steer.revision,
            },
            requirements=original.requirements,
            ownership=original.ownership,
        )

    @staticmethod
    def _planner_context(
        *,
        request: ProviderRunRequest,
        metadata: dict[str, Any],
        session_id: str,
        run_id: str,
        branch_id: str,
        browser_session_id: str,
        page_state: dict[str, Any],
        events: list[dict[str, Any]],
        policy_decision: BrowserBranchPolicyDecision,
        instruction_revision: int,
    ) -> dict[str, Any]:
        return {
            "parent_session_id": session_id,
            "branch_id": branch_id,
            "provider_run_id": run_id,
            "user_message": with_main_role_reference(
                request.task,
                metadata=metadata,
                execution_provider=request.provider,
            ),
            # Historical URLs and summaries are context. Only this separate
            # field authorizes the next action scope.
            "latest_user_instruction": str(
                metadata.get("branch_user_message")
                or metadata.get("source_user_text")
                or request.task
            ).strip(),
            "instruction_revision": instruction_revision,
            "browser_session_id": browser_session_id,
            "page": _planner_page_state(page_state),
            "conversation_checkpoint": metadata.get("conversation_checkpoint")
            if isinstance(metadata.get("conversation_checkpoint"), dict)
            else {},
            "branch_transcript": metadata.get("branch_visible_messages")
            if isinstance(metadata.get("branch_visible_messages"), list)
            else [],
            "branch_hidden_summary": str(metadata.get("branch_hidden_summary") or ""),
            "hidden_context": {
                "dom": page_state.get("dom") or "",
                "text": page_state.get("text") or "",
            },
            "interaction_refs": page_state.get("interaction_refs") or [],
            "events": events[-24:],
            "request_metadata": dict(metadata),
            "policy": policy_decision.to_metadata(),
        }

    @staticmethod
    def _action_metadata(action: dict[str, Any], parent_metadata: dict[str, Any], browser_session_id: str) -> dict[str, Any]:
        action_name = str(action.get("action") or "observe").strip().lower()
        metadata: dict[str, Any] = {
            **dict(parent_metadata),
            "source": "browser_branch",
            "browser_session_id": browser_session_id,
            "browser_action": action_name,
            "browser_mode": action_name,
        }
        for key in (
            "url",
            "query",
            "text",
            "label",
            "selector_text",
            "ref",
            "action_ref",
            "target_ref",
            "value",
            "input",
            "submit",
        ):
            value = action.get(key)
            if value not in (None, ""):
                metadata[key] = value
        return metadata

    async def _capture_hidden_page_state(
        self,
        branch,
        browser_session_id: str,
        *,
        label: str,
        include_dom: bool,
    ) -> dict[str, Any]:
        state = await self.base.inspect_session(browser_session_id, include_dom=include_dom)
        branch.add_message(
            role="system",
            content=str(state.get("dom") or ""),
            visibility="hidden",
            source=label,
            metadata={
                "content_type": "text/html",
                "url": state.get("url") or "",
                "title": state.get("title") or "",
                "interaction_ref_count": len(state.get("interaction_refs") or []),
            },
        )
        refs_payload = {
            "browser_session_id": browser_session_id,
            "url": state.get("url") or "",
            "title": state.get("title") or "",
            "interaction_refs": state.get("interaction_refs") or [],
        }
        branch.add_message(
            role="tool",
            content=json.dumps(refs_payload, ensure_ascii=False, indent=2),
            visibility="hidden",
            source=f"{label}_interaction_refs",
            metadata={"content_type": "application/json"},
        )
        return state

    def _persist_events(self, branch, events: list[dict[str, Any]], *, label: str) -> None:
        if not events:
            return
        branch.add_message(
            role="tool",
            content=json.dumps(events, ensure_ascii=False, indent=2),
            visibility="hidden",
            source=label,
            metadata={"content_type": "application/json", "event_count": len(events)},
        )
        for event in events:
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if event.get("type") == "artifact.created":
                branch.add_artifact(dict(payload))

    @staticmethod
    def _merge_result(result: ProviderRunResult, merge: dict[str, Any]) -> ProviderRunResult:
        metadata = dict(result.metadata)
        browser_metadata = metadata.get("browser") if isinstance(metadata.get("browser"), dict) else {}
        metadata["browser"] = {
            **dict(browser_metadata),
            **dict(merge.get("next_state") or {}),
        }
        metadata["provider_branch"] = {
            "branch_id": merge.get("branch_id"),
            "provider": merge.get("provider"),
            "status": merge.get("status"),
            "branch_store_path": merge.get("branch_store_path"),
            "final_report": merge.get("final_report"),
            "compact_digest": merge.get("compact_digest"),
            "hidden_message_count": merge.get("hidden_message_count"),
            "visible_messages": merge.get("visible_messages") or [],
            "artifacts": merge.get("artifacts") or [],
            "actions": merge.get("actions") or [],
            "risks": merge.get("risks") or [],
            "next_state": merge.get("next_state") or {},
            "policy": merge.get("policy") or {},
        }
        metadata["result_type"] = "ok" if merge.get("status") == "done" else "error"
        evidence = ProviderOutcomeEvidence.from_dict(merge.get("outcome_evidence"))
        return ProviderRunResult(
            status=merge.get("status") if merge.get("status") in {"done", "error", "cancelled"} else result.status,
            result=str(merge.get("final_report") or result.result or ""),
            error=result.error,
            metadata=metadata,
            outcome_evidence=evidence,
        )


def _planner_page_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "browser_session_id": state.get("browser_session_id") or "",
        "url": state.get("url") or "",
        "title": state.get("title") or "",
        "text_excerpt": str(state.get("text") or "")[:1000],
    }


def _semantic_operation(request: ProviderRunRequest) -> str:
    metadata = request.metadata if isinstance(request.metadata, dict) else {}
    return str(
        metadata.get("semantic_operation")
        or metadata.get("branch_original_action")
        or metadata.get("browser_action")
        or metadata.get("action")
        or request.mode
        or "observe"
    ).strip().lower()


def _remaining_actions_match_page(
    actions: list[dict[str, Any]],
    *,
    previous_state: dict[str, Any],
    current_state: dict[str, Any],
) -> bool:
    """Check that remaining ref actions still address the observed controls.

    Planner refs belong to one page observation. A navigation always ends that
    observation epoch. For same-URL DOM updates, a remaining ref may continue
    only when its stable control fingerprint is unchanged.
    """

    previous_url = str(previous_state.get("url") or "").strip()
    current_url = str(current_state.get("url") or "").strip()
    if previous_url != current_url:
        return False

    previous_refs = _ref_fingerprints(previous_state.get("interaction_refs"))
    current_refs = _ref_fingerprints(current_state.get("interaction_refs"))
    for action in actions:
        action_name = str(action.get("action") or "").strip().lower()
        if action_name not in {"click_ref", "fill_ref"}:
            continue
        ref = str(
            action.get("ref")
            or action.get("action_ref")
            or action.get("target_ref")
            or ""
        ).strip()
        if not ref or previous_refs.get(ref) != current_refs.get(ref):
            return False
    return True


def _ref_fingerprints(raw_refs: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(raw_refs, list):
        return {}
    result: dict[str, tuple[str, ...]] = {}
    for item in raw_refs:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or "").strip()
        if not ref:
            continue
        result[ref] = tuple(
            str(item.get(key) or "").strip()
            for key in ("kind", "role", "selector", "href", "label")
        )
    return result


def _short_text(text: str, limit: int) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 3)].rstrip() + "..."


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _default_branch_planner(context: dict[str, Any]) -> dict[str, Any]:
    try:
        from server.browser_branch_planner import decide_with_browser_branch_llm

        decision = await decide_with_browser_branch_llm(context)
        if isinstance(decision, dict):
            return _with_deterministic_action_if_needed(decision, context)
    except ModuleNotFoundError as exc:
        logger.info("browser branch planner unavailable; using deterministic fallback: %s", exc)
    except Exception:
        logger.exception("browser branch planner failed before deterministic fallback")
    fallback = _deterministic_branch_fallback(context)
    if fallback:
        return fallback
    return {
        "assistant_message": "",
        "actions": [],
        "final_report": "今のページは確認できたけど、操作対象を特定できなかったわ。もう少し具体的に指示して。",
        "compact_digest": "Browser branch observed the page but could not determine a safe browser action.",
        "context_seen": {
            "title": context.get("page", {}).get("title"),
            "ref_count": len(context.get("interaction_refs") or []),
        },
        "planner": "browser_branch_no_action_fallback",
    }


def _with_deterministic_action_if_needed(decision: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    actions = decision.get("actions") if isinstance(decision.get("actions"), list) else []
    if actions:
        return decision
    fallback = _deterministic_branch_fallback(context)
    if not fallback:
        return decision
    logger.info(
        "browser branch planner returned no actions; using deterministic fallback planner=%s query=%r",
        decision.get("planner") or "unknown",
        fallback.get("actions", [{}])[0].get("value"),
    )
    fallback["llm_decision"] = {
        "assistant_message": decision.get("assistant_message") or "",
        "final_report": decision.get("final_report") or "",
        "reason": decision.get("reason") or "",
        "confidence": decision.get("confidence"),
        "planner": decision.get("planner") or "browser_branch_llm",
    }
    return fallback


def _deterministic_branch_fallback(context: dict[str, Any]) -> dict[str, Any] | None:
    query = _extract_search_query(context)
    if not query:
        return None
    target = _best_fillable_ref(context.get("interaction_refs") or [])
    if not target:
        return None
    ref = str(target.get("ref") or "").strip()
    if not ref:
        return None
    return {
        "assistant_message": "",
        "actions": [
            {
                "action": "fill_ref",
                "ref": ref,
                "value": query,
                "submit": True,
                "task": f"Search current page for {query}",
            }
        ],
        "final_report": f"{query} で検索したわ。結果ページを確認できる状態にしてある。",
        "compact_digest": f"Deterministic fallback submitted current-page search for {query!r} using ref {ref}.",
        "reason": "LLM planner produced no usable browser action for an explicit current-page search.",
        "confidence": 0.68,
        "planner": "browser_branch_deterministic_fallback",
    }


def _extract_search_query(context: dict[str, Any]) -> str:
    metadata = context.get("request_metadata") if isinstance(context.get("request_metadata"), dict) else {}
    explicit_query = _clean_search_query(str(metadata.get("query") or ""))
    if explicit_query:
        return explicit_query
    latest = _latest_instruction_text(context)
    # A deterministic action may only be authorized by the latest instruction.
    # Replaying an older query after "continue" can overwrite a page transition
    # or contradict a newer conditional instruction such as "if there is no
    # result, only report the current state". Branch history remains available
    # to the LLM planner, but it is not host authorization to repeat an action.
    return _extract_search_query_from_text(latest)


def _latest_instruction_text(context: dict[str, Any]) -> str:
    authoritative = str(context.get("latest_user_instruction") or "").strip()
    if authoritative:
        return authoritative
    task = str(context.get("user_message") or "")
    for line in task.splitlines():
        label = "Latest user instruction:"
        if line.startswith(label):
            return line[len(label):].strip().strip(".")
    metadata = context.get("request_metadata") if isinstance(context.get("request_metadata"), dict) else {}
    for key in ("branch_user_message", "user_message"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value
    transcript = context.get("branch_transcript") if isinstance(context.get("branch_transcript"), list) else []
    for item in reversed(transcript):
        if not isinstance(item, dict) or str(item.get("role") or "") != "user":
            continue
        value = str(item.get("content") or item.get("text") or "").strip()
        if value:
            return value
    return task.strip()


def _extract_search_query_from_text(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if not cleaned:
        return ""
    explicit_keyword = re.search(
        r"(?:关键词|關鍵詞|关键字|關鍵字|keyword)\s*[:：]?\s*(?P<q>[^。！？!?；;\n]+)",
        cleaned,
        re.I,
    )
    if explicit_keyword:
        query = _clean_search_query(explicit_keyword.group("q"))
        if query:
            return query
    # The deterministic fallback is deliberately narrower than natural
    # language understanding. It should recover only an unmistakable search
    # command; ambiguous cases stay with the LLM planner or fail closed.
    lead = (
        r"^(?:(?:好|好的|那就|然后|然後|接着|接著|再|请|請|请你|請你|帮我|幫我|"
        r"麻烦|麻煩|麻烦你|麻煩你|给我|給我)\s*[,，]?\s*)*"
    )
    location = r"(?:(?:在|到|去)\s*[^。！？!?；;\n]{1,40}?(?:里|裡|上|中)?\s*)?"
    patterns = (
        lead + location + r"(?:搜索|搜一下|搜下|搜|查找|查询|查詢|检索|檢索|搜尋)\s*(?:一下|下|看看)?\s*[:：]?\s*(?P<q>[^。！？!?；;\n]+)",
        r"^(?:(?:please|could you|can you|go ahead and|then)\s+)*(?:(?:on|in)\s+[^.!?;\n]{1,50}?\s+)?(?:search|look up|find)\s+(?:for\s+)?(?P<q>[^.!?;\n]+)",
        r"^(?:(?:この|現在の)?(?:ページ|サイト)(?:で|内で)\s*)?(?P<q>[A-Za-z0-9\u3040-\u30ff\u4e00-\u9fff][^。！？!?；;\n]{0,120}?)\s*(?:を|について)?\s*(?:検索|探して|調べ)(?:して|する|て)?",
    )
    for pattern in patterns:
        match = re.search(pattern, cleaned, re.I)
        if not match:
            continue
        query = _clean_search_query(match.group("q"))
        if query:
            return query
    return ""


def _clean_search_query(query: str) -> str:
    value = str(query or "").strip()
    value = re.sub(
        r"^(?:关键词|關鍵詞|关键字|關鍵字|keyword)\s*[:：]?\s*",
        "",
        value,
        flags=re.I,
    ).strip()
    value = re.sub(
        r"^(?:一下|下|看看|一下子|for|关于|關於|有关|有關|について|を)\s+",
        "",
        value,
        flags=re.I,
    ).strip()
    value = re.split(
        r"(?:\s+然后|\s+然後|\s+并|，|,|。|！|!|？|\?|；|;|\s+on\s+this\s+page|\s+in\s+this\s+page)",
        value,
        maxsplit=1,
        flags=re.I,
    )[0].strip()
    value = value.strip(" \t\r\n'\"`“”‘’<>[](){}：:。.!?！？；;,，、")
    trailing_particles = ("的视频", "的影片", "的结果", "結果", "する", "して", "を")
    for suffix in trailing_particles:
        if value.endswith(suffix) and len(value) > len(suffix):
            value = value[: -len(suffix)].strip()
    if value in {"框", "按钮", "按鈕", "搜索框", "搜尋框", "搜索按钮", "搜尋按鈕", "button", "search box", "search button"}:
        return ""
    return value[:120]


def _best_fillable_ref(refs: Any) -> dict[str, Any] | None:
    if not isinstance(refs, list):
        return None
    best: tuple[int, dict[str, Any]] | None = None
    for index, item in enumerate(refs):
        if not isinstance(item, dict) or not bool(item.get("fillable")):
            continue
        haystack = " ".join(
            str(item.get(key) or "")
            for key in ("label", "role", "kind", "selector", "href")
        ).lower()
        score = 1000 - index
        if item.get("role") == "textbox":
            score += 50
        if item.get("kind") == "input":
            score += 50
        if any(token in haystack for token in ("search", "搜索", "搜", "检索", "搜尋", "検索", "keyword", "query")):
            score += 300
        bbox = item.get("bbox") if isinstance(item.get("bbox"), dict) else {}
        try:
            if float(bbox.get("y", 9999)) < 160:
                score += 40
        except Exception:
            pass
        if best is None or score > best[0]:
            best = (score, item)
    return dict(best[1]) if best else None
