"""Host-owned live preview sessions for static web WorkItems.

The trusted Slice may identify one durable WorkItem and its currently projected
attempt.  It never supplies a filesystem path, URL, port, or command.  This
module resolves those facts from the Work Ledger, discovers one bounded static
HTML entry point, and serves only that entry's web root on a random loopback
port.

V0 is intentionally static-only.  It does not inspect package scripts, start a
development server, or infer a command from Provider output.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import mimetypes
import os
import secrets
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_host.work_ledger_store import WorkLedgerConflict, WorkLedgerStore
from agent_host.work_ledger_types import canonicalize_path
from server.event_bus import bus
from server.protocol import Method
from server.work_export_service import WorkExportService
from server.ws_handler import RequestHandler

logger = logging.getLogger("server")


_IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".cache",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "cache",
        "node_modules",
        "venv",
    }
)
_SENSITIVE_FILE_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ed25519",
        "id_rsa",
        "known_hosts",
    }
)
_SENSITIVE_SUFFIXES = frozenset(
    {
        ".cer",
        ".crt",
        ".db",
        ".der",
        ".key",
        ".p12",
        ".pfx",
        ".pem",
        ".sqlite",
        ".sqlite3",
    }
)
_SERVABLE_SUFFIXES = frozenset(
    {
        ".aac",
        ".avif",
        ".bin",
        ".bmp",
        ".cjs",
        ".css",
        ".flac",
        ".frag",
        ".gif",
        ".glsl",
        ".htm",
        ".html",
        ".ico",
        ".jpeg",
        ".jpg",
        ".js",
        ".json",
        ".m4a",
        ".mjs",
        ".mp3",
        ".mp4",
        ".oga",
        ".ogg",
        ".ogv",
        ".otf",
        ".png",
        ".svg",
        ".ttf",
        ".txt",
        ".vert",
        ".wasm",
        ".wav",
        ".webm",
        ".webp",
        ".woff",
        ".woff2",
    }
)
_MAX_DISCOVERY_FILES = 5_000
_MAX_DISCOVERY_DIRECTORIES = 512
_MAX_DISCOVERY_DEPTH = 7
_MAX_REQUEST_HEAD_BYTES = 32 * 1024
_MAX_STATIC_FILE_BYTES = 128 * 1024 * 1024


class WorkPreviewError(RuntimeError):
    """A bounded preview failure with a renderer-safe error code."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = str(code or "preview_error")


@dataclass(frozen=True, slots=True)
class _Discovery:
    status: str
    entry: Path | None = None
    web_root: Path | None = None
    error: str = ""


@dataclass(slots=True)
class PreviewSession:
    preview_id: str
    work_item_id: str
    workspace_identity: str
    workspace_path: Path
    preview_root: Path
    title: str
    attempt_id: str
    attempt_generation: int
    status: str = "waiting"
    error: str = ""
    web_root: Path | None = None
    entry: Path | None = None
    server: asyncio.AbstractServer | None = None
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    port: int = 0
    revision: int = 1
    content_revision: int = 0
    content_signature: str = ""
    lifecycle: str = "live"
    attempt_execution: str = ""
    handoff_artifact_ref: str = ""
    handoff_work_item_id: str = ""
    handoff_attempt_id: str = ""
    handoff_host_surface_id: str = ""
    handoff_deadline: float = 0.0
    app_session_id: str = ""
    app_session_status: str = ""
    attached_artifact_ref: str = ""
    attached_attempt_id: str = ""
    attached_host_surface_id: str = ""
    watcher: asyncio.Task[None] | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.work_item_id, self.workspace_identity


EventPublisher = Callable[[str, dict[str, Any]], Awaitable[None]]


