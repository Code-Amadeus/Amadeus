"""Browser terminal narration must follow structured host facts, not prose."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.adapters.browser_branch import BrowserBranchAdapter
from agent_host.browser_interaction_policy import BrowserBranchPolicyDecision
from agent_host.provider_outcome import ProviderOutcomeEvidence
from agent_host.provider_types import ProviderRunRequest, ProviderRunResult
from server.interaction_branch import InteractionBranchCoordinator
from server.provider_branch import ProviderBranchStore


FALSE_REPORT = "最初のAmadeus動画を開いたわ。"


def _coordinator(*, language: str = "japanese") -> InteractionBranchCoordinator:
    async def provider_run(_params):
        return {"run": {"run_id": "unused", "status": "running"}}

    return InteractionBranchCoordinator(
        provider_run=provider_run,
        root=tempfile.mkdtemp(prefix="browser_truth_"),
        display_language=lambda: language,
    )


def _terminal_run(
    *,
    run_id: str = "run_1",
    branch_id: str = "branch_1",
    observed_url: str = "https://video.example/watch/right",
    observed_title: str = "Amadeus — Right video",
    observed_text: str = "",
    expected_url: str = "",
    expected_title: str = "",
    final_report: str = FALSE_REPORT,
    branch_intent: str = "new",
    status: str = "done",
) -> dict:
    return {
        "provider": "browser",
        "run_id": run_id,
        "status": status,
        "task": "open an Amadeus video",
        "result": final_report,
        "metadata": {
            "session_id": "session_1",
            "source": "llm_delegate",
            "branch_intent": branch_intent,
            "interaction_branch_id": branch_id,
            "outcome_evidence": ProviderOutcomeEvidence(
                facet="browser.page_state",
                operation="click_ref",
                observed={
                    "url": observed_url,
                    "title": observed_title,
                    "text": observed_text,
                },
                expected={"url": expected_url, "title": expected_title},
            ).to_dict(),
            "browser": {
                "browser_session_id": "browser_1",
                "current_url": observed_url,
                "title": observed_title,
            },
            "provider_branch": {
                "branch_id": branch_id,
                "final_report": final_report,
                "compact_digest": "provider-only hidden digest",
                "actions": [{"action": "click_ref", "ref": "br_1"}],
                "next_state": {
                    "browser_session_id": "browser_1",
                    "current_url": observed_url,
                    "page_title": observed_title,
                    "expected_state": {
                        "url": expected_url,
                        "title": expected_title,
                    },
                },
            },
        },
    }


def test_contradicted_report_is_not_repeated() -> None:
    coordinator = _coordinator()
    run = _terminal_run(
        observed_url="https://search.example/results?q=amadeus",
        observed_title="Search results for Amadeus",
        expected_url="https://video.example/watch/first",
    )

    branch = coordinator._update_from_run(run)

    assert branch is not None
    assert branch.attention == "conflict"
    assert branch.completeness == "partial"
    assert FALSE_REPORT not in branch.visible_summary
    assert "Search results for Amadeus" in branch.visible_summary
    assert coordinator._display_text_for_run(run, branch) == branch.visible_summary
    # The provider's wording is retained as a hidden report for audit/debug.
    assert branch.hidden_messages[-2]["content"] == FALSE_REPORT


def test_matching_structured_state_allows_the_existing_report() -> None:
    coordinator = _coordinator()
    url = "https://video.example/watch/right"
    run = _terminal_run(expected_url=url)

    branch = coordinator._update_from_run(run)

    assert branch is not None
    assert branch.visible_summary == FALSE_REPORT
    assert branch.completeness == "complete"
    assert branch.attention == "review"
    assert branch.metadata["outcome_verdict"]["provider_report_allowed"] is True

    replayed = coordinator._update_from_run(run)
    assert replayed is branch
    assert replayed.metadata["outcome_verdict"]["provider_report_allowed"] is True


def test_tracking_query_does_not_turn_the_same_page_into_a_conflict() -> None:
    coordinator = _coordinator()
    expected = "https://www.bilibili.com/video/BV1SP746KEBM/"
    observed = expected + "?spm_id_from=333.337.search-card.all.click&utm_source=test"
    run = _terminal_run(observed_url=observed, expected_url=expected)

    branch = coordinator._update_from_run(run)

    assert branch is not None
    assert branch.visible_summary == FALSE_REPORT
    assert branch.completeness == "complete"
    assert branch.attention == "review"
    assert branch.metadata["outcome_verdict"]["provider_report_allowed"] is True


def test_direct_open_uses_adapter_expected_state_and_accepts_www_redirect() -> None:
    coordinator = _coordinator()
    report = "ビリビリを開いたわ。"
    run = {
        "provider": "browser",
        "run_id": "direct_open",
        "status": "done",
        "task": "打开哔哩哔哩网站（bilibili.com）",
        "result": report,
        "metadata": {
            "session_id": "session_1",
            "source": "llm_delegate",
            "outcome_evidence": ProviderOutcomeEvidence(
                facet="browser.page_state",
                operation="open",
                observed={
                    "url": "https://www.bilibili.com/",
                    "title": "哔哩哔哩_bilibili",
                },
                expected={"url": "https://bilibili.com/"},
            ).to_dict(),
            "browser": {
                "browser_session_id": "browser_1",
                "current_url": "https://www.bilibili.com/",
                "title": "哔哩哔哩_bilibili",
                "expected_state": {"url": "https://bilibili.com/"},
            },
        },
    }

    branch = coordinator._update_from_run(run)

    assert branch is not None
    assert branch.visible_summary == report
    assert branch.completeness == "complete"
    assert branch.metadata["outcome_verdict"]["expected"]["url"] == "https://bilibili.com/"
    assert branch.metadata["outcome_verdict"]["provider_report_allowed"] is True


def test_host_observed_redirect_chain_accepts_the_final_page() -> None:
    coordinator = _coordinator()
    expected = "https://ja.wikipedia.org/wiki/牧瀬紅莉栖"
    observed = "https://ja.wikipedia.org/wiki/STEINS;GATEの登場人物#牧瀬紅莉栖"
    run = _terminal_run(observed_url=observed, expected_url=expected)
    run["metadata"]["outcome_evidence"]["observed"]["navigation_chain"] = [
        expected,
        observed,
    ]

    branch = coordinator._update_from_run(run)

    assert branch is not None
    assert branch.completeness == "complete"
    assert branch.attention == "review"
    assert branch.metadata["outcome_verdict"]["provider_report_allowed"] is True


def test_unproven_same_origin_page_change_remains_a_conflict() -> None:
    coordinator = _coordinator()
    expected = "https://docs.example/products/amadeus"
    observed = "https://docs.example/products/other"
    run = _terminal_run(observed_url=observed, expected_url=expected)

    branch = coordinator._update_from_run(run)

    assert branch is not None
    assert branch.attention == "conflict"
    assert branch.metadata["outcome_verdict"]["provider_report_allowed"] is False


def test_semantic_query_difference_remains_a_conflict() -> None:
    coordinator = _coordinator()
    run = _terminal_run(
        observed_url="https://search.example/results?q=other",
        expected_url="https://search.example/results?q=amadeus",
    )

    branch = coordinator._update_from_run(run)

    assert branch is not None
    assert branch.attention == "conflict"
    assert branch.metadata["outcome_verdict"]["provider_report_allowed"] is False


def test_verified_report_in_the_wrong_language_uses_localized_host_facts() -> None:
    coordinator = _coordinator(language="japanese")
    url = "https://search.bilibili.com/all?keyword=Amadeus"
    chinese_report = "已返回上一页的 Amadeus 搜索结果。"
    run = _terminal_run(
        observed_url=url,
        observed_title="Amadeus-哔哩哔哩_bilibili",
        expected_url=url,
        final_report=chinese_report,
    )

    branch = coordinator._update_from_run(run)

    assert branch is not None
    assert branch.completeness == "complete"
    assert branch.metadata["outcome_verdict"]["provider_report_allowed"] is False
    assert chinese_report not in branch.visible_summary
    assert "操作は完了したわ" in branch.visible_summary
    assert "Amadeus-哔哩哔哩_bilibili" in branch.visible_summary


def test_unverifiable_report_uses_only_page_title_and_url_facts() -> None:
    coordinator = _coordinator(language="english")
    unverified = "Opened the first result. DOM SECRET SHOULD NEVER BE SPOKEN."
    run = _terminal_run(
        final_report=unverified,
        observed_url="https://search.example/results?q=amadeus",
        observed_title="Search results for Amadeus",
    )

    branch = coordinator._update_from_run(run)

    assert branch is not None
    assert branch.attention == "review"
    assert branch.completeness == "partial"
    assert "DOM SECRET" not in branch.visible_summary
    assert "Search results for Amadeus" in branch.visible_summary
    assert unverified not in branch.page_summary
    assert branch.page_summary == (
        "Search results for Amadeus — https://search.example/results?q=amadeus"
    )


def test_robot_check_is_a_host_observed_blocker_not_a_successful_search() -> None:
    coordinator = _coordinator(language="simplified_chinese")
    false_report = "已经找到2026年菲尔兹奖获奖者。"
    run = _terminal_run(
        observed_url="https://www.google.com/sorry/index?continue=search",
        observed_title="About this page",
        observed_text=(
            "Our systems have detected unusual traffic. This page checks to see "
            "if it is really you and not a robot."
        ),
        expected_url="https://www.google.com/search?q=2026+Fields+Medal+winners",
        final_report=false_report,
    )

    branch = coordinator._update_from_run(run)

    assert branch is not None
    assert branch.completeness == "incomplete"
    assert branch.attention == "input"
    assert false_report not in branch.visible_summary
    assert "人工验证" in branch.visible_summary


def test_browser_error_page_is_not_ready_for_review() -> None:
    coordinator = _coordinator(language="english")
    run = _terminal_run(
        observed_url="https://duckduckgo.com/static-pages/418.html",
        observed_title="DuckDuckGo Unexpected error",
        observed_text="Unexpected error. Please try again.",
        final_report="Opened the requested search results.",
    )

    branch = coordinator._update_from_run(run)

    assert branch is not None
    assert branch.completeness == "incomplete"
    assert branch.attention == "error"
    assert "requested result was not reached" in branch.visible_summary


def test_missing_article_is_not_reported_as_a_successful_open() -> None:
    coordinator = _coordinator(language="japanese")
    url = "https://ja.wikipedia.org/wiki/nonexistent"
    run = _terminal_run(
        observed_url=url,
        observed_title="Nonexistent - Wikipedia",
        observed_text="ウィキペディアには現在この名前の項目はありません。",
        expected_url=url,
        final_report="ページを開いたわ。",
    )

    branch = coordinator._update_from_run(run)

    assert branch is not None
    assert branch.completeness == "incomplete"
    assert branch.attention == "error"
    assert branch.metadata["outcome_verdict"]["provider_report_allowed"] is False
    assert "存在しない" in branch.visible_summary


def test_article_discussing_browser_blocks_is_not_itself_a_blocked_page() -> None:
    coordinator = _coordinator(language="english")
    url = "https://docs.example/browser-security"
    report = "Opened the browser security documentation."
    run = _terminal_run(
        observed_url=url,
        observed_title="Understanding CAPTCHA and forbidden responses",
        observed_text=(
            "This article explains CAPTCHA, security check design, and why a "
            "server may return a forbidden response."
        ),
        expected_url=url,
        final_report=report,
    )

    branch = coordinator._update_from_run(run)

    assert branch is not None
    assert branch.completeness == "complete"
    assert branch.attention == "review"
    assert branch.visible_summary == report


def test_new_failed_run_never_reuses_the_previous_visible_summary() -> None:
    coordinator = _coordinator(language="english")
    previous_report = "Opened the Amadeus video."
    previous = coordinator._update_from_run(
        _terminal_run(
            expected_url="https://video.example/watch/right",
            final_report=previous_report,
        )
    )
    assert previous is not None and previous.visible_summary == previous_report
    failed = {
        "provider": "browser",
        "run_id": "run_failed",
        "status": "error",
        "result": "provider claimed an unrelated success",
        "metadata": {"provider_branch": {"final_report": "provider claimed an unrelated success"}},
    }

    display = coordinator._display_text_for_run(failed, previous)

    assert display != previous_report
    assert "The operation did not complete" in display


def test_continue_reuses_branch_new_supersedes_and_close_is_explicit() -> None:
    coordinator = _coordinator(language="english")
    first = coordinator._update_from_run(
        _terminal_run(expected_url="https://video.example/watch/right")
    )
    assert first is not None

    continued = coordinator._update_from_run(
        _terminal_run(
            run_id="run_2",
            branch_id="branch_1",
            observed_url="https://video.example/watch/next",
            observed_title="Amadeus — Next video",
            expected_url="https://video.example/watch/next",
            branch_intent="continue",
        )
    )
    assert continued is first
    assert continued.url.endswith("/next")
    assert continued.merge_count == 2

    replacement = coordinator._update_from_run(
        _terminal_run(
            run_id="run_3",
            branch_id="branch_2",
            observed_url="https://docs.example/amadeus",
            observed_title="Amadeus documentation",
            expected_url="https://docs.example/amadeus",
            branch_intent="new",
        )
    )
    assert replacement is not None and replacement is not first
    assert first.status == "closed"
    assert coordinator.active_branch_for_session("session_1") is replacement
    assert coordinator.close_active_branch("session_1", reason="test_close") is True
    assert coordinator.active_branch_for_session("session_1") is None


class _FakeBrowserBase:
    def __init__(self) -> None:
        self.run_count = 0
        self.inspect_count = 0

    async def run(self, _request, _run_id, _emit) -> ProviderRunResult:
        self.run_count += 1
        return ProviderRunResult(
            status="done",
            result="base result",
            metadata={"browser": {"browser_session_id": "browser_1"}},
        )

    async def inspect_session(self, _session_id: str, *, include_dom: bool = False) -> dict:
        self.inspect_count += 1
        if self.inspect_count == 1:
            return {
                "browser_session_id": "browser_1",
                "url": "https://search.example/results?q=amadeus",
                "title": "Search results",
                "text": "search page text",
                "dom": "<html>hidden</html>" if include_dom else "",
                "interaction_refs": [
                    {
                        "ref": "br_1",
                        "label": "Amadeus video",
                        "href": "/watch/right",
                    }
                ],
            }
        return {
            "browser_session_id": "browser_1",
            "url": "https://search.example/watch/right",
            "title": "Amadeus — Right video",
            "text": "video page text",
            "dom": "<html>hidden video page</html>" if include_dom else "",
            "interaction_refs": [],
        }

    async def cancel(self, _run_id: str) -> None:
        return None

    async def shutdown(self) -> None:
        return None


def test_adapter_exports_expected_state_from_action_ref_href() -> None:
    async def run() -> None:
        async def planner(_context):
            return {
                "actions": [{"action": "click_ref", "ref": "br_1", "task": "open result"}],
                "final_report": FALSE_REPORT,
                "compact_digest": "clicked a structured ref",
            }

        async def emit(_event):
            return None

        base = _FakeBrowserBase()
        with tempfile.TemporaryDirectory(prefix="provider_branch_truth_") as root:
            adapter = BrowserBranchAdapter(
                base_adapter=base,
                store=ProviderBranchStore(Path(root)),
                branch_planner=planner,
            )
            request = ProviderRunRequest(
                provider="browser",
                task="open the first Amadeus result",
                mode="observe",
                metadata={"session_id": "session_1", "provider_branch": True},
            )
            policy = BrowserBranchPolicyDecision(
                use_branch=True,
                entry_kind="dom_branch",
                initial_request=request,
                max_actions=3,
                capture_hidden_dom=True,
                merge_strategy="compact_visible_merge",
                reason="test",
            )
            result = await adapter._run_branch(
                request,
                "run_1",
                emit,
                policy_decision=policy,
            )

        next_state = result.metadata["provider_branch"]["next_state"]
        assert next_state["expected_state"] == {
            "url": "https://search.example/watch/right"
        }
        assert next_state["current_url"] == "https://search.example/watch/right"
        evidence = result.outcome_evidence
        assert evidence is not None
        assert evidence.expected == next_state["expected_state"]
        assert evidence.observed["url"] == next_state["current_url"]

    asyncio.run(run())


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all browser narration truth tests passed")


if __name__ == "__main__":
    _main()
