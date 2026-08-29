"""Two-phase, user-approved exports from a WorkItem workspace.

The selected workspace provider remains the code execution provider.  When the
user's requested final destination is outside the selected workspace (initially
the Windows Desktop), the provider writes and validates files in an
attempt-owned staging directory.
Amadeus then records an exact, durable permission request and performs the
external copy itself after the user approves that immutable request.

The renderer never supplies source or destination paths to the resolver.  They
come only from the ledger record and are revalidated, including the staged
file hash, immediately before export.
"""

from __future__ import annotations

import difflib
import hashlib
import mimetypes
import os
import re
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from agent_host.provider_workspace import workspace_route_authority
from agent_host.work_ledger_store import (
    WorkLedgerConflict,
    WorkLedgerNotFound,
    WorkLedgerStore,
)
from agent_host.work_ledger_types import (
    PermissionRequestRecord,
    RunAttemptRecord,
    WorkItemRecord,
    path_is_within,
)


_DESKTOP_MARKERS = ("desktop", "桌面", "デスクトップ")
_MAX_EXPORT_ROOT_ENTRIES = 6
_MAX_EXPORT_FILES = 128
_MAX_EXPORT_DIRECTORIES = 24
_MAX_EXPORT_BYTES = 64 * 1024 * 1024
_MAX_DIFF_BYTES = 512 * 1024
_MAX_DIFF_LINES = 2400

_FILENAME_PATTERNS = (
    re.compile(
        r"(?:file\s*name|filename)\s*(?:is|should\s+be|[:=])?\s*[`\"']?"
        r"(?P<name>[A-Za-z0-9_. ()\-]+\.[A-Za-z0-9]{1,12})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:文件名|檔名)\s*(?:为|為|是|[:：=])?\s*[`\"']?"
        r"(?P<name>[^`\"'\\/:*?<>|\r\n]+\.[A-Za-z0-9]{1,12})",
        re.IGNORECASE,
    ),
    re.compile(
        r"ファイル名\s*(?:は|を|[:：=])?\s*[`\"']?"
        r"(?P<name>[^`\"'\\/:*?<>|\r\n]+\.[A-Za-z0-9]{1,12})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:desktop|桌面|デスクトップ)[\\/]+"
        r"(?P<name>[^`\"'\\/:*?<>|\r\n]+\.[A-Za-z0-9]{1,12})",
        re.IGNORECASE,
    ),
)


def _windows_known_desktop_path() -> Path | None:
    """Resolve the current user's redirected Desktop through Known Folders."""

    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _Guid(ctypes.Structure):
            _fields_ = (
                ("data1", ctypes.c_uint32),
                ("data2", ctypes.c_uint16),
                ("data3", ctypes.c_uint16),
                ("data4", ctypes.c_ubyte * 8),
            )

        # FOLDERID_Desktop = {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
        folder_id = _Guid.from_buffer_copy(
            uuid.UUID("b4bfcc3a-db2c-424c-b029-7fe99a87c641").bytes_le
        )
        raw_path = ctypes.c_void_p()
        shell32 = ctypes.windll.shell32
        shell32.SHGetKnownFolderPath.argtypes = (
            ctypes.POINTER(_Guid),
            wintypes.DWORD,
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_void_p),
        )
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        result = shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, None, ctypes.byref(raw_path)
        )
        if result != 0 or not raw_path.value:
            return None
        try:
            value = ctypes.wstring_at(raw_path.value).strip()
            return Path(value) if value else None
        finally:
            ole32 = ctypes.windll.ole32
            ole32.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
            ole32.CoTaskMemFree.restype = None
            ole32.CoTaskMemFree(raw_path)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _default_desktop_path() -> Path:
    override = str(os.environ.get("AMADEUS_DESKTOP_PATH") or "").strip()
    if override:
        return Path(override).expanduser()
    known_folder = _windows_known_desktop_path()
    if known_folder is not None:
        return known_folder
    return Path.home() / "Desktop"


@dataclass(frozen=True, slots=True)
class ExportResolution:
    permission: PermissionRequestRecord
    exported_paths: tuple[str, ...] = ()


_PreparedExport = tuple[Path, Path, Path, str, bool, str]


