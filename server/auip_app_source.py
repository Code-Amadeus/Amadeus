"""Resolve one registered artifact into a verified external-app entry point.

`auip_runtime` owns AppSession truth and must not learn where apps come from.
This module owns the opposite half: given a work artifact the ledger already
knows about, is the host willing to hand its bytes to a sandboxed surface, and
on what evidence.

The caller names an `artifact_id`, never a path. Everything the host may
launch therefore had to be discovered by the artifact registry inside an
attempt's own workspace delta first; there is no route here from a filename
to a read.  This module validates identity and returns a canonical path.  It
does not read application bytes into a renderer.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Protocol

from agent_host.work_ledger_types import canonicalize_path, path_is_within
from server.auip_contract import AuipProtocolError, parse_manifest


RUNNABLE_SUFFIXES = frozenset({".html", ".htm"})
logger = logging.getLogger(__name__)


class ArtifactSource(Protocol):
    """The narrow slice of the work ledger this module reads."""

    def get_artifact(self, artifact_id: str) -> Any: ...

    def get_work_item(self, work_item_id: str) -> Any: ...

    def get_attempt(self, attempt_id: str) -> Any: ...

    def list_artifacts(self, work_item_id: str, *, attempt_id: str = "") -> list[Any]: ...

    def find_work_item_ids_by_artifact_name(
        self,
        name: str,
        *,
        kind: str = "business.file",
        limit: int = 64,
    ) -> list[str]: ...


def validate_registered_app(store: ArtifactSource, artifact_id: str) -> dict[str, Any]:
    """Return one unchanged stable workspace app entry, or fail closed."""

    return _validate_registered_app(store, artifact_id, allow_proposed_export=False)


def _validate_registered_app(
    store: ArtifactSource,
    artifact_id: str,
    *,
    allow_proposed_export: bool,
) -> dict[str, Any]:
    """Validate registered bytes for either launch or Host staging evidence."""

    clean_id = str(artifact_id or "").strip()
    if not clean_id:
        raise AuipProtocolError("missing_value", "artifact_id")

    record = store.get_artifact(clean_id)
    if record is None:
        raise AuipProtocolError("unknown_artifact", clean_id)
    if str(record.kind) != "business.file":
        raise AuipProtocolError("artifact_not_a_file", str(record.kind))
    # "pending" means the registry could not attribute the file to this
    # attempt. Running it would present someone else's file as this
    # WorkItem's app, so ambiguity fails closed rather than being resolved
    # by a guess here.
    if str(record.status) != "registered":
        raise AuipProtocolError("artifact_not_registered", str(record.status))

    item = store.get_work_item(str(record.work_item_id))
    if item is None:
        raise AuipProtocolError("unknown_work_item", str(record.work_item_id))

    path = _contained_path(str(item.workspace_path), str(record.path))
    if path is None:
        raise AuipProtocolError("artifact_outside_workspace", str(record.path))
    if _is_proposed_export_path(path) and not allow_proposed_export:
        # Provider staging is transaction input, not a durable application
        # revision.  The export transaction may remove these bytes as soon as
        # approval commits, so launching them would race the authority
        # boundary and could execute a file that disappears between Attach
        # preparation and renderer navigation.
        raise AuipProtocolError("artifact_is_proposed_export", str(record.path))
    if path.suffix.lower() not in RUNNABLE_SUFFIXES:
        raise AuipProtocolError("artifact_not_runnable", path.suffix or "no suffix")

    # The registered artifact is a specific revision of the app. If the bytes
    # have moved on, the ledger's fact is stale, and launching the path would
    # run code the ledger does not describe. Re-registering is the caller's
    # move.
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AuipProtocolError("artifact_unreadable", exc.strerror or str(exc)) from exc
    registered_digest = str(record.sha256 or "")
    actual_digest = hashlib.sha256(raw).hexdigest()
    if registered_digest and registered_digest != actual_digest:
        raise AuipProtocolError("artifact_revision_changed", actual_digest[:16])
    normalized_entry = raw.replace(b"\\", b"/").lower()
    if b".amadeus/runtime/authoring_inputs/" in normalized_entry:
        raise AuipProtocolError("artifact_depends_on_private_authoring_input")

    return {
        "artifact_id": str(record.artifact_id),
        "work_item_id": str(record.work_item_id),
        "attempt_id": str(record.attempt_id),
        "title": str(record.title or path.name),
        "artifact_ref": f"artifact:{record.artifact_id}@{actual_digest[:16]}",
        "sha256": actual_digest,
        "entry_path": str(path),
    }


def discover_registered_auip_app(
    store: ArtifactSource,
    work_item_id: str,
) -> dict[str, Any] | None:
    """Discover one current AUIP application in a WorkItem.

    A generic HTML delivery is not an AUIP application.  The host requires one
    registered ``auip.manifest.json`` and a registered entry document beside
    it.  Both records must still describe the bytes on disk.  This is a
    capability check, not language classification and not Provider output.

    AUIP v0 intentionally has no manifest ``entry`` field.  Its bounded
    convention is ``index.html`` when present, otherwise exactly one HTML file
    beside the manifest.  Multiple apps in one WorkItem therefore fail closed
    until a later protocol version can name their entry points explicitly.
    """

    return _discover_registered_auip_app(
        store,
        work_item_id,
        attempt_id="",
        proposed_only=False,
    )


def discover_staged_auip_app(
    store: ArtifactSource,
    work_item_id: str,
    attempt_id: str,
) -> dict[str, Any] | None:
    """Verify one exact Attempt's proposed AUIP bundle without launching it.

    This is outcome evidence for the Host-managed authoring transaction. A
    returned bundle remains unlaunchable until its external delivery is
    approved and recorded as ``business.export``.
    """

    clean_attempt_id = str(attempt_id or "").strip()
    if not clean_attempt_id:
        return None
    return _discover_registered_auip_app(
        store,
        work_item_id,
        attempt_id=clean_attempt_id,
        proposed_only=True,
    )


def _discover_registered_auip_app(
    store: ArtifactSource,
    work_item_id: str,
    *,
    attempt_id: str,
    proposed_only: bool,
) -> dict[str, Any] | None:

    clean_work_item_id = str(work_item_id or "").strip()
    if not clean_work_item_id or store.get_work_item(clean_work_item_id) is None:
        return None
    # Artifacts are attempt-scoped evidence, so an amendment may register the
    # same path again without replacing the older row.  Capability discovery
    # is about the WorkItem's current tree: keep the newest record per path
    # before applying the registration and manifest rules.  Filtering first
    # would let an older "registered" row outvote a newer rejected revision.
    artifacts = _latest_artifacts_by_path(
        store,
        store.list_artifacts(clean_work_item_id, attempt_id=attempt_id),
    )
    manifests = [
        record
        for record in artifacts
        if _registered_file(record)
        and Path(str(record.path)).name.casefold() == "auip.manifest.json"
    ]
    if len(manifests) != 1:
        return None
    manifest_record = manifests[0]
    manifest_path = _verified_artifact_path(store, manifest_record)
    if manifest_path is None:
        return None
    if _is_proposed_export_path(manifest_path) is not proposed_only:
        return None
    try:
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = parse_manifest(raw_manifest)
    except (OSError, UnicodeError, json.JSONDecodeError, AuipProtocolError):
        return None

    siblings: list[tuple[Any, dict[str, Any]]] = []
    for record in artifacts:
        if not (
            _registered_file(record)
            and Path(str(record.path)).suffix.casefold() in RUNNABLE_SUFFIXES
            and _path_key(str(Path(str(record.path)).parent))
            == _path_key(str(manifest_path.parent))
        ):
            continue
        try:
            verified_entry = _validate_registered_app(
                store,
                str(record.artifact_id),
                allow_proposed_export=proposed_only,
            )
        except AuipProtocolError:
            # Historical artifact rows are immutable evidence, but a deleted
            # or superseded entry is not a current application candidate and
            # must not win merely because it used the conventional filename.
            continue
        siblings.append((record, verified_entry))
    preferred = [
        item
        for item in siblings
        if Path(str(item[0].path)).name.casefold() == "index.html"
    ]
    entries = preferred if len(preferred) == 1 else siblings if not preferred else []
    if len(entries) != 1:
        return None
    entry_record, entry = entries[0]
    try:
        validation = _validate_host_managed_workspace_bundle(
            store,
            manifest_path=manifest_path,
            entry_path=Path(str(entry["entry_path"])),
            attempt_ids={
                str(entry_record.attempt_id or ""),
                str(manifest_record.attempt_id or ""),
            }
            - {""},
        )
    except AuipProtocolError as exc:
        logger.warning(
            "[AUIP-ATTACH] Host-managed bundle rejected work_item=%s "
            "code=%s detail=%s",
            clean_work_item_id,
            exc.code,
            str(exc.detail or "")[:240],
        )
        return None
    except Exception:
        logger.exception(
            "[AUIP-ATTACH] Host-managed bundle validation failed work_item=%s",
            clean_work_item_id,
        )
        return None
    return {
        **entry,
        "manifest_artifact_id": str(manifest_record.artifact_id),
        "manifest_attempt_id": str(manifest_record.attempt_id or ""),
        "app": {
            "id": manifest.app_id,
            "title": manifest.title,
            "version": manifest.version,
            **({"objective": manifest.objective} if manifest.objective else {}),
        },
        "stances": list(manifest.stances),
        "contributing_attempt_ids": sorted(
            {
                str(entry_record.attempt_id or ""),
                str(manifest_record.attempt_id or ""),
            }
            - {""}
        ),
        **(
            {"bundle_validation": validation}
            if isinstance(validation, dict)
            else {}
        ),
    }


def discover_approved_auip_app(
    store: ArtifactSource,
    work_item_id: str,
) -> dict[str, Any] | None:
    """Discover one unchanged AUIP bundle from an approved external revision."""

    clean_work_item_id = str(work_item_id or "").strip()
    if not clean_work_item_id or store.get_work_item(clean_work_item_id) is None:
        return None
    approved = _latest_artifacts_by_path(
        store,
        [
            record
            for record in store.list_artifacts(clean_work_item_id)
            if _approved_export_file(record)
        ],
    )
    by_registration: dict[str, list[Any]] = {}
    for record in approved:
        path = _verified_export_path(record)
        if path is None:
            continue
        metadata = (
            record.metadata
            if isinstance(getattr(record, "metadata", None), dict)
            else {}
        )
        permission_id = str(metadata.get("permission_request_id") or "").strip()
        key = f"permission:{permission_id}" if permission_id else f"path:{path.parent}"
        by_registration.setdefault(key, []).append(record)
    candidates = [
        candidate
        for candidate in (
            _exported_bundle_candidate(clean_work_item_id, records)
            for records in by_registration.values()
        )
        if candidate is not None
    ]
    return candidates[0] if len(candidates) == 1 else None


def discover_launchable_auip_app(
    store: ArtifactSource,
    work_item_id: str,
) -> dict[str, Any] | None:
    """Resolve the newest Host-verified workspace or approved app revision."""

    workspace = discover_registered_auip_app(store, work_item_id)
    exported = discover_approved_auip_app(store, work_item_id)
    if workspace is None:
        return exported
    if exported is None:
        return workspace

    def revision(candidate: dict[str, Any]) -> int:
        numbers: list[int] = []
        for attempt_id in candidate.get("contributing_attempt_ids") or ():
            attempt = store.get_attempt(str(attempt_id or ""))
            numbers.append(int(getattr(attempt, "attempt_number", 0) or 0))
        return max(numbers, default=0)

    # At the same causal revision, the approved bytes are the user's durable
    # delivery and therefore the app that should open. A newer registered
    # workspace revision wins only when Work has genuinely advanced.
    return workspace if revision(workspace) > revision(exported) else exported


def validate_launchable_app(store: ArtifactSource, artifact_id: str) -> dict[str, Any]:
    """Validate one opaque launch identity at its current immutable revision."""

    clean_id = str(artifact_id or "").strip()
    if not clean_id:
        raise AuipProtocolError("missing_value", "artifact_id")
    record = store.get_artifact(clean_id)
    if record is None:
        raise AuipProtocolError("unknown_artifact", clean_id)
    if str(getattr(record, "kind", "")) == "business.file":
        return validate_registered_app(store, clean_id)
    if (
        str(getattr(record, "kind", "")) == "business.export"
        and str(getattr(record, "status", "")) == "approved"
    ):
        candidate = discover_approved_auip_app(
            store,
            str(getattr(record, "work_item_id", "")),
        )
        if candidate is not None and str(candidate.get("artifact_id") or "") == clean_id:
            return candidate
        raise AuipProtocolError("artifact_revision_changed", clean_id)
    raise AuipProtocolError(
        "artifact_not_launchable",
        str(getattr(record, "kind", "")),
    )


def discover_exported_auip_apps(
    store: ArtifactSource,
    proposed_manifest: dict[str, Any],
    *,
    limit: int = 64,
) -> list[dict[str, Any]]:
    """Return approved external bundles matching one app's exact contract.

    A Desktop export is already a durable Host fact: the user approved an
    immutable file manifest and the export transaction verified every target
    before recording ``business.export/approved``.  Reusing those rows as the
    application registration avoids a second registry with competing hashes.

    The running app's manifest remains untrusted lookup evidence.  Every file
    in a candidate bundle is re-hashed, and the full parsed manifest must equal
    the approved one before the Host may offer an Attach decision.
    """

    proposed = parse_manifest(proposed_manifest)
    finder = getattr(store, "find_work_item_ids_by_artifact_name", None)
    if not callable(finder):
        return []
    work_item_ids = finder(
        "auip.manifest.json",
        kind="business.export",
        limit=max(1, min(int(limit), 200)),
    )
    matches: list[dict[str, Any]] = []
    for work_item_id in work_item_ids:
        if store.get_work_item(str(work_item_id)) is None:
            continue
        # Pending or denied publication does not change the external bytes and
        # therefore cannot retire the last approved registration. A later
        # approved revision at the same path does supersede it through the
        # ordinary attempt ordering below.
        approved = _latest_artifacts_by_path(
            store,
            [
                record
                for record in store.list_artifacts(str(work_item_id))
                if _approved_export_file(record)
            ],
        )
        by_registration: dict[str, list[Any]] = {}
        for record in approved:
            try:
                parent = Path(str(record.path)).resolve().parent
            except (OSError, RuntimeError, ValueError):
                continue
            metadata = (
                record.metadata if isinstance(getattr(record, "metadata", None), dict) else {}
            )
            permission_id = str(metadata.get("permission_request_id") or "").strip()
            key = f"permission:{permission_id}" if permission_id else f"path:{parent}"
            by_registration.setdefault(key, []).append(record)
        for records in by_registration.values():
            candidate = _exported_bundle_candidate(
                str(work_item_id),
                records,
            )
            if candidate is None:
                continue
            if candidate["manifest"] != proposed.to_dict():
                continue
            matches.append(candidate)
    return sorted(
        matches,
        key=lambda value: (
            str(value.get("app", {}).get("title") or "").casefold(),
            str(value.get("artifact_ref") or ""),
        ),
    )


def is_runnable_artifact(record: Any) -> bool:
    """Whether a listed artifact is a Host-trusted runnable delivery.

    Preparation is allowed to start from either the registered workspace file
    or the user's approved external delivery.  The latter is the normal shape
    of a Desktop export: it has no ``business.file`` row, but it is still the
    durable artifact owned by the same WorkItem.  Pending proposed exports are
    deliberately excluded; approval is the authority boundary.

    This check only discovers authoring context.  It never makes an artifact
    launchable and never reads external bytes.  The amendment/export path
    revalidates the approved target and its digest before a Provider can edit
    it, while direct launch continues to require ``validate_registered_app``.
    """

    if record is None:
        return False
    kind = str(getattr(record, "kind", ""))
    status = str(getattr(record, "status", ""))
    trusted = (
        (kind == "business.file" and status == "registered")
        or (kind == "business.export" and status == "approved")
    )
    if not trusted:
        return False
    path = Path(str(getattr(record, "path", "")))
    return (
        path.suffix.lower() in RUNNABLE_SUFFIXES
        and not _is_proposed_export_path(path)
    )


def list_current_runnable_artifacts(
    store: ArtifactSource,
    work_item_id: str,
) -> list[Any]:
    """Return current trusted HTML deliveries without declaring AUIP capability.

    This is authoring context only. Launch still requires a verified manifest
    through :func:`discover_registered_auip_app`; an HTML artifact in this list
    must never be opened as an AUIP application by itself.  Approved exports
    remain external delivery facts, not workspace ownership or launch proof.
    """

    clean_work_item_id = str(work_item_id or "").strip()
    if not clean_work_item_id or store.get_work_item(clean_work_item_id) is None:
        return []
    return [
        record
        for record in _latest_artifacts_by_path(
            store,
            store.list_artifacts(clean_work_item_id)
        )
        if is_runnable_artifact(record)
    ]


def _registered_file(record: Any) -> bool:
    return (
        record is not None
        and str(getattr(record, "kind", "")) == "business.file"
        and str(getattr(record, "status", "")) == "registered"
        and bool(str(getattr(record, "path", "")).strip())
    )


def _latest_artifacts_by_path(store: ArtifactSource, records: list[Any]) -> list[Any]:
    """Collapse attempt history into the WorkItem's current path view.

    Artifact timestamps can tie at the platform clock's resolution.  Attempt
    number is the ledger's stable causal order, so a later delivery always
    supersedes an older record for the same path.  Timestamps only order
    updates within one attempt.  Narrow simulation stores without attempt
    records retain deterministic fallback ordering.
    """

    attempt_numbers: dict[str, int] = {}

    def revision_key(record: Any) -> tuple[int, float, float, str]:
        attempt_id = str(getattr(record, "attempt_id", "") or "").strip()
        number = attempt_numbers.get(attempt_id)
        if number is None:
            number = 0
            get_attempt = getattr(store, "get_attempt", None)
            if attempt_id and callable(get_attempt):
                attempt = get_attempt(attempt_id)
                number = int(getattr(attempt, "attempt_number", 0) or 0)
            attempt_numbers[attempt_id] = number
        return (
            number,
            float(getattr(record, "updated_at", 0.0) or 0.0),
            float(getattr(record, "created_at", 0.0) or 0.0),
            str(getattr(record, "artifact_id", "") or ""),
        )

    ordered = sorted(
        records,
        key=revision_key,
        reverse=True,
    )
    latest: list[Any] = []
    seen: set[str] = set()
    for record in ordered:
        path = str(getattr(record, "path", "") or "").strip()
        identity = str(getattr(record, "path_identity", "") or "").strip()
        key = identity or _path_key(path)
        if not key or key in seen:
            continue
        seen.add(key)
        latest.append(record)
    return latest


def _approved_export_file(record: Any) -> bool:
    return (
        record is not None
        and str(getattr(record, "kind", "")) == "business.export"
        and str(getattr(record, "status", "")) == "approved"
        and bool(str(getattr(record, "path", "")).strip())
        and bool(str(getattr(record, "sha256", "")).strip())
    )


def _exported_bundle_candidate(
    work_item_id: str,
    records: list[Any],
) -> dict[str, Any] | None:
    verified: list[tuple[Any, Path, str]] = []
    for record in records:
        path = _verified_export_path(record)
        if path is None:
            return None
        verified.append((record, path, str(getattr(record, "sha256", ""))))
    manifests = [
        item
        for item in verified
        if item[1].name.casefold() == "auip.manifest.json"
    ]
    if len(manifests) != 1:
        return None
    manifest_record, manifest_path, _manifest_hash = manifests[0]
    directory = manifest_path.parent
    if any(
        path != directory and directory not in path.parents
        for _record, path, _digest in verified
    ):
        return None
    try:
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        parsed = parse_manifest(raw_manifest)
    except (OSError, UnicodeError, json.JSONDecodeError, AuipProtocolError):
        return None
    html = [
        item
        for item in verified
        if item[1].suffix.casefold() in RUNNABLE_SUFFIXES
        and item[1].parent == directory
    ]
    preferred = [item for item in html if item[1].name.casefold() == "index.html"]
    entries = preferred if len(preferred) == 1 else html if not preferred else []
    if len(entries) != 1:
        return None
    entry_record, entry_path, entry_hash = entries[0]
    bundle_digest = hashlib.sha256(
        "\n".join(
            f"{path.relative_to(directory).as_posix().casefold()}|{digest}"
            for _record, path, digest in sorted(
                verified,
                key=lambda item: item[1].as_posix().casefold(),
            )
        ).encode("utf-8")
    ).hexdigest()
    return {
        "artifact_id": str(getattr(entry_record, "artifact_id", "")),
        "manifest_artifact_id": str(
            getattr(manifest_record, "artifact_id", "")
        ),
        "work_item_id": work_item_id,
        "attempt_id": str(getattr(entry_record, "attempt_id", "") or ""),
        "title": str(getattr(entry_record, "title", "") or entry_path.name),
        "artifact_ref": (
            f"export-bundle:{getattr(manifest_record, 'artifact_id', '')}"
            f"@{bundle_digest[:16]}"
        ),
        "sha256": entry_hash,
        "bundle_sha256": bundle_digest,
        "entry_path": str(entry_path),
        "bundle_path": str(directory),
        "manifest": parsed.to_dict(),
        "app": {
            "id": parsed.app_id,
            "title": parsed.title,
            "version": parsed.version,
            **({"objective": parsed.objective} if parsed.objective else {}),
        },
        "stances": list(parsed.stances),
        "contributing_attempt_ids": sorted(
            {
                str(getattr(record, "attempt_id", "") or "")
                for record, _path, _digest in verified
            }
            - {""}
        ),
    }


def _validate_host_managed_workspace_bundle(
    store: ArtifactSource,
    *,
    manifest_path: Path,
    entry_path: Path,
    attempt_ids: set[str],
) -> dict[str, Any] | None:
    """Revalidate Host-owned runtime assets when an authoring Attempt used them.

    Older registered applications have no such Attempt metadata and retain the
    existing compatibility path. Once the Host declares that it materialized
    a bundle's runtime, however, Attach must prove those exact bytes and exact
    script paths again rather than trusting the earlier completion verdict.
    """

    try:
        manifest_path = Path(canonicalize_path(manifest_path).canonical_path)
        entry_path = Path(canonicalize_path(entry_path).canonical_path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise AuipProtocolError("auip_bundle_location_invalid") from exc

    required_files: set[str] = set()
    bundle_roots: dict[str, Path] = {}
    required = False
    for attempt_id in sorted(attempt_ids):
        attempt = store.get_attempt(attempt_id)
        metadata = (
            getattr(attempt, "metadata", {})
            if isinstance(getattr(attempt, "metadata", {}), dict)
            else {}
        )
        if metadata.get("auip_host_validates_bundle") is not True:
            continue
        required = True
        required_files.update(
            str(value)
            for value in metadata.get("auip_host_materialized_files") or []
            if str(value)
        )
        try:
            root = canonicalize_path(
                str(metadata.get("auip_bundle_root") or "")
            )
            bundle_roots[root.identity_key] = Path(root.canonical_path)
        except (OSError, RuntimeError, ValueError) as exc:
            raise AuipProtocolError(
                "auip_bundle_root_invalid",
                str(attempt_id or "")[:160],
            ) from exc
    if not required:
        return None
    if len(bundle_roots) != 1:
        raise AuipProtocolError("auip_bundle_root_ambiguous")
    bundle_root = next(iter(bundle_roots.values()))
    bundle_identity = canonicalize_path(bundle_root).identity_key
    if (
        canonicalize_path(manifest_path.parent).identity_key != bundle_identity
        or canonicalize_path(entry_path.parent).identity_key != bundle_identity
    ):
        raise AuipProtocolError("auip_bundle_location_mismatch")
    from server.auip_bundle_validation import validate_staged_auip_web_bundle

    return validate_staged_auip_web_bundle(
        bundle_root,
        entry_filename=entry_path.name,
        materialized_files=tuple(sorted(required_files)),
    )


def _verified_export_path(record: Any) -> Path | None:
    try:
        path = Path(str(getattr(record, "path", ""))).resolve()
        if path.is_symlink() or not path.is_file():
            return None
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, RuntimeError, ValueError):
        return None
    expected = str(getattr(record, "sha256", "") or "")
    return path if expected and actual == expected else None


def _path_key(value: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        return ""
    try:
        return canonicalize_path(clean).identity_key
    except (OSError, ValueError):
        return clean.casefold()


def _is_proposed_export_path(path: Path) -> bool:
    """Whether a path belongs to Amadeus' reserved delivery staging tree."""

    parts = tuple(part.casefold() for part in path.parts)
    return any(
        parts[index : index + 2] == (".amadeus", "proposed_exports")
        for index in range(max(0, len(parts) - 1))
    )


