"""Provider-neutral records used by the persistent Amadeus work ledger.

The ledger deliberately models user-visible work rather than provider internals:

``Project -> WorkItem -> WorkOperation -> RunAttempt -> Artifact / PermissionRequest``

Provider-specific identifiers remain optional bindings on a run attempt.  The
types in this module have no dependency on the event bus, a provider adapter,
or SQLite so that both persistence and presentation layers can consume them.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


WorkItemState = Literal["open", "review_ready", "accepted", "archived"]
ExecutionStatus = Literal[
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "orphaned",
]
Completeness = Literal["unknown", "incomplete", "partial", "complete"]
AttentionState = Literal[
    "none",
    "review",
    "input",
    "permission",
    "conflict",
    "error",
]
FocusMode = Literal["auto", "pinned"]
ArtifactLocation = Literal["workspace", "project", "external", "virtual"]
ArtifactStatus = Literal[
    "registered",
    "pending",
    "missing",
    "approved",
    "rejected",
]
WorkspaceLeaseStatus = Literal["active", "released", "stale"]
PermissionRequestStatus = Literal["pending", "allowed", "denied", "expired"]


WORK_ITEM_STATES: frozenset[str] = frozenset({"open", "review_ready", "accepted", "archived"})
EXECUTION_STATUSES: frozenset[str] = frozenset(
    {"queued", "running", "succeeded", "failed", "cancelled", "orphaned"}
)
COMPLETENESS_STATES: frozenset[str] = frozenset({"unknown", "incomplete", "partial", "complete"})
ATTENTION_STATES: frozenset[str] = frozenset(
    {"none", "review", "input", "permission", "conflict", "error"}
)
FOCUS_MODES: frozenset[str] = frozenset({"auto", "pinned"})
ARTIFACT_LOCATIONS: frozenset[str] = frozenset({"workspace", "project", "external", "virtual"})
ARTIFACT_STATUSES: frozenset[str] = frozenset(
    {"registered", "pending", "missing", "approved", "rejected"}
)
WORKSPACE_LEASE_STATUSES: frozenset[str] = frozenset({"active", "released", "stale"})
PERMISSION_REQUEST_STATUSES: frozenset[str] = frozenset(
    {"pending", "allowed", "denied", "expired"}
)


def new_ledger_id(prefix: str) -> str:
    """Return an opaque, locally unique ledger identifier."""

    clean_prefix = "".join(ch for ch in str(prefix or "item").lower() if ch.isalnum() or ch == "_")
    return f"{clean_prefix or 'item'}_{uuid.uuid4().hex}"


def utc_timestamp() -> float:
    """Return an epoch timestamp matching existing ProviderRun timestamps."""

    return time.time()


@dataclass(frozen=True, slots=True)
class CanonicalPath:
    """A display path plus its resolved identity used for deduplication."""

    display_path: str
    canonical_path: str
    identity_key: str


def canonicalize_path(value: str | os.PathLike[str]) -> CanonicalPath:
    """Resolve relative paths, symlinks, and Windows junction aliases.

    ``identity_key`` follows the host filesystem's case rules through
    :func:`os.path.normcase`.  Windows receives an additional ``casefold`` so
    junction/case aliases cannot create duplicate Project rows.

    The target does not need to exist.  In that case ``realpath`` still
    canonicalizes the existing parent portion and normalizes ``..`` segments.
    """

    text = os.fspath(value).strip()
    if not text:
        raise ValueError("path is required")
    expanded = os.path.expandvars(os.path.expanduser(text))
    display = os.path.normpath(os.path.abspath(expanded))
    canonical = os.path.normpath(os.path.realpath(display))
    identity = os.path.normcase(canonical)
    if os.name == "nt":
        identity = identity.casefold()
    return CanonicalPath(
        display_path=display,
        canonical_path=canonical,
        identity_key=identity,
    )


def path_is_within(path: str, parent: str) -> bool:
    """Return whether ``path`` belongs to ``parent`` after canonicalization."""

    child_key = canonicalize_path(path).identity_key
    parent_key = canonicalize_path(parent).identity_key
    try:
        return os.path.commonpath([child_key, parent_key]) == parent_key
    except (ValueError, OSError):
        # Different Windows drives, malformed paths, and mixed path styles are
        # all outside the requested scope.
        return False


PROJECT_STATES = ("active", "retired")
CONVERSATION_BINDING_KINDS = ("project", "work_item")
WORK_OPERATION_INTENTS = frozenset({"execute", "amend"})


@dataclass(slots=True)
class ProjectRecord:
    project_id: str
    name: str
    display_path: str
    canonical_path: str
    path_identity: str
    created_at: float
    updated_at: float
    # Retired means "stop offering this as somewhere to send work". Nothing is
    # deleted and nothing becomes unreachable: its files stay, its tasks stay,
    # and its past stays answerable. Without it the list of places only ever
    # grows, and something worth two days of attention sits in the menu forever.
    state: str = "active"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "display_path": self.display_path,
            "canonical_path": self.canonical_path,
            "path_identity": self.path_identity,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "state": self.state,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class ConversationBindingRecord:
    """Durable context selected by one main-chat conversation.

    The project is the routing destination.  ``anchor_work_item_id`` is an
    optional, narrower conversational reference; it never changes workspace
    ownership or revives a terminal provider run by itself.
    """

    session_id: str
    project_id: str
    anchor_work_item_id: str
    binding_kind: str
    created_at: float
    updated_at: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "project_id": self.project_id,
            "anchor_work_item_id": self.anchor_work_item_id,
            "binding_kind": self.binding_kind,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class SessionWorkContextRecord:
    """The WorkItem currently foregrounded by one main-chat Session.

    This is intentionally independent from :class:`ConversationBindingRecord`:
    a Project is the default destination for otherwise-unplaced new work, while
    the active WorkItem is the narrow referent for continuation and status.
    A Session may therefore foreground an unkept Draft without losing its
    selected Project.
    """

    session_id: str
    active_work_item_id: str
    created_at: float
    updated_at: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "active_work_item_id": self.active_work_item_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class WorkItemRecord:
    work_item_id: str
    project_id: str
    title: str
    goal: str
    state: WorkItemState
    workspace_mode: str
    workspace_path: str
    workspace_identity: str
    branch: str
    base_revision: str
    created_at: float
    updated_at: float
    last_activity_at: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_item_id": self.work_item_id,
            "project_id": self.project_id,
            "title": self.title,
            "goal": self.goal,
            "state": self.state,
            "workspace_mode": self.workspace_mode,
            "workspace_path": self.workspace_path,
            "workspace_identity": self.workspace_identity,
            "branch": self.branch,
            "base_revision": self.base_revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_activity_at": self.last_activity_at,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class WorkOperationRecord:
    """One semantic user instruction inside a stable WorkItem goal."""

    operation_id: str
    work_item_id: str
    operation_number: int
    intent: str
    instruction: str
    created_at: float
    updated_at: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "work_item_id": self.work_item_id,
            "operation_number": self.operation_number,
            "intent": self.intent,
            "instruction": self.instruction,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class RunAttemptRecord:
    attempt_id: str
    work_item_id: str
    operation_id: str
    attempt_number: int
    provider: str
    provider_run_id: str
    task: str
    mode: str
    execution_status: ExecutionStatus
    result: str
    error: str
    created_at: float
    updated_at: float
    started_at: float | None
    finished_at: float | None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "work_item_id": self.work_item_id,
            "operation_id": self.operation_id,
            "attempt_number": self.attempt_number,
            "provider": self.provider,
            "provider_run_id": self.provider_run_id,
            "task": self.task,
            "mode": self.mode,
            "execution_status": self.execution_status,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class ArtifactRecord:
    artifact_id: str
    work_item_id: str
    attempt_id: str
    kind: str
    title: str
    uri: str
    path: str
    path_identity: str
    location: ArtifactLocation
    status: ArtifactStatus
    sha256: str
    size_bytes: int | None
    modified_at: float | None
    created_at: float
    updated_at: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "work_item_id": self.work_item_id,
            "attempt_id": self.attempt_id,
            "kind": self.kind,
            "title": self.title,
            "uri": self.uri,
            "path": self.path,
            "path_identity": self.path_identity,
            "location": self.location,
            "status": self.status,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "modified_at": self.modified_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class PermissionRequestRecord:
    """A provider-neutral user intervention for a gated capability.

    Provider tool-call identifiers belong in ``idempotency_key`` or
    ``metadata``.  The stable ledger fields describe the requested action and
    its scope so any Amadeus surface can render and resolve the same request.
    """

    request_id: str
    work_item_id: str
    attempt_id: str
    idempotency_key: str
    capability: str
    action: str
    scope_paths: list[str]
    reason: str
    reversibility: str
    status: PermissionRequestStatus
    options: list[str]
    created_at: float
    updated_at: float
    resolved_at: float | None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        """Canvas-schema compatibility alias for ``request_id``."""

        return self.request_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "id": self.request_id,
            "work_item_id": self.work_item_id,
            "attempt_id": self.attempt_id,
            "idempotency_key": self.idempotency_key,
            "capability": self.capability,
            "action": self.action,
            "scope_paths": list(self.scope_paths),
            # Keep the established canvas-schema spelling available at the
            # persistence boundary without making it the database column name.
            "scope": list(self.scope_paths),
            "reason": self.reason,
            "reversibility": self.reversibility,
            "status": self.status,
            "options": list(self.options),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "resolved_at": self.resolved_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CompletionDecision:
    execution_status: ExecutionStatus
    completeness: Completeness
    attention: AttentionState
    work_item_state: WorkItemState
    rationale: str
    terminal: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_status": self.execution_status,
            "completeness": self.completeness,
            "attention": self.attention,
            "work_item_state": self.work_item_state,
            "rationale": self.rationale,
            "terminal": self.terminal,
        }


@dataclass(slots=True)
class CompletionAssessmentRecord:
    assessment_id: str
    work_item_id: str
    attempt_id: str
    source: str
    execution_status: ExecutionStatus
    completeness: Completeness
    attention: AttentionState
    work_item_state: WorkItemState
    rationale: str
    terminal: bool
    evidence: dict[str, Any]
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "assessment_id": self.assessment_id,
            "work_item_id": self.work_item_id,
            "attempt_id": self.attempt_id,
            "source": self.source,
            "execution_status": self.execution_status,
            "completeness": self.completeness,
            "attention": self.attention,
            "work_item_state": self.work_item_state,
            "rationale": self.rationale,
            "terminal": self.terminal,
            "evidence": dict(self.evidence),
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class FocusRecord:
    surface: str
    work_item_id: str
    mode: FocusMode
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "work_item_id": self.work_item_id,
            "mode": self.mode,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class WorkspaceLeaseRecord:
    lease_id: str
    workspace_path: str
    workspace_identity: str
    work_item_id: str
    attempt_id: str
    status: WorkspaceLeaseStatus
    acquired_at: float
    heartbeat_at: float
    released_at: float | None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "workspace_path": self.workspace_path,
            "workspace_identity": self.workspace_identity,
            "work_item_id": self.work_item_id,
            "attempt_id": self.attempt_id,
            "status": self.status,
            "acquired_at": self.acquired_at,
            "heartbeat_at": self.heartbeat_at,
            "released_at": self.released_at,
            "metadata": dict(self.metadata),
        }


def project_name_from_path(path: str) -> str:
    name = Path(path).name.strip()
    return name or path
