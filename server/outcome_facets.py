"""Built-in structured outcome facet verifiers."""

from __future__ import annotations

from agent_host.provider_outcome import ProviderOutcomeEvidence
from server.outcome_verification import (
    ProviderOutcomeVerdict,
    register_host_outcome_observer,
    register_outcome_verifier,
)
from server.browser_page_outcome import (
    BrowserPageOutcomeEvidence,
    assess_browser_page_outcome,
)


_SUCCESS_STATUSES = {"done", "complete", "completed", "succeeded", "success"}


def verify_browser_page_state(
    evidence: ProviderOutcomeEvidence,
    execution_status: str,
    provider_report: str,
    display_language: str,
) -> ProviderOutcomeVerdict:
    expected = dict(evidence.expected)
    observed = dict(evidence.observed)
    decision = assess_browser_page_outcome(
        BrowserPageOutcomeEvidence(
            execution_status=execution_status,
            provider_report=provider_report,
            observed_title=str(observed.get("title") or ""),
            observed_url=str(observed.get("url") or ""),
            observed_text=str(observed.get("text") or ""),
            observed_navigation_chain=tuple(
                str(item or "")
                for item in (
                    observed.get("navigation_chain")
                    if isinstance(observed.get("navigation_chain"), list)
                    else []
                )
            ),
            expected_title=str(expected.get("title") or ""),
            expected_url=str(expected.get("url") or ""),
            pending_input=evidence.pending_input,
        ),
        display_language=display_language,
    )
    return ProviderOutcomeVerdict(
        facet=evidence.facet,
        operation=evidence.operation,
        summary=decision.summary,
        completeness=decision.completeness,
        attention=decision.attention,
        rationale=decision.rationale,
        verified=decision.completeness == "complete",
        provider_report_allowed=decision.provider_report_allowed,
        expected=expected,
        observed=observed,
        observation_authority=evidence.observation_authority,
    )


register_outcome_verifier("browser.page_state", verify_browser_page_state)


def verify_mcp_tool_result(
    evidence: ProviderOutcomeEvidence,
    execution_status: str,
    provider_report: str,
    display_language: str,
) -> ProviderOutcomeVerdict:
    """Verify protocol completion without pretending to know tool semantics.

    The MCP adapter is the Host-side client, so the received result/resource
    bytes are observations.  A protocol result proves only that the exact
    discovered tool completed and returned the declared evidence; domain
    success remains in any explicit structured expectation supplied by the
    selected Provider binding.
    """

    expected = dict(evidence.expected)
    observed = dict(evidence.observed)
    status_ok = str(execution_status or "").strip().lower() in _SUCCESS_STATUSES
    expected_tool = str(expected.get("tool") or "").strip()
    observed_tool = str(observed.get("tool") or "").strip()
    tool_matches = bool(expected_tool and observed_tool == expected_tool)
    protocol_complete = (
        observed.get("is_error") is False
        and str(observed.get("result_type") or "").strip().lower() == "complete"
        and observed.get("result_present") is True
        and not evidence.pending_input
    )
    expected_structured = expected.get("structured_content")
    structured_matches = (
        True
        if expected_structured is None
        else _contains_expected(
            observed.get("structured_content"),
            expected_structured,
        )
    )
    required_resources = {
        str(value or "").strip()
        for value in expected.get("resource_uris") or []
        if str(value or "").strip()
    }
    observed_resources = {
        str(item.get("uri") or "").strip()
        for item in observed.get("resources") or []
        if isinstance(item, dict)
        and item.get("contents")
        and str(item.get("result_type") or "complete") == "complete"
    }
    resources_match = required_resources.issubset(observed_resources)
    verified = bool(
        status_ok
        and tool_matches
        and protocol_complete
        and structured_matches
        and resources_match
    )

    if verified:
        summary = _mcp_success_summary(
            provider_report,
            display_language=display_language,
        )
        completeness = "complete"
        attention = "none"
        rationale = (
            "The Host MCP client discovered and called the declared server tool, "
            "received a complete non-error result, and observed every required "
            "structured/resource fact."
        )
    else:
        summary = _mcp_failure_summary(
            execution_status,
            display_language=display_language,
        )
        completeness = "partial" if status_ok else "incomplete"
        attention = "conflict" if status_ok else "error"
        failed: list[str] = []
        if not tool_matches:
            failed.append("tool identity")
        if not protocol_complete:
            failed.append("complete non-error result")
        if not structured_matches:
            failed.append("structured result")
        if not resources_match:
            failed.append("required resources")
        rationale = "MCP outcome verification failed: " + ", ".join(failed or ["execution"])
    return ProviderOutcomeVerdict(
        facet=evidence.facet,
        operation=evidence.operation,
        summary=summary,
        completeness=completeness,
        attention=attention,
        rationale=rationale,
        verified=verified,
        provider_report_allowed=bool(verified and str(provider_report or "").strip()),
        expected=expected,
        observed=observed,
        observation_authority=evidence.observation_authority,
    )