def _verified_artifact_path(store: ArtifactSource, record: Any) -> Path | None:
    item = store.get_work_item(str(getattr(record, "work_item_id", "")))
    if item is None:
        return None
    path = _contained_path(
        str(getattr(item, "workspace_path", "")),
        str(getattr(record, "path", "")),
    )
    if path is None:
        return None
    try:
        actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
    expected_digest = str(getattr(record, "sha256", "") or "")
    if expected_digest and expected_digest != actual_digest:
        return None
    return path


def _contained_path(workspace_path: str, artifact_path: str) -> Path | None:
    """Resolve the artifact inside its workspace, or refuse.

    The registry writes absolute paths, so this is not translating a relative
    path -- it is checking that a stored one still points where it claims,
    before the host reads it on a renderer's request.
    """

    root_text = str(workspace_path or "").strip()
    target_text = str(artifact_path or "").strip()
    if not root_text or not target_text:
        return None
    try:
        root = canonicalize_path(root_text)
        candidate = canonicalize_path(target_text)
    except (OSError, ValueError):
        return None
    if candidate.identity_key == root.identity_key or not path_is_within(
        candidate.canonical_path,
        root.canonical_path,
    ):
        return None
    candidate_path = Path(candidate.canonical_path)
    if not candidate_path.is_file():
        return None
    return candidate_path
