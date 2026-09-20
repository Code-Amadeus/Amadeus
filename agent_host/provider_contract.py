"""Provider discovery, capability semantics, and deterministic selection.

The execution contract intentionally stays in :mod:`agent_host.provider_types`.
This module describes what a registered provider can do and matches an
Amadeus-owned task requirement to that description.  It has no dependency on
the server, Work Ledger, settings, or any concrete provider implementation, so
selection can be tested without starting a provider or importing the UI host.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Literal


WorkspaceAccess = Literal["none", "read", "write"]
WorkspaceOwnership = Literal["none", "caller", "provider", "negotiated"]
Durability = Literal["turn", "process", "host_restart", "remote"]
SteeringMode = Literal["none", "next_turn", "immediate"]
ResumeMode = Literal["none", "same_attempt", "attach"]
CancellationMode = Literal["best_effort", "confirmed"]
InteractionMode = Literal["none", "diagnostic", "bidirectional"]
EventModel = Literal["canonical", "canonical+native"]
ProviderOwnershipMode = Literal["managed", "attached"]
PreferencePolicy = Literal["prefer", "require", "force"]
OperationExecution = Literal["direct", "observe_then_plan"]


_WORKSPACE_ACCESS_RANK: dict[str, int] = {"none": 0, "read": 1, "write": 2}
_DURABILITY_VALUES = {"turn", "process", "host_restart", "remote"}
_STEERING_VALUES = {"none", "next_turn", "immediate"}
_RESUME_VALUES = {"none", "same_attempt", "attach"}
_INTERACTION_VALUES = {"none", "diagnostic", "bidirectional"}
_WORKSPACE_OWNERSHIP_VALUES = {"none", "caller", "provider", "negotiated"}


@dataclass(frozen=True, slots=True)
class ProviderOperation:
    """One public provider operation and how the host must execute it.

    Operations describe provider semantics, not UI verbs.  ``atomic`` means
    the adapter can execute the operation itself; non-atomic operations must
    be lowered to atomic operations by an interaction branch.  ``outcome_facet``
    names the structured evidence contract emitted after execution.
    """

    operation_id: str
    execution: OperationExecution = "direct"
    atomic: bool = True
    outcome_facet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "execution": self.execution,
            "atomic": self.atomic,
            "outcome_facet": self.outcome_facet,
        }


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Semantic capabilities that can affect routing or control-plane safety."""

    task_kinds: tuple[str, ...] = ("general",)
    workspace_access: WorkspaceAccess = "none"
    workspace_ownership: WorkspaceOwnership = "none"
    durability: Durability = "process"
    steering: SteeringMode = "none"
    resume: ResumeMode = "none"
    cancellation: CancellationMode = "best_effort"
    interaction: InteractionMode = "none"
    event_model: EventModel = "canonical"
    operations: tuple[ProviderOperation, ...] = ()
    # Host-installed Skills and MCP connections stay outside the main role.
    # A Work Provider must explicitly name each catalog projection it can use.
    capability_projections: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        projections = tuple(
            dict.fromkeys(
                str(value or "").strip().lower()
                for value in self.capability_projections
                if str(value or "").strip()
            )
        )
        object.__setattr__(self, "capability_projections", projections)
        seen: set[str] = set()
        for operation in self.operations:
            if not isinstance(operation, ProviderOperation):
                raise TypeError("provider operations must be ProviderOperation values")
            operation_id = operation.operation_id.strip().lower()
            if not operation_id:
                raise ValueError("provider operation_id is required")
            if operation_id in seen:
                raise ValueError(f"duplicate provider operation: {operation_id}")
            seen.add(operation_id)
            if operation.execution not in {"direct", "observe_then_plan"}:
                raise ValueError(
                    f"invalid execution policy for provider operation {operation_id}: "
                    f"{operation.execution}"
                )
            if not operation.atomic and operation.execution == "direct":
                raise ValueError(
                    f"non-atomic provider operation {operation_id} requires a planning boundary"
                )
            if operation.outcome_facet and "." not in operation.outcome_facet:
                raise ValueError(
                    f"provider outcome facet must be namespaced: {operation.outcome_facet}"
                )

    def operation(self, operation_id: str) -> ProviderOperation | None:
        clean_id = str(operation_id or "").strip().lower()
        return next(
            (
                item
                for item in self.operations
                if item.operation_id.strip().lower() == clean_id
            ),
            None,
        )

    def atomic_operation_ids(self) -> frozenset[str]:
        return frozenset(
            item.operation_id.strip().lower()
            for item in self.operations
            if item.atomic and item.operation_id.strip()
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_kinds": list(self.task_kinds),
            "workspace_access": self.workspace_access,
            "workspace_ownership": self.workspace_ownership,
            "durability": self.durability,
            "steering": self.steering,
            "resume": self.resume,
            "cancellation": self.cancellation,
            "interaction": self.interaction,
            "event_model": self.event_model,
            "operations": [item.to_dict() for item in self.operations],
            "capability_projections": list(self.capability_projections),
        }


