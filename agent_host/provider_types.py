from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Protocol

from agent_host.provider_contract import (
    ProviderOwnershipMode,
    ProviderRequirements,
)
from agent_host.provider_outcome import ProviderOutcomeEvidence


ProviderStatus = Literal["queued", "running", "done", "error", "cancelled", "orphaned"]
ProviderSessionScope = Literal["work_item", "attempt"]
ACTIVITY_EVIDENCE_METADATA_KEY = "activity_evidence"


@dataclass(frozen=True, slots=True)
class ProviderSessionHandle:
    """Opaque Provider-owned context that may be attached to another Attempt.

    A Provider Session is deliberately not a WorkItem or an Operation.  The
    host owns those durable semantic identities; the Provider owns this opaque
    execution context.  Adapters may use it to preserve a conversation,
    browser target, or other native state without exposing native transport
    fields to routing or UI code.
    """

    provider: str
    session_id: str
    scope: ProviderSessionScope = "work_item"
    version: int = 1

    def __post_init__(self) -> None:
        provider = str(self.provider or "").strip().lower()
        session_id = str(self.session_id or "").strip()
        if not provider:
            raise ValueError("provider session provider is required")
        if not session_id:
            raise ValueError("provider session id is required")
        if len(provider) > 80 or len(session_id) > 512:
            raise ValueError("provider session identity is too long")
        if self.scope not in {"work_item", "attempt"}:
            raise ValueError(f"invalid provider session scope: {self.scope}")
        if int(self.version) != 1:
            raise ValueError(f"unsupported provider session version: {self.version}")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "version", 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "session_id": self.session_id,
            "scope": self.scope,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ProviderSessionHandle":
        if not isinstance(value, dict):
            raise TypeError("provider session must be an object")
        return cls(
            provider=str(value.get("provider") or ""),
            session_id=str(value.get("session_id") or value.get("sessionId") or ""),
            scope=str(value.get("scope") or "work_item"),  # type: ignore[arg-type]
            version=int(value.get("version") or 1),
        )


@dataclass(frozen=True, slots=True)
class ProviderActivityEvidence:
    """Host-observed shape of one native Provider execution turn.

    This evidence does not prove that the requested outcome exists. It only
    lets the control plane distinguish a terminal turn that performed some
    observable execution from one that stopped after progress reporting. The
    adapter, rather than provider-authored prose or metadata, must construct
    this typed value from a completely observed native event stream.
    """

    terminal_observed: bool
    progress_milestones: int = 0
    execution_items: int = 0
    observation_authority: str = "host"
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.terminal_observed, bool):
            raise TypeError("provider activity terminal_observed must be boolean")
        if self.observation_authority != "host":
            raise ValueError("provider activity evidence must use host observation authority")
        if int(self.schema_version) != 1:
            raise ValueError(
                f"unsupported provider activity evidence version: {self.schema_version}"
            )
        if (
            not isinstance(self.progress_milestones, int)
            or isinstance(self.progress_milestones, bool)
            or not isinstance(self.execution_items, int)
            or isinstance(self.execution_items, bool)
        ):
            raise TypeError("provider activity evidence counts must be integers")
        progress_milestones = self.progress_milestones
        execution_items = self.execution_items
        if progress_milestones < 0 or execution_items < 0:
            raise ValueError("provider activity evidence counts cannot be negative")
        object.__setattr__(self, "progress_milestones", progress_milestones)
        object.__setattr__(self, "execution_items", execution_items)
        object.__setattr__(self, "schema_version", 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "terminal_observed": self.terminal_observed,
            "progress_milestones": self.progress_milestones,
            "execution_items": self.execution_items,
            "observation_authority": self.observation_authority,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class ProviderRecoveryContext:
    """Host-owned lineage for one bounded successor execution Attempt."""

    reason: str
    root_attempt_id: str
    predecessor_attempt_id: str
    ordinal: int = 1

    def __post_init__(self) -> None:
        reason = str(self.reason or "").strip().lower()
        root_attempt_id = str(self.root_attempt_id or "").strip()
        predecessor_attempt_id = str(self.predecessor_attempt_id or "").strip()
        ordinal = int(self.ordinal)
        if reason != "progress_only_completion":
            raise ValueError(f"unsupported provider recovery reason: {reason}")
        if not root_attempt_id or not predecessor_attempt_id:
            raise ValueError("provider recovery attempt lineage is required")
        if len(root_attempt_id) > 160 or len(predecessor_attempt_id) > 160:
            raise ValueError("provider recovery attempt lineage is too long")
        if ordinal != 1:
            raise ValueError("provider recovery is bounded to one successor attempt")
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "root_attempt_id", root_attempt_id)
        object.__setattr__(self, "predecessor_attempt_id", predecessor_attempt_id)
        object.__setattr__(self, "ordinal", ordinal)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "root_attempt_id": self.root_attempt_id,
            "predecessor_attempt_id": self.predecessor_attempt_id,
            "ordinal": self.ordinal,
        }