class WorkExportService:
    """Prepare, discover, and resolve bounded Desktop export requests."""

    def __init__(
        self,
        store: WorkLedgerStore,
        *,
        desktop_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.store = store
        selected_desktop = (
            Path(desktop_path)
            if desktop_path not in (None, "")
            else _default_desktop_path()
        )
        self.desktop_path = selected_desktop.expanduser().resolve()

    def prepare_plan(
        self,
        *,
        provider: str,
        mode: str,
        task: str,
        item: WorkItemRecord,
        attempt: RunAttemptRecord,
        metadata: dict[str, Any],
        provider_capabilities: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Return an external-export plan only for explicit Desktop intent.

        Callers may provide ``external_export.target = "desktop"``.  Natural
        language detection is deliberately narrow so an incidental mention of
        a desktop application does not rewrite an unrelated coding task.
        """

        if str(mode or "").strip().lower() in {"plan", "read", "observe", "research"}:
            return None
        explicit = metadata.get("external_export") if isinstance(metadata.get("external_export"), dict) else {}
        explicit_target = str(explicit.get("target") or explicit.get("target_kind") or "").strip().lower()
        explicit_filename = self._requested_filename(
            str(explicit.get("filename") or "")
        )
        source_user_text = str(metadata.get("source_user_text") or "").strip()
        source_filename = self._requested_filename(
            source_user_text,
            allow_bare=False,
        )
        task_filename = self._requested_filename(task, allow_bare=False)
        # The Provider task is a semantic paraphrase, not Artifact identity.
        # When the exact user turn is available, only a filename the user said
        # (or an explicit Host field) may constrain an approved-export lookup.
        # Passing an empty name lets a unique durable Artifact own anaphoric
        # continuations such as "connect it", even if role prose invents a
        # different display filename.
        filename = explicit_filename or (
            source_filename if source_user_text else task_filename
        )
        host_requirement = (
            metadata.get("host_outcome_requirement")
            if isinstance(metadata.get("host_outcome_requirement"), dict)
            else {}
        )
        publishes_application_bundle = (
            str(host_requirement.get("facet") or "").strip().lower()
            == "auip.application"
        )
        inherited = (
            self._inherited_amend_bundle(
                metadata,
                work_item_id=item.work_item_id,
            )
            if publishes_application_bundle
            else self._inherited_amend_export(
                metadata,
                filename,
                work_item_id=item.work_item_id,
            )
        )
        has_source_turn = bool(source_user_text)
        if (
            inherited is None
            and explicit_target not in {"desktop", "user_desktop"}
            and (
                has_source_turn
                or not self._has_desktop_destination(task)
            )
        ):
            return None

        # Staging is a workspace capability, not a provider identity.  The
        # production intake supplies the selected manifest; direct service
        # callers may omit it when testing the export mechanism in isolation.
        if provider_capabilities is not None:
            access = str(
                provider_capabilities.get("workspace_access") or "none"
            ).strip().lower()
            ownership = str(
                provider_capabilities.get("workspace_ownership") or "none"
            ).strip().lower()
            if access != "write" or workspace_route_authority(ownership) != "host":
                raise WorkLedgerConflict(
                    "selected provider cannot stage an external export in a "
                    "host-controlled writable workspace"
                )

        workspace = Path(item.workspace_path).resolve()
        staging_root = self.ensure_private_workspace_child(
            workspace,
            "proposed_exports",
            attempt.attempt_id,
        )
        if inherited is not None:
            files = inherited.get("files")
            if isinstance(files, tuple):
                self._copy_inherited_bundle(staging_root, files)
                filename = str(inherited.get("entry_filename") or filename)
            else:
                filename = str(inherited["filename"])
                staged = (staging_root / filename).resolve()
                if staged.parent != staging_root:
                    raise WorkLedgerConflict("inherited export filename escaped staging")
                expected_hash = str(inherited["sha256"])
                source = Path(str(inherited["target_path"])).resolve()
                if staged.exists():
                    if staged.is_symlink() or not self._target_matches(staged, expected_hash):
                        raise WorkLedgerConflict(
                            "amendment staging already exists and differs from the approved export"
                        )
                else:
                    shutil.copy2(source, staged)
                if not self._target_matches(staged, expected_hash):
                    raise WorkLedgerConflict(
                        "approved Desktop export could not be inherited safely"
                    )
        plan = {
            "version": 1,
            "kind": "desktop",
            "status": "staging",
            "detected_by": (
                "related_approved_export"
                if inherited is not None
                else "metadata" if explicit_target else "task"
            ),
            "staging_root": str(staging_root),
            "target_root": str(self.desktop_path),
            # A normal export publishes one explicitly named file.  An AUIP
            # application is a delivery bundle: entry, manifest, SDK and any
            # relative assets must cross the approval boundary together or
            # the Desktop copy is not the application the Host verified.
            "requested_filename": "" if publishes_application_bundle else filename,
            "original_task": str(task or ""),
            "provider": str(provider or "").strip().lower(),
        }
        if publishes_application_bundle:
            directory_seed = (
                Path(filename).stem
                if filename and Path(filename).stem.casefold() != "index"
                else item.title
            )
            plan.update(
                {
                    "publication_shape": "bundle",
                    "entry_filename": filename,
                    "target_relative_root": self._bundle_directory_name(
                        directory_seed
                    ),
                }
            )
        if inherited is not None:
            if publishes_application_bundle:
                # The approved single-file delivery is authoring input, not
                # the publication target of the new multi-file application.
                # Replacing it in place would either omit the sidecars or
                # silently delete user-owned bytes.  The bundle is therefore
                # a new recoverable Desktop directory.
                inheritance = {
                    "inherited_source": True,
                    "inherited_from_work_item_id": str(inherited["work_item_id"]),
                    "inherited_permission_request_id": str(
                        inherited["permission_request_id"]
                    ),
                    "inherited_staging_paths": [
                        str(entry["staging_relative_path"])
                        for entry in inherited["files"]
                    ],
                }
            else:
                inheritance = {
                    "expected_target_sha256": str(inherited["sha256"]),
                    "inherited_target_path": str(inherited["target_path"]),
                    "inherited_from_work_item_id": str(inherited["work_item_id"]),
                    "inherited_from_artifact_id": str(inherited["artifact_id"]),
                    "replace_existing": True,
                }
            plan.update(inheritance)
        return plan

    def _inherited_amend_bundle(
        self,
        metadata: dict[str, Any],
        *,
        work_item_id: str = "",
    ) -> dict[str, Any] | None:
        """Resolve one whole approved publication batch as AUIP authoring input.

        An application is an immutable delivery revision, not a bag of latest
        filenames.  Every inherited file must therefore come from the same
        allowed permission request; files from separate Attempts or approval
        batches are never combined.
        """

        if str(metadata.get("intent") or "").strip().lower() != "amend":
            return None
        related_id = str(
            metadata.get("related_work_item_id") or work_item_id
        ).strip()
        if not related_id:
            return None
        approved = [
            artifact
            for artifact in self.store.list_artifacts(related_id)
            if artifact.kind == "business.export"
            and artifact.status == "approved"
            and str(artifact.sha256 or "").strip()
            and str(artifact.path or "").strip()
            and str((artifact.metadata or {}).get("permission_request_id") or "").strip()
        ]
        artifacts_by_permission: dict[str, dict[str, Any]] = {}
        for artifact in approved:
            request_id = str(artifact.metadata["permission_request_id"])
            artifacts_by_permission.setdefault(request_id, {})[
                str(Path(artifact.path).resolve()).casefold()
            ] = artifact

        permissions = sorted(
            (
                request
                for request in self.store.list_permission_requests(related_id)
                if request.status == "allowed"
                and request.capability == "filesystem.export"
                and request.action == "copy_to_desktop"
                and request.metadata.get("kind") == "desktop_export"
            ),
            key=lambda request: float(
                request.resolved_at or request.updated_at or request.created_at
            ),
            reverse=True,
        )
        for permission in permissions:
            raw_entries = permission.metadata.get("entries")
            if not isinstance(raw_entries, list) or not raw_entries:
                continue
            batch_artifacts = artifacts_by_permission.get(permission.request_id, {})
            inherited_files: list[dict[str, str]] = []
            for raw_entry in raw_entries:
                if not isinstance(raw_entry, dict):
                    inherited_files = []
                    break
                target = Path(str(raw_entry.get("target_path") or "")).resolve()
                expected_hash = str(raw_entry.get("sha256") or "").strip()
                relative = self._safe_relative_path(
                    str(raw_entry.get("staging_relative_path") or "")
                )
                artifact = batch_artifacts.get(str(target).casefold())
                if (
                    artifact is None
                    or not expected_hash
                    or str(artifact.sha256).strip() != expected_hash
                    or not self._same_or_child(target, self.desktop_path)
                    or self._is_link_or_junction(target)
                    or not target.is_file()
                    or not self._target_matches(target, expected_hash)
                ):
                    inherited_files = []
                    break
                inherited_files.append(
                    {
                        "artifact_id": artifact.artifact_id,
                        "target_path": str(target),
                        "sha256": expected_hash,
                        "staging_relative_path": relative.as_posix(),
                    }
                )
            if not inherited_files:
                continue
            html_entries = [
                entry["staging_relative_path"]
                for entry in inherited_files
                if Path(entry["staging_relative_path"]).suffix.casefold() == ".html"
            ]
            return {
                "work_item_id": related_id,
                "permission_request_id": permission.request_id,
                "entry_filename": html_entries[0] if len(html_entries) == 1 else "",
                "files": tuple(inherited_files),
            }
        return None

    def _copy_inherited_bundle(
        self,
        staging_root: Path,
        files: tuple[dict[str, str], ...],
    ) -> None:
        for entry in files:
            relative = self._safe_relative_path(entry["staging_relative_path"])
            staged = (staging_root / relative).resolve()
            if not self._same_or_child(staged, staging_root):
                raise WorkLedgerConflict("inherited bundle path escaped staging")
            source = Path(entry["target_path"]).resolve()
            expected_hash = str(entry["sha256"])
            staged.parent.mkdir(parents=True, exist_ok=True)
            cursor = staging_root
            for part in relative.parts[:-1]:
                cursor = cursor / part
                if self._is_link_or_junction(cursor):
                    raise WorkLedgerConflict(
                        "inherited bundle staging contains a link"
                    )
            if staged.exists() and self._is_link_or_junction(staged):
                raise WorkLedgerConflict("inherited bundle staging contains a link")
            if staged.exists():
                if not self._target_matches(staged, expected_hash):
                    raise WorkLedgerConflict(
                        "amendment staging already differs from the approved bundle"
                    )
            else:
                shutil.copy2(source, staged)
            if not self._target_matches(staged, expected_hash):
                raise WorkLedgerConflict(
                    "approved Desktop bundle could not be inherited safely"
                )

    def _inherited_amend_export(
        self,
        metadata: dict[str, Any],
        requested_filename: str,
        *,
        work_item_id: str = "",
    ) -> dict[str, str] | None:
        """Resolve the last approved external deliverable for an amendment.

        Workspace routing alone is insufficient for two-phase exports: the
        file the workspace provider edited lives in attempt-owned staging while the user's
        durable copy lives on Desktop.  Amendments therefore inherit the
        approved target and its hash explicitly.  A target that has changed
        since approval is user-owned drift and must never be overwritten.
        """

        if str(metadata.get("intent") or "").strip().lower() != "amend":
            return None
        related_id = str(
            metadata.get("related_work_item_id") or work_item_id
        ).strip()
        if not related_id:
            return None
        approved = [
            artifact
            for artifact in self.store.list_artifacts(related_id)
            if artifact.kind == "business.export"
            and artifact.status == "approved"
            and str(artifact.sha256 or "").strip()
            and str(artifact.path or "").strip()
        ]
        clean_filename = Path(str(requested_filename or "")).name.casefold()
        if clean_filename:
            approved = [
                artifact
                for artifact in approved
                if Path(str(artifact.path)).name.casefold() == clean_filename
            ]
        else:
            distinct_names = {
                Path(str(artifact.path)).name.casefold() for artifact in approved
            }
            if len(distinct_names) != 1:
                return None
        if not approved:
            return None

        artifact = approved[0]
        target = Path(str(artifact.path)).resolve()
        if (
            target.parent != self.desktop_path
            or self._is_link_or_junction(target)
            or not target.is_file()
        ):
            raise WorkLedgerConflict(
                f"approved Desktop target is missing or unsafe: {target.name}"
            )
        expected_hash = str(artifact.sha256).strip()
        if not self._target_matches(target, expected_hash):
            raise WorkLedgerConflict(
                f"Desktop target changed since its last approval: {target.name}"
            )
        return {
            "artifact_id": artifact.artifact_id,
            "work_item_id": related_id,
            "filename": target.name,
            "target_path": str(target),
            "sha256": expected_hash,
        }

    @staticmethod
    def provider_prompt(original_task: str, plan: dict[str, Any]) -> str:
        staging_root = str(plan.get("staging_root") or "")
        requested_filename = str(plan.get("requested_filename") or "")
        if str(plan.get("publication_shape") or "") == "bundle":
            entry_filename = str(plan.get("entry_filename") or "").strip()
            entry_instruction = (
                f" Keep `{entry_filename}` as the application entry document."
                if entry_filename
                else " Keep one unambiguous HTML entry document."
            )
            filename_instruction = (
                f" Produce the complete application bundle beneath that directory;"
                f" include its manifest, runtime assets, and every relative file it needs."
                f"{entry_instruction}"
            )
        else:
            filename_instruction = (
                f" Use the exact requested filename `{requested_filename}`."
                if requested_filename
                else " Preserve the user-requested filenames beneath that directory."
            )
        inherited_instruction = ""
        if plan.get("replace_existing") is True:
            inherited_instruction = (
                " The last user-approved Desktop version has already been copied into this staging directory. "
                "Open and modify that existing staged file in place; preserve unaffected behavior and do not rebuild it from an empty file."
            )
        elif plan.get("inherited_source") is True:
            inherited_instruction = (
                " The complete files from one immutable, user-approved Desktop publication have already been copied into this staging directory. "
                "Treat them as one application revision: inspect and modify that staged bundle in place, preserve unaffected behavior, and do not rebuild it from empty files."
            )
        host_auip_instruction = ""
        materialized = [
            str(value)
            for value in (plan.get("host_materialized_files") or [])
            if str(value).strip()
        ]
        if plan.get("host_validates_auip_bundle") is True and materialized:
            host_auip_instruction = (
                " Amadeus has already materialized these official opaque runtime assets in "
                f"the staging directory: {', '.join(materialized)}. Reference them in place; "
                "do not open, copy, edit, regenerate, or validate their implementation. "
                "After this run, Amadeus will deterministically validate the manifest, embedded "
                "manifest synchronization, runtime asset integrity, and entry wiring before it "
                "can request export approval. Execute only the opaque manifest and embed-sync preflights named "
                "by the authoring contract; do not search for or duplicate runtime-integrity "
                "or entry-wiring checks. Validate the application-specific state, action mapping, "
                "and standalone/connected mechanics."
            )
        validation_instruction = (
            "Validate the application-specific behavior in this run; Amadeus owns the generic AUIP bundle checks."
            if plan.get("host_validates_auip_bundle") is True
            else "Validate the staged files in this run."
        )
        return (
            f"{original_task}\n\n"
            "[AMADEUS TWO-PHASE EXPORT POLICY]\n"
            "The requested final destination is outside the current workspace. "
            "Do not write, move, or copy anything to Desktop or any other path outside the workspace during this run.\n"
            f"Create the actual deliverable file(s) under this exact staging directory: `{staging_root}`."
            f"{filename_instruction}{inherited_instruction}{host_auip_instruction}\n"
            "The files must contain the complete requested implementation, not a description or placeholder. "
            f"{validation_instruction} Keep validation bounded to the requested mechanics and observed defects; "
            "after the required checks pass, do not explore alternative toolchains or create optional render/image conversions. "
            "Do not ask the external agent for interactive Desktop permission.\n"
            "After staging and validation, report the staged filenames and state that Amadeus must request the user's "
            "explicit export approval. Amadeus will perform the final Desktop copy after approval."
        )

    def discover_staged_exports(
        self,
        attempt: RunAttemptRecord,
        item: WorkItemRecord,
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        """Register staged files and create one immutable export permission."""

        staging_root = self._validated_staging_root(item, attempt, plan)
        target_root = self._validated_target_root(plan)
        existing_exports = [
            request
            for request in self.store.list_permission_requests(
                item.work_item_id,
                attempt_id=attempt.attempt_id,
            )
            if request.capability == "filesystem.export"
            and request.action == "copy_to_desktop"
            and request.metadata.get("kind") == "desktop_export"
        ]
        if existing_exports:
            # One attempt owns one immutable export contract.  Provider-result
            # replay or later staging mutations must never create a second,
            # hidden request with a different preview/scope.
            return self._outcome_for_permission(item, attempt, existing_exports[-1])

        requested_filename = self._requested_filename(str(plan.get("requested_filename") or ""))
        if requested_filename:
            requested_source = staging_root / requested_filename
            files = (
                [requested_source.resolve()]
                if requested_source.is_file()
                and not requested_source.is_symlink()
                and requested_source.parent.resolve() == staging_root
                else []
            )
        else:
            files = self._bounded_files(staging_root)
        if not files:
            return {
                "available": False,
                "reason": "staged_export_missing",
                "entries": [],
                "permission": None,
                "patch": "",
                "changed_files": [],
            }

        entries: list[dict[str, Any]] = []
        preview_inputs: list[tuple[dict[str, Any], bytes]] = []
        opaque_preview_files: list[str] = []
        host_materialized_assets = (
            plan.get("host_materialized_assets")
            if plan.get("host_validates_auip_bundle") is True
            and isinstance(plan.get("host_materialized_assets"), dict)
            else {}
        )
        snapshot_bytes = 0
        target_relative_root = self._safe_relative_path(
            str(plan.get("target_relative_root") or "")
        ) if str(plan.get("target_relative_root") or "").strip() else Path()
        for source in files:
            relative = source.relative_to(staging_root)
            published_relative = target_relative_root / relative
            target = (target_root / published_relative).resolve()
            if not self._same_or_child(target, target_root):
                raise WorkLedgerConflict("proposed export target escaped the Desktop scope")
            try:
                raw = source.read_bytes()
                modified_at = source.stat().st_mtime
            except OSError as exc:
                raise WorkLedgerConflict(
                    f"staged export cannot be snapshotted: {source.name}"
                ) from exc
            snapshot_bytes += len(raw)
            if snapshot_bytes > _MAX_EXPORT_BYTES:
                raise WorkLedgerConflict("staged export exceeds the bounded size limit")
            entry = {
                "source_path": str(source),
                "target_path": str(target),
                "relative_path": published_relative.as_posix(),
                "staging_relative_path": relative.as_posix(),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
                "modified_at": modified_at,
                "preview_status": "complete_text",
            }
            opaque_identity = host_materialized_assets.get(relative.as_posix())
            if isinstance(opaque_identity, dict):
                expected_hash = str(opaque_identity.get("sha256") or "").strip()
                expected_size = opaque_identity.get("size_bytes")
                if (
                    not expected_hash
                    or entry["sha256"] != expected_hash
                    or not isinstance(expected_size, int)
                    or entry["size_bytes"] != expected_size
                ):
                    raise WorkLedgerConflict(
                        f"host-materialized runtime asset identity changed: {relative.as_posix()}"
                    )
                entry["preview_status"] = "host_verified_opaque"
                entry["host_materialized"] = True
            if plan.get("replace_existing") is True:
                inherited_target = Path(
                    str(plan.get("inherited_target_path") or "")
                ).resolve()
                expected_old_hash = str(
                    plan.get("expected_target_sha256") or ""
                ).strip()
                if target != inherited_target or not expected_old_hash:
                    raise WorkLedgerConflict(
                        "amendment export no longer matches its inherited Desktop target"
                    )
                if not self._target_matches(target, expected_old_hash):
                    raise WorkLedgerConflict(
                        f"Desktop target changed since its last approval: {target.name}"
                    )
                entry.update(
                    {
                        "replace_existing": True,
                        "expected_target_sha256": expected_old_hash,
                        "inherited_from_work_item_id": str(
                            plan.get("inherited_from_work_item_id") or ""
                        ),
                        "inherited_from_artifact_id": str(
                            plan.get("inherited_from_artifact_id") or ""
                        ),
                    }
                )
            entries.append(entry)
            if entry.get("host_materialized") is True:
                opaque_preview_files.append(f"Desktop/{published_relative.as_posix()}")
            else:
                preview_inputs.append((entry, raw))

        # Hashes, sizes, and the approval diff all derive from these same
        # in-memory bytes.  The provider cannot swap the source between a hash
        # pass and a later preview pass.
        patch, changed_files = self._proposed_patch(preview_inputs)
        binary_preview_count = sum(
            entry.get("preview_status") == "binary_identity" for entry in entries
        )

        entries_hash = hashlib.sha256(
            "\n".join(
                f"{entry['source_path']}|{entry['target_path']}|{entry['sha256']}|"
                f"{entry.get('replace_existing', False)}|{entry.get('expected_target_sha256', '')}"
                for entry in entries
            ).encode("utf-8", errors="replace")
        ).hexdigest()
        directory_paths = self._directory_paths_for_entries(target_root, entries)
        # Publication needs a same-volume sibling before a no-replace hard
        # link.  Make that path deterministic and include it in the immutable
        # approval contract so even a hard-crash residue is inside the exact
        # user-authorized scope and can be recovered by this transaction.
        for index, entry in enumerate(entries):
            target = Path(str(entry["target_path"]))
            entry["temporary_path"] = str(
                target.parent
                / f".{target.name}.amadeus-{entries_hash[:16]}-{index}.tmp"
            )
        scope_paths = [str(path) for path in directory_paths] + [
            path
            for entry in entries
            for path in (entry["target_path"], entry["temporary_path"])
        ]
        replaces_existing = any(entry.get("replace_existing") is True for entry in entries)
        permission = self.store.create_permission_request(
            item.work_item_id,
            attempt_id=attempt.attempt_id,
            capability="filesystem.export",
            action="copy_to_desktop",
            scope_paths=scope_paths,
            reason=(
                f"{'Replace' if replaces_existing else 'Export'} {len(entries)} validated staged file(s) "
                f"from the WorkItem workspace {'on' if replaces_existing else 'to'} Desktop."
                + (
                    f" {binary_preview_count} binary file(s) are represented by exact path, "
                    "media-type hint, size, and SHA-256 identity."
                    if binary_preview_count
                    else ""
                )
            ),
            reversibility=(
                (
                    "Replaces only the listed Desktop file when it still exactly matches the last approved version; "
                    "the prior approved bytes remain in the WorkItem staging history."
                    if replaces_existing
                    else "Creates only the listed target and transaction-temp paths; existing paths are never overwritten."
                )
            ),
            options=["allow_once", "deny"],
            idempotency_key=f"desktop-export:{attempt.attempt_id}:{entries_hash}",
            metadata={
                "kind": "desktop_export",
                "staging_root": str(staging_root),
                "target_root": str(target_root),
                "entries": entries,
                "directory_paths": [str(path) for path in directory_paths],
                "entries_hash": entries_hash,
                "preview_version": 2 if binary_preview_count else 1,
                "preview_complete": True,
                "preview_patch": patch,
                "preview_changed_files": changed_files,
                "preview_opaque_files": opaque_preview_files,
                "replaces_existing": replaces_existing,
            },
        )
        return self._outcome_for_permission(item, attempt, permission)

    def validated_staging_files(
        self,
        attempt: RunAttemptRecord,
        item: WorkItemRecord,
        plan: dict[str, Any],
    ) -> tuple[Path, tuple[Path, ...]]:
        """Return every bounded file in one Attempt-owned staging directory.

        External publication and internal artifact discovery are different
        authority boundaries.  ``discover_staged_exports`` intentionally
        narrows publication to the filename the user authorized, while a Host
        outcome verifier may need the complete application bundle (for
        example an entry document, manifest, and relative runtime assets).

        This method only proves the staging identity and bounded file set.  It
        creates no artifact, permission, or external side effect.
        """

        staging_root = self._validated_staging_root(item, attempt, plan)
        return staging_root, tuple(self._bounded_files(staging_root))

    def observe_staged_files(
        self,
        attempt: RunAttemptRecord,
        item: WorkItemRecord,
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        """Read a bounded, attempt-owned staging directory without committing it.

        Status lookup must be able to say that an external deliverable exists
        before provider completion, but it must not create a permission,
        snapshot file contents, or follow a substituted directory.  This is a
        deliberately narrower read path than :meth:`discover_staged_exports`.
        """

        workspace = Path(item.workspace_path).resolve()
        staging_root = Path(str(plan.get("staging_root") or "")).resolve()
        expected = self._staging_root(workspace, attempt.attempt_id).resolve()
        if staging_root != expected or not path_is_within(str(staging_root), str(workspace)):
            raise WorkLedgerConflict(
                "staged export directory does not belong to this WorkItem attempt"
            )
        for candidate in (
            workspace / ".amadeus",
            workspace / ".amadeus" / "proposed_exports",
            staging_root,
        ):
            if candidate.exists() and self._is_link_or_junction(candidate):
                raise WorkLedgerConflict("staged export path cannot be a link or junction")
        if not staging_root.is_dir():
            return {
                "available": False,
                "reason": "staged_export_missing",
                "changed_files": [],
            }
        requested_filename = self._requested_filename(str(plan.get("requested_filename") or ""))
        if requested_filename:
            requested = (staging_root / requested_filename).resolve()
            files = (
                [requested]
                if requested.is_file()
                and not requested.is_symlink()
                and requested.parent == staging_root
                else []
            )
        else:
            files = self._bounded_files(staging_root)
        return {
            "available": bool(files),
            "reason": "observed" if files else "staged_export_missing",
            "changed_files": [path.relative_to(staging_root).as_posix() for path in files],
        }

    def _outcome_for_permission(
        self,
        item: WorkItemRecord,
        attempt: RunAttemptRecord,
        permission: PermissionRequestRecord,
    ) -> dict[str, Any]:
        metadata = permission.metadata if isinstance(permission.metadata, dict) else {}
        entries = metadata.get("entries") if isinstance(metadata.get("entries"), list) else []
        if not entries:
            raise WorkLedgerConflict("Desktop export permission has no immutable entries")
        committed = permission.status == "allowed" and self.is_committed_export(
            permission,
            entries,
        )
        abandoned = permission.status == "allowed" and self.is_abandoned_export(permission)
        targets_intact = committed and self.committed_targets_match(permission, entries)
        if permission.status == "pending":
            target_status, export_status, reason = (
                "pending",
                "pending",
                "external_export_pending",
            )
        elif abandoned:
            target_status, export_status, reason = (
                "rejected",
                "abandoned",
                "external_export_abandoned",
            )
        elif permission.status == "allowed" and committed and targets_intact:
            target_status, export_status, reason = (
                "approved",
                "approved",
                "external_export_complete",
            )
        elif permission.status == "allowed" and committed:
            target_status, export_status, reason = (
                "missing",
                "drifted",
                "external_export_drift",
            )
        elif permission.status == "allowed":
            # Authorization and publication are intentionally distinct.  An
            # interrupted authorized export must be recovered and verified;
            # permission status alone is never evidence that Desktop contains
            # a complete deliverable.
            target_status, export_status, reason = (
                "pending",
                "authorized_uncommitted",
                "external_export_recovery_required",
            )
        elif permission.status == "denied":
            target_status, export_status, reason = (
                "rejected",
                "rejected",
                "external_export_denied",
            )
        else:
            target_status, export_status, reason = (
                "rejected",
                "expired",
                "external_export_expired",
            )
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise WorkLedgerConflict("Desktop export permission entry is malformed")
            source = Path(str(entry.get("source_path") or ""))
            relative = Path(str(entry["relative_path"]))
            target = Path(str(entry["target_path"]))
            self.store.register_artifact(
                item.work_item_id,
                attempt_id=attempt.attempt_id,
                kind="business.proposed_export",
                title=relative.name,
                path=str(source),
                identity=f"proposed-export:{attempt.attempt_id}:{index}:{relative.as_posix()}",
                status="registered",
                sha256=entry["sha256"],
                size_bytes=entry["size_bytes"],
                modified_at=(
                    float(entry.get("modified_at"))
                    if entry.get("modified_at") is not None
                    else None
                ),
                metadata={
                    "target_path": str(target),
                    "relative_path": relative.as_posix(),
                    "export_status": export_status,
                },
            )
            self.store.register_artifact(
                item.work_item_id,
                attempt_id=attempt.attempt_id,
                kind="business.export",
                title=f"Export {relative.name} to Desktop",
                path=str(target),
                identity=f"export-target:{attempt.attempt_id}:{index}:{relative.as_posix()}",
                status=target_status,
                sha256=entry["sha256"],
                size_bytes=entry["size_bytes"],
                metadata={
                    "source_path": str(source),
                    "relative_path": relative.as_posix(),
                    "export_status": export_status,
                    "permission_request_id": permission.request_id,
                },
            )
        patch = str(metadata.get("preview_patch") or "")
        changed_files = [str(path) for path in metadata.get("preview_changed_files") or []]
        return {
            "available": bool(metadata.get("preview_complete") is True),
            "reason": reason,
            "pending_export": permission.status == "pending",
            "recovery_required": (
                permission.status == "allowed" and not committed and not abandoned
            ),
            "entries": entries,
            "permission": permission,
            "patch": patch,
            "changed_files": changed_files,
        }

    def resolve(self, request_id: str, *, allow: bool) -> ExportResolution:
        """Apply one fresh user decision; only pending requests are accepted."""

        return self._resolve(request_id, allow=allow, resume_authorized=False)

    def resume_authorized(self, request_id: str) -> ExportResolution:
        """Internally resume a durable authorization interrupted before commit."""

        return self._resolve(request_id, allow=True, resume_authorized=True)

    def _resolve(
        self,
        request_id: str,
        *,
        allow: bool,
        resume_authorized: bool,
    ) -> ExportResolution:
        permission = self.store.get_permission_request(request_id)
        if permission is None:
            raise WorkLedgerNotFound(f"unknown permission request: {request_id}")
        if permission.capability != "filesystem.export" or permission.action != "copy_to_desktop":
            raise WorkLedgerConflict("permission request is not a Desktop export")
        if permission.metadata.get("kind") != "desktop_export":
            raise WorkLedgerConflict("permission request lacks the Desktop export contract")
        if resume_authorized and not allow:
            raise WorkLedgerConflict("an authorized export can only be resumed")
        expected_status = "allowed" if resume_authorized else "pending"
        if permission.status != expected_status:
            raise WorkLedgerConflict(
                f"permission request {request_id} is already {permission.status}"
            )
        if not allow:
            denied = self.store.resolve_permission_request(
                request_id,
                "denied",
                metadata={"resolution": "user_denied"},
            )
            metadata = permission.metadata if isinstance(permission.metadata, dict) else {}
            entries = metadata.get("entries") if isinstance(metadata.get("entries"), list) else []
            for index, raw in enumerate(entries):
                if not isinstance(raw, dict):
                    continue
                target = str(raw.get("target_path") or "")
                relative = str(raw.get("relative_path") or Path(target).name)
                self.store.register_artifact(
                    permission.work_item_id,
                    attempt_id=permission.attempt_id,
                    kind="business.export",
                    title=f"Declined export of {Path(target).name}",
                    path=target,
                    identity=f"export-target:{permission.attempt_id}:{index}:{relative}",
                    status="rejected",
                    sha256=str(raw.get("sha256") or ""),
                    size_bytes=int(raw.get("size_bytes") or 0),
                    metadata={
                        "export_status": "rejected",
                        "permission_request_id": request_id,
                    },
                )
            return ExportResolution(permission=denied)

        metadata = permission.metadata if isinstance(permission.metadata, dict) else {}
        entries = metadata.get("entries") if isinstance(metadata.get("entries"), list) else []
        preview_version = int(metadata.get("preview_version") or 1)
        if preview_version >= 2:
            if metadata.get("preview_complete") is not True:
                raise WorkLedgerConflict(
                    "Desktop export permission lacks a complete approval preview"
                )
            supported_preview_statuses = {
                "complete_text",
                "binary_identity",
                "host_verified_opaque",
            }
            for raw in entries:
                status = str(raw.get("preview_status") or "") if isinstance(raw, dict) else ""
                if status not in supported_preview_statuses:
                    raise WorkLedgerConflict(
                        "Desktop export permission has an unsupported approval preview"
                    )
        staging_root = Path(str(metadata.get("staging_root") or "")).resolve()
        target_root = Path(str(metadata.get("target_root") or "")).resolve()
        attempt = self.store.get_attempt(permission.attempt_id)
        item = self.store.get_work_item(permission.work_item_id)
        if attempt is None or item is None:  # pragma: no cover - protected by FK
            raise WorkLedgerConflict("permission request lost its WorkItem attempt")
        if resume_authorized:
            if not self.can_resume_authorized(permission):
                raise WorkLedgerConflict("authorized export has no recoverable transaction journal")
        expected_staging = self.ensure_private_workspace_child(
            Path(item.workspace_path).resolve(),
            "proposed_exports",
            attempt.attempt_id,
        )
        if staging_root != expected_staging or not path_is_within(
            str(staging_root), item.workspace_path
        ):
            raise WorkLedgerConflict("permission staging scope no longer matches its attempt")
        if target_root != self.desktop_path:
            raise WorkLedgerConflict("permission Desktop scope no longer matches the configured Desktop")
        if not entries:
            raise WorkLedgerConflict("permission request has no staged export entries")
        directory_paths = self._directory_paths_for_entries(target_root, entries)
        declared_directories = [
            str(Path(str(path)).resolve())
            for path in metadata.get("directory_paths") or []
        ]
        if declared_directories != [str(path) for path in directory_paths]:
            raise WorkLedgerConflict(
                "export directory scope does not match its immutable file manifest"
            )
        if not resume_authorized:
            existing_directory = next(
                (path for path in directory_paths if path.exists()),
                None,
            )
            if existing_directory is not None:
                raise WorkLedgerConflict(
                    "Desktop deliverable directory already exists and will not be merged: "
                    f"{existing_directory.name}"
                )

        prepared: list[_PreparedExport] = []
        entries_hash = str(metadata.get("entries_hash") or "")
        for index, raw in enumerate(entries):
            if not isinstance(raw, dict):
                raise WorkLedgerConflict("permission export entry is malformed")
            relative = self._safe_relative_path(str(raw.get("relative_path") or ""))
            source = Path(str(raw.get("source_path") or "")).resolve()
            target = Path(str(raw.get("target_path") or "")).resolve()
            temporary_value = str(raw.get("temporary_path") or "").strip()
            if not temporary_value:
                raise WorkLedgerConflict(
                    "export permission lacks an approved transaction temp path"
                )
            temporary = Path(temporary_value).resolve()
            expected_hash = str(raw.get("sha256") or "")
            replace_existing = raw.get("replace_existing") is True
            expected_target_hash = str(
                raw.get("expected_target_sha256") or ""
            ).strip()
            staging_relative = self._safe_relative_path(
                str(raw.get("staging_relative_path") or raw.get("relative_path") or "")
            )
            expected_source = (staging_root / staging_relative).resolve()
            expected_target = (target_root / relative).resolve()
            if source != expected_source or not self._same_or_child(source, staging_root):
                raise WorkLedgerConflict("staged export source escaped its immutable relative path")
            if target != expected_target or not self._same_or_child(target, target_root):
                raise WorkLedgerConflict("export target escaped its immutable Desktop path")
            expected_temporary = (
                target.parent
                / f".{target.name}.amadeus-{entries_hash[:16]}-{index}.tmp"
            ).resolve()
            if (
                not entries_hash
                or temporary != expected_temporary
                or temporary.parent != target.parent
            ):
                raise WorkLedgerConflict(
                    "export transaction temp path does not match its immutable contract"
                )
            if source.is_symlink() or not source.is_file():
                raise WorkLedgerConflict(f"staged export is missing: {source.name}")
            actual_hash = self._sha256(source)
            if not expected_hash or actual_hash != expected_hash:
                raise WorkLedgerConflict(f"staged export changed after approval was requested: {source.name}")
            if replace_existing:
                if not expected_target_hash:
                    raise WorkLedgerConflict(
                        "replacement export lacks the previously approved target hash"
                    )
                if not (
                    self._target_matches(target, expected_hash)
                    or self._target_matches(target, expected_target_hash)
                ):
                    raise WorkLedgerConflict(
                        f"Desktop target changed since its last approval: {target.name}"
                    )
            elif target.exists():
                if not self._target_matches(target, expected_hash):
                    raise WorkLedgerConflict(
                        f"Desktop target already exists and will not be overwritten: {target.name}"
                    )
            prepared.append(
                (
                    source,
                    target,
                    temporary,
                    expected_hash,
                    replace_existing,
                    expected_target_hash,
                )
            )

        approved_scope = [str(path) for path in directory_paths] + [
            path
            for _source, target, temporary, _hash, _replace, _old_hash in prepared
            for path in (str(target), str(temporary))
        ]
        if approved_scope != list(permission.scope_paths):
            raise WorkLedgerConflict("permission entry targets do not match the approved scope")

        if not resume_authorized:
            # Persist authorization before the first external side effect.  A
            # concurrent deny can no longer win after this CAS succeeds, while
            # a losing allow performs no Desktop write at all.  Publication is
            # tracked separately so an allowed-but-interrupted transaction can
            # be recovered without pretending it completed.
            allowed = self.store.resolve_permission_request(
                request_id,
                "allowed",
                metadata={
                    "resolution": "user_allowed",
                    "authorized_paths": approved_scope,
                },
            )
        else:
            allowed = permission

        self.store.update_attempt(
            permission.attempt_id,
            metadata={
                "export_resolution": {
                    "permission_request_id": request_id,
                    "status": "authorized",
                    "authorized_paths": approved_scope,
                }
            },
        )
        try:
            self._prepare_export_directories(
                directory_paths,
                prepared,
                resume_authorized=resume_authorized,
            )
            for (
                source,
                target,
                temporary,
                expected_hash,
                replace_existing,
                expected_target_hash,
            ) in prepared:
                # A previous process may have atomically published this exact
                # file before it crashed.  Treat it as recoverable progress,
                # then re-check every target again before committing the
                # ledger transaction below.
                if self._target_matches(target, expected_hash):
                    self._unlink_transaction_temp_if_exact(temporary, expected_hash)
                    continue
                if replace_existing:
                    if not self._target_matches(target, expected_target_hash):
                        raise WorkLedgerConflict(
                            f"Desktop target changed during approval: {target.name}"
                        )
                    self._publish_atomic_replace(
                        source,
                        target,
                        expected_hash=expected_hash,
                        expected_target_hash=expected_target_hash,
                        temporary_path=temporary,
                    )
                    continue
                if target.exists():
                    raise WorkLedgerConflict(
                        f"Desktop target already exists and will not be overwritten: {target.name}"
                    )
                self._publish_atomic_no_replace(
                    source,
                    target,
                    expected_hash=expected_hash,
                    temporary_path=temporary,
                )

            # This second pass closes the old already-present gap and is the
            # only point from which an export may be recorded as committed.
            for _source, target, _temporary, expected_hash, _replace, _old_hash in prepared:
                if not self._target_matches(target, expected_hash):
                    raise WorkLedgerConflict(
                        f"export verification failed before commit: {target.name}"
                    )
            self._validate_export_directory_tree(directory_paths, prepared)
        except Exception as exc:
            # Never remove a published target during rollback: another process
            # may have replaced the path after publication.  Exact targets are
            # harmless recoverable progress; each file becomes visible only
            # after a complete temporary copy was fsynced and linked.
            self.store.update_attempt(
                permission.attempt_id,
                metadata={
                    "export_resolution": {
                        "permission_request_id": request_id,
                        "status": "authorized_uncommitted",
                        "error": exc.__class__.__name__,
                        "verified_paths": [
                            str(target)
                            for _source, target, _temporary, expected_hash, _replace, _old_hash in prepared
                            if self._target_matches(target, expected_hash)
                        ],
                    }
                },
            )
            raise

        exported_paths = tuple(
            str(target)
            for _source, target, _temporary, _hash, _replace, _old_hash in prepared
        )
        for index, raw in enumerate(entries):
            target = str(raw.get("target_path") or "") if isinstance(raw, dict) else ""
            relative = str(raw.get("relative_path") or Path(target).name) if isinstance(raw, dict) else Path(target).name
            self.store.register_artifact(
                permission.work_item_id,
                attempt_id=permission.attempt_id,
                kind="business.export",
                title=f"Exported {Path(target).name}",
                path=target,
                identity=f"export-target:{permission.attempt_id}:{index}:{relative}",
                status="approved",
                sha256=str(raw.get("sha256") or "") if isinstance(raw, dict) else "",
                size_bytes=int(raw.get("size_bytes") or 0) if isinstance(raw, dict) else None,
                metadata={
                    "export_status": "approved",
                    "permission_request_id": request_id,
                    "replace_existing": bool(raw.get("replace_existing") is True)
                    if isinstance(raw, dict)
                    else False,
                    "replaced_sha256": str(raw.get("expected_target_sha256") or "")
                    if isinstance(raw, dict)
                    else "",
                },
            )
        self.store.update_attempt(
            permission.attempt_id,
            metadata={
                "export_resolution": {
                    "permission_request_id": request_id,
                    "status": "committed",
                    "exported_paths": list(exported_paths),
                }
            },
        )
        return ExportResolution(permission=allowed, exported_paths=exported_paths)

    def is_committed_export(
        self,
        permission: PermissionRequestRecord,
        entries: Iterable[dict[str, Any]],
    ) -> bool:
        """Return the durable publication receipt, independent of later drift."""

        attempt = self.store.get_attempt(permission.attempt_id)
        if attempt is None:
            return False
        resolution = attempt.metadata.get("export_resolution")
        if not isinstance(resolution, dict):
            return False
        return bool(
            resolution.get("permission_request_id") == permission.request_id
            and resolution.get("status") == "committed"
        )

    def is_abandoned_export(self, permission: PermissionRequestRecord) -> bool:
        """Return whether recovery was explicitly closed by the user."""

        attempt = self.store.get_attempt(permission.attempt_id)
        if attempt is None:
            return False
        resolution = attempt.metadata.get("export_resolution")
        return bool(
            isinstance(resolution, dict)
            and resolution.get("permission_request_id") == permission.request_id
            and resolution.get("status") == "abandoned"
        )

    def committed_targets_match(
        self,
        permission: PermissionRequestRecord,
        entries: Iterable[dict[str, Any]],
    ) -> bool:
        """Check current artifact integrity without authorizing a replay."""

        for raw in entries:
            if not isinstance(raw, dict):
                return False
            target = Path(str(raw.get("target_path") or ""))
            expected_hash = str(raw.get("sha256") or "")
            if not expected_hash or not self._target_matches(target, expected_hash):
                return False
        return True

    def can_resume_authorized(self, permission: PermissionRequestRecord) -> bool:
        """Return whether an allowed export has an unfinished durable receipt."""

        if (
            permission.status != "allowed"
            or permission.capability != "filesystem.export"
            or permission.action != "copy_to_desktop"
            or permission.metadata.get("kind") != "desktop_export"
        ):
            return False
        attempt = self.store.get_attempt(permission.attempt_id)
        if attempt is None:
            return False
        journal = attempt.metadata.get("export_resolution")
        if isinstance(journal, dict):
            if journal.get("permission_request_id") != permission.request_id:
                return False
            return journal.get("status") in {"authorized", "authorized_uncommitted"}

        # ``resolve_permission_request`` persists the user's decision and the
        # exact authorized paths in one SQLite transaction.  A process can
        # still stop in the narrow interval before the attempt journal is
        # written.  That permission receipt is sufficient to reconstruct the
        # journal, but only when it proves the exact immutable scope.
        metadata = permission.metadata if isinstance(permission.metadata, dict) else {}
        authorized_paths = metadata.get("authorized_paths")
        return bool(
            metadata.get("resolution") == "user_allowed"
            and isinstance(authorized_paths, list)
            and [str(path) for path in authorized_paths] == list(permission.scope_paths)
        )

    def abandon_authorized(self, request_id: str) -> PermissionRequestRecord:
        """Close an unfinished authorization without another filesystem write.

        The historical allow-once receipt remains immutable.  Only the attempt
        transaction journal is closed, so a failed recovery cannot deadlock the
        WorkItem forever.  Already-published partial targets are deliberately
        left untouched because removing them would be a new external side
        effect that the recovery card did not authorize.
        """

        permission = self.store.get_permission_request(request_id)
        if permission is None:
            raise WorkLedgerNotFound(f"unknown permission request: {request_id}")
        if not self.can_resume_authorized(permission):
            raise WorkLedgerConflict("Desktop export has no recoverable authorization")
        attempt = self.store.get_attempt(permission.attempt_id)
        if attempt is None:  # pragma: no cover - protected by FK
            raise WorkLedgerConflict("permission request lost its WorkItem attempt")
        existing = attempt.metadata.get("export_resolution")
        journal = dict(existing) if isinstance(existing, dict) else {}
        journal.update(
            {
                "permission_request_id": request_id,
                "status": "abandoned",
                "authorized_paths": list(permission.scope_paths),
            }
        )
        self.store.update_attempt(
            permission.attempt_id,
            metadata={"export_resolution": journal},
        )
        return permission

    def _publish_atomic_no_replace(
        self,
        source: Path,
        target: Path,
        *,
        expected_hash: str,
        temporary_path: Path,
    ) -> None:
        """Fsync a private sibling, then atomically publish via hard link.

        ``os.link`` has no replace mode: if the target appears at any point,
        publication fails without touching it.  The temporary path is unlinked
        only when its filesystem identity still matches the file created here.
        """

        if (
            not target.parent.is_dir()
            or self._is_link_or_junction(target.parent)
            or not self._same_or_child(target.parent.resolve(), self.desktop_path)
        ):
            raise WorkLedgerConflict("export target parent escaped the approved Desktop scope")
        temporary = temporary_path.resolve()
        if temporary.parent != target.parent.resolve():
            raise WorkLedgerConflict("export transaction temp escaped Desktop scope")
        descriptor: int | None
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(temporary, flags, 0o600)
        except FileExistsError as exc:
            if not self._target_matches(temporary, expected_hash):
                raise WorkLedgerConflict(
                    f"export transaction temp already exists and differs: {target.name}"
                ) from exc
            descriptor = None
        created_stat = os.fstat(descriptor) if descriptor is not None else temporary.lstat()
        identity = (int(created_stat.st_dev), int(created_stat.st_ino))
        try:
            if descriptor is not None:
                with os.fdopen(descriptor, "wb") as destination, source.open("rb") as source_handle:
                    shutil.copyfileobj(source_handle, destination, length=1024 * 1024)
                    destination.flush()
                    os.fsync(destination.fileno())
            if not self._target_matches(temporary, expected_hash):
                raise WorkLedgerConflict(f"temporary export verification failed: {target.name}")
            try:
                os.link(temporary, target, follow_symlinks=False)
            except FileExistsError as exc:
                if self._target_matches(target, expected_hash):
                    return
                raise WorkLedgerConflict(
                    f"Desktop target appeared during approval and was not overwritten: {target.name}"
                ) from exc
            if not self._target_matches(target, expected_hash):
                raise WorkLedgerConflict(f"export verification failed: {target.name}")
        finally:
            self._unlink_if_identity_matches(temporary, identity)

    def _publish_atomic_replace(
        self,
        source: Path,
        target: Path,
        *,
        expected_hash: str,
        expected_target_hash: str,
        temporary_path: Path,
    ) -> None:
        """Atomically replace one previously approved Desktop deliverable.

        This path is reachable only from an immutable permission entry that
        pins both the new staged hash and the exact old Desktop hash.  The
        target identity and bytes are checked again immediately before
        ``os.replace`` so ordinary exports retain their strict no-overwrite
        behavior while amendments cannot erase user edits made after approval.
        """

        if (
            not target.parent.is_dir()
            or self._is_link_or_junction(target.parent)
            or not self._same_or_child(target.parent.resolve(), self.desktop_path)
        ):
            raise WorkLedgerConflict("export target parent escaped the approved Desktop scope")
        temporary = temporary_path.resolve()
        if temporary.parent != target.parent.resolve():
            raise WorkLedgerConflict("export transaction temp escaped Desktop scope")
        try:
            target_before = target.lstat()
        except OSError as exc:
            raise WorkLedgerConflict(
                f"approved Desktop target is missing: {target.name}"
            ) from exc
        if (
            not stat.S_ISREG(target_before.st_mode)
            or target.is_symlink()
            or self._sha256(target) != expected_target_hash
        ):
            raise WorkLedgerConflict(
                f"Desktop target changed since its last approval: {target.name}"
            )
        target_identity = (int(target_before.st_dev), int(target_before.st_ino))

        descriptor: int | None
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(temporary, flags, 0o600)
        except FileExistsError as exc:
            if not self._target_matches(temporary, expected_hash):
                raise WorkLedgerConflict(
                    f"export transaction temp already exists and differs: {target.name}"
                ) from exc
            descriptor = None
        created_stat = os.fstat(descriptor) if descriptor is not None else temporary.lstat()
        temp_identity = (int(created_stat.st_dev), int(created_stat.st_ino))
        try:
            if descriptor is not None:
                with os.fdopen(descriptor, "wb") as destination, source.open("rb") as source_handle:
                    shutil.copyfileobj(source_handle, destination, length=1024 * 1024)
                    destination.flush()
                    os.fsync(destination.fileno())
            if not self._target_matches(temporary, expected_hash):
                raise WorkLedgerConflict(f"temporary export verification failed: {target.name}")
            target_now = target.lstat()
            if (
                not stat.S_ISREG(target_now.st_mode)
                or target.is_symlink()
                or (int(target_now.st_dev), int(target_now.st_ino)) != target_identity
                or self._sha256(target) != expected_target_hash
            ):
                raise WorkLedgerConflict(
                    f"Desktop target changed during approval: {target.name}"
                )
            os.replace(temporary, target)
            if not self._target_matches(target, expected_hash):
                raise WorkLedgerConflict(f"replacement export verification failed: {target.name}")
        finally:
            self._unlink_if_identity_matches(temporary, temp_identity)

    @classmethod
    def _unlink_transaction_temp_if_exact(cls, path: Path, expected_hash: str) -> None:
        """Remove only an unchanged exact transaction temp after target commit."""

        try:
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode) or path.is_symlink():
                return
            identity = (int(before.st_dev), int(before.st_ino))
            if cls._sha256(path) != expected_hash:
                return
            after = path.lstat()
            if (int(after.st_dev), int(after.st_ino)) != identity:
                return
            cls._unlink_if_identity_matches(path, identity)
        except OSError:
            return

    @staticmethod
    def _target_matches(path: Path, expected_hash: str) -> bool:
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or path.is_symlink():
                return False
            return WorkExportService._sha256(path) == expected_hash
        except OSError:
            return False

    @staticmethod
    def _unlink_if_identity_matches(path: Path, identity: tuple[int, int]) -> None:
        try:
            info = path.lstat()
            if (
                stat.S_ISREG(info.st_mode)
                and (int(info.st_dev), int(info.st_ino)) == identity
            ):
                path.unlink()
        except OSError:
            # A leftover private temp is safer than deleting a path that can no
            # longer be proven to be ours.
            return

    def _validated_staging_root(
        self,
        item: WorkItemRecord,
        attempt: RunAttemptRecord,
        plan: dict[str, Any],
    ) -> Path:
        root = Path(str(plan.get("staging_root") or "")).resolve()
        expected = self.ensure_private_workspace_child(
            Path(item.workspace_path).resolve(),
            "proposed_exports",
            attempt.attempt_id,
        )
        if root != expected or not path_is_within(str(root), item.workspace_path):
            raise WorkLedgerConflict("staged export directory does not belong to this WorkItem attempt")
        return root

    @staticmethod
    def _staging_root(workspace: Path, attempt_id: str) -> Path:
        return Path(workspace).resolve() / ".amadeus" / "proposed_exports" / attempt_id

    @classmethod
    def ensure_private_workspace_root(cls, workspace: Path) -> Path:
        """Create Amadeus' workspace-private namespace without dirtying Git."""

        resolved_workspace = Path(workspace).resolve()
        private_root = resolved_workspace / ".amadeus"
        if cls._is_link_or_junction(private_root):
            raise WorkLedgerConflict(
                "the .amadeus workspace namespace cannot be a symlink or junction"
            )
        try:
            private_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkLedgerConflict(
                "the .amadeus workspace namespace is not a usable directory"
            ) from exc
        if cls._is_link_or_junction(private_root):
            raise WorkLedgerConflict(
                "the .amadeus workspace namespace became a symlink or junction"
            )
        resolved_private = private_root.resolve()
        try:
            resolved_private.relative_to(resolved_workspace)
        except ValueError as exc:
            raise WorkLedgerConflict("the .amadeus workspace namespace escaped the workspace") from exc
        ignore_file = resolved_private / ".gitignore"
        if cls._is_link_or_junction(ignore_file):
            raise WorkLedgerConflict("the private .gitignore cannot be a symlink or junction")
        try:
            with ignore_file.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write("*\n")
        except FileExistsError:
            pass
        return resolved_private

    @classmethod
    def ensure_private_workspace_child(cls, workspace: Path, *parts: str) -> Path:
        """Create and validate each private child without following reparse points.

        Checking only ``.amadeus`` is insufficient on Windows because a child
        such as ``proposed_exports`` may itself be a junction.  Every segment
        is therefore checked before and after creation, then resolved back
        beneath the already-validated private root.
        """

        private_root = cls.ensure_private_workspace_root(workspace)
        current = private_root
        for raw_part in parts:
            part = str(raw_part or "").strip()
            parsed = Path(part)
            if (
                not part
                or part in {".", ".."}
                or parsed.is_absolute()
                or len(parsed.parts) != 1
                or parsed.name != part
            ):
                raise WorkLedgerConflict("invalid private workspace path segment")
            candidate = current / part
            if cls._is_link_or_junction(candidate):
                raise WorkLedgerConflict(
                    f"the private workspace child cannot be a symlink or junction: {part}"
                )
            try:
                candidate.mkdir(exist_ok=True)
            except OSError as exc:
                raise WorkLedgerConflict(
                    f"the private workspace child is not a usable directory: {part}"
                ) from exc
            if cls._is_link_or_junction(candidate):
                raise WorkLedgerConflict(
                    f"the private workspace child became a symlink or junction: {part}"
                )
            resolved_candidate = candidate.resolve()
            try:
                resolved_candidate.relative_to(private_root)
            except ValueError as exc:
                raise WorkLedgerConflict(
                    "the private workspace child escaped the workspace"
                ) from exc
            current = resolved_candidate
        return current

    @staticmethod
    def _is_link_or_junction(path: Path) -> bool:
        try:
            if path.is_symlink():
                return True
            is_junction = getattr(path, "is_junction", None)
            if callable(is_junction) and is_junction():
                return True
            # ``Path.is_junction`` was added in Python 3.12, while Amadeus
            # supports 3.10/3.11 too.  On older Windows runtimes inspect the
            # reparse-point attribute directly; it is absent on POSIX.
            try:
                info = path.lstat()
            except FileNotFoundError:
                return False
            attributes = int(getattr(info, "st_file_attributes", 0) or 0)
            reparse_flag = int(
                getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400) or 0x400
            )
            return bool(attributes & reparse_flag)
        except OSError:
            # A path whose reparse identity cannot be inspected is not safe as
            # an authority-bearing staging or runtime directory.
            return True

    def _validated_target_root(self, plan: dict[str, Any]) -> Path:
        root = Path(str(plan.get("target_root") or "")).resolve()
        if root != self.desktop_path:
            raise WorkLedgerConflict("export plan target is not the configured Desktop")
        if not root.is_dir() or self._is_link_or_junction(root):
            raise WorkLedgerConflict("the configured Desktop is not a safe existing directory")
        return root

    @staticmethod
    def _safe_relative_path(value: str) -> Path:
        normalized = str(value or "").strip().replace("\\", "/")
        parts = normalized.split("/") if normalized else []
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise WorkLedgerConflict("export permission contains an unsafe relative path")
        relative = Path(*parts)
        if relative.is_absolute() or relative.drive or relative.root:
            raise WorkLedgerConflict("export permission contains an absolute relative path")
        return relative

    @classmethod
    def _directory_paths_for_entries(
        cls,
        target_root: Path,
        entries: Iterable[dict[str, Any]],
    ) -> list[Path]:
        """Derive every Desktop directory needed by the immutable file manifest."""

        resolved_root = target_root.resolve()
        directories: set[Path] = set()
        for raw in entries:
            if not isinstance(raw, dict):
                raise WorkLedgerConflict("permission export entry is malformed")
            relative = cls._safe_relative_path(str(raw.get("relative_path") or ""))
            current = (resolved_root / relative).parent.resolve()
            while current != resolved_root:
                if not cls._same_or_child(current, resolved_root):
                    raise WorkLedgerConflict("export directory escaped Desktop scope")
                directories.add(current)
                parent = current.parent.resolve()
                if parent == current:
                    raise WorkLedgerConflict("export directory has no Desktop ancestor")
                current = parent
        if len(directories) > _MAX_EXPORT_DIRECTORIES:
            raise WorkLedgerConflict("staged export exceeds the bounded directory limit")
        return sorted(
            directories,
            key=lambda path: (
                len(path.relative_to(resolved_root).parts),
                path.as_posix().casefold(),
            ),
        )

    def _prepare_export_directories(
        self,
        directories: Iterable[Path],
        prepared: Iterable[_PreparedExport],
        *,
        resume_authorized: bool,
    ) -> None:
        """Create only approved directories, then prove the tree contains no extras."""

        created: set[Path] = set()
        materialized = list(directories)
        for directory in materialized:
            resolved = directory.resolve()
            if resolved == self.desktop_path or not self._same_or_child(
                resolved, self.desktop_path
            ):
                raise WorkLedgerConflict("export directory escaped the approved Desktop scope")
            if directory.exists():
                if directory not in created and not resume_authorized:
                    raise WorkLedgerConflict(
                        "Desktop deliverable directory appeared during approval and was not merged: "
                        f"{directory.name}"
                    )
            else:
                try:
                    directory.mkdir()
                except FileExistsError as exc:
                    if not resume_authorized:
                        raise WorkLedgerConflict(
                            "Desktop deliverable directory appeared during approval and was not merged: "
                            f"{directory.name}"
                        ) from exc
            if not directory.is_dir() or self._is_link_or_junction(directory):
                raise WorkLedgerConflict(
                    f"Desktop deliverable directory is unsafe: {directory.name}"
                )
            created.add(directory)
        self._validate_export_directory_tree(materialized, prepared)

    def _validate_export_directory_tree(
        self,
        directories: Iterable[Path],
        prepared: Iterable[_PreparedExport],
    ) -> None:
        """Reject merges: every visible child must belong to the approved manifest."""

        materialized_directories = [path.resolve() for path in directories]
        prepared_entries = list(prepared)
        allowed = set(materialized_directories)
        allowed.update(
            target.resolve()
            for _source, target, _temp, _hash, _replace, _old in prepared_entries
        )
        allowed.update(
            temp.resolve()
            for _source, _target, temp, _hash, _replace, _old in prepared_entries
        )
        for directory in materialized_directories:
            if not directory.is_dir() or self._is_link_or_junction(directory):
                raise WorkLedgerConflict(
                    f"Desktop deliverable directory is missing or unsafe: {directory.name}"
                )
            for child in directory.iterdir():
                if self._is_link_or_junction(child):
                    raise WorkLedgerConflict(
                        f"Desktop deliverable contains an unsafe link: {child.name}"
                    )
                resolved_child = child.resolve()
                if resolved_child not in allowed:
                    raise WorkLedgerConflict(
                        "Desktop deliverable directory contains an unapproved path: "
                        f"{child.name}"
                    )

    @classmethod
    def _bounded_files(cls, root: Path) -> list[Path]:
        if not root.is_dir() or cls._is_link_or_junction(root):
            return []
        output: list[Path] = []
        total_bytes = 0
        directory_count = 0
        root_entries: set[str] = set()
        ignored_suffixes = {".bak", ".log", ".pyc", ".pyo", ".tmp"}
        ignored_directories = {"__pycache__"}
        resolved_root = root.resolve()

        def visit(directory: Path) -> None:
            nonlocal total_bytes, directory_count
            for candidate in sorted(
                directory.iterdir(),
                key=lambda value: value.name.casefold(),
            ):
                if cls._is_link_or_junction(candidate):
                    raise WorkLedgerConflict(
                        f"staged export contains an unsafe link: {candidate.name}"
                    )
                if candidate.name.startswith("."):
                    continue
                info = candidate.lstat()
                if stat.S_ISDIR(info.st_mode):
                    if candidate.name.casefold() in ignored_directories:
                        continue
                    resolved_directory = candidate.resolve()
                    if not cls._same_or_child(resolved_directory, resolved_root):
                        raise WorkLedgerConflict("staged export directory escaped its attempt")
                    directory_count += 1
                    if directory_count > _MAX_EXPORT_DIRECTORIES:
                        raise WorkLedgerConflict(
                            "staged export exceeds the bounded directory limit"
                        )
                    visit(resolved_directory)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise WorkLedgerConflict(
                        f"staged export contains an unsupported path: {candidate.name}"
                    )
                if candidate.suffix.lower() in ignored_suffixes:
                    continue
                resolved = candidate.resolve()
                if not cls._same_or_child(resolved, resolved_root):
                    raise WorkLedgerConflict("staged export file escaped its attempt")
                size = info.st_size
                root_entries.add(resolved.relative_to(resolved_root).parts[0].casefold())
                if len(root_entries) > _MAX_EXPORT_ROOT_ENTRIES:
                    raise WorkLedgerConflict(
                        "staged export exceeds the bounded top-level deliverable limit"
                    )
                if len(output) >= _MAX_EXPORT_FILES or total_bytes + size > _MAX_EXPORT_BYTES:
                    raise WorkLedgerConflict(
                        "staged export exceeds the bounded file or size limit"
                    )
                output.append(resolved)
                total_bytes += size

        visit(resolved_root)
        return output

    @staticmethod
    def _has_desktop_destination(value: str) -> bool:
        text = " ".join(str(value or "").split())
        if not text:
            return False
        # Natural-language matching is deliberately high precision.  A bare
        # Desktop path or phrase such as "files on the Desktop" can describe
        # an input, reference, or runtime platform; it is not export authority.
        negated = (
            r"\b(?:do\s+not|don't|dont|never|without|not)\b.{0,140}\bdesktop\b",
            r"(?:不要|别|別|请勿|請勿|不用|无需|無需|禁止).{0,100}桌面",
            r"デスクトップ.{0,80}(?:保存しない|出力しない|書き込まない)",
        )
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in negated):
            return False

        english_to_target = (
            r"\b(?:build|copy|create|deliver|develop|export|generate|make|place|put|save|write)\b"
            r".{0,120}\b(?:to|onto)\s+(?:(?:the|my)\s+)?desktop"
            r"(?:\s+(?:folder|directory))?(?=$|[\s.,;:!?])"
        )
        english_path_target = (
            r"\b(?:copy|deliver|export|place|put|save|write)\b.{0,120}\b(?:to|onto)\s+"
            r"(?:[a-z]:)?[^\s,;]{0,120}[\\/]desktop(?:[\\/][^\s,;]+)?"
        )
        if re.search(english_to_target, text, re.IGNORECASE) or re.search(
            english_path_target, text, re.IGNORECASE
        ):
            return True

        # "on/in Desktop" is more ambiguous than "to Desktop".  Accept it
        # only for an output-shaped clause, and reject when a later source or
        # runtime verb owns the Desktop phrase (list/read/run/about/from...).
        lowered = text.casefold()
        desktop_index = lowered.rfind("desktop")
        if desktop_index >= 0:
            prefix = lowered[:desktop_index]
            destination_matches = list(
                re.finditer(
                    r"\b(?:build|create|deliver|develop|export|generate|make|place|put|save|write)\b",
                    prefix,
                )
            )
            source_matches = list(
                re.finditer(
                    r"\b(?:about|from|inspect|list|manage|monitor|read|run|scan|using)\b",
                    prefix,
                )
            )
            source_dominates = bool(
                source_matches
                and (
                    not destination_matches
                    or source_matches[-1].start() > destination_matches[-1].start()
                )
            )
            on_desktop = r"\b(?:on|in)\s+(?:(?:the|my)\s+)?desktop\b"
            named_file = re.search(
                r"(?<![\w.])[\w@()+-][\w@().+-]*\."
                r"(?:c|cc|conf|cpp|cs|css|csv|cfg|go|h|hpp|html|ini|ipynb|java|js|jsx|"
                r"json|md|markdown|php|ps1|py|rb|rs|sh|sql|svg|toml|ts|tsx|tsv|txt|xml|"
                r"yaml|yml)\b",
                lowered,
            )
            output_clause = re.search(
                r"\b(?:deliver|export|place|put|save)\b.{0,100}" + on_desktop,
                lowered,
            )
            named_output_clause = named_file is not None and re.search(
                r"\b(?:build|create|develop|generate|make|save|write)\b.{0,140}" + on_desktop,
                lowered,
            )
            artifact_output_clause = re.search(
                r"\b(?:build|create|develop|generate|make)\b.{0,90}"
                r"\b(?:app|application|artifact|deliverable|game|program)\b.{0,30}"
                + on_desktop,
                lowered,
            )
            if not source_dominates and (
                output_clause or named_output_clause or artifact_output_clause
            ):
                return True

        destination_patterns = (
            r"(?:保存|儲存|存|输出|輸出|导出|導出|复制|複製)"
            r"(?:到|至|在|入|为|為|往)(?:我的)?桌面(?:上)?",
            r"(?:移|移动|移動|搬)(?:到|至|往|入)(?:我的)?桌面(?:上)?",
            r"放(?:到|至|在|于|於|往)?(?:我的)?桌面(?:上)?",
            r"(?:写|寫|创建|創建|生成|开发|開發|制作|製作|给我做|幫我做|帮我做|做个|做個|做一个|做一個)"
            r".{0,80}(?:到|至|在|于|於|往|放(?:到|在|至|于|於)?|存(?:到|在|至|于|於)?)"
            r"(?:我的)?桌面(?:上)?",
            r"(?:在|到|至|往)(?:我的)?桌面(?:上)?.{0,24}"
            r"(?:保存|儲存|写入|寫入|写|寫|创建|創建|生成|开发|開發|制作|製作)",
            r"(?:请|請|帮|幫)?(?:在)?我的桌面(?:上)?.{0,24}"
            r"(?:写|寫|创建|創建|生成|开发|開發|制作|製作|做个|做個|做一个|做一個)",
            r"デスクトップ(?:に|へ|上に|上へ).{0,40}(?:保存|作成|生成|書き|出力|コピー)",
            r"(?:保存|作成|生成|書き|出力|コピー).{0,40}デスクトップ(?:に|へ|上に|上へ)",
        )
        return any(
            re.search(pattern, text, re.IGNORECASE)
            for pattern in destination_patterns
        )

    @staticmethod
    def _requested_filename(value: str, *, allow_bare: bool = True) -> str:
        text = str(value or "").strip()
        if (
            allow_bare
            and text
            and not any(marker.casefold() in text.casefold() for marker in _DESKTOP_MARKERS)
        ):
            # Explicit metadata may contain only the filename.
            candidate = text.strip("`\"' ")
            if re.fullmatch(r"[A-Za-z0-9_. ()\-]+\.[A-Za-z0-9]{1,12}", candidate):
                return Path(candidate).name
        for pattern in _FILENAME_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            candidate = str(match.group("name") or "").strip("`\"' .")
            if candidate and candidate not in {".", ".."}:
                return Path(candidate).name
        return ""

    @staticmethod
    def _bundle_directory_name(value: str) -> str:
        """Return one readable, Windows-safe directory component.

        The name is presentation only; the immutable permission manifest owns
        the actual publication scope.  Avoiding random suffixes keeps a
        user's first approved application easy to find, while an existing
        directory still fails closed instead of being merged.
        """

        candidate = re.sub(
            r'[<>:"/\\|?*\x00-\x1f]+',
            "-",
            " ".join(str(value or "").split()),
        ).strip(" .-")[:80]
        if not candidate or candidate.casefold() in {
            "con",
            "prn",
            "aux",
            "nul",
            *(f"com{index}" for index in range(1, 10)),
            *(f"lpt{index}" for index in range(1, 10)),
        }:
            return "AUIP App"
        return candidate

    @staticmethod
    def _same_or_child(path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except (OSError, ValueError):
            return path.resolve() == root.resolve()

    @staticmethod
    def _file_fact(path: Path) -> dict[str, Any]:
        stat = path.stat()
        return {
            "size_bytes": stat.st_size,
            "modified_at": stat.st_mtime,
            "sha256": WorkExportService._sha256(path),
        }

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _proposed_patch(
        previews: Iterable[tuple[dict[str, Any], bytes]],
    ) -> tuple[str, list[str]]:
        """Build one explicit approval representation for every Provider file.

        UTF-8 text remains a complete diff. Binary or undecodable bytes are
        represented by their exact path, size, media-type hint, and SHA-256;
        they are never silently omitted or decoded lossy. Over-budget UTF-8
        text still fails closed rather than changing from reviewable text into
        an opaque identity merely because it is large.

        Host-materialized runtime assets are excluded before this function only
        after their exact Host-recorded hash and size are reverified; permission
        metadata still lists them as ``host_verified_opaque``. The bytes passed
        here are the exact provider-controlled snapshot bytes used for the
        permission hashes.
        """

        parts: list[str] = []
        changed_files: list[str] = []
        total_source_bytes = 0
        total_source_lines = 0
        for entry, raw in previews:
            source = Path(str(entry.get("source_path") or ""))
            relative = str(entry.get("relative_path") or source.name).replace("\\", "/")
            display = f"Desktop/{relative}"
            changed_files.append(display)
            text: str | None = None
            try:
                if b"\x00" in raw:
                    raise UnicodeDecodeError("utf-8", raw, 0, 1, "NUL byte")
                text = raw.decode("utf-8")
                if any(
                    ord(character) < 32 and character not in "\t\r\n\f"
                    for character in text
                ):
                    text = None
            except UnicodeDecodeError:
                text = None
            if text is None:
                media_type = mimetypes.guess_type(relative, strict=False)[0]
                entry["preview_status"] = "binary_identity"
                entry["media_type_hint"] = media_type or "application/octet-stream"
                parts.append(f"diff --git a/{display} b/{display}")
                if entry.get("replace_existing") is not True:
                    parts.append("new file mode 100644")
                parts.extend(
                    [
                        f"Binary file identity: {display}",
                        f"Media-Type: {entry['media_type_hint']}",
                        f"Size: {len(raw)} bytes",
                        f"SHA-256: {entry['sha256']}",
                    ]
                )
                continue
            lines = text.splitlines()
            total_source_bytes += len(raw)
            total_source_lines += len(lines)
            if entry.get("replace_existing") is True:
                target = Path(str(entry.get("target_path") or ""))
                expected_old_hash = str(
                    entry.get("expected_target_sha256") or ""
                ).strip()
                if not expected_old_hash or not WorkExportService._target_matches(
                    target,
                    expected_old_hash,
                ):
                    raise WorkLedgerConflict(
                        f"Desktop target changed while building its approval preview: {source.name}"
                    )
                old_raw = target.read_bytes()
                try:
                    if b"\x00" in old_raw:
                        raise UnicodeDecodeError("utf-8", old_raw, 0, 1, "NUL byte")
                    old_text = old_raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise WorkLedgerConflict(
                        f"existing Desktop export cannot be fully previewed as UTF-8 text: {source.name}"
                    ) from exc
                if any(
                    ord(character) < 32 and character not in "\t\r\n\f"
                    for character in old_text
                ):
                    raise WorkLedgerConflict(
                        f"existing Desktop export contains binary control bytes: {source.name}"
                    )
                old_lines = old_text.splitlines()
                total_source_bytes += len(old_raw)
                total_source_lines += len(old_lines)
                if total_source_bytes > _MAX_DIFF_BYTES or total_source_lines > _MAX_DIFF_LINES:
                    raise WorkLedgerConflict(
                        "staged export is too large for a complete approval preview"
                    )
                parts.append(f"diff --git a/{display} b/{display}")
                parts.extend(
                    difflib.unified_diff(
                        old_lines,
                        lines,
                        fromfile=f"a/{display}",
                        tofile=f"b/{display}",
                        lineterm="",
                    )
                )
                continue
            if total_source_bytes > _MAX_DIFF_BYTES or total_source_lines > _MAX_DIFF_LINES:
                raise WorkLedgerConflict(
                    "staged export is too large for a complete approval preview"
                )
            parts.extend(
                [
                    f"diff --git a/{display} b/{display}",
                    "new file mode 100644",
                    "--- /dev/null",
                    f"+++ b/{display}",
                    f"@@ -0,0 +1,{len(lines)} @@",
                    *(f"+{line}" for line in lines),
                ]
            )
            if text and not text.endswith(("\n", "\r")):
                parts.append("\\ No newline at end of file")
        patch = "\n".join(parts)
        if len(patch.encode("utf-8")) > _MAX_DIFF_BYTES:
            raise WorkLedgerConflict(
                "staged export diff is too large for a complete approval preview"
            )
        return patch, changed_files
