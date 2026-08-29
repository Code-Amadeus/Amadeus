"""Browser page-state verifier constrained by host-observed facts.

Provider closing text is a claim.  A user-facing terminal line may repeat it
only when structured expected state agrees with structured state observed by
the host. The provider-neutral registry lives in ``outcome_verification``;
this module is the concrete ``browser.page_state`` facet implementation.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from urllib.parse import parse_qsl, urlparse

from server.work_completion import CompletionEvidence, assess_completion


_TRACKING_QUERY_KEYS = frozenset(
    {
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "spm",
        "spm_id_from",
    }
)


@dataclass(frozen=True, slots=True)
class BrowserPageOutcomeEvidence:
    execution_status: str
    provider_report: str = ""
    observed_title: str = ""
    observed_url: str = ""
    observed_text: str = ""
    expected_title: str = ""
    expected_url: str = ""
    observed_navigation_chain: tuple[str, ...] = ()
    pending_input: bool = False


@dataclass(frozen=True, slots=True)
class BrowserPageOutcomeDecision:
    summary: str
    completeness: str
    attention: str
    rationale: str
    provider_report_allowed: bool
    observed_title: str
    observed_url: str
    expected_title: str
    expected_url: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def assess_browser_page_outcome(
    evidence: BrowserPageOutcomeEvidence,
    *,
    display_language: str = "english",
) -> BrowserPageOutcomeDecision:
    """Decide what may be said after a provider attempt ends.

    Agreement and conflict are evaluated only from structured fields.  When
    expected state is absent, the report remains unverified and narration is
    generated from the observed title/URL instead of repeating the report.
    """

    status = str(evidence.execution_status or "failed").strip().lower()
    observed_title = _clean(evidence.observed_title, 180)
    observed_url = _clean(evidence.observed_url, 800)
    observed_text = _clean(evidence.observed_text, 2000)
    expected_title = _clean(evidence.expected_title, 180)
    expected_url = _clean(evidence.expected_url, 800)

    obstruction = _observed_obstruction(
        title=observed_title,
        url=observed_url,
        text=observed_text,
    )
    if obstruction:
        kind, detail = obstruction
        attention = "input" if kind in {"human_verification", "authentication"} else "error"
        return BrowserPageOutcomeDecision(
            summary=_obstruction_summary(
                kind,
                display_language=display_language,
            ),
            completeness="incomplete",
            attention=attention,
            rationale=(
                "The host-observed page is an obstruction rather than the requested "
                f"result ({kind}: {detail})."
            ),
            provider_report_allowed=False,
            observed_title=observed_title,
            observed_url=observed_url,
            expected_title=expected_title,
            expected_url=expected_url,
        )

    comparisons: list[bool] = []
    conflict_fields: list[str] = []
    missing_observations: list[str] = []
    if expected_title:
        if not observed_title:
            missing_observations.append("title")
        else:
            matched = _same_title(expected_title, observed_title)
            comparisons.append(matched)
            if not matched:
                conflict_fields.append("title")
    if expected_url:
        if not observed_url:
            missing_observations.append("URL")
        else:
            matched = _same_url(expected_url, observed_url) or _verified_redirect(
                expected_url,
                observed_url,
                evidence.observed_navigation_chain,
            )
            comparisons.append(matched)
            if not matched:
                conflict_fields.append("URL")

    has_expected_state = bool(expected_title or expected_url)
    verified = has_expected_state and bool(comparisons) and all(comparisons) and not missing_observations
    missing_requirements: tuple[str, ...] = ()
    if status in {"done", "complete", "completed", "succeeded", "success"} and not verified:
        missing_requirements = (
            "terminal report lacks matching structured host observations",
        )

    completion = assess_completion(
        CompletionEvidence(
            execution_status=status,
            explicit_complete=verified,
            pending_inputs=1 if evidence.pending_input else 0,
            conflicts=(
                ("terminal expected state conflicts with host-observed " + ", ".join(conflict_fields),)
                if conflict_fields
                else ()
            ),
            missing_requirements=missing_requirements,
        )
    )
    provider_report_allowed = bool(
        completion.execution_status == "succeeded"
        and verified
        and completion.attention not in {"conflict", "error", "permission", "input"}
        and str(evidence.provider_report or "").strip()
        and _report_matches_display_language(
            str(evidence.provider_report or ""),
            display_language,
        )
    )
    if provider_report_allowed:
        summary = _clean(evidence.provider_report, 520)
    else:
        summary = _host_summary(
            execution_status=completion.execution_status,
            attention=completion.attention,
            title=observed_title,
            url=observed_url,
            display_language=display_language,
            verified=verified,
        )

    rationale = completion.rationale
    if conflict_fields:
        rationale = (
            "The provider attempt ended, but structured expected state conflicts "
            "with host-observed " + ", ".join(conflict_fields) + "."
        )
    elif completion.execution_status == "succeeded" and not verified:
        detail = (
            "host observations are missing for " + ", ".join(missing_observations)
            if missing_observations
            else "the provider supplied no verifiable expected page state"
        )
        rationale = "The provider attempt ended, but " + detail + "."
    elif (
        completion.execution_status == "succeeded"
        and verified
        and str(evidence.provider_report or "").strip()
        and not _report_matches_display_language(
            str(evidence.provider_report or ""),
            display_language,
        )
    ):
        rationale = (
            "Structured expected state matched host observations, but the provider "
            "report did not match the active display language."
        )

    return BrowserPageOutcomeDecision(
        summary=summary,
        completeness=completion.completeness,
        attention=completion.attention,
        rationale=rationale,
        provider_report_allowed=provider_report_allowed,
        observed_title=observed_title,
        observed_url=observed_url,
        expected_title=expected_title,
        expected_url=expected_url,
    )


def _observed_obstruction(*, title: str, url: str, text: str) -> tuple[str, str] | None:
    """Classify a host-observed blocked/error page, independent of provider.

    Process exit and navigation success say only that a page loaded.  These
    markers identify pages whose own content says the user's requested result
    was not reached.  The body excerpt is bounded at the adapter boundary.
    """

    title_material = title.lower()
    url_material = url.lower()
    body_material = text.lower()
    # A healthy search result or article may merely discuss CAPTCHAs,
    # forbidden responses, or security checks.  Broad nouns are therefore not
    # evidence at all; require obstruction-shaped phrases or known error URLs.
    strong_material = " ".join((title_material, url_material))
    markers = (
        (
            "human_verification",
            (
                "verify you are human",
                "verification required",
                "not a robot",
                "unusual traffic",
                "/sorry/",
                "人机验证",
                "机器人验证",
                "ロボットではない",
            ),
        ),
        (
            "authentication",
            (
                "sign in to continue",
                "log in to continue",
                "authentication required",
                "登录后继续",
                "请先登录",
                "ログインしてください",
            ),
        ),
        (
            "rate_limited",
            (
                "too many requests",
                "rate limit",
                "temporarily blocked",
                "请求过于频繁",
                "アクセスが集中",
            ),
        ),
        (
            "access_denied",
            (
                "access denied",
                "request blocked",
                "访问被拒绝",
                "アクセスが拒否",
            ),
        ),
        (
            "page_error",
            (
                "unexpected error",
                "something went wrong",
                "service unavailable",
                "internal server error",
                "page unavailable",
                "/static-pages/418.html",
                "发生意外错误",
                "予期しないエラー",
            ),
        ),
        (
            "missing_resource",
            (
                "this page does not exist",
                "there is currently no text in this page",
                "page not found",
                "ウィキペディアには現在この名前の項目はありません",
                "该页面不存在",
                "此页面尚未创建",
            ),
        ),
    )
    for kind, candidates in markers:
        marker = next(
            (
                candidate
                for candidate in candidates
                if candidate in strong_material or candidate in body_material
            ),
            "",
        )
        if marker:
            return kind, marker
    return None


def _obstruction_summary(
    kind: str,
    *,
    display_language: str,
) -> str:
    language = _normalize_language(display_language)
    needs_person = kind in {"human_verification", "authentication"}
    missing_resource = kind == "missing_resource"
    if language == "japanese":
        if needs_person:
            return "ページが本人確認を求めているため、依頼された結果にはまだ到達していないわ。"
        if missing_resource:
            return "指定されたページは存在しないため、依頼された内容には到達できなかったわ。"
        return "ページ側でエラーまたはアクセス制限が発生し、依頼された結果には到達できなかったわ。"
    if language == "simplified_chinese":
        if needs_person:
            return "页面要求人工验证或登录，尚未到达请求的结果。"
        if missing_resource:
            return "指定页面不存在，尚未到达请求的内容。"
        return "页面返回错误或访问限制，未能到达请求的结果。"
    if needs_person:
        return "The page requires human verification or authentication; the requested result was not reached."
    if missing_resource:
        return "The requested page does not exist; the requested result was not reached."
    return "The page returned an error or access restriction; the requested result was not reached."


def _host_summary(
    *,
    execution_status: str,
    attention: str,
    title: str,
    url: str,
    display_language: str,
    verified: bool = False,
) -> str:
    label = _page_label(title, url)
    language = _normalize_language(display_language)
    if language == "japanese":
        if execution_status != "succeeded":
            return (
                f"操作は完了しなかったわ。現在のページは{label}よ。"
                if label
                else "操作は完了しなかったわ。"
            )
        if attention == "input":
            return (
                f"続けるには追加の情報が必要よ。現在のページは{label}。"
                if label
                else "続けるには追加の情報が必要よ。"
            )
        if attention == "conflict":
            return (
                f"操作結果の報告と現在のページが一致していないわ。現在のページは{label}。確認が必要よ。"
                if label
                else "操作結果の報告を確認できなかったわ。確認が必要よ。"
            )
        if verified:
            return (
                f"操作は完了したわ。現在のページは{label}よ。"
                if label
                else "操作は完了したわ。"
            )
        return (
            f"操作は終了したわ。現在のページは{label}。報告の内容はまだ確認が必要よ。"
            if label
            else "操作は終了したけれど、結果の内容はまだ確認が必要よ。"
        )
    if language == "simplified_chinese":
        if execution_status != "succeeded":
            return f"操作没有完成。当前页面是{label}。" if label else "操作没有完成。"
        if attention == "input":
            return f"继续操作还需要补充信息。当前页面是{label}。" if label else "继续操作还需要补充信息。"
        if attention == "conflict":
            return (
                f"操作报告与当前页面不一致。当前页面是{label}，需要核对。"
                if label
                else "操作报告无法由当前页面证实，需要核对。"
            )
        if verified:
            return f"操作已完成。当前页面是{label}。" if label else "操作已完成。"
        return (
            f"操作已经结束。当前页面是{label}；报告内容仍需核对。"
            if label
            else "操作已经结束，但结果内容仍需核对。"
        )
    if execution_status != "succeeded":
        return (
            f"The operation did not complete. The current page is {label}."
            if label
            else "The operation did not complete."
        )
    if attention == "input":
        return (
            f"More information is needed to continue. The current page is {label}."
            if label
            else "More information is needed to continue."
        )
    if attention == "conflict":
        return (
            f"The reported outcome conflicts with the current page, {label}. It needs review."
            if label
            else "The reported outcome could not be verified and needs review."
        )
    if verified:
        return (
            f"The operation completed. The current page is {label}."
            if label
            else "The operation completed."
        )
    return (
        f"The operation ended on {label}, but the reported outcome still needs review."
        if label
        else "The operation ended, but its reported outcome still needs review."
    )


def _page_label(title: str, url: str) -> str:
    host = ""
    try:
        host = (urlparse(url).hostname or "").removeprefix("www.")
    except Exception:
        host = ""
    if title and host and host.casefold() not in title.casefold():
        return f"“{title}” ({host})"
    if title:
        return f"“{title}”"
    return host


def _same_title(expected: str, observed: str) -> bool:
    return " ".join(expected.casefold().split()) == " ".join(observed.casefold().split())


def _same_url(expected: str, observed: str) -> bool:
    expected_value = _canonical_url(expected)
    observed_value = _canonical_url(observed)
    if not expected_value[0] or not expected_value[1]:
        return False
    if not observed_value[0] or not observed_value[1]:
        return False
    return expected_value == observed_value


def _verified_redirect(
    expected: str,
    observed: str,
    navigation_chain: tuple[str, ...],
) -> bool:
    """Accept a different final URL only when the browser observed the link."""
    chain = tuple(
        str(item or "").strip()
        for item in navigation_chain
        if str(item or "").strip()
    )
    if len(chain) < 2:
        return False
    return _same_url(expected, chain[0]) and _same_url(observed, chain[-1])


def _canonical_url(value: str) -> tuple[object, ...]:
    parsed = urlparse(str(value or "").strip())
    scheme = parsed.scheme.casefold()
    # A very common navigation canonicalization is an origin redirect between
    # example.com and www.example.com.  Treat that as the same destination;
    # other subdomains remain distinct and therefore still surface conflicts.
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    try:
        port = parsed.port
    except ValueError:
        port = None
    if (scheme, port) in {("http", 80), ("https", 443)}:
        port = None
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = tuple(
        sorted(
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if not _is_tracking_query_key(key)
        )
    )
    return scheme, host, port, path, query


def _is_tracking_query_key(key: str) -> bool:
    normalized = str(key or "").strip().casefold()
    return normalized in _TRACKING_QUERY_KEYS or normalized.startswith("utm_")


def _normalize_language(value: str) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"ja", "jp", "ja_jp", "japanese", "日文", "日本語"}:
        return "japanese"
    if text in {"zh", "zh_cn", "chinese", "simplified_chinese", "中文", "简体中文"}:
        return "simplified_chinese"
    return "english"


def _report_matches_display_language(report: str, display_language: str) -> bool:
    text = str(report or "").strip()
    if not text:
        return False
    language = _normalize_language(display_language)
    has_kana = bool(re.search(r"[\u3040-\u30ff]", text))
    cjk_count = len(re.findall(r"[\u3400-\u9fff]", text))
    latin_count = len(re.findall(r"[A-Za-z]", text))
    has_cjk = cjk_count > 0
    has_latin = latin_count > 0
    if language == "japanese":
        # A user-facing Japanese sentence should contain kana. This rejects a
        # Chinese/English planner report while still allowing arbitrary page
        # titles inside an otherwise Japanese sentence.
        return has_kana
    if language == "simplified_chinese":
        return has_cjk and not has_kana
    return has_latin and not has_kana and latin_count >= max(3, cjk_count)


def _clean(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."
