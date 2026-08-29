"""Read-only Work Ledger projections used by status, report, and UI surfaces.

The store owns durable facts.  This module owns their interpretation as a
current WorkItem or Project view.  It deliberately has no event bus and no
mutation methods, so rendering a view cannot create work or change focus.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agent_host.work_ledger_store import WorkLedgerNotFound, WorkLedgerStore
from agent_host.work_ledger_types import PermissionRequestRecord, WorkItemRecord
from server.auip_app_source import discover_launchable_auip_app
from server.scratch_workspace import is_scratch_path
from server.work_activity_snapshot import ACTIVITY_METADATA_KEY, activity_report_fields


_ACTIVE_EXECUTION = frozenset({"queued", "running"})


class WorkReadModel:
    """Project durable Work Ledger facts without acquiring write authority."""

    def __init__(
        self,
        store: WorkLedgerStore,
        *,
        clock: Callable[[], float] = time.time,
        is_unkept_draft: Callable[[str], bool],
        is_desktop_export_permission: Callable[[PermissionRequestRecord], bool],
        can_resume_authorized_export: Callable[[PermissionRequestRecord], bool],
    ) -> None:
        self.store = store
        self._clock = clock
        self._is_unkept_draft = is_unkept_draft
        self._is_desktop_export_permission = is_desktop_export_permission
        self._can_resume_authorized_export = can_resume_authorized_export

    def detail(self, work_item_id: str) -> dict[str, Any]:
        item = self.store.get_work_item(work_item_id)
        if item is None:
            raise WorkLedgerNotFound(f"unknown work item: {work_item_id}")
        projected = self.project_item(item)
        projected["operations"] = [
            record.to_dict() for record in self.store.list_operations(work_item_id)
        ]
        projected["attempts"] = [
            record.to_dict() for record in self.store.list_attempts(work_item_id)
        ]
        projected["artifacts"] = [
            record.to_dict() for record in self.store.list_artifacts(work_item_id)
        ]
        projected["completionHistory"] = [
            record.to_dict() for record in self.store.list_completions(work_item_id)
        ]
        projected["permissions"] = [
            record.to_dict()
            for record in self.store.list_permission_requests(work_item_id)
        ]
        return projected

    def project_status_snapshot(self, project_id: str) -> dict[str, Any] | None:
        project = self.store.get_project(str(project_id or "").strip())
        if project is None:
            return None
        projected = [
            self.project_item(item)
            for item in self.store.list_work_items(project_id=project.project_id, limit=200)
        ]
        current = [
            item for item in projected if item.get("state") not in {"accepted", "archived"}
        ]
        running = [
            item for item in current if item.get("execution") in _ACTIVE_EXECUTION
        ]
        needs_you = [
            item for item in current if item.get("attention") not in {"", "none"}
        ]
        return {
            "projectId": project.project_id,
            "projectName": project.name or Path(project.canonical_path).name,
            "state": project.state,
            "counts": {
                "current": len(current),
                "running": len(running),
                "needsYou": len(needs_you),
                "history": max(0, len(projected) - len(current)),
            },
            "recent": projected[:5],
        }

    def project_apps(self, project_id: str, *, limit: int = 100) -> dict[str, Any]:
        """List the current verified AUIP delivery for each Project WorkItem.

        WorkItem is the durable product lineage. Artifact ids remain revision
        identities, so this projection deliberately exposes one launchable
        current revision per WorkItem instead of inventing a second catalog.
        """

        app_limit = max(1, min(int(limit), 200))
        items = self.store.list_work_items(
            project_id=str(project_id or "").strip(),
            limit=200,
        )
        apps: list[dict[str, Any]] = []
        for item in items:
            app = discover_launchable_auip_app(self.store, item.work_item_id)
            if app is None:
                continue
            apps.append(self._app_summary(item, app, can_promote=False))
            if len(apps) >= app_limit:
                break
        return {
            "apps": apps,
            "complete": len(items) < 200,
        }

    def draft_apps(self, *, limit: int = 5) -> dict[str, Any]:
        """Return only the most recent launchable AUIP apps in unkept Drafts."""

        app_limit = max(1, min(int(limit), 20))
        scan_limit = 500
        items = self.store.list_work_items(limit=scan_limit)
        apps: list[dict[str, Any]] = []
        for item in items:
            if not self._is_unkept_draft(item.workspace_path):
                continue
            app = discover_launchable_auip_app(self.store, item.work_item_id)
            if app is None:
                continue
            apps.append(self._app_summary(item, app, can_promote=True))
            if len(apps) >= app_limit:
                break
        return {
            "apps": apps,
            "complete": len(items) < scan_limit,
            "recentLimit": app_limit,
        }

    def _app_summary(
        self,
        item: WorkItemRecord,
        app: dict[str, Any],
        *,
        can_promote: bool,
    ) -> dict[str, Any]:
        app_meta = app.get("app") if isinstance(app.get("app"), dict) else {}
        attempts = {
            attempt.attempt_id: attempt
            for attempt in self.store.list_attempts(item.work_item_id)
        }
        contributing = [
            attempts[attempt_id]
            for attempt_id in app.get("contributing_attempt_ids") or []
            if str(attempt_id or "") in attempts
        ]
        latest_attempt = max(
            contributing,
            key=lambda attempt: attempt.attempt_number,
            default=(list(attempts.values())[-1] if attempts else None),
        )
        attempt_metadata = (
            latest_attempt.metadata
            if latest_attempt is not None and isinstance(latest_attempt.metadata, dict)
            else {}
        )
        artifact = self.store.get_artifact(str(app.get("artifact_id") or ""))
        stances = [str(value) for value in app.get("stances") or []]
        return {
            "projectId": item.project_id,
            "workItemId": item.work_item_id,
            "workTitle": item.title,
            "artifactId": str(app.get("artifact_id") or ""),
            "artifactRef": str(app.get("artifact_ref") or ""),
            "appId": str(app_meta.get("id") or app.get("artifact_id") or ""),
            "title": str(app_meta.get("title") or app.get("title") or "AUIP app"),
            "version": str(app_meta.get("version") or "0"),
            "objective": str(app_meta.get("objective") or ""),
            "interactionSummary": str(app_meta.get("interactionSummary") or ""),
            "modes": [
                "observe",
                *(["collaborate", "delegate"] if "participant" in stances else []),
            ],
            "revision": int(latest_attempt.attempt_number if latest_attempt else 0),
            "updatedAt": self._iso_time(item.last_activity_at),
            "workState": item.state,
            "execution": str(latest_attempt.execution_status if latest_attempt else "idle"),
            "artifactStatus": str(artifact.status if artifact is not None else "registered"),
            "location": str(artifact.location if artifact is not None else "workspace"),
            "sourceSessionId": str(
                attempt_metadata.get("session_id")
                or attempt_metadata.get("sessionId")
                or attempt_metadata.get("chat_session_id")
                or ""
            ),
            "canPromote": bool(
                can_promote
                and (
                    latest_attempt is None
                    or latest_attempt.execution_status not in _ACTIVE_EXECUTION
                )
            ),
        }

    @staticmethod
    def default_focus_id(items: list[dict[str, Any]]) -> str:
        candidates = [item for item in items if item.get("state") != "archived"]
        if not candidates:
            return ""

        def rank(item: dict[str, Any]) -> int:
            if item.get("attention") not in {None, "", "none"}:
                return 0
            if item.get("execution") in _ACTIVE_EXECUTION:
                return 1
            if item.get("state") in {"open", "review_ready"}:
                return 2
            return 3

        # Input order is already last_activity DESC, so min preserves recency
        # inside Needs-you/Running/Open priority buckets.
        return str(min(candidates, key=rank).get("id") or "")

    def project_item(self, item: WorkItemRecord) -> dict[str, Any]:
        operations = self.store.list_operations(item.work_item_id)
        attempts = self.store.list_attempts(item.work_item_id)
        latest_attempt = attempts[-1] if attempts else None
        latest_operation = (
            self.store.get_operation(latest_attempt.operation_id)
            if latest_attempt is not None and latest_attempt.operation_id
            else operations[-1] if operations else None
        )
        completion = self.store.latest_completion(item.work_item_id)
        completion_matches = bool(
            completion
            and latest_attempt
            and completion.attempt_id == latest_attempt.attempt_id
        )
        execution = latest_attempt.execution_status if latest_attempt else "idle"
        completeness = completion.completeness if completion_matches else "unknown"
        attention = completion.attention if completion_matches else "none"
        pending_permissions = (
            self.store.list_permission_requests(
                item.work_item_id,
                attempt_id=latest_attempt.attempt_id,
                status="pending",
            )
            if latest_attempt is not None
            else []
        )
        latest_permission = pending_permissions[-1] if pending_permissions else None
        retry_authorizations = (
            [
                request
                for request in self.store.list_permission_requests(
                    item.work_item_id,
                    attempt_id=latest_attempt.attempt_id,
                )
                if request.status in {"denied", "expired"}
                and "allow_once" not in request.options
                and request.metadata.get("kind") == "provider_permission"
                and str(request.metadata.get("provider") or "").strip().lower()
                == latest_attempt.provider.strip().lower()
                and request.metadata.get("diagnostic_only") is True
                and request.metadata.get("retry_required") is True
            ]
            if latest_attempt is not None
            else []
        )
        retry_authorization = retry_authorizations[-1] if retry_authorizations else None
        recoverable_exports = (
            [
                request
                for request in self.store.list_permission_requests(
                    item.work_item_id,
                    attempt_id=latest_attempt.attempt_id,
                    status="allowed",
                )
                if self._is_desktop_export_permission(request)
                and self._can_resume_authorized_export(request)
            ]
            if latest_attempt is not None
            else []
        )
        recoverable_export = recoverable_exports[-1] if recoverable_exports else None
        has_workspace = item.workspace_mode != "none"
        workspace_exists = bool(has_workspace and Path(item.workspace_path).is_dir())
        if execution == "orphaned" or (has_workspace and not workspace_exists):
            attention = "error"
        elif pending_permissions:
            attention = "permission"
        elif recoverable_export is not None:
            attention = "conflict"
        if item.state == "accepted":
            attention = "none"
        attempt_metadata = (
            latest_attempt.metadata
            if latest_attempt and isinstance(latest_attempt.metadata, dict)
            else {}
        )
        provider_result = (
            attempt_metadata.get("provider_result")
            if isinstance(attempt_metadata.get("provider_result"), dict)
            else {}
        )
        session_id = str(
            attempt_metadata.get("session_id")
            or provider_result.get("session_id")
            or provider_result.get("sessionId")
            or ""
        ).strip()
        activity_snapshot = (
            attempt_metadata.get(ACTIVITY_METADATA_KEY)
            if isinstance(attempt_metadata.get(ACTIVITY_METADATA_KEY), dict)
            else {}
        )
        activity_fields = (
            activity_report_fields(
                activity_snapshot,
                execution_status=execution,
                created_at=latest_attempt.created_at,
                started_at=latest_attempt.started_at,
                finished_at=latest_attempt.finished_at,
                now=float(self._clock()),
            )
            if latest_attempt is not None
            else {}
        )
        activity_projection = {
            "phase": str(activity_fields.get("activity_phase") or "idle"),
            "elapsedSeconds": float(activity_fields.get("activity_elapsed_seconds") or 0.0),
            "silentSeconds": float(activity_fields.get("activity_silent_seconds") or 0.0),
            "lastEventAt": self._optional_iso(activity_fields.get("activity_last_event_at")),
            "lastProviderEventAt": self._optional_iso(
                activity_fields.get("activity_last_provider_event_at")
            ),
            "lastSemanticProgressAt": self._optional_iso(
                activity_fields.get("activity_last_semantic_progress_at")
            ),
            "lastDirectionalUpdateAt": self._optional_iso(
                activity_fields.get("activity_last_directional_update_at")
            ),
            "lastEventType": str(activity_fields.get("activity_last_event_type") or ""),
            "semanticSummary": str(activity_fields.get("activity_semantic_summary") or ""),
            "semanticSource": str(activity_fields.get("activity_semantic_source") or ""),
            "semanticVerified": activity_fields.get("activity_semantic_verified") is True,
            "semanticMilestone": str(activity_fields.get("activity_semantic_milestone") or ""),
            "directionSummary": str(activity_fields.get("activity_direction_summary") or ""),
            "directionSource": str(activity_fields.get("activity_direction_source") or ""),
            "milestones": dict(activity_fields.get("activity_milestones") or {}),
            "lastTool": str(activity_fields.get("activity_last_tool") or ""),
            "toolCount": int(activity_fields.get("activity_tool_count") or 0),
            "artifactCount": int(activity_fields.get("activity_artifact_count") or 0),
            "liveness": dict(activity_fields.get("activity_liveness") or {}),
            "steering": dict(activity_fields.get("activity_steering") or {}),
            "uncertainty": str(activity_fields.get("activity_uncertainty") or ""),
        }
        provider_liveness = (
            attempt_metadata.get("provider_liveness")
            if isinstance(attempt_metadata.get("provider_liveness"), dict)
            else {}
        )
        if execution in _ACTIVE_EXECUTION:
            liveness = str(provider_liveness.get("state") or "active")
        elif latest_attempt is not None:
            liveness = "terminal"
        else:
            liveness = "idle"
        try:
            silent_for_seconds = max(0.0, float(provider_liveness.get("silence_s") or 0.0))
        except (TypeError, ValueError):
            silent_for_seconds = 0.0
        if liveness not in {"stalled", "cancel_pending"}:
            silent_for_seconds = 0.0
        try:
            last_provider_event_at = float(
                provider_liveness.get("last_provider_event_at") or 0.0
            )
        except (TypeError, ValueError):
            last_provider_event_at = 0.0
        git_delta = (
            attempt_metadata.get("git_delta")
            if isinstance(attempt_metadata.get("git_delta"), dict)
            else {}
        )
        git_baseline = (
            attempt_metadata.get("git_baseline")
            if isinstance(attempt_metadata.get("git_baseline"), dict)
            else {}
        )
        git_branch = str(
            item.branch or git_delta.get("current_branch") or git_baseline.get("branch") or ""
        )
        base_revision = str(item.base_revision or git_baseline.get("head") or "")
        workspace_name = (
            Path(item.workspace_path).name or item.workspace_mode if has_workspace else ""
        )
        metadata = dict(item.metadata or {})
        is_scratch = bool(has_workspace and is_scratch_path(item.workspace_path))
        unkept_draft = bool(has_workspace and self._is_unkept_draft(item.workspace_path))
        project = self.store.get_project(item.project_id)
        project_name = (
            "" if unkept_draft or not has_workspace else str(project.name if project else "")
        )
        if not has_workspace:
            kind = "no workspace"
        elif is_scratch:
            kind = "draft" if unkept_draft else "draft · kept"
        elif item.workspace_mode == "worktree":
            kind = "worktree"
        else:
            kind = "main directory"
        workspace_label = (
            "No filesystem workspace"
            if not has_workspace
            else f"{kind} · {workspace_name}"
            + (f" · Git branch {git_branch}" if git_branch else "")
        )
        lease = self.store.get_writer_lease(latest_attempt.attempt_id) if latest_attempt else None
        workspace_policy = (
            metadata.get("workspace_policy")
            if isinstance(metadata.get("workspace_policy"), dict)
            else {}
        )
        artifacts = self.store.list_artifacts(item.work_item_id)
        business_artifact_count = sum(
            1 for artifact in artifacts if artifact.kind.startswith("business.")
        )
        return {
            "id": item.work_item_id,
            "workItemId": item.work_item_id,
            "projectId": item.project_id,
            "projectName": project_name,
            "projectState": (
                "" if unkept_draft or not has_workspace else str(project.state if project else "")
            ),
            "title": item.title,
            "goal": item.goal,
            "state": item.state,
            "execution": execution,
            "activity": activity_projection,
            "liveness": liveness,
            "livenessStage": str(provider_liveness.get("stage") or ""),
            "probeStatus": str(provider_liveness.get("probe_status") or ""),
            "silentForSeconds": silent_for_seconds,
            "lastProviderEventAt": (
                self._iso_time(last_provider_event_at) if last_provider_event_at > 0 else ""
            ),
            "completion": completeness,
            "attention": attention,
            "workspaceMode": item.workspace_mode,
            "workspacePath": item.workspace_path,
            "workspaceExists": workspace_exists,
            "workspaceLabel": workspace_label,
            "isScratch": is_scratch,
            "canPromoteToProject": unkept_draft,
            "canReopen": bool(
                item.state in {"accepted", "archived", "closed"}
                and (workspace_exists or not has_workspace)
                and execution not in _ACTIVE_EXECUTION
            ),
            "branch": git_branch,
            "baseRevision": base_revision,
            "updatedAt": self._iso_time(item.last_activity_at),
            "currentRunId": latest_attempt.provider_run_id if latest_attempt else "",
            "runId": latest_attempt.provider_run_id if latest_attempt else "",
            "attemptId": latest_attempt.attempt_id if latest_attempt else "",
            "attemptNumber": latest_attempt.attempt_number if latest_attempt else 0,
            # Existing Host-owned attempt identity, exposed only so UI views can
            # distinguish this conversation from older conversations. It is not
            # a new routing or lifecycle authority.
            "sessionId": session_id,
            "operationId": latest_operation.operation_id if latest_operation else "",
            "operationNumber": latest_operation.operation_number if latest_operation else 0,
            "operationIntent": latest_operation.intent if latest_operation else "",
            "operationCount": len(operations),
            "provider": (
                latest_attempt.provider
                if latest_attempt
                else str(metadata.get("provider") or "")
            ),
            "mode": latest_attempt.mode if latest_attempt else "",
            "canRetry": bool(
                latest_attempt
                and not pending_permissions
                and recoverable_export is None
                and execution in {"failed", "cancelled"}
                and item.state in {"open", "review_ready"}
            ),
            "retryAuthorizationRequestId": (
                retry_authorization.request_id if retry_authorization is not None else ""
            ),
            "canResume": bool(
                latest_attempt
                and not pending_permissions
                and recoverable_export is None
                and execution == "orphaned"
                and bool(latest_attempt.provider_run_id)
                and attempt_metadata.get("runtime_resumable") is True
                and item.state in {"open", "review_ready"}
            ),
            "artifactCount": business_artifact_count,
            "businessArtifactCount": business_artifact_count,
            "runtimeArtifactCount": len(artifacts) - business_artifact_count,
            "pendingPermissionCount": len(pending_permissions),
            "pendingPermissionRequestId": (
                latest_permission.request_id if latest_permission is not None else ""
            ),
            "recoverableExportRequestId": (
                recoverable_export.request_id if recoverable_export is not None else ""
            ),
            "completionRationale": completion.rationale if completion_matches else "",
            "workspacePolicy": workspace_policy,
            "isolation": (
                "single writer" if bool(workspace_policy.get("write_intent")) else "shared read-only"
            ),
            "selectionReason": str(
                workspace_policy.get("reason") or "Legacy workspace binding."
            ),
            "writerLeaseStatus": lease.status if lease is not None else "",
        }

    @classmethod
    def _optional_iso(cls, value: Any) -> str:
        try:
            timestamp = float(value or 0.0)
        except (TypeError, ValueError):
            return ""
        return cls._iso_time(timestamp) if timestamp > 0 else ""

    @staticmethod
    def _iso_time(timestamp: float) -> str:
        return datetime.fromtimestamp(
            float(timestamp), tz=timezone.utc
        ).isoformat(timespec="seconds")
