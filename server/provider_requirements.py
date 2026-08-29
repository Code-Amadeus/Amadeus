"""Compile host-owned delegate facts into Provider requirements.

This module is deliberately pure: it does not read settings, runtime
registration, prompts, projects, or Provider identities beyond the explicit
selection policy carried by a delegate.  Natural-language classification stays
at the intake edge; this boundary decides how already-observed facts constrain
an executor.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agent_host.provider_contract import (
    InteractionMode,
    ProviderRequirements,
    SteeringMode,
    WorkspaceAccess,
)


_BROWSER_PROVIDER_ALIASES = frozenset({"web", "browser_provider", "playwright"})
_BROWSER_STATE_ACTIONS = frozenset(
    {
        "observe",
        "snapshot",
        "extract",
        "click_text",
        "click_ref",
        "fill_ref",
        "back",
        "close",
        "close_session",
    }
)


@dataclass(frozen=True, slots=True)
class DelegateRequirementFacts:
    """Normalized execution facts used to compile ProviderRequirements.

    ``task_requests_workspace_mutation`` is evidence in the model-authored
    provider task. ``source_requests_workspace_mutation`` is evidence in the
    user's exact utterance and cannot be erased by a model paraphrase.

    Report, retract, and taskless focus are consumed before this execution
    boundary. A compound focus is rewritten to execute before it arrives here.
    """

    requested_provider: str = ""
    declared_intent: str = ""
    task_requests_workspace_mutation: bool = False
    source_requests_workspace_mutation: bool = False
    user_forced_provider: bool = False
    required_steering: SteeringMode | None = None
    required_interaction: InteractionMode | None = None
    target_workspace_mode: str = ""
    continuation_provider: str = ""
    requested_action: str = ""
    branch_intent: str = ""
    source_has_browser_address: bool = False
    required_workspace_access: WorkspaceAccess | None = None

    @classmethod
    def from_delegate(
        cls,
        attrs: Mapping[str, Any] | None,
        *,
        task_requests_workspace_mutation: bool,
        source_requests_workspace_mutation: bool = False,
        target_workspace_mode: str = "",
        continuation_provider: str = "",
        source_has_browser_address: bool = False,
        required_workspace_access: str = "",
    ) -> "DelegateRequirementFacts":
        values = attrs if isinstance(attrs, Mapping) else {}
        requested_provider = str(values.get("provider") or "").strip().lower()
        if requested_provider in _BROWSER_PROVIDER_ALIASES:
            requested_provider = "browser"
        normalized_workspace_access = (
            str(required_workspace_access or "").strip().lower()
        )
        if normalized_workspace_access not in {"", "none", "read", "write"}:
            raise ValueError(
                f"invalid required workspace access: {normalized_workspace_access}"
            )
        return cls(
            requested_provider=requested_provider,
            declared_intent=str(values.get("intent") or "").strip().lower(),
            task_requests_workspace_mutation=bool(
                task_requests_workspace_mutation
            ),
            source_requests_workspace_mutation=bool(
                source_requests_workspace_mutation
            ),
            user_forced_provider=(
                str(values.get("force_provider") or "").strip().lower() == "user"
            ),
            target_workspace_mode=str(target_workspace_mode or "").strip().lower(),
            continuation_provider=str(continuation_provider or "").strip().lower(),
            requested_action=str(
                values.get("action") or values.get("browser_action") or ""
            ).strip().lower(),
            branch_intent=str(values.get("branch") or "").strip().lower(),
            source_has_browser_address=bool(source_has_browser_address),
            required_workspace_access=(
                normalized_workspace_access or None
            ),  # type: ignore[arg-type]
        )

    @property
    def mutates_workspace(self) -> bool:
        # Explicit file/code evidence always requires a workspace. ``amend``
        # only implies a filesystem mutation when the referenced WorkItem has
        # (or has not yet resolved) a workspace. Workspace-less WorkItems can
        # be continued by an Agent without manufacturing a file destination.
        return bool(
            self.task_requests_workspace_mutation
            or self.source_requests_workspace_mutation
            or self.required_workspace_access == "write"
            or (
                self.declared_intent == "amend"
                and self.target_workspace_mode != "none"
            )
        )

    @property
    def requires_browser_state(self) -> bool:
        """Whether the operation contract, rather than a label, needs Browser."""

        if self.required_interaction not in {None, "none"}:
            return True
        if self.branch_intent == "close":
            return True
        if self.branch_intent in {"continue", "new"} and not (
            self.requested_action == "open"
            and not self.source_has_browser_address
        ):
            return True
        if (
            self.declared_intent == "amend"
            and self.continuation_provider == "browser"
        ):
            return True
        if self.requested_action == "open":
            # A model-authored URL is a proposed translation, not evidence that
            # the user asked Amadeus to own a particular live page.  Exact
            # source addresses do require host-verifiable Browser state; an
            # address-less "open/find" goal remains research for an Agent.
            return self.source_has_browser_address
        return self.requested_action in _BROWSER_STATE_ACTIONS


def compile_delegate_requirements(
    facts: DelegateRequirementFacts,
) -> ProviderRequirements:
    """Translate normalized task facts into the narrow Provider contract."""

    mutates_workspace = facts.mutates_workspace
    reads_workspace = (
        facts.required_workspace_access == "read" and not mutates_workspace
    )
    requires_browser_state = facts.requires_browser_state
    preferred_provider = facts.requested_provider or facts.continuation_provider
    preference_policy = "prefer"
    if facts.requested_provider:
        if facts.user_forced_provider:
            preference_policy = "force"
        elif facts.requested_provider == "openclaw" and mutates_workspace:
            # Preserve the measured omission/fallback guard: a model-authored
            # OpenClaw label alone cannot override write compatibility.
            preference_policy = "prefer"
        elif facts.requested_provider == "openclaw" and requires_browser_state:
            # Capability beats a model label.  A user-forced Provider was
            # handled above; otherwise an exact page-state operation may fall
            # through to Browser rather than failing selection.
            preference_policy = "prefer"
        elif facts.requested_provider == "browser" and not requires_browser_state:
            # A Browser label cannot manufacture live page-state requirements.
            # Address-less, branch-less search is research and may fall through
            # to a Provider that declares that capability.
            preference_policy = "prefer"
        else:
            preference_policy = "require"

    return ProviderRequirements(
        task_kind=(
            "workspace_mutation"
            if mutates_workspace
            else "workspace_read"
            if reads_workspace
            else "browser"
            if requires_browser_state
            else "research"
            if facts.requested_provider == "browser"
            else "general"
        ),
        workspace_access=(
            "write" if mutates_workspace else "read" if reads_workspace else "none"
        ),
        ownership="managed",
        preferred_provider=preferred_provider,
        preference_policy=preference_policy,
        steering=facts.required_steering,
        interaction=facts.required_interaction,
    )