@dataclass(slots=True)
class ProviderRunRequest:
    provider: str
    task: str
    cwd: str | None = None
    mode: str = "agent"
    metadata: dict[str, Any] = field(default_factory=dict)
    requirements: ProviderRequirements | None = None
    ownership: ProviderOwnershipMode = "managed"
    session: ProviderSessionHandle | None = None
    recovery: ProviderRecoveryContext | None = None


@dataclass(slots=True)
class ProviderSteerRequest:
    """Replace the remaining plan of an active provider run.

    Steering does not rewrite the durable task identity and does not imply
    cancellation of an in-flight external side effect.  ``revision`` is
    monotonic per run; adapters that accept immediate steering apply only the
    newest revision at their next safe boundary.
    """

    task: str
    revision: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderPermissionResponse:
    """One Host-authorized answer to a pending Provider permission request.

    The Host owns the durable permission identity and decision. Provider-native
    callback ids and wire responses remain adapter details.
    """

    request_id: str
    allow: bool

    def __post_init__(self) -> None:
        request_id = str(self.request_id or "").strip()
        if not request_id:
            raise ValueError("provider permission request_id is required")
        if len(request_id) > 240:
            raise ValueError("provider permission request_id is too long")
        object.__setattr__(self, "request_id", request_id)


@dataclass(slots=True)
class ProviderEvent:
    """Provider-neutral execution fact emitted by an adapter.

    Event names describe the strength of the normalized fact, not the source
    provider.  In particular, ``assistant.delta`` is raw stream material,
    ``assistant.update`` is a bounded provider-authored update that remains an
    unverified candidate, and ``semantic.progress`` is an adapter-classified
    task milestone. Provider-authored milestones use the shared ``design``,
    ``diagnostic``, ``capability`` or ``validation`` category; evidence
    strength remains a separate field. None of them imply terminal completion
    or grant new execution authority.
    """

    provider: str
    run_id: str
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    time_ms: int = 0
    task_id: str = ""
    attempt_id: str = ""
    attempt_epoch: int = 0
    sequence: int = 0
    observed_at: float = 0.0
    replay: bool = False
    ownership: ProviderOwnershipMode = "managed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "run_id": self.run_id,
            "type": self.type,
            "payload": self.payload,
            "metadata": self.metadata,
            "time_ms": self.time_ms,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "attempt_epoch": self.attempt_epoch,
            "sequence": self.sequence,
            "observed_at": self.observed_at,
            "replay": self.replay,
            "ownership": self.ownership,
        }


@dataclass(slots=True)
class ProviderRunResult:
    status: ProviderStatus
    result: str = ""
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    outcome_evidence: ProviderOutcomeEvidence | None = None
    activity_evidence: ProviderActivityEvidence | None = None
    session: ProviderSessionHandle | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "metadata": self.metadata,
        }
        if self.outcome_evidence is not None:
            payload["outcome_evidence"] = self.outcome_evidence.to_dict()
        if self.activity_evidence is not None:
            payload[ACTIVITY_EVIDENCE_METADATA_KEY] = self.activity_evidence.to_dict()
        if self.session is not None:
            payload["provider_session"] = self.session.to_dict()
        return payload


EmitProviderEvent = Callable[[ProviderEvent], Awaitable[None]]


class ProviderAdapter(Protocol):
    provider_id: str

    async def run(
        self,
        request: ProviderRunRequest,
        run_id: str,
        emit: EmitProviderEvent,
    ) -> ProviderRunResult:
        ...

    async def cancel(self, run_id: str) -> dict[str, Any] | None:
        ...