@dataclass(frozen=True, slots=True)
class ProviderManifest:
    provider_id: str
    display_name: str
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    contract_version: str = "0.2"
    runtime_kind: str = "agent"
    ownership_modes: tuple[ProviderOwnershipMode, ...] = ("managed",)
    experience_extensions: tuple[str, ...] = ()
    selection_priority: int = 0
    declared: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "contract_version": self.contract_version,
            "runtime_kind": self.runtime_kind,
            "ownership_modes": list(self.ownership_modes),
            "experience_extensions": list(self.experience_extensions),
            "selection_priority": self.selection_priority,
            "declared": self.declared,
            "capabilities": self.capabilities.to_dict(),
        }

    @classmethod
    def compatibility(cls, provider_id: str) -> "ProviderManifest":
        """Describe a legacy adapter without inventing capabilities for it.

        Compatibility adapters remain runnable by explicit id through the old
        Runtime API.  They are deliberately poor automatic-routing candidates:
        no workspace write, no resume, and no interaction are claimed.
        """

        clean_id = str(provider_id or "provider").strip().lower() or "provider"
        return cls(
            provider_id=clean_id,
            display_name=clean_id,
            capabilities=ProviderCapabilities(task_kinds=("general",)),
            contract_version="0.1-compat",
            declared=False,
            selection_priority=-100,
        )


@dataclass(frozen=True, slots=True)
class ProviderRequirements:
    """Provider-facing requirements derived from an Amadeus task intent.

    Project, WorkItem, execute/amend, and user-facing completion do not belong
    here.  Those remain Amadeus control-plane facts.
    """

    task_kind: str = "general"
    workspace_access: WorkspaceAccess = "none"
    workspace_ownership: WorkspaceOwnership | None = None
    durability: Durability | None = None
    steering: SteeringMode | None = None
    resume: ResumeMode | None = None
    interaction: InteractionMode | None = None
    ownership: ProviderOwnershipMode = "managed"
    preferred_provider: str = ""
    preference_policy: PreferencePolicy = "prefer"

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "ProviderRequirements":
        source = value if isinstance(value, dict) else {}

        def choice(key: str, allowed: set[str], default: str) -> str:
            candidate = str(source.get(key) or default).strip().lower()
            if candidate not in allowed:
                raise ValueError(f"invalid provider requirement {key}: {candidate}")
            return candidate

        def optional_choice(key: str, allowed: set[str]) -> str | None:
            candidate = str(source.get(key) or "").strip().lower()
            if not candidate or candidate == "any":
                return None
            if candidate not in allowed:
                raise ValueError(f"invalid provider requirement {key}: {candidate}")
            return candidate

        return cls(
            task_kind=str(source.get("task_kind") or "general").strip().lower() or "general",
            workspace_access=choice(
                "workspace_access", set(_WORKSPACE_ACCESS_RANK), "none"
            ),  # type: ignore[arg-type]
            workspace_ownership=optional_choice(
                "workspace_ownership", _WORKSPACE_OWNERSHIP_VALUES
            ),  # type: ignore[arg-type]
            durability=optional_choice(
                "durability", _DURABILITY_VALUES
            ),  # type: ignore[arg-type]
            steering=optional_choice(
                "steering", _STEERING_VALUES
            ),  # type: ignore[arg-type]
            resume=optional_choice("resume", _RESUME_VALUES),  # type: ignore[arg-type]
            interaction=optional_choice(
                "interaction", _INTERACTION_VALUES
            ),  # type: ignore[arg-type]
            ownership=choice(
                "ownership", {"managed", "attached"}, "managed"
            ),  # type: ignore[arg-type]
            preferred_provider=str(source.get("preferred_provider") or "").strip().lower(),
            preference_policy=choice(
                "preference_policy", {"prefer", "require", "force"}, "prefer"
            ),  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_kind": self.task_kind,
            "workspace_access": self.workspace_access,
            "workspace_ownership": self.workspace_ownership or "any",
            "durability": self.durability or "any",
            "steering": self.steering or "any",
            "resume": self.resume or "any",
            "interaction": self.interaction or "any",
            "ownership": self.ownership,
            "preferred_provider": self.preferred_provider,
            "preference_policy": self.preference_policy,
        }


@dataclass(frozen=True, slots=True)
class ProviderSelection:
    provider_id: str
    reason: str
    compatible_candidates: tuple[str, ...]
    rejected: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "reason": self.reason,
            "compatible_candidates": list(self.compatible_candidates),
            "rejected": {key: list(value) for key, value in self.rejected.items()},
        }


class ProviderSelectionError(ValueError):
    def __init__(self, message: str, *, rejected: dict[str, tuple[str, ...]] | None = None) -> None:
        super().__init__(message)
        self.rejected = dict(rejected or {})


