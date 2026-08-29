"""Provider-neutral session attachment at the WorkItem boundary.

Provider adapters may return an opaque :class:`ProviderSessionHandle`, but an
intake caller never owns the authority to attach it.  The durable predecessor,
the selected Provider, and the Provider's declared resume capability must all
agree before the handle can leave the ledger and enter a new run request.

Keeping this rule pure and independent from ``WorkLedgerCoordinator`` makes
the ownership boundary reviewable without creating a second source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from agent_host.provider_types import ProviderSessionHandle
from agent_host.work_ledger_store import WorkLedgerConflict
from agent_host.work_ledger_types import RunAttemptRecord


@dataclass(frozen=True, slots=True)
class ProviderSessionAttachment:
    """A validated session plus the bounded audit fact persisted on attach."""

    session: ProviderSessionHandle | None = None
    audit: dict[str, Any] = field(default_factory=dict)


def resolve_provider_session_attachment(
    *,
    has_existing_item: bool,
    previous_attempt: RunAttemptRecord | None,
    continuation: str,
    provider_capabilities: Mapping[str, Any] | None,
    request_provider: str,
    recovery_reason: str = "",
) -> ProviderSessionAttachment:
    """Resolve one ledger-owned session attachment or return no attachment.

    Absence is normal when the operation is new, the Provider does not declare
    ``resume=attach``, the predecessor belongs to another Provider, or the
    predecessor did not produce a work-item-scoped session.  A malformed or
    cross-Provider stored handle is a durable-state conflict and fails closed.
    """

    clean_provider = str(request_provider or "").strip().lower()
    clean_continuation = str(continuation or "").strip().lower()
    clean_recovery_reason = str(recovery_reason or "").strip().lower()
    capabilities = (
        provider_capabilities if isinstance(provider_capabilities, Mapping) else {}
    )
    if (
        not has_existing_item
        or previous_attempt is None
        or not (
            clean_continuation == "amend"
            or (
                clean_continuation == "retry"
                and clean_recovery_reason == "progress_only_completion"
            )
        )
        or str(capabilities.get("resume") or "none").strip().lower() != "attach"
        or previous_attempt.provider.strip().lower() != clean_provider
    ):
        return ProviderSessionAttachment()

    previous_result = (
        previous_attempt.metadata.get("provider_result")
        if isinstance(previous_attempt.metadata.get("provider_result"), dict)
        else {}
    )
    raw_session = previous_attempt.metadata.get("provider_session")
    if not isinstance(raw_session, dict):
        raw_session = previous_result.get("provider_session")
    if not isinstance(raw_session, dict):
        return ProviderSessionAttachment()

    try:
        session = ProviderSessionHandle.from_dict(raw_session)
    except (TypeError, ValueError) as exc:
        raise WorkLedgerConflict(
            "stored provider session is invalid; refusing an ungrounded attachment"
        ) from exc
    if session.provider != clean_provider:
        raise WorkLedgerConflict(
            "stored provider session belongs to a different provider"
        )
    if session.scope != "work_item":
        return ProviderSessionAttachment()
    return ProviderSessionAttachment(
        session=session,
        audit={
            "state": "attached",
            "provider": session.provider,
            "previous_attempt_id": previous_attempt.attempt_id,
            **(
                {"recovery_reason": clean_recovery_reason}
                if clean_recovery_reason
                else {}
            ),
        },
    )