class WorkPreviewManager:
    """Own static preview discovery, serving, and refresh generations."""

    def __init__(
        self,
        store: WorkLedgerStore,
        *,
        export_service: WorkExportService | None = None,
        publisher: EventPublisher | None = None,
        poll_interval: float = 0.35,
        debounce: float = 0.35,
        handoff_timeout: float = 60.0,
    ) -> None:
        self.store = store
        self._export_service = export_service or WorkExportService(store)
        self._publisher = publisher or bus.emit
        self._poll_interval = max(0.03, float(poll_interval))
        self._debounce = max(0.0, float(debounce))
        self._handoff_timeout = max(0.05, float(handoff_timeout))
        self._sessions: dict[tuple[str, str], PreviewSession] = {}
        self._work_keys: dict[str, tuple[str, str]] = {}
        self._lock = asyncio.Lock()

    async def open(
        self,
        work_item_id: str,
        *,
        expected_attempt_id: str,
    ) -> dict[str, Any]:
        """Open or foreground one canonical WorkItem preview.

        ``expected_attempt_id`` is only a stale-UI guard.  Workspace facts and
        the actual latest attempt are always resolved from the ledger.
        """

        item, attempt = self._resolve_item(work_item_id)
        if not expected_attempt_id:
            raise WorkPreviewError("missing_attempt_id")
        if attempt is None or attempt.attempt_id != expected_attempt_id:
            raise WorkPreviewError("work_attempt_not_current")
        workspace = self._safe_workspace(item.workspace_path, item.workspace_identity)
        preview_root = self._resolve_preview_root(item, attempt, workspace)
        key = (item.work_item_id, item.workspace_identity)

        async with self._lock:
            old_key = self._work_keys.get(item.work_item_id)
            if old_key is not None and old_key != key:
                old = self._sessions.pop(old_key, None)
                if old is not None:
                    await self._stop_session(old, publish=False)
            session = self._sessions.get(key)
            created = session is None
            if session is None:
                session = PreviewSession(
                    preview_id=f"preview_{secrets.token_hex(12)}",
                    work_item_id=item.work_item_id,
                    workspace_identity=item.workspace_identity,
                    workspace_path=workspace,
                    preview_root=preview_root,
                    title=item.title,
                    attempt_id=attempt.attempt_id,
                    attempt_generation=attempt.attempt_number,
                    attempt_execution=attempt.execution_status,
                )
                self._sessions[key] = session
                self._work_keys[item.work_item_id] = key
                await self._refresh_discovery(session)
                await self._apply_lifecycle(
                    session,
                    self._desired_lifecycle(session, item, attempt),
                )
                session.watcher = asyncio.create_task(
                    self._watch(session),
                    name=f"work-preview:{item.work_item_id}",
                )
            elif (
                session.attempt_id != attempt.attempt_id
                or session.attempt_generation != attempt.attempt_number
            ):
                session.attempt_id = attempt.attempt_id
                session.attempt_generation = attempt.attempt_number
                session.attempt_execution = attempt.execution_status
                root_changed = session.preview_root != preview_root
                session.preview_root = preview_root
                self._clear_handoff(session)
                self._clear_auip_binding(session)
                session.revision += 1
                if root_changed:
                    await self._refresh_discovery(session)
                await self._apply_lifecycle(
                    session,
                    self._desired_lifecycle(session, item, attempt),
                )
                await self._publish_updated(session, reason="attempt_changed")
            elif session.preview_root != preview_root:
                session.preview_root = preview_root
                session.revision += 1
                await self._refresh_discovery(session)
                await self._publish_updated(session, reason="preview_root_changed")
            projection = self._projection(session)

        if created:
            await self._publish_updated(session, reason="opened")
        await self._publisher(
            Method.WORK_PREVIEW_OPEN_REQUESTED,
            {"preview": projection},
        )
        return projection

    async def get(self, work_item_id: str) -> dict[str, Any]:
        clean_id = str(work_item_id or "").strip()
        async with self._lock:
            key = self._work_keys.get(clean_id)
            session = self._sessions.get(key) if key is not None else None
            if session is None:
                return {"status": "closed", "workItemId": clean_id}
            return self._projection(session)

    async def close(self, work_item_id: str) -> dict[str, Any]:
        clean_id = str(work_item_id or "").strip()
        async with self._lock:
            key = self._work_keys.pop(clean_id, None)
            session = self._sessions.pop(key, None) if key is not None else None
            if session is None:
                return {"status": "closed", "workItemId": clean_id}
            had_url = self._has_content_url(session)
            await self._stop_session(session, publish=False)
            session.status = "closed"
            session.error = ""
            if had_url:
                session.content_revision += 1
            session.revision += 1
            projection = self._projection(session)
        await self._publisher(
            Method.WORK_PREVIEW_UPDATED,
            {"preview": projection, "reason": "closed"},
        )
        return projection

    async def close_all(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._work_keys.clear()
            for session in sessions:
                await self._stop_session(session, publish=False)

    async def begin_auip_handoff(self, source: dict[str, Any]) -> dict[str, Any]:
        """Pause one exact Preview only after trusted AUIP prepare succeeded."""

        work_item_id = str(source.get("work_item_id") or "").strip()
        attempt_id = str(source.get("attempt_id") or "").strip()
        artifact_ref = str(source.get("artifact_ref") or "").strip()
        host_surface_id = str(source.get("host_surface_id") or "").strip()
        if not work_item_id or not attempt_id or not artifact_ref or not host_surface_id:
            raise WorkPreviewError("invalid_auip_handoff_source")
        async with self._lock:
            key = self._work_keys.get(work_item_id)
            needs_surface = key is None or self._sessions.get(key) is None
        if needs_surface:
            # Attach may be the first reason an application needs a Host
            # surface. Reuse the ordinary Preview identity/open event instead
            # of creating a second AUIP-only window contract. ``open`` still
            # re-resolves the trusted ledger and rejects stale Attempts.
            await self.open(work_item_id, expected_attempt_id=attempt_id)
        publish: PreviewSession | None = None
        async with self._lock:
            key = self._work_keys.get(work_item_id)
            session = self._sessions.get(key) if key is not None else None
            if session is None:
                return {"status": "closed", "workItemId": work_item_id}
            if session.attempt_id != attempt_id:
                return self._projection(session)
            if (
                session.attached_attempt_id == attempt_id
                and session.app_session_status in {"active", "completed", "closing"}
            ):
                # A second prepare cannot replace an AppSession which the AUIP
                # runtime still considers live.  Preview presentation has no
                # authority to close or supersede that session.
                return self._projection(session)
            if session.handoff_artifact_ref:
                # Issued attach tickets cannot be revoked here.  Keep the
                # first exact pending surface until it registers or times out,
                # rather than allowing a later prepare to steal its handoff.
                return self._projection(session)
            self._clear_auip_binding(session)
            session.handoff_artifact_ref = artifact_ref
            session.handoff_work_item_id = work_item_id
            session.handoff_attempt_id = attempt_id
            session.handoff_host_surface_id = host_surface_id
            session.handoff_deadline = (
                asyncio.get_running_loop().time() + self._handoff_timeout
            )
            if await self._apply_lifecycle(session, "handoff"):
                publish = session
            projection = self._projection(session)
        if publish is not None:
            await self._publish_updated(publish, reason="auip_handoff")
        return projection

    async def on_auip_updated(
        self,
        method_or_payload: str | dict[str, Any],
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Track one AppSession only through its exact trusted AUIP identity."""

        data = method_or_payload if isinstance(method_or_payload, dict) else payload or {}
        status = str(data.get("status") or "").strip().lower()
        if status not in {"active", "completed", "closed", "disconnected"}:
            return
        artifact_ref = str(data.get("artifact_ref") or "").strip()
        app_session_id = str(data.get("app_session_id") or "").strip()
        host_surface_id = str(data.get("host_surface_id") or "").strip()
        surface_close_status = str(data.get("surface_close_status") or "").strip().lower()
        if not artifact_ref or not app_session_id or not host_surface_id:
            return
        changed: list[tuple[PreviewSession, str]] = []
        async with self._lock:
            for session in self._sessions.values():
                if status == "active":
                    if (
                        session.app_session_id == app_session_id
                        and session.attached_artifact_ref == artifact_ref
                        and session.attached_host_surface_id == host_surface_id
                        and session.attached_attempt_id == session.attempt_id
                    ):
                        # Do not let a delayed active update reopen a session
                        # whose later completed/closed state was already seen.
                        if session.app_session_status == "active":
                            continue
                        if session.app_session_status in {
                            "completed",
                            "closing",
                            "closed",
                            "disconnected",
                        }:
                            continue
                    if (
                        session.lifecycle != "handoff"
                        or session.handoff_artifact_ref != artifact_ref
                        or session.handoff_host_surface_id != host_surface_id
                        or session.handoff_work_item_id != session.work_item_id
                        or session.handoff_attempt_id != session.attempt_id
                    ):
                        continue
                    session.app_session_id = app_session_id
                    session.app_session_status = "active"
                    session.attached_artifact_ref = artifact_ref
                    session.attached_attempt_id = session.handoff_attempt_id
                    session.attached_host_surface_id = host_surface_id
                    self._clear_handoff(session)
                    if await self._apply_lifecycle(session, "attached"):
                        changed.append((session, "auip_attached"))
                    continue

                if (
                    session.app_session_id != app_session_id
                    or session.attached_artifact_ref != artifact_ref
                    or session.attached_host_surface_id != host_surface_id
                    or session.attached_attempt_id != session.attempt_id
                ):
                    continue
                previous_status = session.app_session_status
                if previous_status in {"closed", "disconnected"}:
                    continue
                if status == "completed":
                    session.app_session_status = status
                    # App completion is a terminal application fact, not surface
                    # closure.  The attached experience remains available.
                    if session.lifecycle != "attached":
                        if await self._apply_lifecycle(session, "attached"):
                            changed.append((session, "auip_completed"))
                    continue
                if status == "closed" and surface_close_status in {"pending", "failed"}:
                    # auip.leave closes protocol participation first, then the
                    # Electron surface acknowledges a separate close request.
                    # Keep the shell attached until that exact receipt arrives.
                    session.app_session_status = "closing"
                    if session.lifecycle != "attached":
                        if await self._apply_lifecycle(session, "attached"):
                            changed.append((session, "auip_surface_closing"))
                    continue
                session.app_session_status = status
                if await self._apply_lifecycle(session, "frozen"):
                    changed.append((session, f"auip_{status}"))
        for session, reason in changed:
            await self._publish_updated(session, reason=reason)

    def _resolve_item(self, work_item_id: str):
        clean_id = str(work_item_id or "").strip()
        if not clean_id:
            raise WorkPreviewError("missing_work_item_id")
        item = self.store.get_work_item(clean_id)
        if item is None:
            raise WorkPreviewError("unknown_work_item")
        attempts = self.store.list_attempts(item.work_item_id)
        return item, attempts[-1] if attempts else None

    def _resolve_preview_root(self, item: Any, attempt: Any, workspace: Path) -> Path:
        """Resolve one Host-owned authoring root for the current Attempt.

        Ordinary web work is previewed from its workspace. External exports are
        different: the Provider is deliberately confined to an Attempt-owned
        private staging directory until the user approves publication. That
        exact root is safe to preview only after the export service has
        revalidated its durable identity; arbitrary hidden workspace paths are
        never searched or served.
        """

        metadata = attempt.metadata if isinstance(attempt.metadata, dict) else {}
        plan = metadata.get("export_plan")
        staging_root = (
            str(plan.get("staging_root") or "").strip()
            if isinstance(plan, dict)
            else ""
        )
        if not staging_root:
            return workspace
        declared_root = self._declared_preview_root(attempt, workspace)
        assert isinstance(plan, dict)
        try:
            # This read-only boundary proves both the canonical
            # workspace/.amadeus/proposed_exports/<attempt> identity and that
            # no existing segment is a symlink or junction. It creates no
            # artifact, permission, or external side effect.
            self._export_service.observe_staged_files(attempt, item, plan)
        except (OSError, WorkLedgerConflict) as exc:
            raise WorkPreviewError("invalid_preview_staging_root") from exc
        return declared_root

    @staticmethod
    def _declared_preview_root(attempt: Any, workspace: Path) -> Path:
        metadata = attempt.metadata if isinstance(attempt.metadata, dict) else {}
        plan = metadata.get("export_plan")
        staging_root = (
            str(plan.get("staging_root") or "").strip()
            if isinstance(plan, dict)
            else ""
        )
        return Path(staging_root).resolve() if staging_root else workspace

    @staticmethod
    def _safe_workspace(path: str, expected_identity: str) -> Path:
        clean_path = str(path or "").strip()
        if not clean_path:
            raise WorkPreviewError("work_has_no_workspace")
        canonical = canonicalize_path(clean_path)
        if canonical.identity_key != str(expected_identity or ""):
            raise WorkPreviewError("workspace_identity_mismatch")
        workspace = Path(canonical.canonical_path)
        if not workspace.is_dir():
            raise WorkPreviewError("workspace_unavailable")
        anchor = Path(workspace.anchor).resolve()
        if workspace.resolve() == anchor:
            raise WorkPreviewError("workspace_scope_too_broad")
        try:
            is_home = workspace.resolve() == Path.home().resolve()
        except OSError:
            is_home = False
        if is_home:
            raise WorkPreviewError("workspace_scope_too_broad")
        return workspace.resolve()

    @staticmethod
    def _has_content_url(session: PreviewSession) -> bool:
        return bool(
            session.status == "ready"
            and session.server is not None
            and session.port
            and session.entry is not None
            and session.web_root is not None
        )

    @classmethod
    def _content_identity(cls, session: PreviewSession) -> tuple[Any, ...]:
        return (
            cls._has_content_url(session),
            session.port if cls._has_content_url(session) else 0,
            str(session.entry or ""),
            str(session.web_root or ""),
            session.content_signature,
        )

    @staticmethod
    def _clear_handoff(session: PreviewSession) -> None:
        session.handoff_artifact_ref = ""
        session.handoff_work_item_id = ""
        session.handoff_attempt_id = ""
        session.handoff_host_surface_id = ""
        session.handoff_deadline = 0.0

    @staticmethod
    def _clear_auip_binding(session: PreviewSession) -> None:
        session.app_session_id = ""
        session.app_session_status = ""
        session.attached_artifact_ref = ""
        session.attached_attempt_id = ""
        session.attached_host_surface_id = ""

    @staticmethod
    def _is_auip_authoring(attempt: Any) -> bool:
        metadata = attempt.metadata if isinstance(attempt.metadata, dict) else {}
        return bool(
            str(metadata.get("auip_authoring_skill_path") or "").strip()
            and str(metadata.get("auip_authoring_bundle_mode") or "").strip()
        )

    @staticmethod
    def _has_auip_manifest(session: PreviewSession) -> bool:
        roots = [session.preview_root, session.workspace_path]
        if session.web_root is not None and session.web_root not in roots:
            roots.append(session.web_root)
        return any((root / "auip.manifest.json").is_file() for root in roots)

    def _desired_lifecycle(
        self,
        session: PreviewSession,
        item: Any,
        attempt: Any,
    ) -> str:
        if session.attached_attempt_id == attempt.attempt_id:
            if session.app_session_status in {"active", "completed", "closing"}:
                # accepted/archive is a Work ledger fact, not authority to
                # close an active AUIP AppSession.  The upper coordinator must
                # request/observe AUIP closure; only that closure freezes this
                # exact binding.
                return "attached"
            if session.app_session_status in {"closed", "disconnected"}:
                return "frozen"
        if session.handoff_artifact_ref and session.handoff_attempt_id == attempt.attempt_id:
            return "handoff"
        if str(item.state or "").strip().lower() in {"accepted", "archived"}:
            return "frozen"
        execution = str(attempt.execution_status or "").strip().lower()
        active = execution in {"queued", "running"}
        if (
            active
            and self._is_auip_authoring(attempt)
            and self._has_auip_manifest(session)
        ):
            return "assembling"
        return "live" if active else "holding"

    async def _apply_lifecycle(
        self,
        session: PreviewSession,
        lifecycle: str,
    ) -> bool:
        if lifecycle not in {
            "live",
            "holding",
            "assembling",
            "handoff",
            "attached",
            "frozen",
        }:
            raise WorkPreviewError("invalid_preview_lifecycle", lifecycle)
        if lifecycle == session.lifecycle:
            return False
        previous_content = self._content_identity(session)
        previous_content_revision = session.content_revision
        if lifecycle in {"assembling", "handoff", "attached", "frozen"}:
            await self._stop_server(session)
        elif lifecycle in {"live", "holding"} and session.server is None:
            await self._refresh_discovery(session)
        if (
            previous_content != self._content_identity(session)
            and session.content_revision == previous_content_revision
        ):
            session.content_revision += 1
        session.lifecycle = lifecycle
        session.revision += 1
        return True

    async def _refresh_discovery(self, session: PreviewSession) -> None:
        previous_descriptor = (
            session.status,
            session.error,
            session.entry,
            session.web_root,
            session.port,
            session.content_signature,
        )
        previous_content = self._content_identity(session)
        discovery = await asyncio.to_thread(_discover_static_entry, session.preview_root)
        if discovery.status != "ready":
            await self._stop_server(session)
            session.status = discovery.status
            session.error = discovery.error
            session.entry = None
            session.web_root = None
            session.content_signature = ""
        else:
            assert discovery.entry is not None and discovery.web_root is not None
            root_changed = session.web_root != discovery.web_root
            session.entry = discovery.entry
            session.web_root = discovery.web_root
            session.status = "ready"
            session.error = ""
            if root_changed or session.server is None:
                await self._stop_server(session)
                session.server = await asyncio.start_server(
                    lambda reader, writer: self._serve_request(session, reader, writer),
                    host="127.0.0.1",
                    port=0,
                )
                sockets = session.server.sockets or []
                if not sockets:
                    raise WorkPreviewError("preview_server_unavailable")
                session.port = int(sockets[0].getsockname()[1])
            session.content_signature = await asyncio.to_thread(
                _content_signature,
                session.web_root,
            )
        current_descriptor = (
            session.status,
            session.error,
            session.entry,
            session.web_root,
            session.port,
            session.content_signature,
        )
        if previous_content != self._content_identity(session):
            session.content_revision += 1
        if previous_descriptor != current_descriptor:
            session.revision += 1

    async def _watch(self, session: PreviewSession) -> None:
        pending_signature = ""
        pending_since = 0.0
        loop = asyncio.get_running_loop()
        try:
            while True:
                await asyncio.sleep(self._poll_interval)
                publish_reason = ""
                async with self._lock:
                    if self._sessions.get(session.key) is not session:
                        return
                    item = self.store.get_work_item(session.work_item_id)
                    if item is None:
                        await self._set_error(session, "work_item_unavailable")
                        continue
                    if item.workspace_identity != session.workspace_identity:
                        await self._set_error(session, "workspace_identity_changed")
                        continue
                    attempts = self.store.list_attempts(session.work_item_id)
                    latest = attempts[-1] if attempts else None
                    if latest is None:
                        await self._set_error(session, "work_attempt_unavailable")
                        continue
                    previous_execution = session.attempt_execution
                    attempt_changed = (
                        latest.attempt_id != session.attempt_id
                        or latest.attempt_number != session.attempt_generation
                    )
                    root_declaration_changed = (
                        self._declared_preview_root(latest, session.workspace_path)
                        != session.preview_root
                    )
                    if attempt_changed or root_declaration_changed:
                        try:
                            preview_root = self._resolve_preview_root(
                                item,
                                latest,
                                session.workspace_path,
                            )
                        except WorkPreviewError as exc:
                            await self._set_error(session, exc.code)
                            continue
                        if attempt_changed:
                            session.attempt_id = latest.attempt_id
                            session.attempt_generation = latest.attempt_number
                            session.attempt_execution = latest.execution_status
                        root_changed = session.preview_root != preview_root
                        session.preview_root = preview_root
                        if attempt_changed:
                            self._clear_handoff(session)
                            self._clear_auip_binding(session)
                        session.revision += 1
                        if root_changed:
                            await self._refresh_discovery(session)
                        publish_reason = (
                            "attempt_changed"
                            if attempt_changed
                            else "preview_root_changed"
                        )
                    elif latest.execution_status != session.attempt_execution:
                        session.attempt_execution = latest.execution_status
                        session.revision += 1
                        publish_reason = "attempt_execution_changed"

                    if (
                        session.lifecycle == "handoff"
                        and session.handoff_deadline > 0.0
                        and loop.time() >= session.handoff_deadline
                    ):
                        self._clear_handoff(session)
                        publish_reason = "auip_handoff_timeout"

                    became_terminal = (
                        previous_execution in {"queued", "running"}
                        and latest.execution_status not in {"queued", "running"}
                    )
                    if (
                        became_terminal
                        and session.lifecycle == "live"
                        and session.web_root is not None
                    ):
                        signature = await asyncio.to_thread(
                            _content_signature,
                            session.web_root,
                        )
                        if signature != session.content_signature:
                            session.content_signature = signature
                            session.content_revision += 1
                            session.revision += 1
                        publish_reason = publish_reason or "attempt_terminal"

                    desired = self._desired_lifecycle(session, item, latest)
                    if await self._apply_lifecycle(session, desired):
                        publish_reason = publish_reason or "lifecycle_changed"

                    if session.lifecycle != "live":
                        pending_signature = ""
                        pending_since = 0.0
                    elif session.entry is None or not session.entry.is_file():
                        before = (session.status, session.error, session.revision)
                        await self._refresh_discovery(session)
                        after = (session.status, session.error, session.revision)
                        if after != before:
                            publish_reason = publish_reason or "discovery_changed"
                        pending_signature = ""
                        pending_since = 0.0
                    else:
                        assert session.web_root is not None
                        signature = await asyncio.to_thread(
                            _content_signature,
                            session.web_root,
                        )
                        if signature == session.content_signature:
                            pending_signature = ""
                            pending_since = 0.0
                        else:
                            now = loop.time()
                            if signature != pending_signature:
                                pending_signature = signature
                                pending_since = now
                            elif now - pending_since >= self._debounce:
                                session.content_signature = signature
                                session.content_revision += 1
                                session.revision += 1
                                pending_signature = ""
                                pending_since = 0.0
                                publish_reason = "content_changed"
                if publish_reason:
                    await self._publish_updated(session, reason=publish_reason)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("work preview watcher failed for %s", session.work_item_id)
            async with self._lock:
                if self._sessions.get(session.key) is session:
                    await self._set_error(session, "preview_watcher_failed")

    async def _set_error(self, session: PreviewSession, error: str) -> None:
        if session.status == "error" and session.error == error:
            return
        had_url = self._has_content_url(session)
        await self._stop_server(session)
        session.status = "error"
        session.error = error
        if had_url:
            session.content_revision += 1
        session.revision += 1
        await self._publish_updated(session, reason="error")

    async def _stop_server(self, session: PreviewSession) -> None:
        server = session.server
        session.server = None
        session.port = 0
        if server is not None:
            server.close()
            await server.wait_closed()

    async def _stop_session(self, session: PreviewSession, *, publish: bool) -> None:
        watcher = session.watcher
        session.watcher = None
        if watcher is not None and watcher is not asyncio.current_task():
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
        await self._stop_server(session)
        if publish:
            await self._publish_updated(session, reason="closed")

    async def _publish_updated(self, session: PreviewSession, *, reason: str) -> None:
        await self._publisher(
            Method.WORK_PREVIEW_UPDATED,
            {"preview": self._projection(session), "reason": reason},
        )

    def _projection(self, session: PreviewSession) -> dict[str, Any]:
        entry = ""
        if session.entry is not None and session.web_root is not None:
            with contextlib.suppress(ValueError):
                entry = session.entry.relative_to(session.web_root).as_posix()
        url = ""
        if session.status == "ready" and session.port and entry:
            quoted_entry = "/".join(
                urllib.parse.quote(part, safe="") for part in entry.split("/")
            )
            url = f"http://127.0.0.1:{session.port}/{session.token}/{quoted_entry}"
        artifact_ref = session.attached_artifact_ref or session.handoff_artifact_ref
        host_surface_id = (
            session.attached_host_surface_id or session.handoff_host_surface_id
        )
        return {
            "previewId": session.preview_id,
            "workItemId": session.work_item_id,
            "title": session.title,
            "attemptId": session.attempt_id,
            "attemptGeneration": session.attempt_generation,
            "status": session.status,
            "error": session.error,
            "entry": entry,
            "url": url,
            "revision": session.revision,
            "contentRevision": session.content_revision,
            "mode": "static",
            "lifecycle": session.lifecycle,
            **(
                {"artifactRef": artifact_ref} if artifact_ref else {}
            ),
            **(
                {"appSessionId": session.app_session_id}
                if session.app_session_id
                else {}
            ),
            **(
                {"hostSurfaceId": host_surface_id} if host_surface_id else {}
            ),
        }

    async def _serve_request(
        self,
        session: PreviewSession,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        status = 500
        headers: dict[str, str] = {}
        body = b""
        head_only = False
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
            if len(raw) > _MAX_REQUEST_HEAD_BYTES:
                raise WorkPreviewError("request_too_large")
            request_line = raw.split(b"\r\n", 1)[0].decode("ascii", "strict")
            request_headers = _parse_request_headers(raw)
            parts = request_line.split(" ")
            if len(parts) != 3 or parts[2] not in {"HTTP/1.0", "HTTP/1.1"}:
                status = 400
            elif request_headers.get("host", "") != f"127.0.0.1:{session.port}":
                # The unguessable path/cookie authenticates the preview, while
                # exact loopback Host validation also closes DNS-rebinding and
                # forwarded-Host routes into the local server.
                status = 400
            elif parts[0] not in {"GET", "HEAD"}:
                status = 405
                headers["Allow"] = "GET, HEAD"
            else:
                head_only = parts[0] == "HEAD"
                cookie_authenticated = _cookie_has_token(
                    request_headers.get("cookie", ""),
                    session.token,
                )
                target, token_authenticated = _resolve_request_target(
                    session,
                    parts[1],
                    cookie_authenticated=cookie_authenticated,
                )
                if target is None:
                    status = 404
                else:
                    size = target.stat().st_size
                    if size > _MAX_STATIC_FILE_BYTES:
                        status = 413
                    else:
                        body = await asyncio.to_thread(target.read_bytes)
                        status = 200
                        headers["Content-Type"] = (
                            mimetypes.guess_type(str(target))[0]
                            or "application/octet-stream"
                        )
                        if token_authenticated:
                            headers["Set-Cookie"] = (
                                f"amadeus_preview={session.token}; Path=/; "
                                "HttpOnly; SameSite=Strict"
                            )
        except (asyncio.IncompleteReadError, UnicodeDecodeError, ValueError):
            status = 400
        except (asyncio.TimeoutError, WorkPreviewError):
            status = 400
        except (FileNotFoundError, IsADirectoryError, OSError):
            status = 404
        except Exception:
            logger.exception("static preview request failed")
            status = 500
        try:
            reason = {
                200: "OK",
                400: "Bad Request",
                404: "Not Found",
                405: "Method Not Allowed",
                413: "Content Too Large",
                500: "Internal Server Error",
            }.get(status, "Error")
            payload = body if status == 200 else reason.encode("ascii")
            response_headers = {
                "Content-Length": str(len(payload)),
                "Cache-Control": "no-store",
                "Connection": "close",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": (
                    "default-src 'self' data: blob:; "
                    "script-src 'self' 'unsafe-inline'; "
                    "style-src 'self' 'unsafe-inline'; "
                    "connect-src 'self'; object-src 'none'; base-uri 'none'; "
                    "form-action 'none'; frame-ancestors 'none'"
                ),
                **headers,
            }
            head = f"HTTP/1.1 {status} {reason}\r\n" + "".join(
                f"{name}: {value}\r\n" for name, value in response_headers.items()
            ) + "\r\n"
            writer.write(head.encode("ascii"))
            if not head_only:
                writer.write(payload)
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


class WorkPreviewHandler(RequestHandler):
    """Trusted request boundary for a Host-resolved preview session."""

    methods = [
        Method.WORK_PREVIEW_OPEN,
        Method.WORK_PREVIEW_GET,
        Method.WORK_PREVIEW_CLOSE,
    ]

    def __init__(self, coordinator, manager: WorkPreviewManager) -> None:
        self.coordinator = coordinator
        self.manager = manager

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if method == Method.WORK_PREVIEW_OPEN:
            return await self.open_from_work_action(params)
        if method == Method.WORK_PREVIEW_GET:
            return {"preview": await self.manager.get(_work_item_id(params))}
        if method == Method.WORK_PREVIEW_CLOSE:
            return {"preview": await self.manager.close(_work_item_id(params))}
        return None

    async def open_from_work_action(self, params: dict[str, Any]) -> dict[str, Any]:
        current = self.coordinator.snapshot()

        def reject(error: str) -> dict[str, Any]:
            return {"ok": False, "error": error, "work": current}

        revision = str(
            params.get("revision") or params.get("surface_revision") or ""
        ).strip()
        if not revision:
            return reject("missing_revision")
        if revision != str(current.get("revision") or ""):
            return reject("stale_revision")
        work_item_id = _work_item_id(params)
        if not work_item_id:
            return reject("missing_work_item_id")
        if str(current.get("selectedWorkItemId") or "") != work_item_id:
            return reject("work_item_not_selected")
        selected = current.get("selected") if isinstance(current.get("selected"), dict) else {}
        attempt_id = str(
            params.get("attempt_id") or params.get("attemptId") or ""
        ).strip()
        if not attempt_id:
            return reject("missing_attempt_id")
        if str(selected.get("attemptId") or "") != attempt_id:
            return reject("work_attempt_not_current")
        try:
            preview = await self.manager.open(
                work_item_id,
                expected_attempt_id=attempt_id,
            )
        except WorkPreviewError as exc:
            return reject(exc.code)
        return {"ok": True, "preview": preview}


def _work_item_id(params: dict[str, Any]) -> str:
    return str(params.get("work_item_id") or params.get("workItemId") or "").strip()


def _discover_static_entry(workspace: Path) -> _Discovery:
    root = workspace.resolve()
    direct = _case_insensitive_child(root, "index.html")
    if direct is not None and _safe_regular_file(direct, root):
        return _Discovery("ready", entry=direct.resolve(), web_root=root)

    indexes: list[Path] = []
    html_files: list[Path] = []
    directory_count = 0
    file_count = 0
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        try:
            depth = len(current_path.relative_to(root).parts)
        except ValueError:
            continue
        if depth >= _MAX_DISCOVERY_DEPTH:
            directory_names[:] = []
        else:
            directory_names[:] = sorted(
                name
                for name in directory_names
                if not _ignored_directory(name)
            )
        directory_count += 1
        if directory_count > _MAX_DISCOVERY_DIRECTORIES:
            return _Discovery("error", error="preview_discovery_scope_exceeded")
        for name in sorted(file_names):
            file_count += 1
            if file_count > _MAX_DISCOVERY_FILES:
                return _Discovery("error", error="preview_discovery_scope_exceeded")
            candidate = current_path / name
            if not _safe_regular_file(candidate, root):
                continue
            if candidate.suffix.lower() not in {".html", ".htm"}:
                continue
            html_files.append(candidate.resolve())
            if name.lower() == "index.html":
                indexes.append(candidate.resolve())

    if len(indexes) == 1:
        entry = indexes[0]
        return _Discovery("ready", entry=entry, web_root=entry.parent)
    if len(indexes) > 1:
        return _Discovery("ambiguous", error="multiple_preview_entries")
    if len(html_files) == 1:
        entry = html_files[0]
        return _Discovery("ready", entry=entry, web_root=entry.parent)
    if len(html_files) > 1:
        return _Discovery("ambiguous", error="multiple_preview_entries")
    return _Discovery("waiting", error="preview_entry_not_ready")


def _case_insensitive_child(root: Path, name: str) -> Path | None:
    try:
        matches = [child for child in root.iterdir() if child.name.lower() == name]
    except OSError:
        return None
    return matches[0] if len(matches) == 1 else None


def _ignored_directory(name: str) -> bool:
    clean = str(name or "")
    return clean.startswith(".") or clean.lower() in _IGNORED_DIRECTORY_NAMES


def _sensitive_path(path: Path, root: Path) -> bool:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return True
    if any(part.startswith(".") for part in relative.parts):
        return True
    name = path.name.lower()
    return name in _SENSITIVE_FILE_NAMES or path.suffix.lower() in _SENSITIVE_SUFFIXES


def _safe_regular_file(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve())
        return (
            resolved.is_file()
            and resolved.suffix.lower() in _SERVABLE_SUFFIXES
            and not _sensitive_path(resolved, root)
        )
    except (OSError, RuntimeError, ValueError):
        return False


def _content_signature(root: Path) -> str:
    digest = hashlib.sha256()
    file_count = 0
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names[:] = sorted(
            name for name in directory_names if not _ignored_directory(name)
        )
        current_path = Path(current)
        for name in sorted(file_names):
            file_count += 1
            if file_count > _MAX_DISCOVERY_FILES:
                digest.update(b"scope-exceeded")
                return digest.hexdigest()
            candidate = current_path / name
            if not _safe_regular_file(candidate, root):
                continue
            try:
                stat = candidate.stat()
                relative = candidate.resolve().relative_to(root.resolve()).as_posix()
            except (OSError, RuntimeError, ValueError):
                continue
            digest.update(relative.encode("utf-8", "surrogatepass"))
            digest.update(str(stat.st_size).encode("ascii"))
            digest.update(str(stat.st_mtime_ns).encode("ascii"))
    return digest.hexdigest()


def _parse_request_headers(raw: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in raw.split(b"\r\n")[1:]:
        if not line:
            break
        if b":" not in line:
            raise ValueError("malformed HTTP header")
        name, value = line.split(b":", 1)
        clean_name = name.decode("ascii", "strict").strip().lower()
        clean_value = value.decode("latin-1", "strict").strip()
        if not clean_name or clean_name in headers:
            raise ValueError("duplicate or empty HTTP header")
        headers[clean_name] = clean_value
    return headers


def _cookie_has_token(cookie_header: str, token: str) -> bool:
    for cookie_field in str(cookie_header or "").split(";"):
        name, separator, value = cookie_field.strip().partition("=")
        if separator and name == "amadeus_preview":
            return secrets.compare_digest(value, token)
    return False


def _resolve_request_target(
    session: PreviewSession,
    request_target: str,
    *,
    cookie_authenticated: bool = False,
) -> tuple[Path | None, bool]:
    root = session.web_root
    if root is None:
        return None, False
    parsed = urllib.parse.urlsplit(request_target)
    try:
        decoded = urllib.parse.unquote(parsed.path, errors="strict")
    except (UnicodeDecodeError, ValueError):
        return None, False
    if "\x00" in decoded or "\\" in decoded:
        return None, False
    prefix = f"/{session.token}/"
    token_authenticated = decoded.startswith(prefix)
    if token_authenticated:
        relative_text = decoded[len(prefix):]
    elif cookie_authenticated and decoded.startswith("/"):
        # Root-absolute asset URLs are common in web projects.  The first
        # token-bearing document response installs an HttpOnly, same-site
        # cookie, so subsequent /assets/... requests remain authenticated
        # without teaching the application its preview transport prefix.
        relative_text = decoded[1:]
    else:
        return None, False
    if not relative_text:
        if session.entry is None:
            return None, token_authenticated
        relative_text = session.entry.name
    relative = Path(*[part for part in relative_text.split("/") if part])
    if relative.is_absolute() or any(part in {".", ".."} for part in relative.parts):
        return None, token_authenticated
    candidate = (root / relative).resolve()
    return (
        candidate if _safe_regular_file(candidate, root) else None,
        token_authenticated,
    )
