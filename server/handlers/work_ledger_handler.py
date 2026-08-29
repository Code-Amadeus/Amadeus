"""WebSocket and Slice intent API for durable WorkItems."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_host.provider_types import ProviderPermissionResponse
from agent_host.work_ledger_store import WorkLedgerConflict, WorkLedgerNotFound
from server.auip_app_source import discover_registered_auip_app
from server.protocol import Method
from server.work_ledger_coordinator import DEFAULT_WORK_SURFACE, WorkLedgerCoordinator
from server.ws_handler import RequestHandler

logger = logging.getLogger("server")


class WorkLedgerHandler(RequestHandler):
    methods = [
        Method.WORK_LIST,
        Method.WORK_GET,
        Method.WORK_START,
        Method.WORK_FOCUS,
        Method.WORK_RETRY,
        Method.WORK_RESUME,
        Method.WORK_ACCEPT,
        Method.WORK_REOPEN,
        Method.WORK_PROMOTE,
        Method.WORK_PROJECT_STATE,
        Method.WORK_ARCHIVE,
        Method.WORK_PERMISSION_RESOLVE,
        Method.PROJECT_APPS_LIST,
        Method.DRAFT_APPS_LIST,
    ]

    def __init__(
        self,
        coordinator: WorkLedgerCoordinator,
        *,
        provider_run: Callable[[dict[str, Any]], Any] | None = None,
        provider_permission: Callable[[str, ProviderPermissionResponse], Any] | None = None,
        preview_open: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.coordinator = coordinator
        self._provider_run = provider_run
        self._provider_permission = provider_permission
        self._preview_open = preview_open

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if method == Method.WORK_LIST:
            return self._projection_response(self.coordinator.snapshot(surface=self._surface(params)))
        if method == Method.WORK_GET:
            work_item_id = self._work_item_id(params)
            detail = self.coordinator.detail(work_item_id)
            app = discover_registered_auip_app(self.coordinator.store, work_item_id)
            if app is not None:
                app_meta = app.get("app") if isinstance(app.get("app"), dict) else {}
                detail["auipApp"] = {
                    "artifactId": str(app.get("artifact_id") or ""),
                    "title": str(app_meta.get("title") or app.get("title") or "AUIP app"),
                    "version": str(app_meta.get("version") or ""),
                    "modes": [
                        "observe",
                        *(
                            ["collaborate", "delegate"]
                            if "participant" in (app.get("stances") or [])
                            else []
                        ),
                    ],
                }
            return {
                "item": detail,
                **self._projection_response(self.coordinator.snapshot(surface=self._surface(params))),
            }
        if method == Method.WORK_START:
            return await self._start(params)
        if method == Method.WORK_FOCUS:
            return await self._focus(params)
        if method == Method.WORK_RETRY:
            return await self._retry(params)
        if method == Method.WORK_RESUME:
            return await self._resume(params)
        if method == Method.WORK_ACCEPT:
            return await self._accept(params)
        if method == Method.WORK_REOPEN:
            return await self._reopen(params)
        if method == Method.WORK_PROMOTE:
            return await self._promote(params)
        if method == Method.WORK_PROJECT_STATE:
            return await self._set_project_state(params)
        if method == Method.WORK_ARCHIVE:
            return await self._archive(params)
        if method == Method.WORK_PERMISSION_RESOLVE:
            return await self._resolve_permission(params)
        if method == Method.PROJECT_APPS_LIST:
            try:
                return {
                    "ok": True,
                    **self.coordinator.project_apps(
                        str(params.get("project_id") or params.get("projectId") or ""),
                        limit=int(params.get("limit") or 100),
                    ),
                }
            except (ValueError, WorkLedgerConflict, WorkLedgerNotFound) as exc:
                return {"ok": False, "error": str(exc)}
        if method == Method.DRAFT_APPS_LIST:
            try:
                return {
                    "ok": True,
                    **self.coordinator.draft_apps(
                        limit=int(params.get("limit") or 5),
                    ),
                }
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
        return None

    async def route_action(self, payload: dict[str, Any] | None) -> dict[str, Any]:
        """Allowlisted view-state intents from the untrusted canvas surface."""
        data = payload or {}
        action = str(data.get("action") or "").strip().lower().replace("-", "_")
        revision = str(data.get("revision") or data.get("surface_revision") or "").strip()
        if revision:
            current = self.coordinator.snapshot(surface=DEFAULT_WORK_SURFACE)
            if revision != str(current.get("revision") or ""):
                return {
                    "ok": False,
                    "error": "stale_revision",
                    **self._projection_response(current),
                }
        target = str(data.get("target") or "").strip().lower().replace("-", "_")
        if target == "work_destination" and action == "exit_project":
            current = self.coordinator.snapshot(surface=DEFAULT_WORK_SURFACE)
            if not revision:
                return {
                    "ok": False,
                    "error": "missing_revision",
                    **self._projection_response(current),
                }
            from core import session_manager as sm

            session_id = str(sm.get_current_session_id() or "").strip()
            if not session_id:
                return {
                    "ok": False,
                    "error": "missing_session",
                    **self._projection_response(current),
                }
            self.coordinator.clear_session_project(session_id)
            snapshot = self.coordinator.snapshot(surface=DEFAULT_WORK_SURFACE)
            await self.coordinator.publish_snapshot(
                reason="canvas.exit_project",
                surface=DEFAULT_WORK_SURFACE,
            )
            return {
                "ok": True,
                "target": "work_destination",
                "action": action,
                **self._projection_response(snapshot),
            }
        if target == "permission" and action in {
            "allow",
            "allow_once",
            "approve_once",
            "deny",
            "reject",
            "retry_export",
            "abandon_export",
        }:
            return await self._resolve_permission(data, canvas_action=action)
        if target == "work_item" and action == "open_preview":
            if self._preview_open is None:
                return {
                    "ok": False,
                    "error": "work_preview_unavailable",
                    **self._projection_response(
                        self.coordinator.snapshot(surface=DEFAULT_WORK_SURFACE)
                    ),
                }
            result = self._preview_open(
                {
                    "work_item_id": str(
                        data.get("work_item_id") or data.get("workItemId") or ""
                    ),
                    "attempt_id": str(
                        data.get("attempt_id") or data.get("attemptId") or ""
                    ),
                    "revision": revision,
                }
            )
            if inspect.isawaitable(result):
                result = await result
            return result if isinstance(result, dict) else {"ok": True, "preview": result}
        if target == "work_item" and action in {"accept", "archive"}:
            current = self.coordinator.snapshot(surface=DEFAULT_WORK_SURFACE)

            def reject(error: str) -> dict[str, Any]:
                return {
                    "ok": False,
                    "error": error,
                    **self._projection_response(current),
                }

            if not revision:
                return reject("missing_revision")
            work_item_id = str(
                data.get("work_item_id") or data.get("workItemId") or ""
            ).strip()
            if not work_item_id:
                return reject("missing_work_item_id")
            projected = next(
                (
                    item
                    for item in current.get("items") or []
                    if isinstance(item, dict) and str(item.get("id") or "") == work_item_id
                ),
                None,
            )
            if projected is None:
                return reject("unknown_work_item")
            state = str(projected.get("state") or "").strip().lower()
            execution = str(projected.get("execution") or "").strip().lower()
            if action == "accept" and state not in {"review_ready", "accepted"}:
                return reject("work_action_not_available")
            if action == "archive" and (
                execution in {"queued", "running"}
                or state not in {"open", "review_ready", "archived"}
            ):
                return reject("work_action_not_available")
            snapshot = await self.coordinator.dispose_work_item(
                work_item_id,
                action=action,
                rationale=(
                    "User accepted the reviewed WorkItem from the Slice disposition menu."
                    if action == "accept"
                    else "User archived the WorkItem from the Slice disposition menu."
                ),
                surface=DEFAULT_WORK_SURFACE,
            )
            return {
                "ok": True,
                "target": "work_item",
                "action": action,
                **self._projection_response(snapshot),
            }
        if target == "work_item" and action == "reopen":
            current = self.coordinator.snapshot(surface=DEFAULT_WORK_SURFACE)

            def reject(error: str) -> dict[str, Any]:
                return {
                    "ok": False,
                    "error": error,
                    **self._projection_response(current),
                }

            if not revision:
                return reject("missing_revision")
            work_item_id = str(
                data.get("work_item_id") or data.get("workItemId") or ""
            ).strip()
            if not work_item_id:
                return reject("missing_work_item_id")
            projected = next(
                (
                    item
                    for item in current.get("items") or []
                    if isinstance(item, dict) and str(item.get("id") or "") == work_item_id
                ),
                None,
            )
            if projected is None:
                return reject("unknown_work_item")
            if projected.get("canReopen") is not True:
                return reject("work_action_not_available")
            try:
                await self._reopen({"work_item_id": work_item_id})
            except (WorkLedgerConflict, WorkLedgerNotFound) as exc:
                logger.warning("reopen refused: %s", exc)
                return reject("work_action_not_available")
            snapshot = self.coordinator.snapshot(surface=DEFAULT_WORK_SURFACE)
            await self.coordinator.publish_snapshot(
                reason="canvas.reopen", surface=DEFAULT_WORK_SURFACE
            )
            return {
                "ok": True,
                "target": "work_item",
                "action": action,
                **self._projection_response(snapshot),
            }
        if target == "work_item" and action == "promote_to_project":
            current = self.coordinator.snapshot(surface=DEFAULT_WORK_SURFACE)

            def reject(error: str) -> dict[str, Any]:
                return {
                    "ok": False,
                    "error": error,
                    **self._projection_response(current),
                }

            if not revision:
                return reject("missing_revision")
            work_item_id = str(
                data.get("work_item_id") or data.get("workItemId") or ""
            ).strip()
            if not work_item_id:
                return reject("missing_work_item_id")
            projected = next(
                (
                    item
                    for item in current.get("items") or []
                    if isinstance(item, dict) and str(item.get("id") or "") == work_item_id
                ),
                None,
            )
            if projected is None:
                return reject("unknown_work_item")
            # The projection is the single source of what the surface may do;
            # re-deriving the condition here would let the two drift apart.
            if projected.get("canPromoteToProject") is not True:
                return reject("work_action_not_available")
            try:
                promoted = self._promote_work_item(work_item_id)
            except (WorkLedgerConflict, WorkLedgerNotFound) as exc:
                logger.warning("promote to project refused: %s", exc)
                return reject("work_action_not_available")
            snapshot = self.coordinator.snapshot(surface=DEFAULT_WORK_SURFACE)
            await self.coordinator.publish_snapshot(
                reason="canvas.promote_to_project", surface=DEFAULT_WORK_SURFACE
            )
            return {
                "ok": True,
                "target": "work_item",
                "action": action,
                "promoted": promoted,
                **self._projection_response(snapshot),
            }
        if target == "work_item" and action in {"retry", "resume"}:
            current = self.coordinator.snapshot(surface=DEFAULT_WORK_SURFACE)

            def reject(error: str) -> dict[str, Any]:
                return {
                    "ok": False,
                    "error": error,
                    **self._projection_response(current),
                }

            if not revision:
                return reject("missing_revision")
            work_item_id = str(
                data.get("work_item_id") or data.get("workItemId") or ""
            ).strip()
            if not work_item_id:
                return reject("missing_work_item_id")
            if str(current.get("selectedWorkItemId") or "") != work_item_id:
                return reject("work_item_not_selected")
            selected = (
                current.get("selected")
                if isinstance(current.get("selected"), dict)
                else {}
            )
            attempt_id = str(
                data.get("attempt_id") or data.get("attemptId") or ""
            ).strip()
            if not attempt_id:
                return reject("missing_attempt_id")
            if str(selected.get("attemptId") or "") != attempt_id:
                return reject("work_attempt_not_current")
            capability = {
                "retry": "canRetry",
                "resume": "canResume",
            }[action]
            if selected.get(capability) is not True:
                return reject("work_action_not_available")
            result = (
                await self._resume(data)
                if action == "resume"
                else await self._retry(data)
            )
            return {
                "ok": True,
                "target": "work_item",
                "action": action,
                **result,
            }
        if action == "select":
            current = self.coordinator.snapshot(surface=DEFAULT_WORK_SURFACE)
            if not revision:
                return {
                    "ok": False,
                    "error": "missing_revision",
                    **self._projection_response(current),
                }
            work_item_id = self._work_item_id(data)
            snapshot = self.coordinator.select(work_item_id, surface=DEFAULT_WORK_SURFACE)
            await self.coordinator.publish_snapshot(reason="canvas.select", surface=DEFAULT_WORK_SURFACE)
            return {"ok": True, "target": "work_item", "action": action, **self._projection_response(snapshot)}
        if action == "set_focus":
            current = self.coordinator.snapshot(surface=DEFAULT_WORK_SURFACE)
            if not revision:
                return {
                    "ok": False,
                    "error": "missing_revision",
                    **self._projection_response(current),
                }
            mode = str(data.get("focus_mode") or data.get("focusMode") or "auto")
            work_item_id = str(data.get("work_item_id") or data.get("workItemId") or "").strip()
            if mode.strip().lower() == "pinned":
                if not work_item_id:
                    return {
                        "ok": False,
                        "error": "missing_work_item_id",
                        **self._projection_response(current),
                    }
                if str(current.get("selectedWorkItemId") or "") != work_item_id:
                    return {
                        "ok": False,
                        "error": "work_item_not_selected",
                        **self._projection_response(current),
                    }
            snapshot = self.coordinator.set_focus(
                mode=mode,
                work_item_id=work_item_id,
                surface=DEFAULT_WORK_SURFACE,
            )
            await self.coordinator.publish_snapshot(reason="canvas.set_focus", surface=DEFAULT_WORK_SURFACE)
            return {"ok": True, "target": "work_item", "action": action, **self._projection_response(snapshot)}
        return {"ok": False, "error": "unsupported_action"}

    async def _resolve_permission(
        self,
        params: dict[str, Any],
        *,
        canvas_action: str = "",
    ) -> dict[str, Any]:
        current = self.coordinator.snapshot(surface=DEFAULT_WORK_SURFACE)

        def reject(error: str) -> dict[str, Any]:
            return {
                "ok": False,
                "error": error,
                **self._projection_response(current),
            }

        request_id = str(
            params.get("permission_request_id")
            or params.get("permissionRequestId")
            or params.get("request_id")
            or ""
        ).strip()
        if not request_id:
            return reject("missing_permission_request_id")
        work_item_id = str(
            params.get("work_item_id") or params.get("workItemId") or ""
        ).strip()
        if not work_item_id:
            return reject("missing_work_item_id")
        attempt_id = str(
            params.get("attempt_id") or params.get("attemptId") or ""
        ).strip()
        if not attempt_id:
            return reject("missing_attempt_id")
        revision = str(
            params.get("revision") or params.get("surface_revision") or ""
        ).strip()
        if not revision:
            return reject("missing_revision")
        if revision != str(current.get("revision") or ""):
            return reject("stale_revision")
        if str(current.get("selectedWorkItemId") or "") != work_item_id:
            return reject("permission_work_item_not_selected")
        selected = current.get("selected") if isinstance(current.get("selected"), dict) else {}
        if str(selected.get("attemptId") or "") != attempt_id:
            return reject("permission_attempt_not_selected")
        action = str(
            canvas_action
            or params.get("action")
            or params.get("decision")
            or params.get("status")
            or ""
        ).strip().lower().replace("-", "_")
        if not action and isinstance(params.get("allow"), bool):
            action = "allow_once" if params.get("allow") else "deny"
        if action not in {
            "allow",
            "allow_once",
            "approve_once",
            "deny",
            "reject",
            "retry_export",
            "abandon_export",
        }:
            return reject("invalid_permission_decision")
        current_request_field = (
            "recoverableExportRequestId"
            if action in {"retry_export", "abandon_export"}
            else "pendingPermissionRequestId"
        )
        if str(selected.get(current_request_field) or "") != request_id:
            return reject(
                "export_recovery_not_current"
                if action in {"retry_export", "abandon_export"}
                else "permission_request_not_current"
            )

        request = self.coordinator.store.get_permission_request(request_id)
        if request is None:
            return reject("permission_request_not_found")
        if request.work_item_id != work_item_id:
            return reject("permission_work_item_mismatch")
        if request.attempt_id != attempt_id:
            return reject("permission_attempt_mismatch")

        # The immutable ledger request is the authority contract.  Renderer
        # input may select only an option that contract actually offered;
        # otherwise a forged Canvas/WS action could turn a deny-only request
        # into an approval.  Deny remains available as a fail-safe even for a
        # malformed legacy request with no options.
        if (
            action in {"allow", "allow_once", "approve_once"}
            and "allow_once" not in request.options
        ):
            return reject("permission_option_not_allowed")

        provider_permission = (
            request.metadata.get("kind") == "provider_permission"
            and request.metadata.get("diagnostic_only") is not True
            and request.metadata.get("host_seeded") is not True
        )
        if provider_permission:
            if self._provider_permission is None:
                return reject("provider_permission_runtime_unavailable")
            run_id = str(request.metadata.get("provider_run_id") or "").strip()
            if not run_id:
                return reject("provider_permission_run_missing")
            outcome = self._provider_permission(
                run_id,
                ProviderPermissionResponse(
                    request_id=str(request.metadata.get("provider_request_id") or ""),
                    allow=action in {"allow", "allow_once", "approve_once"},
                ),
            )
            if inspect.isawaitable(outcome):
                outcome = await outcome
            if not isinstance(outcome, dict) or outcome.get("accepted") is not True:
                return reject(
                    str(
                        (outcome or {}).get("reason")
                        if isinstance(outcome, dict)
                        else "provider_permission_rejected"
                    )
                    or "provider_permission_rejected"
                )

        if action == "retry_export":
            result = await self.coordinator.resume_export(
                request_id,
                work_item_id=work_item_id,
                attempt_id=attempt_id,
            )
        elif action == "abandon_export":
            result = await self.coordinator.abandon_export(
                request_id,
                work_item_id=work_item_id,
                attempt_id=attempt_id,
            )
        else:
            result = await self.coordinator.resolve_permission(
                request_id,
                allow=action in {"allow", "allow_once", "approve_once"},
                work_item_id=work_item_id,
                attempt_id=attempt_id,
            )
        snapshot = result.get("work") if isinstance(result.get("work"), dict) else self.coordinator.snapshot()
        return {
            "ok": True,
            "target": "permission",
            "action": (
                action
                if action in {"retry_export", "abandon_export"}
                else (
                    "allow_once"
                    if action in {"allow", "allow_once", "approve_once"}
                    else "deny"
                )
            ),
            "permission": result.get("permission"),
            "exportedPaths": list(result.get("exportedPaths") or []),
            **self._projection_response(snapshot),
        }

    async def _focus(self, params: dict[str, Any]) -> dict[str, Any]:
        surface = self._surface(params)
        work_item_id = str(params.get("work_item_id") or params.get("workItemId") or "").strip()
        focus_mode = str(params.get("focus_mode") or params.get("focusMode") or "").strip()
        if focus_mode:
            snapshot = self.coordinator.set_focus(
                mode=focus_mode,
                work_item_id=work_item_id,
                surface=surface,
            )
        else:
            if not work_item_id:
                raise ValueError("work_item_id or focus_mode is required")
            snapshot = self.coordinator.select(work_item_id, surface=surface)
        await self.coordinator.publish_snapshot(reason="work.focus", surface=surface)
        return self._projection_response(snapshot)

    async def _start(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._provider_run is None:
            raise RuntimeError("provider runtime is unavailable")
        provider = str(params.get("provider") or "").strip().lower()
        task = str(params.get("task") or "").strip()
        if not provider or not task:
            raise ValueError("provider and task are required")
        request = dict(params)
        for key in (
            "work_item_id",
            "workItemId",
            "attempt_id",
            "attemptId",
            "resume",
        ):
            request.pop(key, None)
        metadata = dict(request.get("metadata")) if isinstance(request.get("metadata"), dict) else {}
        raw_work = metadata.get("work") if isinstance(metadata.get("work"), dict) else {}
        requested_work = {
            key: raw_work[key]
            for key in (
                "project_id",
                "projectId",
                "workspace_mode",
                "workspace_path",
            )
            if raw_work.get(key) not in (None, "")
        }
        for key in (
            "work_item_id",
            "workItemId",
            "attempt_id",
            "attemptId",
            "previous_attempt_id",
            "retry_of",
            "continued_from",
        ):
            metadata.pop(key, None)
        metadata["continuation"] = "new"
        metadata["work"] = requested_work
        metadata["work_surface"] = self._surface(params)
        request["metadata"] = metadata
        result = self._provider_run(request)
        if inspect.isawaitable(result):
            result = await result
        snapshot = self.coordinator.snapshot(surface=self._surface(params))
        response = dict(result) if isinstance(result, dict) else {"result": result}
        response.update(self._projection_response(snapshot))
        return response

    async def _retry(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._provider_run is None:
            raise RuntimeError("provider runtime is unavailable")
        work_item_id = self._work_item_id(params)
        item = self.coordinator.store.get_work_item(work_item_id)
        if item is None:
            raise WorkLedgerNotFound(f"unknown work item: {work_item_id}")
        if item.state in {"accepted", "archived"}:
            raise WorkLedgerConflict(f"work item {work_item_id} must be reopened before Retry")
        attempts = self.coordinator.store.list_attempts(work_item_id)
        latest = attempts[-1] if attempts else None
        if latest is None:
            raise WorkLedgerConflict(f"work item {work_item_id} has no provider attempt to Retry")
        if self.coordinator.store.list_permission_requests(
            work_item_id,
            attempt_id=latest.attempt_id,
            status="pending",
        ):
            raise WorkLedgerConflict("resolve the pending permission before Retry")
        if latest.execution_status in {"queued", "running"}:
            raise WorkLedgerConflict(f"work item {work_item_id} already has an active attempt")
        if latest.execution_status not in {"failed", "cancelled"}:
            raise WorkLedgerConflict("Retry is only valid after a failed or cancelled attempt")

        supplied_provider = str(params.get("provider") or "").strip().lower()
        supplied_mode = str(params.get("mode") or "").strip().lower()
        if supplied_provider and supplied_provider != latest.provider:
            raise WorkLedgerConflict(
                "Retry cannot change provider; submit changed intent as a new WorkItem"
            )
        if supplied_mode and supplied_mode != latest.mode:
            raise WorkLedgerConflict(
                "Retry cannot change mode; submit changed intent as a new WorkItem"
            )
        provider = latest.provider
        mode = latest.mode or "agent"
        handoff = self._checkpoint_handoff(item, latest)
        supplied_task = str(params.get("task") or "").strip()
        if supplied_task and supplied_task != latest.task:
            raise WorkLedgerConflict(
                "Retry replays the same instruction; submit changed intent as a new WorkItem"
            )
        amendment_text = params.get("amendment_text")
        if amendment_text is None and "amendmentText" in params:
            amendment_text = params.get("amendmentText")
        authorization_request_id = params.get("authorization_permission_request_id")
        if authorization_request_id is None and "authorizationPermissionRequestId" in params:
            authorization_request_id = params.get("authorizationPermissionRequestId")
        task, amendment_lineage = self.coordinator.retry_instruction(
            item,
            latest,
            amendment_text,
            authorization_request_id,
        )
        metadata_raw = params.get("metadata")
        metadata = dict(metadata_raw) if isinstance(metadata_raw, dict) else {}
        metadata.update(
            {
                "source": "work.retry",
                "continuation": "retry",
                "checkpoint_handoff": handoff,
                "retry_of": latest.attempt_id,
                "work_surface": self._surface(params),
                **amendment_lineage,
                "work": {
                    "project_id": item.project_id,
                    "work_item_id": item.work_item_id,
                    "workspace_mode": item.workspace_mode,
                    "workspace_path": item.workspace_path,
                    "previous_attempt_id": latest.attempt_id,
                },
            }
        )
        result = self._provider_run(
            {
                "provider": provider,
                "task": task,
                "cwd": item.workspace_path,
                "mode": mode,
                "metadata": metadata,
            }
        )
        if inspect.isawaitable(result):
            result = await result
        snapshot = self.coordinator.snapshot(surface=self._surface(params))
        response = dict(result) if isinstance(result, dict) else {"result": result}
        response.update(self._projection_response(snapshot))
        return response

    async def _resume(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._provider_run is None:
            raise RuntimeError("provider runtime is unavailable")
        work_item_id = self._work_item_id(params)
        item = self.coordinator.store.get_work_item(work_item_id)
        if item is None:
            raise WorkLedgerNotFound(f"unknown work item: {work_item_id}")
        if item.state in {"accepted", "archived"}:
            raise WorkLedgerConflict(f"work item {work_item_id} must be reopened before Resume")
        attempts = self.coordinator.store.list_attempts(work_item_id)
        attempt = attempts[-1] if attempts else None
        if attempt is None or attempt.execution_status != "orphaned":
            raise WorkLedgerConflict("Resume is only valid for an orphaned provider attempt")
        if not attempt.provider_run_id:
            raise WorkLedgerConflict("orphaned attempt has no resumable provider run id")
        if attempt.metadata.get("runtime_resumable") is not True:
            raise WorkLedgerConflict(
                "the interrupted provider checkpoint is unavailable; start a new WorkItem"
            )
        if self.coordinator.store.list_permission_requests(
            work_item_id,
            attempt_id=attempt.attempt_id,
            status="pending",
        ):
            raise WorkLedgerConflict("resolve the pending permission before Resume")
        if any(
            self.coordinator._is_desktop_export_permission(permission)
            and self.coordinator.export_service.can_resume_authorized(permission)
            for permission in self.coordinator.store.list_permission_requests(
                work_item_id,
                attempt_id=attempt.attempt_id,
                status="allowed",
            )
        ):
            raise WorkLedgerConflict(
                "recover or abandon the authorized Desktop export before Resume"
            )
        supplied_task = str(params.get("task") or "").strip()
        if supplied_task and supplied_task != attempt.task:
            raise WorkLedgerConflict(
                "Resume restores the interrupted run; submit changed intent as a new WorkItem"
            )
        resume_writer_mode = attempt.mode in {"agent", "delegate", "edit", "write", "execute"}
        acquired_resume_lease = False
        if resume_writer_mode:
            lease = self.coordinator.store.get_writer_lease(attempt.attempt_id)
            if lease is None or lease.status != "active":
                self.coordinator.store.acquire_writer_lease(
                    item.work_item_id,
                    attempt.attempt_id,
                    workspace_path=item.workspace_path,
                    metadata={"recovered_by": "work.resume"},
                )
                acquired_resume_lease = True
        metadata_raw = params.get("metadata")
        metadata = dict(metadata_raw) if isinstance(metadata_raw, dict) else {}
        metadata.update(
            {
                "source": "work.resume",
                "work_surface": self._surface(params),
                "work": {
                    "project_id": item.project_id,
                    "work_item_id": item.work_item_id,
                    "attempt_id": attempt.attempt_id,
                },
            }
        )
        try:
            result = self._provider_run(
                {
                    "provider": attempt.provider,
                    "task": attempt.task,
                    "cwd": item.workspace_path,
                    "mode": attempt.mode,
                    "metadata": metadata,
                    "resume": attempt.provider_run_id,
                }
            )
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            if acquired_resume_lease:
                self.coordinator.store.release_writer_lease(
                    attempt.attempt_id,
                    status="stale",
                    metadata={"resume_failed": "provider_checkpoint_unavailable"},
                )
            self.coordinator.store.update_attempt(
                attempt.attempt_id,
                metadata={
                    "runtime_resumable": False,
                    "resume_failed": "provider_checkpoint_unavailable",
                },
            )
            await self.coordinator.publish_snapshot(
                reason="work.resume.failed",
                surface=self._surface(params),
            )
            raise
        snapshot = self.coordinator.snapshot(surface=self._surface(params))
        response = dict(result) if isinstance(result, dict) else {"result": result}
        response.update(self._projection_response(snapshot))
        return response

    async def _accept(self, params: dict[str, Any]) -> dict[str, Any]:
        work_item_id = self._work_item_id(params)
        snapshot = await self.coordinator.dispose_work_item(
            work_item_id,
            action="accept",
            rationale=str(params.get("reason") or "User accepted the reviewed work item."),
            surface=self._surface(params),
        )
        return self._projection_response(snapshot)

    async def _reopen(self, params: dict[str, Any]) -> dict[str, Any]:
        work_item_id = self._work_item_id(params)
        self._require_no_active_attempt(work_item_id)
        item = self.coordinator.store.get_work_item(work_item_id)
        if item is None:
            raise WorkLedgerNotFound(f"unknown work item: {work_item_id}")
        if not Path(item.workspace_path).is_dir():
            raise WorkLedgerConflict(
                f"workspace is missing and requires recovery: {item.workspace_path}"
            )
        if item.state != "open":
            self.coordinator.store.set_work_item_state(work_item_id, "open")
        self.coordinator.store.update_work_item_metadata(
            work_item_id,
            {
                "last_reopen": {
                    "reason": str(params.get("reason") or "User reopened the work item."),
                }
            },
            touch_activity=True,
        )
        snapshot = await self.coordinator.publish_snapshot(
            reason="work.reopen",
            surface=self._surface(params),
        )
        return self._projection_response(snapshot)

    async def _archive(self, params: dict[str, Any]) -> dict[str, Any]:
        work_item_id = self._work_item_id(params)
        snapshot = await self.coordinator.dispose_work_item(
            work_item_id,
            action="archive",
            rationale=str(params.get("reason") or "User archived the work item."),
            surface=self._surface(params),
        )
        return self._projection_response(snapshot)

    async def _promote(self, params: dict[str, Any]) -> dict[str, Any]:
        """Keep a draft as a project. Same act as the Slice menu item.

        Electron showed whether a task was a draft but had no way to keep one,
        so the decision was reachable from one surface only.
        """

        work_item_id = self._work_item_id(params)
        promoted = self._promote_work_item(work_item_id)
        surface = self._surface(params)
        await self.coordinator.publish_snapshot(reason="work.promote", surface=surface)
        return {
            "promoted": promoted,
            **self._projection_response(self.coordinator.snapshot(surface=surface)),
        }

    def _promote_work_item(self, work_item_id: str) -> dict[str, Any]:
        """Promote a draft and keep its originating conversation attached."""

        from core import session_manager as sm

        session_id = str(sm.get_current_session_id() or "").strip()
        previous_context = (
            self.coordinator.conversation_binding(session_id) if session_id else None
        )
        selected_item = self.coordinator.store.get_work_item(work_item_id)
        previous_work_item_id = str(
            (previous_context or {}).get("workItemId") or ""
        ).strip()
        previous_item = (
            self.coordinator.store.get_work_item(previous_work_item_id)
            if previous_work_item_id
            else None
        )
        same_draft_workspace = bool(
            selected_item is not None
            and previous_item is not None
            and selected_item.workspace_path
            and previous_item.workspace_path
            and Path(selected_item.workspace_path).resolve()
            == Path(previous_item.workspace_path).resolve()
        )
        selected_session_match = any(
            str(
                (attempt.metadata or {}).get("session_id")
                or (attempt.metadata or {}).get("sessionId")
                or (attempt.metadata or {}).get("chat_session_id")
                or ""
            ).strip()
            == session_id
            for attempt in self.coordinator.store.list_attempts(work_item_id)
        )
        promoted = self.coordinator.promote_work_item_to_project(work_item_id)
        if (
            session_id
            and not str((previous_context or {}).get("projectId") or "")
            and (
                previous_work_item_id == work_item_id
                or same_draft_workspace
                or selected_session_match
            )
        ):
            self.coordinator.bind_session_context(
                session_id,
                str(promoted["projectId"]),
                work_item_id=(
                    previous_work_item_id
                    if previous_work_item_id == work_item_id or same_draft_workspace
                    else work_item_id
                ),
                source="work.promote",
            )
        return promoted

    async def _set_project_state(self, params: dict[str, Any]) -> dict[str, Any]:
        """Retire a project, or put it back on the menu."""

        project_id = str(
            params.get("project_id") or params.get("projectId") or ""
        ).strip()
        if not project_id:
            raise WorkLedgerConflict("a project id is required")
        retired = bool(params.get("retired"))
        result = self.coordinator.set_project_retired(project_id, retired=retired)
        surface = self._surface(params)
        await self.coordinator.publish_snapshot(
            reason="work.project.state", surface=surface
        )
        return {
            "project": result,
            **self._projection_response(self.coordinator.snapshot(surface=surface)),
        }

    def _require_no_active_attempt(self, work_item_id: str) -> None:
        attempts = self.coordinator.store.list_attempts(work_item_id)
        if any(attempt.execution_status in {"queued", "running"} for attempt in attempts):
            raise WorkLedgerConflict(f"work item {work_item_id} still has an active attempt")

    def _checkpoint_handoff(self, item, attempt) -> dict[str, Any]:
        completion = self.coordinator.store.latest_completion(item.work_item_id)
        artifacts = self.coordinator.store.list_artifacts(
            item.work_item_id,
            attempt_id=attempt.attempt_id,
        )
        artifact_summary = [
            {
                "artifact_id": artifact.artifact_id,
                "kind": artifact.kind,
                "title": artifact.title,
                "status": artifact.status,
                "location": artifact.location,
            }
            for artifact in artifacts[:24]
        ]
        git_delta = (
            attempt.metadata.get("git_delta")
            if isinstance(attempt.metadata.get("git_delta"), dict)
            else {}
        )
        return {
            "version": 1,
            "goal": item.goal or item.title,
            "workspace": {
                "mode": item.workspace_mode,
                "path": item.workspace_path,
                "branch": item.branch,
                "base_revision": item.base_revision,
            },
            "previous_attempt": {
                "attempt_id": attempt.attempt_id,
                "attempt_number": attempt.attempt_number,
                "provider_run_id": attempt.provider_run_id,
                "execution_status": attempt.execution_status,
                "result_summary": " ".join(str(attempt.result or "").split())[:1200],
            },
            "unresolved": {
                "work_item_state": item.state,
                "completeness": completion.completeness if completion is not None else "unknown",
                "attention": completion.attention if completion is not None else "none",
                "rationale": completion.rationale if completion is not None else "",
            },
            "artifacts": artifact_summary,
            "git_delta": {
                "available": bool(git_delta.get("available")),
                "changed_files": [str(value) for value in (git_delta.get("changed_files") or [])[:80]],
                "conflicts": [str(value) for value in (git_delta.get("conflicts") or [])[:20]],
            },
        }

    @staticmethod
    def _surface(params: dict[str, Any]) -> str:
        return str(params.get("surface") or DEFAULT_WORK_SURFACE).strip() or DEFAULT_WORK_SURFACE

    @staticmethod
    def _work_item_id(params: dict[str, Any]) -> str:
        value = str(params.get("work_item_id") or params.get("workItemId") or "").strip()
        if not value:
            raise ValueError("work_item_id is required")
        return value

    @staticmethod
    def _projection_response(snapshot: dict[str, Any]) -> dict[str, Any]:
        # ``projection`` is a short-lived compatibility alias for the first
        # Electron task-rail cut; ``work`` is the canonical API field.
        return {"work": snapshot, "projection": snapshot}
