"""Durable Project, Draft, and Session destination ownership.

The service owns where otherwise-unplaced work goes and what a Session means
by its current Project or WorkItem.  It does not create WorkItems, select a
Provider, project UI rows, or interpret user language.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from agent_host.work_ledger_store import (
    WorkLedgerConflict,
    WorkLedgerNotFound,
    WorkLedgerStore,
)
from agent_host.work_ledger_types import ProjectRecord, canonicalize_path
from server.project_registry import cwd_in_project_registry
from server.scratch_workspace import (
    ScratchUnavailable,
    ensure_scratch_root,
    is_scratch_path,
    is_scratch_root,
)


logger = logging.getLogger(__name__)

WORKSPACE_ROUTING_SURFACE = "runtime.routing"
ACTIVE_EXECUTION = frozenset({"queued", "running"})
_MAX_SESSION_PROJECTS = 32
_MAX_PROJECT_ALIASES = 8


def _attr_is_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _iso_time(timestamp: float) -> str:
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).isoformat()


class WorkDestinationService:
    """Resolve and persist Provider-neutral work destinations."""

    def __init__(
        self,
        store: WorkLedgerStore,
        *,
        registry_check: Callable[[str], bool] | None = None,
        scratch_root_provider: Callable[[], Path] | None = None,
    ) -> None:
        self.store = store
        self._registry_check = registry_check or cwd_in_project_registry
        self._scratch_root_provider = scratch_root_provider or ensure_scratch_root
        self._session_projects: dict[str, str] = {}
        self._session_project_feedback: dict[str, dict[str, str]] = {}

    @staticmethod
    def identity_aliases(values: Iterable[Any], *, primary: str = "") -> list[str]:
        primary_key = " ".join(str(primary or "").split()).casefold()
        seen: set[str] = set()
        aliases: list[str] = []
        for value in values:
            text = " ".join(str(value or "").split())[:240].strip()
            key = text.casefold()
            if not text or key == primary_key or key in seen:
                continue
            seen.add(key)
            aliases.append(text)
            if len(aliases) >= _MAX_PROJECT_ALIASES:
                break
        return aliases

    @classmethod
    def stored_project_aliases(cls, project: ProjectRecord) -> list[str]:
        metadata = project.metadata if isinstance(project.metadata, dict) else {}
        values = metadata.get("semantic_aliases")
        if not isinstance(values, list):
            return []
        return cls.identity_aliases(values, primary=project.name)

    def workspace_routing_focus(self) -> dict[str, Any]:
        focus = self.store.get_focus(WORKSPACE_ROUTING_SURFACE)
        if focus is None or focus.mode != "pinned" or not focus.work_item_id:
            return {
                "mode": "auto",
                "workItemId": "",
                "projectId": "",
                "workspacePath": "",
            }
        item = self.store.get_work_item(focus.work_item_id)
        if item is None:
            return {
                "mode": "auto",
                "workItemId": "",
                "projectId": "",
                "workspacePath": "",
            }
        return {
            "mode": "pinned",
            "workItemId": item.work_item_id,
            "projectId": item.project_id,
            "workspacePath": item.workspace_path,
            "workspaceName": Path(item.workspace_path).name,
            "title": item.title,
        }

    def workspace_routing_context(self, *, limit: int = 8) -> dict[str, Any]:
        candidate_limit = max(1, int(limit))
        candidates: list[dict[str, Any]] = []
        candidate_count = 0
        for project in self.store.list_projects():
            path = project.canonical_path
            if is_scratch_root(path):
                continue
            if not Path(path).is_dir() or not self._registry_check(path):
                continue
            candidate_count += 1
            if len(candidates) < candidate_limit:
                candidates.append(
                    {
                        "projectId": project.project_id,
                        "projectName": project.name or Path(path).name,
                        "projectAliases": self.stored_project_aliases(project),
                        "workspacePath": path,
                    }
                )
        return {
            "focus": self.workspace_routing_focus(),
            "candidates": candidates,
            "candidateCount": candidate_count,
            "candidatesComplete": len(candidates) == candidate_count,
        }

    def resolve_workspace_route(
        self,
        attrs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        values = attrs if isinstance(attrs, dict) else {}
        pinned = self.workspace_routing_focus()
        if pinned.get("mode") == "pinned":
            return self.validated_workspace_route(
                str(pinned.get("workspacePath") or ""),
                project_id=str(pinned.get("projectId") or ""),
                source="workspace_pin",
            )

        project_id = str(
            values.get("project_id") or values.get("projectId") or ""
        ).strip()
        workspace_ref = str(
            values.get("workspace_ref") or values.get("workspaceRef") or ""
        ).strip()
        explicit_workspace = next(
            (
                str(values.get(key)).strip()
                for key in (
                    "cwd",
                    "workspace",
                    "workspace_path",
                    "project",
                    "project_dir",
                )
                if values.get(key) not in (None, "")
            ),
            "",
        )
        if workspace_ref:
            item = self.store.get_work_item(workspace_ref)
            if item is None:
                return {
                    "status": "invalid",
                    "reason": "unknown_workspace_ref",
                    "projectId": project_id,
                    "cwd": "",
                }
            if project_id and item.project_id != project_id:
                return {
                    "status": "invalid",
                    "reason": "workspace_project_mismatch",
                    "projectId": project_id,
                    "cwd": "",
                }
            route = self.validated_workspace_route(
                item.workspace_path,
                project_id=item.project_id,
                source="intent_workspace_ref",
            )
            if route.get("status") == "resolved":
                route["workItemId"] = item.work_item_id
                route["workspaceMode"] = item.workspace_mode
            return route

        if project_id:
            project = self.store.get_project(project_id)
            if project is None:
                return {
                    "status": "invalid",
                    "reason": "unknown_project_id",
                    "projectId": project_id,
                    "cwd": "",
                }
            if explicit_workspace:
                known_workspace_identities = {
                    canonicalize_path(project.canonical_path).identity_key,
                    *(
                        canonicalize_path(candidate.workspace_path).identity_key
                        for candidate in self.store.list_work_items(
                            project_id=project_id,
                            limit=1000,
                        )
                    ),
                }
                explicit_identity = canonicalize_path(explicit_workspace).identity_key
                if explicit_identity not in known_workspace_identities:
                    return {
                        "status": "invalid",
                        "reason": "workspace_project_mismatch",
                        "projectId": project_id,
                        "cwd": "",
                        "source": "intent_project",
                    }
            if not explicit_workspace:
                explicit_workspace = project.canonical_path
            return self.validated_workspace_route(
                explicit_workspace,
                project_id=project_id,
                source="intent_project",
            )

        if explicit_workspace:
            project = self.store.get_project_by_path(explicit_workspace)
            return self.validated_workspace_route(
                explicit_workspace,
                project_id=project.project_id if project is not None else "",
                source="intent_cwd",
            )

        if _attr_is_true(values.get("one_off") or values.get("oneOff")):
            return self.scratch_route()

        session_project = self.session_project(
            str(values.get("session_id") or values.get("sessionId") or "")
        )
        if session_project:
            project = self.store.get_project(session_project)
            if project is not None:
                return self.validated_workspace_route(
                    project.canonical_path,
                    project_id=project.project_id,
                    source="session_project",
                )
        return self.scratch_route()

    def available_project(self, project_id: str) -> ProjectRecord:
        project = self.store.get_project(project_id)
        if project is None:
            raise WorkLedgerNotFound(f"unknown project: {project_id}")
        if is_scratch_root(project.canonical_path):
            raise WorkLedgerConflict("the scratch container is not a project")
        if not Path(project.canonical_path).is_dir():
            raise WorkLedgerConflict(
                f"project workspace no longer exists: {project.canonical_path}"
            )
        if not self._registry_check(project.canonical_path):
            raise WorkLedgerConflict(
                "project workspace is outside the trusted registry: "
                f"{project.canonical_path}"
            )
        return project

    def is_unkept_draft(self, workspace_path: str) -> bool:
        return bool(
            is_scratch_path(workspace_path)
            and Path(workspace_path).is_dir()
            and self.store.get_project_by_path(workspace_path) is None
        )

    def bind_session_context(
        self,
        session_id: str,
        project_id: str,
        *,
        work_item_id: str = "",
        source: str = "",
    ) -> dict[str, Any]:
        clean_session = str(session_id or "").strip()
        if not clean_session:
            raise WorkLedgerConflict("a session is required to choose a project")
        clean_work_item = str(work_item_id or "").strip()
        item = self.store.get_work_item(clean_work_item) if clean_work_item else None
        if clean_work_item and item is None:
            raise WorkLedgerNotFound(f"unknown work item: {clean_work_item}")
        project = None
        item_has_persistent_project = bool(
            item is not None
            and item.workspace_mode != "none"
            and not self.is_unkept_draft(item.workspace_path)
        )
        if item is None or item_has_persistent_project:
            project = self.available_project(
                item.project_id if item is not None else project_id
            )
            self.store.bind_conversation(
                clean_session,
                project.project_id,
                anchor_work_item_id=clean_work_item,
                metadata={"source": str(source or "").strip()} if source else None,
            )
            self._remember_session_project(clean_session, project.project_id)
        if item is not None:
            active = self.store.set_session_active_work_item(
                clean_session,
                item.work_item_id,
                metadata={"source": str(source or "context_binding")},
            )
        else:
            self.store.clear_session_active_work_item(clean_session)
            active = None
        self._session_project_feedback.pop(clean_session, None)
        binding_kind = "work_item" if item is not None else "project"
        project_name = (
            "Draft"
            if item is not None and not item_has_persistent_project
            else str(project.name if project is not None else "")
        )
        exposed_project_id = str(project.project_id if project is not None else "")
        logger.info(
            "[WORK-CONTEXT] session=%s kind=%s project=%s work_item=%s",
            clean_session,
            binding_kind,
            exposed_project_id,
            active.active_work_item_id if active is not None else "",
        )
        return {
            "sessionId": clean_session,
            "bindingKind": binding_kind,
            "projectId": exposed_project_id,
            "projectName": project_name,
            "workItemId": item.work_item_id if item is not None else "",
        }

    def conversation_binding(self, session_id: str) -> dict[str, Any] | None:
        clean_session = str(session_id or "").strip()
        binding = self.store.get_conversation_binding(clean_session)
        active_context = self.store.get_session_work_context(clean_session)
        if binding is None and active_context is None:
            return None
        default_project_id = (
            self.session_project(clean_session) if binding is not None else ""
        )
        default_project = (
            self.store.get_project(default_project_id) if default_project_id else None
        )
        item = (
            self.store.get_work_item(active_context.active_work_item_id)
            if active_context is not None and active_context.active_work_item_id
            else None
        )
        if active_context is not None and item is None:
            self.store.clear_session_active_work_item(clean_session)
            active_context = None
            if binding is None:
                return None
        item_is_draft = bool(
            item
            and (
                item.workspace_mode == "none"
                or self.is_unkept_draft(item.workspace_path)
            )
        )
        item_project = (
            self.store.get_project(item.project_id)
            if item is not None and not item_is_draft
            else None
        )
        exposed_project = item_project or default_project
        exposed_project_id = (
            ""
            if item_is_draft
            else str(exposed_project.project_id if exposed_project else "")
        )
        project_name = (
            "Draft"
            if item_is_draft
            else str(
                (exposed_project.name or Path(exposed_project.canonical_path).name)
                if exposed_project is not None
                else ""
            )
        )
        updated_at = max(
            float(binding.updated_at if binding is not None else 0.0),
            float(active_context.updated_at if active_context is not None else 0.0),
        )
        return {
            "sessionId": clean_session,
            "bindingKind": "work_item" if item is not None else "project",
            "projectId": exposed_project_id,
            "projectName": project_name,
            "defaultProjectId": default_project_id,
            "defaultProjectName": (
                default_project.name if default_project is not None else ""
            ),
            "workItemId": item.work_item_id if item is not None else "",
            "workItemTitle": item.title if item is not None else "",
            "canPromoteToProject": bool(
                item is not None
                and item.workspace_mode != "none"
                and self.is_unkept_draft(item.workspace_path)
            ),
            "updatedAt": _iso_time(updated_at),
        }

    def set_session_project(self, session_id: str, project_id: str) -> dict[str, Any]:
        clean_session = str(session_id or "").strip()
        if not clean_session:
            raise WorkLedgerConflict("a session is required to choose a project")
        chosen = self.bind_session_context(
            clean_session,
            project_id,
            source="focus",
        )
        logger.info(
            "[WORK-DESTINATION] session=%s now working in project=%s name=%s",
            clean_session,
            chosen["projectId"],
            chosen["projectName"],
        )
        return {
            "projectId": chosen["projectId"],
            "projectName": chosen["projectName"],
        }

    def _remember_session_project(self, session_id: str, project_id: str) -> None:
        if len(self._session_projects) >= _MAX_SESSION_PROJECTS:
            self._session_projects.pop(next(iter(self._session_projects)))
        self._session_projects.pop(session_id, None)
        self._session_projects[session_id] = project_id

    def session_project(self, session_id: str) -> str:
        clean_session = str(session_id or "").strip()
        if not clean_session:
            return ""
        project_id = self._session_projects.get(clean_session, "")
        if not project_id:
            binding = self.store.get_conversation_binding(clean_session)
            project_id = binding.project_id if binding is not None else ""
            if not project_id:
                return ""
            self._remember_session_project(clean_session, project_id)
        project = self.store.get_project(project_id)
        project_available = bool(
            project is not None
            and Path(project.canonical_path).is_dir()
            and self._registry_check(project.canonical_path)
        )
        if not project_available:
            self._session_projects.pop(clean_session, None)
            self.store.clear_conversation_binding(clean_session)
            self.set_session_project_feedback(
                clean_session,
                status="rejected",
                message=(
                    "The selected project folder is no longer available or trusted. "
                    "Future unnamed work will use Drafts."
                ),
            )
            return ""
        return project_id

    def clear_session_project(self, session_id: str) -> None:
        clean_session = str(session_id or "").strip()
        self._session_projects.pop(clean_session, None)
        self._session_project_feedback.pop(clean_session, None)
        self.store.clear_conversation_binding(clean_session)
        self.store.clear_session_active_work_item(clean_session)

    def set_session_project_feedback(
        self,
        session_id: str,
        *,
        status: str,
        message: str,
    ) -> None:
        clean_session = str(session_id or "").strip()
        clean_message = str(message or "").strip()
        if not clean_session or not clean_message:
            return
        if (
            clean_session not in self._session_project_feedback
            and len(self._session_project_feedback) >= _MAX_SESSION_PROJECTS
        ):
            self._session_project_feedback.pop(
                next(iter(self._session_project_feedback)),
                None,
            )
        self._session_project_feedback.pop(clean_session, None)
        self._session_project_feedback[clean_session] = {
            "status": str(status or "info").strip().lower() or "info",
            "message": clean_message,
        }

    def destination_feedback(self) -> dict[str, str] | None:
        try:
            from core import session_manager as sm

            session_id = str(sm.get_current_session_id() or "").strip()
        except Exception:
            return None
        feedback = self._session_project_feedback.get(session_id)
        return dict(feedback) if feedback else None

    def destination_project_id(self) -> str:
        try:
            from core import session_manager as sm

            return self.session_project(sm.get_current_session_id() or "")
        except Exception:
            return ""

    def destination_label(self) -> str:
        project_id = self.destination_project_id()
        if not project_id:
            return ""
        project = self.store.get_project(project_id)
        if project is None:
            return ""
        return project.name or Path(project.canonical_path).name

    def set_project_retired(self, project_id: str, *, retired: bool) -> dict[str, Any]:
        project = self.store.get_project(project_id)
        if project is None:
            raise WorkLedgerNotFound(f"unknown project: {project_id}")
        if is_scratch_root(project.canonical_path):
            raise WorkLedgerConflict("the scratch container is not a project")
        if retired and any(
            attempt.execution_status in ACTIVE_EXECUTION
            for item in self.store.list_work_items(project_id=project_id, limit=1000)
            for attempt in self.store.list_attempts(item.work_item_id)
        ):
            raise WorkLedgerConflict(
                f"project {project_id} still has work running; retire it once that finishes"
            )
        updated = self.store.set_project_state(
            project_id,
            "retired" if retired else "active",
        )
        logger.info(
            "[WORK-DESTINATION] project=%s state=%s name=%s",
            updated.project_id,
            updated.state,
            updated.name,
        )
        return {
            "projectId": updated.project_id,
            "projectName": updated.name,
            "state": updated.state,
        }

    def scratch_route(self) -> dict[str, Any]:
        try:
            root = self._scratch_root_provider()
        except ScratchUnavailable as exc:
            logger.warning("[WORK-DESTINATION] scratch unavailable: %s", exc)
            return {
                "status": "invalid",
                "reason": "scratch_unavailable",
                "cwd": "",
                "projectId": "",
                "source": "scratch_default",
            }
        project = self.store.create_or_get_project(
            str(root),
            name="scratch",
            metadata={"scratch": True},
        )
        return {
            "status": "resolved",
            "cwd": str(root),
            "projectId": project.project_id,
            "source": "scratch_default",
        }

    def validated_workspace_route(
        self,
        workspace_path: str,
        *,
        project_id: str,
        source: str,
    ) -> dict[str, Any]:
        try:
            resolved = str(Path(workspace_path).resolve())
        except (OSError, RuntimeError, ValueError):
            resolved = ""
        if not resolved or not Path(resolved).is_dir():
            return {
                "status": "invalid",
                "reason": "workspace_missing",
                "cwd": "",
                "projectId": project_id,
                "source": source,
            }
        if not self._registry_check(resolved):
            return {
                "status": "invalid",
                "reason": "workspace_outside_project_registry",
                "cwd": "",
                "projectId": project_id,
                "source": source,
            }
        return {
            "status": "resolved",
            "reason": "",
            "cwd": resolved,
            "projectId": project_id,
            "source": source,
        }
