"""Apply an already-adjudicated delegation through ProviderRuntime.

This boundary does not interpret user language, choose a Provider, resolve a
reference, or decide a workspace.  Its input is the frozen result of those
host-owned decisions.  It assembles the Attempt request, handles an active
amendment through the existing steer/replacement service, and starts exactly
one managed Provider run.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from agent_host.provider_contract import (
    ProviderManifest,
    ProviderRequirements,
    ProviderSelection,
)
from agent_host.provider_identity import (
    MAIN_ROLE_NAME_METADATA_KEY,
    SOURCE_CONTEXT_SCOPE_METADATA_KEY,
)
from agent_host.provider_types import ProviderRunRequest
from server.inherited_role_prompt import MAIN_CONVERSATION_ROLE_NAME
from server.work_export_service import WorkExportService
from server.work_steer_control import route_active_amendment


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DelegateDispatchPlan:
    task_text: str
    attrs: dict[str, Any]
    provider: str
    requirements: ProviderRequirements
    selection: ProviderSelection
    manifest: ProviderManifest
    workspace_route: dict[str, Any]
    workspace_authority: str
    delegate_cwd: str | None
    delegate_mode: str
    action: str
    branch_intent: str
    sanitize_info: dict[str, Any]
    browser_parameters: dict[str, Any]
    browser_audit: dict[str, Any]


def build_delegate_metadata(
    plan: DelegateDispatchPlan,
    *,
    session_id: str,
) -> dict[str, Any]:
    """Build one Provider request envelope from host-authoritative decisions."""

    attrs = dict(plan.attrs)
    source_user_text = " ".join(
        str(attrs.get("_host_source_user_text") or "").split()
    )
    dispatch_source = str(attrs.get("_host_dispatch_source") or "").strip()
    provider_requirements = plan.requirements.to_dict()
    auip_dispatch_sources = {"auip_prepare", "auip_create"}
    metadata: dict[str, Any] = {
        "source": (
            dispatch_source
            if dispatch_source in auip_dispatch_sources
            else "llm_delegate"
        ),
        "session_id": str(session_id or ""),
        "delegate_attrs": {
            key: value
            for key, value in attrs.items()
            if not str(key).startswith("_host_")
        },
        "intent": (
            str(attrs.get("intent") or "").strip().lower()
            if str(attrs.get("intent") or "").strip().lower() in {"execute", "amend"}
            else "execute"
        ),
        "focus_applied": attrs.get("focus_applied") is True,
        "amend_inferred": attrs.get("amend_inferred") is True,
        "project_source_amend": attrs.get("_host_project_source_amend") is True,
        "branch_intent": plan.branch_intent or "",
        "workspace_routing_source": str(plan.workspace_route.get("source") or ""),
        "provider_requirements": provider_requirements,
        "provider_manifest": plan.manifest.to_dict(),
        "provider_selection": plan.selection.to_dict(),
        "write_intent": str(provider_requirements.get("workspace_access") or "")
        == "write",
    }
    if dispatch_source in auip_dispatch_sources:
        metadata["host_outcome_requirement"] = {
            "operation": "prepare",
            "facet": "auip.application",
            "expected": {"current_attempt_contribution": True},
        }
    delegate_recovered = str(attrs.get("delegate_recovered") or "").strip().lower()
    if delegate_recovered:
        metadata["delegate_recovered"] = delegate_recovered
    focus_guard = str(attrs.get("_host_focus_guard") or "").strip().lower()
    if focus_guard:
        metadata["focus_guard"] = focus_guard
    if source_user_text:
        metadata["source_user_text"] = source_user_text[:4000]
        # The source utterance was addressed to Main Chat, not the execution
        # Provider. Keep that stable identity beside the untouched payload so
        # each model-driven adapter can resolve "yourself" without Host text
        # rewriting or another semantic decision.
        metadata[MAIN_ROLE_NAME_METADATA_KEY] = MAIN_CONVERSATION_ROLE_NAME
        source_scope = str(attrs.get("_host_source_context_scope") or "").strip()
        clean_session_id = str(session_id or "").strip()
        if source_scope or clean_session_id:
            metadata[SOURCE_CONTEXT_SCOPE_METADATA_KEY] = (
                source_scope[:800] if source_scope else f"chat:{clean_session_id}"
            )
    source_user_context = "\n".join(
        line.strip()
        for line in str(attrs.get("_host_source_user_context") or "").splitlines()
        if line.strip()
    )
    if source_user_context and source_user_context != source_user_text:
        metadata["source_user_context"] = source_user_context[:2000]
    turn_id = str(attrs.get("_host_turn_id") or "").strip()
    if turn_id:
        # Audit identity also joins a one-shot, same-utterance AUIP launch to
        # the delivery that earned it.  It is not a routing hint and never
        # survives as WorkItem policy.
        metadata["turn_id"] = turn_id[:200]
    payload_source = str(attrs.get("_host_payload_source") or "").strip()
    if payload_source:
        metadata["payload_source"] = payload_source
        metadata["payload_rebase_reason"] = str(
            attrs.get("_host_payload_rebase_reason") or ""
        ).strip()
    provider_handoff = attrs.get("_host_provider_handoff")
    if isinstance(provider_handoff, dict) and provider_handoff:
        metadata["provider_handoff"] = dict(provider_handoff)
    if plan.delegate_cwd:
        metadata["cwd"] = plan.delegate_cwd

    declared_work_item_ref = str(
        attrs.get("workspace_ref")
        or attrs.get("workspaceRef")
        or attrs.get("work_item_id")
        or attrs.get("workItemId")
        or ""
    ).strip()
    if declared_work_item_ref:
        metadata["work"] = {"workspace_ref": declared_work_item_ref}
        if metadata["intent"] == "amend":
            metadata["continuation"] = "amend"
            metadata["work"]["work_item_id"] = declared_work_item_ref
    if plan.workspace_authority == "host" and plan.delegate_cwd:
        project_id = str(plan.workspace_route.get("projectId") or "")
        workspace_ref = str(
            plan.workspace_route.get("workItemId") or declared_work_item_ref or ""
        )
        workspace_mode = str(plan.workspace_route.get("workspaceMode") or "")
        metadata["workspace_path"] = plan.delegate_cwd
        metadata["work"] = {
            **(
                dict(metadata.get("work") or {})
                if isinstance(metadata.get("work"), dict)
                else {}
            ),
            "workspace_path": plan.delegate_cwd,
            **({"project_id": project_id} if project_id else {}),
            **({"workspace_ref": workspace_ref} if workspace_ref else {}),
            **({"workspace_mode": workspace_mode} if workspace_mode else {}),
        }
        if workspace_ref and metadata["intent"] == "amend":
            metadata["continuation"] = "amend"
            metadata["work"]["work_item_id"] = workspace_ref

    metadata["delegate_mode"] = plan.delegate_mode
    if plan.sanitize_info:
        metadata["delegate_sanitized"] = dict(plan.sanitize_info)
    export_target = str(attrs.get("target") or "").strip().lower()
    if source_user_text and WorkExportService._has_desktop_destination(source_user_text):
        metadata["external_export"] = {
            "target": "desktop",
            "intent_source": "source_user_text",
        }
    elif str(attrs.get("_host_external_target_authorized") or "") == "desktop":
        metadata["external_export"] = {
            "target": "desktop",
            "intent_source": "control_decision",
        }
    elif not source_user_text and export_target in {"desktop", "user_desktop"}:
        metadata["external_export"] = {
            "target": "desktop",
            "intent_source": "delegate_target",
        }

    if plan.provider == "browser":
        if plan.action:
            metadata["browser_action"] = plan.action
            metadata["browser_mode"] = plan.action
        for key in (
            "url",
            "query",
            "text",
            "label",
            "selector_text",
            "browser_session_id",
            "browserSessionId",
            "ref",
            "action_ref",
            "target_ref",
            "value",
            "input",
            "submit",
        ):
            value = attrs.get(key)
            if value not in (None, ""):
                metadata[key] = value
        metadata.update(plan.browser_parameters)
        if plan.browser_audit:
            metadata["browser_request_normalization"] = dict(plan.browser_audit)
    return metadata


async def dispatch_delegate(
    plan: DelegateDispatchPlan,
    *,
    announce_start_failure: Callable[[str, Exception], Awaitable[None]],
    route_amendment: Callable[..., Awaitable[dict[str, Any]]] = route_active_amendment,
) -> str:
    """Start one managed Provider run from an adjudicated dispatch plan."""

    from agent_host.provider_runtime import runtime
    from core import session_manager as sm

    provider = plan.provider
    task_text = plan.task_text
    delegate_cwd = plan.delegate_cwd
    delegate_mode = plan.delegate_mode
    metadata = build_delegate_metadata(
        plan,
        session_id=sm.get_current_session_id() or "",
    )
    used_provider_runtime = False
    try:
        active_amendment_ref = (
            str(
                (metadata.get("work") or {}).get("work_item_id")
                if isinstance(metadata.get("work"), dict)
                else ""
            ).strip()
            if str(metadata.get("intent") or "").strip().lower() == "amend"
            else ""
        )
        if active_amendment_ref:
            from server.work_ledger_coordinator import get_work_ledger_coordinator

            ledger_coordinator = get_work_ledger_coordinator()
            if ledger_coordinator is not None:
                amendment_display_task = str(task_text or "")
                amendment_route = await route_amendment(
                    runtime=runtime,
                    coordinator=ledger_coordinator,
                    work_item_id=active_amendment_ref,
                    selected_provider=provider,
                    task_text=amendment_display_task,
                    turn_id=str(plan.attrs.get("_host_turn_id") or ""),
                    source_user_text=str(metadata.get("source_user_text") or ""),
                    source_user_context=str(metadata.get("source_user_context") or ""),
                    source_context_scope=str(
                        metadata.get(SOURCE_CONTEXT_SCOPE_METADATA_KEY) or ""
                    ),
                )
                if amendment_route.get("handled") is True:
                    return str(amendment_route.get("message") or "[amend] handled")
                replacement = (
                    amendment_route.get("replacement")
                    if isinstance(amendment_route.get("replacement"), dict)
                    else None
                )
                if replacement is not None:
                    provider = str(replacement["provider"])
                    delegate_cwd = str(replacement["workspace_path"])
                    delegate_mode = str(replacement["mode"])
                    task_text = str(replacement["instruction"])
                    metadata.update(dict(replacement.get("lineage") or {}))
                    metadata.update(
                        {
                            "continuation": "steer_replacement",
                            "replaces_attempt_id": str(
                                replacement["predecessor_attempt_id"]
                            ),
                            "steer_replacement": dict(replacement.get("control") or {}),
                            "display_task": amendment_display_task,
                            "cwd": delegate_cwd,
                            "workspace_path": delegate_cwd,
                            "delegate_mode": delegate_mode,
                            "work": {
                                "work_item_id": str(replacement["work_item_id"]),
                                "project_id": str(replacement["project_id"]),
                                "workspace_path": delegate_cwd,
                                "workspace_mode": str(replacement["workspace_mode"]),
                            },
                        }
                    )

        record = await runtime.start(
            ProviderRunRequest(
                provider=provider,
                task=str(task_text or ""),
                cwd=delegate_cwd,
                mode=delegate_mode,
                metadata=metadata,
                requirements=plan.requirements,
                ownership="managed",
            )
        )
        used_provider_runtime = True
        if record.task_handle is not None:
            await asyncio.shield(record.task_handle)
        return record.result or (
            f"[{provider} error] {record.error}" if record.error else ""
        )
    except asyncio.CancelledError:
        if used_provider_runtime:
            logger.info(
                "delegate wait interrupted; provider runtime continues provider=%s action=%s",
                provider,
                delegate_mode,
            )
        raise
    except Exception as exc:
        logger.exception("provider delegate failed: %s", provider)
        await announce_start_failure(provider, exc)
        return f"[{provider} error] delegate execution failed"
