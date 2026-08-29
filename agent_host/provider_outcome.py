"""Structured provider outcome evidence owned by the execution boundary.

Providers may describe a result in prose, but prose is never completion
evidence.  An adapter instead emits one facet-scoped expected/observed record.
The server-side verifier decides whether those facts permit a user-facing
claim.  Keeping this data contract below the server prevents Work Ledger and
Observer from reaching into provider-specific metadata shapes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


OUTCOME_EVIDENCE_METADATA_KEY = "outcome_evidence"


@dataclass(frozen=True, slots=True)
class ProviderOutcomeEvidence:
    facet: str
    operation: str
    expected: dict[str, Any] = field(default_factory=dict)
    observed: dict[str, Any] = field(default_factory=dict)
    observation_authority: str = "host"
    pending_input: bool = False
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> "ProviderOutcomeEvidence | None":
        if not isinstance(value, dict):
            return None
        facet = str(value.get("facet") or "").strip()
        operation = str(value.get("operation") or "").strip().lower()
        authority = str(value.get("observation_authority") or "").strip().lower()
        if not facet or not operation or authority != "host":
            return None
        try:
            schema_version = max(1, int(value.get("schema_version") or 1))
        except (TypeError, ValueError):
            return None
        expected = value.get("expected")
        observed = value.get("observed")
        return cls(
            facet=facet,
            operation=operation,
            expected=dict(expected) if isinstance(expected, dict) else {},
            observed=dict(observed) if isinstance(observed, dict) else {},
            observation_authority=authority,
            pending_input=bool(value.get("pending_input")),
            schema_version=schema_version,
        )


def evidence_from_metadata(metadata: object) -> ProviderOutcomeEvidence | None:
    source = metadata if isinstance(metadata, dict) else {}
    return ProviderOutcomeEvidence.from_dict(source.get(OUTCOME_EVIDENCE_METADATA_KEY))
