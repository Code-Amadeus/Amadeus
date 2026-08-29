"""Permission and external-disposition authority for one exact Work Attempt.

UI surfaces supply durable identities and a bounded decision.  This service
validates their relationship and applies the decision through the ledger or
the existing export transaction.  It owns no canvas, narration, or Provider
control; those are consequences rendered by the coordinator facade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from agent_host.work_ledger_store import (
    WorkLedgerConflict,
    WorkLedgerNotFound,
    WorkLedgerStore,
)
from agent_host.work_ledger_types import (
    CompletionDecision,
    PermissionRequestRecord,
    RunAttemptRecord,
)
from server.work_export_service import ExportResolution, WorkExportService


@dataclass(frozen=True, slots=True)
class PermissionContext:
    request: PermissionRequestRecord
    attempt: RunAttemptRecord
    desktop_export: bool


@dataclass(frozen=True, slots=True)
class PermissionResolution:
    context: PermissionContext
    permission: PermissionRequestRecord
    exported_paths: tuple[str, ...] = ()


class WorkPermissionService:
    """Apply one permission decision without accepting presentation authority."""

    def __init__(
        self,
        store: WorkLedgerStore,
        export_service: WorkExportService,
        *,
        auto_accept_approved_exports: bool,
    ) -> None:
        self.store = store
        self.export_service = export_service
        self.auto_accept_approved_exports = bool(auto_accept_approved_exports)

    def context(
        self,
        request_id: str,
        *,
        work_item_id: str,
        attempt_id: str,
    ) -> PermissionContext:
        request = self.store.get_permission_request(str(request_id or "").strip())
        if request is None:
            raise WorkLedgerNotFound(f"unknown permission request: {request_id}")
        clean_work_item = str(work_item_id or "").strip()
        if not clean_work_item:
            raise WorkLedgerConflict(
                "work item identity is required for permission resolution"
            )
        if request.work_item_id != clean_work_item:
            raise WorkLedgerConflict(
                "permission request belongs to a different work item"
            )
        clean_attempt = str(attempt_id or "").strip()
        if not clean_attempt:
            raise WorkLedgerConflict(
                "attempt identity is required for permission resolution"
            )
        if request.attempt_id != clean_attempt:
            raise WorkLedgerConflict(
                "permission request belongs to a different attempt"
            )
        attempt = self.store.get_attempt(request.attempt_id)
        if attempt is None:  # pragma: no cover - protected by FK
            raise WorkLedgerNotFound(
                f"unknown permission attempt: {request.attempt_id}"
            )
        return PermissionContext(
            request=request,
            attempt=attempt,
            desktop_export=self.is_desktop_export_permission(request),
        )

    def resolve(self, context: PermissionContext, *, allow: bool) -> PermissionResolution:
        request = context.request
        if allow and "allow_once" not in request.options:
            raise WorkLedgerConflict("permission request does not allow approval")
        if context.desktop_export:
            resolution: ExportResolution = self.export_service.resolve(
                request.request_id,
                allow=allow,
            )
            return PermissionResolution(
                context=context,
                permission=resolution.permission,
                exported_paths=tuple(resolution.exported_paths),
            )
        resolved = self.store.resolve_permission_request(
            request.request_id,
            "allowed" if allow else "denied",
            metadata={
                "resolution": "user_allowed" if allow else "user_denied",
                "retry_required": bool(request.metadata.get("retry_required")),
            },
        )
        return PermissionResolution(context=context, permission=resolved)

    def expire_provider_checkpoints(
        self,
        attempt: RunAttemptRecord,
        *,
        resolution: str,
    ) -> int:
        """Close unresolved Provider approvals owned by one terminal Attempt.

        This is deliberately narrower than expiring arbitrary permissions.
        Host product permissions such as Desktop export are separate durable
        transactions and remain pending after Provider execution when needed.
        """

        if attempt.execution_status not in {"succeeded", "failed", "cancelled"}:
            return 0
        expired = 0
        for permission in self.store.list_permission_requests(
            attempt.work_item_id,
            attempt_id=attempt.attempt_id,
            status="pending",
        ):
            if str(permission.metadata.get("kind") or "") != "provider_permission":
                continue
            try:
                self.store.resolve_permission_request(
                    permission.request_id,
                    "expired",
                    metadata={
                        "resolution": str(resolution or "attempt_terminal"),
                        "attempt_execution_status": attempt.execution_status,
                    },
                )
                expired += 1
            except WorkLedgerConflict:
                # A concurrent user decision is immutable and wins.
                continue
        return expired

    def resume_export(self, context: PermissionContext) -> PermissionResolution:
        if not context.desktop_export:
            raise WorkLedgerConflict("permission request is not a Desktop export")
        if not self.export_service.can_resume_authorized(context.request):
            raise WorkLedgerConflict("Desktop export has no recoverable authorization")
        resolution = self.export_service.resume_authorized(context.request.request_id)
        return PermissionResolution(
            context=context,
            permission=resolution.permission,
            exported_paths=tuple(resolution.exported_paths),
        )

    def abandon_export(self, context: PermissionContext) -> PermissionResolution:
        if not context.desktop_export:
            raise WorkLedgerConflict("permission request is not a Desktop export")
        permission = self.export_service.abandon_authorized(context.request.request_id)
        return PermissionResolution(context=context, permission=permission)

    @staticmethod
    def is_desktop_export_permission(request: PermissionRequestRecord) -> bool:
        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        return bool(
            request.capability == "filesystem.export"
            and request.action == "copy_to_desktop"
            and metadata.get("kind") == "desktop_export"
        )

    def mark_export_delta_resolved(
        self,
        attempt_id: str,
        *,
        status: str,
        reason: str,
    ) -> None:
        attempt = self.store.get_attempt(attempt_id)
        if attempt is None:
            return
        updates = {}
        for key in ("export_delta", "git_delta"):
            current = attempt.metadata.get(key)
            if (
                not isinstance(current, dict)
                or current.get("artifact_type") != "business.proposed_export"
            ):
                continue
            delta = dict(current)
            delta["reason"] = reason
            delta["pending_export"] = False
            delta["external_export_pending"] = False
            delta["recovery_required"] = reason in {
                "external_export_failed",
                "external_export_recovery_required",
            }
            delta["export_status"] = status
            updates[key] = delta
        if updates:
            self.store.update_attempt(attempt_id, metadata=updates)

    def auto_accept_approved_export(
        self,
        *,
        request: PermissionRequestRecord,
        resolved: PermissionRequestRecord,
        attempt: RunAttemptRecord,
        exported_paths: Iterable[str],
    ) -> bool:
        """Accept only a committed ephemeral export with complete user evidence."""

        if not self.auto_accept_approved_exports:
            return False
        if request.metadata.get("diagnostic_only") is True:
            return False
        if resolved.status != "allowed" or "allow_once" not in request.options:
            return False
        if str(resolved.metadata.get("resolution") or "") != "user_allowed":
            return False
        entries = request.metadata.get("entries")
        if not isinstance(entries, list) or not self.export_service.is_committed_export(
            resolved,
            entries,
        ):
            return False
        item = self.store.get_work_item(request.work_item_id)
        if item is None:
            return False
        presentation = (
            item.metadata.get("presentation")
            if isinstance(item.metadata.get("presentation"), dict)
            else {}
        )
        if str(presentation.get("lifecycle") or "").strip().lower() != "ephemeral":
            return False
        latest = self.store.latest_completion(item.work_item_id)
        if not (
            latest is not None
            and latest.attempt_id == attempt.attempt_id
            and latest.terminal
            and latest.execution_status == "succeeded"
            and attempt.execution_status == "succeeded"
        ):
            return False
        exported = [str(path) for path in exported_paths if str(path)]
        self.store.record_completion(
            item.work_item_id,
            CompletionDecision(
                execution_status="succeeded",
                completeness=latest.completeness,
                attention="none",
                work_item_state="accepted",
                rationale=(
                    "Policy auto-accepted an ephemeral WorkItem after its succeeded "
                    "attempt was explicitly approved once and the Desktop export committed."
                ),
                terminal=True,
            ),
            attempt_id=attempt.attempt_id,
            source="policy",
            evidence={
                "policy": "auto_accept_approved_export",
                "permission_request_id": resolved.request_id,
                "permission_resolution": "user_allowed",
                "export_status": "committed",
                "exported_paths": exported,
            },
        )
        return True