def manifest_for_adapter(adapter: object) -> ProviderManifest:
    """Return and validate an adapter manifest, with legacy compatibility."""

    provider_id = str(getattr(adapter, "provider_id", "") or "").strip().lower()
    if not provider_id:
        raise ValueError("provider adapter must declare provider_id")
    declared = getattr(adapter, "manifest", None)
    if callable(declared):
        declared = declared()
    if declared is None:
        return ProviderManifest.compatibility(provider_id)
    if not isinstance(declared, ProviderManifest):
        raise TypeError(f"provider {provider_id} manifest must be ProviderManifest")
    if declared.provider_id.strip().lower() != provider_id:
        raise ValueError(
            f"provider manifest id {declared.provider_id!r} does not match adapter id {provider_id!r}"
        )
    return declared


def compatibility_errors(
    manifest: ProviderManifest,
    requirements: ProviderRequirements,
) -> tuple[str, ...]:
    capabilities = manifest.capabilities
    errors: list[str] = []
    task_kind = str(requirements.task_kind or "general").strip().lower()
    supported_kinds = {str(kind).strip().lower() for kind in capabilities.task_kinds}
    if "*" not in supported_kinds and task_kind not in supported_kinds:
        errors.append(f"task_kind:{task_kind}")
    if _WORKSPACE_ACCESS_RANK[capabilities.workspace_access] < _WORKSPACE_ACCESS_RANK[requirements.workspace_access]:
        errors.append(f"workspace_access:{requirements.workspace_access}")
    if (
        requirements.workspace_ownership is not None
        and capabilities.workspace_ownership != requirements.workspace_ownership
    ):
        errors.append(f"workspace_ownership:{requirements.workspace_ownership}")
    # These are mechanisms, not strength ladders. A remote session is not a
    # substitute for host-restart persistence, and attaching an existing task
    # is not the same operation as resuming this attempt. Be conservative until
    # a later contract has evidence for explicit supported-mechanism sets.
    if requirements.durability is not None and capabilities.durability != requirements.durability:
        errors.append(f"durability:{requirements.durability}")
    if requirements.steering is not None and capabilities.steering != requirements.steering:
        errors.append(f"steering:{requirements.steering}")
    if requirements.resume is not None and capabilities.resume != requirements.resume:
        errors.append(f"resume:{requirements.resume}")
    if requirements.interaction is not None and capabilities.interaction != requirements.interaction:
        errors.append(f"interaction:{requirements.interaction}")
    if requirements.ownership not in manifest.ownership_modes:
        errors.append(f"ownership:{requirements.ownership}")
    return tuple(errors)


def select_provider(
    requirements: ProviderRequirements,
    manifests: Iterable[ProviderManifest],
    *,
    default_provider: str = "",
) -> ProviderSelection:
    """Select one provider deterministically and preserve an audit explanation."""

    by_id: dict[str, ProviderManifest] = {}
    for manifest in manifests:
        provider_id = manifest.provider_id.strip().lower()
        if not provider_id:
            continue
        if provider_id in by_id:
            raise ProviderSelectionError(
                f"duplicate provider manifest: {provider_id}"
            )
        by_id[provider_id] = manifest
    preferred = str(requirements.preferred_provider or default_provider or "").strip().lower()
    rejected = {
        provider_id: compatibility_errors(manifest, requirements)
        for provider_id, manifest in by_id.items()
    }
    rejected = {provider_id: reasons for provider_id, reasons in rejected.items() if reasons}
    compatible = [manifest for provider_id, manifest in by_id.items() if provider_id not in rejected]
    compatible.sort(key=lambda item: (-item.selection_priority, item.provider_id))
    compatible_ids = tuple(item.provider_id for item in compatible)

    if preferred and requirements.preference_policy == "force":
        if preferred not in by_id:
            raise ProviderSelectionError(f"forced provider is not registered: {preferred}", rejected=rejected)
        return ProviderSelection(
            provider_id=preferred,
            reason="forced_provider",
            compatible_candidates=compatible_ids,
            rejected=rejected,
        )

    if preferred and requirements.preference_policy == "require":
        if preferred not in by_id:
            raise ProviderSelectionError(f"required provider is not registered: {preferred}", rejected=rejected)
        if preferred in rejected:
            reasons = ", ".join(rejected[preferred])
            raise ProviderSelectionError(
                f"required provider {preferred} is incompatible ({reasons})",
                rejected=rejected,
            )
        return ProviderSelection(
            provider_id=preferred,
            reason="required_provider",
            compatible_candidates=compatible_ids,
            rejected=rejected,
        )

    if preferred and preferred in compatible_ids:
        return ProviderSelection(
            provider_id=preferred,
            reason="preferred_provider",
            compatible_candidates=compatible_ids,
            rejected=rejected,
        )
    if compatible:
        reason = "preferred_provider_incompatible" if preferred else "best_compatible_provider"
        return ProviderSelection(
            provider_id=compatible[0].provider_id,
            reason=reason,
            compatible_candidates=compatible_ids,
            rejected=rejected,
        )
    detail = "; ".join(
        f"{provider_id}={','.join(reasons)}" for provider_id, reasons in sorted(rejected.items())
    )
    raise ProviderSelectionError(
        f"no registered provider satisfies the task requirements{': ' + detail if detail else ''}",
        rejected=rejected,
    )
