from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Protocol

from agent_host.provider_contract import (
    ProviderOwnershipMode,
    ProviderRequirements,
)
from agent_host.provider_outcome import ProviderOutcomeEvidence


ProviderStatus = Literal["queued", "running", "done", "error", "cancelled", "orphaned"]
ProviderSessionScope = Literal["work_item", "attempt", "interaction"]

# Runtime removes this key from caller input and sets it only after a configured
# Host request preparer has accepted a cooperative context. Adapters may use the
# fact to choose a native session scope; it is not a Provider-authored authority.
COOPERATIVE_CONTEXT_ACCEPTED_METADATA_KEY = "cooperative_context_accepted"
ProviderSubmissionReconciliationState = Literal[
    "matched_active",
    "matched_terminal",
    "not_observed",
    "ambiguous",
    "unavailable",
]
ACTIVITY_EVIDENCE_METADATA_KEY = "activity_evidence"


@dataclass(frozen=True, slots=True)
class ProviderSessionHandle:
    """Opaque Provider-owned context that may be attached to another Attempt.

    A Provider Session is deliberately not a WorkItem or an Operation.  The
    host owns those durable semantic identities; the Provider owns this opaque
    execution context.  Adapters may use it to preserve a conversation,
    browser target, or other native state without exposing native transport
    fields to routing or UI code.

    ``interaction`` permits explicit Host rebinding to a different Work. It
    does not choose that Work, its workspace, or authorize concurrent turns.
    Older ``work_item`` handles remain confined to their existing Work.
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
        if self.scope not in {"work_item", "attempt", "interaction"}:
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
    feedback: str = ""

    def __post_init__(self) -> None:
        reason = str(self.reason or "").strip().lower()
        root_attempt_id = str(self.root_attempt_id or "").strip()
        predecessor_attempt_id = str(self.predecessor_attempt_id or "").strip()
        ordinal = int(self.ordinal)
        if not isinstance(self.feedback, str):
            raise TypeError("provider recovery feedback must be a string")
        feedback = self.feedback.strip()
        if reason not in {"progress_only_completion", "auip_validation_failed"}:
            raise ValueError(f"unsupported provider recovery reason: {reason}")
        if not root_attempt_id or not predecessor_attempt_id:
            raise ValueError("provider recovery attempt lineage is required")
        if len(root_attempt_id) > 160 or len(predecessor_attempt_id) > 160:
            raise ValueError("provider recovery attempt lineage is too long")
        if ordinal != 1:
            raise ValueError("provider recovery is bounded to one successor attempt")
        if len(feedback) > 4096:
            raise ValueError("provider recovery feedback is too long")
        if reason == "auip_validation_failed" and not feedback:
            raise ValueError("AUIP validation recovery requires Host feedback")
        if reason == "progress_only_completion" and feedback:
            raise ValueError("progress-only recovery does not carry feedback")
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "root_attempt_id", root_attempt_id)
        object.__setattr__(self, "predecessor_attempt_id", predecessor_attempt_id)
        object.__setattr__(self, "ordinal", ordinal)
        object.__setattr__(self, "feedback", feedback)

    def to_dict(self) -> dict[str, Any]:
        result = {
            "reason": self.reason,
            "root_attempt_id": self.root_attempt_id,
            "predecessor_attempt_id": self.predecessor_attempt_id,
            "ordinal": self.ordinal,
        }
        if self.feedback:
            result["feedback"] = self.feedback
        return result


@dataclass(frozen=True, slots=True)
class ProviderRunIntakeAuthority:
    """Host-only authority for one accepted effect's initial intake.

    This value is passed beside, never inside, ``ProviderRunRequest``. It is
    not serialized to adapters or public Provider events. Constructing it is
    not permission: the installed Host intake owner must revalidate the exact
    durable effect before writing its owned local state.
    """

    effect_id: str
    kind: Literal["control_work_effect", "cooperative_provider_effect"] = (
        "control_work_effect"
    )
    version: int = 1

    def __post_init__(self) -> None:
        effect_id = str(self.effect_id or "").strip()
        if not effect_id or len(effect_id) > 240:
            raise ValueError("provider run intake effect identity is required")
        if self.kind not in {"control_work_effect", "cooperative_provider_effect"}:
            raise ValueError(f"unsupported provider run intake authority: {self.kind}")
        if isinstance(self.version, bool) or int(self.version) != 1:
            raise ValueError(
                f"unsupported provider run intake authority version: {self.version}"
            )
        object.__setattr__(self, "effect_id", effect_id)
        object.__setattr__(self, "version", 1)


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


@dataclass(frozen=True, slots=True)
class ProviderRunIntakeReceipt:
    """Host preparer's proof that one accepted effect owns this exact run.

    Work intake supplies its complete Work/Operation/Attempt lineage. A
    non-Work Provider effect supplies none of that lineage; partial lineage is
    never valid.
    """

    effect_id: str
    run_id: str
    work_item_id: str = ""
    operation_id: str = ""
    attempt_id: str = ""

    def __post_init__(self) -> None:
        for label in ("effect_id", "run_id"):
            value = str(getattr(self, label) or "").strip()
            if not value or len(value) > 240:
                raise ValueError(f"provider run intake receipt {label} is required")
            object.__setattr__(self, label, value)
        lineage = []
        for label in ("work_item_id", "operation_id", "attempt_id"):
            value = str(getattr(self, label) or "").strip()
            if len(value) > 240:
                raise ValueError(f"provider run intake receipt {label} is too long")
            object.__setattr__(self, label, value)
            lineage.append(bool(value))
        if any(lineage) and not all(lineage):
            raise ValueError("provider run intake Work lineage must be complete")


@dataclass(slots=True)
class PreparedProviderRun:
    """Prepared request plus Host-only accepted-intake receipt.

    Runtime consumes this wrapper before adapter scheduling; adapters receive
    only ``request``.
    """

    request: ProviderRunRequest
    intake_receipt: ProviderRunIntakeReceipt

    def __post_init__(self) -> None:
        if not isinstance(self.request, ProviderRunRequest):
            raise TypeError("prepared provider run requires ProviderRunRequest")
        if not isinstance(self.intake_receipt, ProviderRunIntakeReceipt):
            raise TypeError("prepared provider run requires typed intake receipt")


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
class ProviderInputDelivery:
    """Transport evidence for one additional input to an exact active run.

    ``delivered`` means the native receiver acknowledged the input, not that
    the model applied it or completed any work. ``rejected`` proves this call
    did not submit it; ``unknown`` must never authorize an automatic resend.
    Durable message acceptance and correlation belong to the calling Host.
    """

    state: Literal["delivered", "rejected", "unknown"]
    reason: str = ""

    def __post_init__(self) -> None:
        if self.state not in {"delivered", "rejected", "unknown"}:
            raise ValueError(f"invalid provider input delivery: {self.state}")

    def to_dict(self) -> dict[str, str]:
        return {"state": self.state, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class ProviderPermissionResponse:
    """One Host-authorized answer to a pending Provider permission request.

    The Host owns the durable permission identity and decision. Provider-native
    callback ids and wire responses remain adapter details.
    """

    request_id: str
    allow: bool
    automatic: bool = False
    reason: str = ""

    def __post_init__(self) -> None:
        request_id = str(self.request_id or "").strip()
        if not request_id:
            raise ValueError("provider permission request_id is required")
        if len(request_id) > 240:
            raise ValueError("provider permission request_id is too long")
        if not isinstance(self.allow, bool) or not isinstance(self.automatic, bool):
            raise TypeError("provider permission decision flags must be boolean")
        reason = str(self.reason or "").strip()
        if len(reason) > 240:
            raise ValueError("provider permission reason is too long")
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "reason", reason)


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

    session.opened carries a typed native address and is awaited before native
    user execution. It does not acknowledge delivery of the user's message.
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
    # session.opened carries a typed native address before native user execution.
    session: ProviderSessionHandle | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
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
        if self.session is not None:
            payload["provider_session"] = self.session.to_dict()
        return payload


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


@dataclass(frozen=True, slots=True)
class ProviderTerminalResultProjection:
    """Minimal Runtime-normalized payload for one terminal Work admission."""

    provider: str
    run_id: str
    work_item_id: str
    attempt_id: str
    status: ProviderStatus
    result: str
    error: str | None
    metadata: dict[str, Any]

    def __post_init__(self) -> None:
        provider = str(self.provider or "").strip().lower()
        run_id = str(self.run_id or "").strip()
        work_item_id = str(self.work_item_id or "").strip()
        attempt_id = str(self.attempt_id or "").strip()
        if not provider or not run_id or not work_item_id or not attempt_id:
            raise ValueError("Provider terminal projection identity is incomplete")
        if self.status not in {"done", "error", "cancelled"}:
            raise ValueError("Provider terminal projection must be terminal")
        if not isinstance(self.metadata, dict):
            raise TypeError("Provider terminal projection metadata must be an object")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "work_item_id", work_item_id)
        object.__setattr__(self, "attempt_id", attempt_id)
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_event_params(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "run_id": self.run_id,
            "task_id": self.work_item_id,
            "attempt_id": self.attempt_id,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ProviderNativeExecutionHandle:
    """Opaque identity for one Provider-owned native execution.

    The Host may compare and persist this value, but routing and Work code must
    not parse it into transport-specific concepts such as threads, turns, jobs,
    or browser actions.
    """

    provider: str
    execution_id: str
    version: int = 1

    def __post_init__(self) -> None:
        provider = str(self.provider or "").strip().lower()
        execution_id = str(self.execution_id or "").strip()
        if not provider:
            raise ValueError("native execution provider is required")
        if not execution_id:
            raise ValueError("native execution id is required")
        if len(provider) > 80 or len(execution_id) > 512:
            raise ValueError("native execution identity is too long")
        if int(self.version) != 1:
            raise ValueError(f"unsupported native execution version: {self.version}")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "execution_id", execution_id)
        object.__setattr__(self, "version", 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "execution_id": self.execution_id,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class ProviderSubmissionReconciliationRequest:
    """Host-owned input to one read-only Provider submission query."""

    provider: str
    run_id: str
    session: ProviderSessionHandle | None = None

    def __post_init__(self) -> None:
        provider = str(self.provider or "").strip().lower()
        run_id = str(self.run_id or "").strip()
        if not provider or not run_id:
            raise ValueError("provider reconciliation identity is required")
        if len(provider) > 80 or len(run_id) > 240:
            raise ValueError("provider reconciliation identity is too long")
        if self.session is not None:
            if not isinstance(self.session, ProviderSessionHandle):
                raise TypeError("provider reconciliation session must use the typed contract")
            if self.session.provider != provider:
                raise ValueError("provider reconciliation session belongs to another provider")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "run_id", run_id)


@dataclass(frozen=True, slots=True)
class ProviderSubmissionReconciliationResult:
    """One non-mutating observation of an unresolved Provider submission.

    Only exact matches may carry a native execution handle. A terminal match
    reuses :class:`ProviderRunResult` so a later promotion can enter the one
    ordinary terminal pipeline rather than inventing a second completion
    contract. This type itself does not authorize that promotion.
    """

    state: ProviderSubmissionReconciliationState
    execution: ProviderNativeExecutionHandle | None = None
    terminal_result: ProviderRunResult | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        allowed = {
            "matched_active",
            "matched_terminal",
            "not_observed",
            "ambiguous",
            "unavailable",
        }
        if self.state not in allowed:
            raise ValueError(f"invalid provider reconciliation state: {self.state}")
        if self.state == "matched_active":
            if not isinstance(self.execution, ProviderNativeExecutionHandle):
                raise ValueError("matched active reconciliation requires native execution")
            if self.terminal_result is not None:
                raise ValueError("matched active reconciliation cannot carry terminal result")
        elif self.state == "matched_terminal":
            if not isinstance(self.execution, ProviderNativeExecutionHandle):
                raise ValueError("matched terminal reconciliation requires native execution")
            if not isinstance(self.terminal_result, ProviderRunResult):
                raise ValueError("matched terminal reconciliation requires typed result")
            if self.terminal_result.status not in {"done", "error", "cancelled"}:
                raise ValueError("reconciled terminal result must be terminal")
        elif self.execution is not None or self.terminal_result is not None:
            raise ValueError(
                "unmatched provider reconciliation cannot carry native execution or result"
            )
        if self.execution is not None and self.terminal_result is not None:
            session = self.terminal_result.session
            if session is not None and session.provider != self.execution.provider:
                raise ValueError("reconciled terminal result belongs to another provider")
        object.__setattr__(self, "reason", str(self.reason or "").strip()[:1000])

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "state": self.state,
            "reason": self.reason,
        }
        if self.execution is not None:
            payload["native_execution"] = self.execution.to_dict()
        if self.terminal_result is not None:
            payload["terminal_result"] = self.terminal_result.to_dict()
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


class ProviderSubmissionReconciler(Protocol):
    """Optional adapter extension for read-only unknown-submit observation."""

    async def reconcile_submission(
        self,
        request: ProviderSubmissionReconciliationRequest,
    ) -> ProviderSubmissionReconciliationResult:
        ...
