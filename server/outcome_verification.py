"""Provider-neutral verification and narration of structured outcomes.

Execution adapters emit :class:`ProviderOutcomeEvidence`.  This module owns
the facet verifier registry and turns that evidence into the only terminal
claim that Work Ledger, InteractionBranch, and Observer may narrate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable

from agent_host.provider_outcome import ProviderOutcomeEvidence, evidence_from_metadata


OUTCOME_VERDICT_METADATA_KEY = "outcome_verdict"


@dataclass(frozen=True, slots=True)
class ProviderOutcomeVerdict:
    facet: str
    operation: str
    summary: str
    completeness: str
    attention: str
    rationale: str
    verified: bool
    provider_report_allowed: bool
    expected: dict[str, Any]
    observed: dict[str, Any]
    observation_authority: str = "host"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> "ProviderOutcomeVerdict | None":
        if not isinstance(value, dict):
            return None
        facet = str(value.get("facet") or "").strip()
        operation = str(value.get("operation") or "").strip().lower()
        if not facet or not operation:
            return None
        expected = value.get("expected")
        observed = value.get("observed")
        return cls(
            facet=facet,
            operation=operation,
            summary=str(value.get("summary") or ""),
            completeness=str(value.get("completeness") or "partial"),
            attention=str(value.get("attention") or "review"),
            rationale=str(value.get("rationale") or ""),
            verified=bool(value.get("verified")),
            provider_report_allowed=bool(value.get("provider_report_allowed")),
            expected=dict(expected) if isinstance(expected, dict) else {},
            observed=dict(observed) if isinstance(observed, dict) else {},
            observation_authority=str(
                value.get("observation_authority") or "host"
            ),
        )


FacetVerifier = Callable[
    [ProviderOutcomeEvidence, str, str, str],
    ProviderOutcomeVerdict,
]
HostOutcomeObserver = Callable[
    [dict[str, Any], Any, Any],
    ProviderOutcomeEvidence | None,
]


def assess_provider_outcome(
    *,
    execution_status: str,
    provider_report: str,
    metadata: object,
    display_language: str = "english",
) -> ProviderOutcomeVerdict | None:
    _ensure_builtin_verifiers()
    evidence = evidence_from_metadata(metadata)
    requirement = _declared_outcome_requirement(metadata)
    if evidence is None:
        if requirement is None:
            return None
        operation, facet = requirement
        evidence = ProviderOutcomeEvidence(
            facet=facet,
            operation=operation,
            expected={},
            observed={},
        )
    elif requirement is not None and (
        evidence.operation != requirement[0] or evidence.facet != requirement[1]
    ):
        return _contract_mismatch(
            evidence,
            required_operation=requirement[0],
            required_facet=requirement[1],
            execution_status=execution_status,
            display_language=display_language,
        )
    verifier = _FACET_VERIFIERS.get(evidence.facet)
    if verifier is None:
        return _unverified_facet(
            evidence,
            execution_status,
            display_language,
        )
    return verifier(evidence, execution_status, provider_report, display_language)


def observe_required_host_outcome(
    metadata: object,
    *,
    store: Any,
    attempt: Any,
) -> ProviderOutcomeEvidence | None:
    """Observe one Host-declared result contract without consulting Provider prose."""

    _ensure_builtin_verifiers()
    source = metadata if isinstance(metadata, dict) else {}
    requirement = source.get("host_outcome_requirement")
    if not isinstance(requirement, dict):
        return None
    operation = str(requirement.get("operation") or "").strip().lower()
    facet = str(requirement.get("facet") or "").strip()
    if not operation or not facet:
        return None
    observer = _HOST_OUTCOME_OBSERVERS.get(facet)
    if observer is None:
        expected = requirement.get("expected")
        return ProviderOutcomeEvidence(
            facet=facet,
            operation=operation,
            expected=dict(expected) if isinstance(expected, dict) else {},
            observed={},
        )
    return observer(dict(requirement), store, attempt)


def localize_outcome_verdict(
    value: object,
    *,
    execution_status: str,
    display_language: str,
) -> str:
    """Render a stored verdict without reintroducing provider prose."""

    _ensure_builtin_verifiers()
    verdict = ProviderOutcomeVerdict.from_dict(value)
    if verdict is None:
        return ""
    evidence = ProviderOutcomeEvidence(
        facet=verdict.facet,
        operation=verdict.operation,
        expected=dict(verdict.expected),
        observed=dict(verdict.observed),
        observation_authority=verdict.observation_authority,
    )
    verifier = _FACET_VERIFIERS.get(evidence.facet)
    if verifier is None:
        return _unverified_facet(
            evidence,
            execution_status,
            display_language,
        ).summary
    localized = verifier(evidence, execution_status, "", display_language)
    if not verdict.verified and localized.verified:
        # Localization may only restate the stored verdict. It must never turn
        # a contract error or another fail-closed decision into success.
        return _unverified_summary(
            execution_status,
            display_language,
            verifier_missing=False,
        )
    return localized.summary


def _declared_outcome_requirement(
    metadata: object,
) -> tuple[str, str] | None:
    source = metadata if isinstance(metadata, dict) else {}
    host_requirement = source.get("host_outcome_requirement")
    if isinstance(host_requirement, dict):
        operation = str(host_requirement.get("operation") or "").strip().lower()
        facet = str(host_requirement.get("facet") or "").strip()
        if operation and facet:
            return operation, facet
    manifest = source.get("provider_manifest")
    if not isinstance(manifest, dict):
        return None
    capabilities = manifest.get("capabilities")
    operations = (
        capabilities.get("operations") if isinstance(capabilities, dict) else None
    )
    if not isinstance(operations, list):
        return None
    operation_id = str(
        source.get("provider_operation")
        or source.get("action")
        or ""
    ).strip().lower()
    if not operation_id:
        return None
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        if str(operation.get("operation_id") or "").strip().lower() != operation_id:
            continue
        facet = str(operation.get("outcome_facet") or "").strip()
        return (operation_id, facet) if facet else None
    return None


def _unverified_facet(
    evidence: ProviderOutcomeEvidence,
    execution_status: str,
    display_language: str,
) -> ProviderOutcomeVerdict:
    summary = _unverified_summary(
        execution_status,
        display_language,
        verifier_missing=True,
    )
    succeeded = str(execution_status or "").strip().lower() in {
        "done", "complete", "completed", "succeeded", "success"
    }
    return ProviderOutcomeVerdict(
        facet=evidence.facet,
        operation=evidence.operation,
        summary=summary,
        completeness="partial" if succeeded else "incomplete",
        attention="review" if succeeded else "error",
        rationale=f"No verifier is registered for outcome facet {evidence.facet!r}.",
        verified=False,
        provider_report_allowed=False,
        expected=dict(evidence.expected),
        observed=dict(evidence.observed),
        observation_authority=evidence.observation_authority,
    )


def _contract_mismatch(
    evidence: ProviderOutcomeEvidence,
    *,
    required_operation: str,
    required_facet: str,
    execution_status: str,
    display_language: str,
) -> ProviderOutcomeVerdict:
    succeeded = str(execution_status or "").strip().lower() in {
        "done", "complete", "completed", "succeeded", "success"
    }
    return ProviderOutcomeVerdict(
        facet=evidence.facet,
        operation=evidence.operation,
        summary=_unverified_summary(
            execution_status,
            display_language,
            verifier_missing=False,
        ),
        completeness="partial" if succeeded else "incomplete",
        attention="conflict",
        rationale=(
            "Outcome evidence does not match the declared operation contract: "
            f"expected {required_operation}/{required_facet}, received "
            f"{evidence.operation}/{evidence.facet}."
        ),
        verified=False,
        provider_report_allowed=False,
        expected=dict(evidence.expected),
        observed=dict(evidence.observed),
        observation_authority=evidence.observation_authority,
    )


def _unverified_summary(
    execution_status: str,
    display_language: str,
    *,
    verifier_missing: bool,
) -> str:
    succeeded = str(execution_status or "").strip().lower() in {
        "done", "complete", "completed", "succeeded", "success"
    }
    language = str(display_language or "english").strip().lower().replace("-", "_")
    if not succeeded:
        if language in {"ja", "jp", "ja_jp", "japanese"}:
            return "操作は完了しなかったわ。"
        if language in {"zh", "zh_cn", "chinese", "simplified_chinese"}:
            return "操作没有完成。"
        return "The operation did not complete."
    if language in {"ja", "jp", "ja_jp", "japanese"}:
        return (
            "操作は終了したけれど、結果を検証する方法がまだないわ。"
            if verifier_missing
            else "操作は終了したけれど、結果は検証できなかったわ。"
        )
    if language in {"zh", "zh_cn", "chinese", "simplified_chinese"}:
        return (
            "操作已经结束，但目前还没有可用的结果验证方式。"
            if verifier_missing
            else "操作已经结束，但结果未能通过验证。"
        )
    return (
        "The operation ended, but no outcome verifier is available."
        if verifier_missing
        else "The operation ended, but its outcome could not be verified."
    )


_FACET_VERIFIERS: dict[str, FacetVerifier] = {}
_HOST_OUTCOME_OBSERVERS: dict[str, HostOutcomeObserver] = {}
_BUILTINS_LOADED = False


def register_outcome_verifier(
    facet: str,
    verifier: FacetVerifier,
    *,
    replace: bool = False,
) -> None:
    clean_facet = str(facet or "").strip()
    if not clean_facet:
        raise ValueError("outcome verifier facet is required")
    if not callable(verifier):
        raise TypeError("outcome verifier must be callable")
    if clean_facet in _FACET_VERIFIERS and not replace:
        raise ValueError(f"outcome verifier already registered: {clean_facet}")
    _FACET_VERIFIERS[clean_facet] = verifier


def register_host_outcome_observer(
    facet: str,
    observer: HostOutcomeObserver,
    *,
    replace: bool = False,
) -> None:
    clean_facet = str(facet or "").strip()
    if not clean_facet:
        raise ValueError("host outcome observer facet is required")
    if not callable(observer):
        raise TypeError("host outcome observer must be callable")
    if clean_facet in _HOST_OUTCOME_OBSERVERS and not replace:
        raise ValueError(f"host outcome observer already registered: {clean_facet}")
    _HOST_OUTCOME_OBSERVERS[clean_facet] = observer


def _ensure_builtin_verifiers() -> None:
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    # The registry core never imports provider adapters or branches. Built-in
    # facet modules register concrete comparison semantics at this boundary.
    from server import outcome_facets  # noqa: F401

    _BUILTINS_LOADED = True