def _contains_expected(observed: object, expected: object) -> bool:
    if isinstance(expected, dict):
        if not isinstance(observed, dict):
            return False
        return all(
            key in observed and _contains_expected(observed[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        if not isinstance(observed, list) or len(observed) < len(expected):
            return False
        return all(
            _contains_expected(observed[index], value)
            for index, value in enumerate(expected)
        )
    return observed == expected


def _mcp_success_summary(provider_report: str, *, display_language: str) -> str:
    report = " ".join(str(provider_report or "").split())[:520]
    if report:
        return report
    language = str(display_language or "english").strip().lower().replace("-", "_")
    if language in {"ja", "jp", "ja_jp", "japanese"}:
        return "外部操作は完了し、結果も確認できたわ。"
    if language in {"zh", "zh_cn", "chinese", "simplified_chinese"}:
        return "外部操作已经完成，结果也已确认。"
    return "The external operation completed and its result was verified."


def _mcp_failure_summary(execution_status: str, *, display_language: str) -> str:
    succeeded = str(execution_status or "").strip().lower() in _SUCCESS_STATUSES
    language = str(display_language or "english").strip().lower().replace("-", "_")
    if language in {"ja", "jp", "ja_jp", "japanese"}:
        return (
            "外部操作は終了したけれど、結果は確認できなかったわ。"
            if succeeded
            else "外部操作は完了しなかったわ。"
        )
    if language in {"zh", "zh_cn", "chinese", "simplified_chinese"}:
        return "外部操作结束了，但结果未通过验证。" if succeeded else "外部操作没有完成。"
    return (
        "The external operation ended, but its result was not verified."
        if succeeded
        else "The external operation did not complete."
    )


register_outcome_verifier("mcp.tool_result", verify_mcp_tool_result)


def observe_auip_application(
    requirement: dict,
    store: object,
    attempt: object,
) -> ProviderOutcomeEvidence:
    """Observe whether this Attempt produced the launchable AUIP revision."""

    from server.auip_app_source import (
        discover_registered_auip_app,
        discover_staged_auip_app,
    )

    work_item_id = str(getattr(attempt, "work_item_id", "") or "")
    attempt_id = str(getattr(attempt, "attempt_id", "") or "")
    current_attempt = (
        store.get_attempt(attempt_id)
        if attempt_id and callable(getattr(store, "get_attempt", None))
        else None
    ) or attempt
    attempt_metadata = (
        getattr(current_attempt, "metadata", {})
        if isinstance(getattr(current_attempt, "metadata", {}), dict)
        else {}
    )
    export_plan = (
        attempt_metadata.get("export_plan")
        if isinstance(attempt_metadata.get("export_plan"), dict)
        else {}
    )
    bundle_validation = (
        attempt_metadata.get("host_auip_bundle_validation")
        if isinstance(attempt_metadata.get("host_auip_bundle_validation"), dict)
        else {}
    )
    bundle_validation_required = bool(
        export_plan.get("host_validates_auip_bundle") is True
        or attempt_metadata.get("auip_host_validates_bundle") is True
    )
    candidate = (
        discover_staged_auip_app(store, work_item_id, attempt_id)
        if export_plan.get("host_validates_auip_bundle") is True
        else discover_registered_auip_app(store, work_item_id)
    )
    contributors = {
        str(value)
        for value in (
            candidate.get("contributing_attempt_ids", [])
            if isinstance(candidate, dict)
            else []
        )
    }
    bundle_validation_verified = bundle_validation.get("verified") is True
    verified = (
        bool(candidate)
        and attempt_id in contributors
        and (not bundle_validation_required or bundle_validation_verified)
    )
    expected = requirement.get("expected")
    observed = {
        "verified": verified,
        "current_attempt_contributed": attempt_id in contributors,
        "bundle_validation_required": bundle_validation_required,
        "bundle_validation_verified": bundle_validation_verified,
    }
    if bundle_validation.get("code"):
        observed["bundle_validation_code"] = str(bundle_validation.get("code"))
    if verified and isinstance(candidate, dict):
        app = candidate.get("app") if isinstance(candidate.get("app"), dict) else {}
        observed.update(
            {
                "app_id": str(app.get("id") or ""),
                "app_title": str(app.get("title") or ""),
            }
        )
    return ProviderOutcomeEvidence(
        facet="auip.application",
        operation=str(requirement.get("operation") or "prepare"),
        expected=dict(expected) if isinstance(expected, dict) else {},
        observed=observed,
    )


def verify_auip_application(
    evidence: ProviderOutcomeEvidence,
    execution_status: str,
    _provider_report: str,
    display_language: str,
) -> ProviderOutcomeVerdict:
    succeeded = str(execution_status or "").strip().lower() in {
        "done",
        "complete",
        "completed",
        "succeeded",
        "success",
    }
    verified = succeeded and evidence.observed.get("verified") is True
    language = str(display_language or "english").strip().lower().replace("-", "_")
    if verified:
        summary = (
            "AUIPアプリとして検証できたわ。起動結果はHostの接続確認で確定する。"
            if language in {"ja", "jp", "ja_jp", "japanese"}
            else "应用已通过 AUIP 验证；是否真正启动仍以 Host 的连接回执为准。"
            if language in {"zh", "zh_cn", "chinese", "simplified_chinese"}
            else "The AUIP application is verified; actual launch still requires a Host connection receipt."
        )
    else:
        summary = (
            "AUIP対応のアプリを確認できなかったため、まだ起動できないわ。"
            if language in {"ja", "jp", "ja_jp", "japanese"}
            else "没有验证到可用的 AUIP 应用，因此尚未启动。"
            if language in {"zh", "zh_cn", "chinese", "simplified_chinese"}
            else "No verified AUIP application was produced, so nothing has been launched."
        )
    return ProviderOutcomeVerdict(
        facet=evidence.facet,
        operation=evidence.operation,
        summary=summary,
        completeness="complete" if verified else "incomplete",
        attention="review" if verified else "error",
        rationale=(
            "The Host verified a launchable AUIP manifest and entry revision contributed by this Attempt."
            if verified
            else "The Host could not verify a launchable AUIP manifest and entry revision contributed by this Attempt."
        ),
        verified=verified,
        provider_report_allowed=verified,
        expected=dict(evidence.expected),
        observed=dict(evidence.observed),
        observation_authority=evidence.observation_authority,
    )


register_host_outcome_observer("auip.application", observe_auip_application)
register_outcome_verifier("auip.application", verify_auip_application)
