"""Current-page search is a semantic Browser branch, not a direct no-op."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.adapters.browser import BrowserAdapter
from agent_host.adapters.browser_branch import (
    BrowserBranchAdapter,
    _extract_search_query,
    _observed_submit_expected_state,
)
from agent_host.browser_interaction_policy import BrowserInteractionPolicy
from agent_host.provider_catalog import BROWSER_MANIFEST
from agent_host.provider_outcome import ProviderOutcomeEvidence
from agent_host.provider_types import ProviderRunRequest, ProviderRunResult
from llm.delegate_tool import DELEGATE_TOOL
from server.provider_branch import ProviderBranchStore
from server.outcome_verification import assess_provider_outcome
from server.work_observer import ObserverSession, WorkObserverCoordinator


async def _noop_emit(_event) -> None:
    return None


def _search_request(**metadata: Any) -> ProviderRunRequest:
    return ProviderRunRequest(
        provider="browser",
        task="在哔哩哔哩搜索栏中搜索关键词Amadeus，并查看搜索结果",
        mode="search",
        metadata={
            "source": "llm_delegate",
            "session_id": "chat_search_contract",
            "source_user_text": "帮我在里面搜索下Amadeus。",
            "browser_action": "search",
            **metadata,
        },
    )


def test_search_is_a_declared_high_level_action() -> None:
    action_schema = DELEGATE_TOOL["function"]["parameters"]["properties"]["action"]
    assert "search" in action_schema["enum"]
    assert "query" in DELEGATE_TOOL["function"]["parameters"]["properties"]
    operation = BROWSER_MANIFEST.capabilities.operation("search")
    assert operation is not None
    assert operation.atomic is False
    assert operation.execution == "observe_then_plan"
    assert operation.outcome_facet == "browser.page_state"


def test_search_enters_an_observe_first_dom_branch() -> None:
    decision = BrowserInteractionPolicy().decide(_search_request())
    assert decision.use_branch is True
    assert decision.entry_kind == "dom_branch_observe_first"
    assert decision.reason == "dom_action_requires_hidden_page_state"
    assert decision.initial_request.metadata["browser_action"] == "observe"
    assert decision.initial_request.metadata["branch_original_action"] == "search"


def test_original_user_text_yields_the_exact_search_query() -> None:
    context = {
        "user_message": "在哔哩哔哩搜索栏中搜索关键词Amadeus，并查看搜索结果",
        "latest_user_instruction": "帮我在里面搜索下Amadeus。",
        "request_metadata": {},
    }
    assert _extract_search_query(context) == "Amadeus"
    context["latest_user_instruction"] = context["user_message"]
    assert _extract_search_query(context) == "Amadeus"


def test_submitted_search_is_certified_only_by_a_matching_page_transition() -> None:
    action = {"action": "fill_ref", "value": "Amadeus", "submit": True}
    expected = _observed_submit_expected_state(
        action,
        previous_state={"url": "https://www.bilibili.com/", "title": "bilibili"},
        current_state={
            "url": "https://search.bilibili.com/all?keyword=Amadeus",
            "title": "Amadeus-哔哩哔哩_bilibili",
        },
    )
    assert expected == {"url": "https://search.bilibili.com/all?keyword=Amadeus"}
    assert (
        _observed_submit_expected_state(
            action,
            previous_state={"url": "https://www.bilibili.com/", "title": "bilibili"},
            current_state={"url": "https://www.bilibili.com/", "title": "bilibili"},
        )
        == {}
    )


def test_explicit_unknown_atomic_action_is_rejected_not_observed() -> None:
    async def run() -> None:
        adapter = BrowserAdapter()
        launched = False

        async def should_not_launch(*_args, **_kwargs):
            nonlocal launched
            launched = True
            raise AssertionError("unsupported actions must fail before Playwright starts")

        adapter._get_or_create_session = should_not_launch  # type: ignore[assignment]
        result = await adapter.run(
            ProviderRunRequest(
                provider="browser",
                task="do an invented browser operation",
                mode="dance",
                metadata={"source": "llm_delegate", "browser_action": "dance"},
            ),
            "run_unknown_browser_action",
            _noop_emit,
        )
        assert result.status == "error"
        assert result.error == "unsupported_browser_action:dance"
        assert launched is False

    asyncio.run(run())


class _SearchEngine:
    provider_id = "browser"
    engine_id = "search-contract-test"

    def __init__(self) -> None:
        self.session_id = "browser_search_contract"
        self.url = "https://www.bilibili.com/"
        self.title = "哔哩哔哩_bilibili"
        self.actions: list[str] = []

    async def run(self, request, _run_id, _emit) -> ProviderRunResult:
        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        action = str(metadata.get("browser_action") or request.mode or "")
        self.actions.append(action)
        if action == "fill_ref":
            assert metadata["ref"] == "br_search"
            assert metadata["value"] == "Amadeus"
            assert metadata["submit"] is True
            self.url = "https://search.bilibili.com/all?keyword=Amadeus"
            self.title = "Amadeus-哔哩哔哩_bilibili"
        return ProviderRunResult(
            status="done",
            result="atomic browser result",
            metadata={
                "browser": {
                    "browser_session_id": self.session_id,
                    "current_url": self.url,
                    "title": self.title,
                }
            },
        )

    async def inspect_session(self, _session_id: str, *, include_dom: bool = True) -> dict[str, Any]:
        return {
            "browser_session_id": self.session_id,
            "url": self.url,
            "title": self.title,
            "text": "Amadeus search results" if "search.bilibili" in self.url else "bilibili home",
            "dom": "<html>browser test</html>" if include_dom else "",
            "interaction_refs": (
                []
                if "search.bilibili" in self.url
                else [
                    {
                        "ref": "br_search",
                        "kind": "input",
                        "role": "textbox",
                        "label": "搜索",
                        "selector": "input.search-input",
                        "fillable": True,
                    }
                ]
            ),
        }

    async def cancel(self, _run_id: str) -> None:
        return None

    async def shutdown(self) -> None:
        return None


def test_search_lowers_to_fill_ref_and_exports_verified_terminal_state() -> None:
    async def run() -> None:
        async def planner(context: dict[str, Any]) -> dict[str, Any]:
            assert context["latest_user_instruction"] == "帮我在里面搜索下Amadeus。"
            return {
                "actions": [
                    {
                        "action": "fill_ref",
                        "ref": "br_search",
                        "value": "Amadeus",
                        "submit": True,
                        "task": "Search current page for Amadeus",
                    }
                ],
                "final_report": "Amadeus の検索結果を開いたわ。",
                "compact_digest": "Submitted the site search.",
            }

        engine = _SearchEngine()
        with tempfile.TemporaryDirectory(prefix="browser_search_contract_") as root:
            adapter = BrowserBranchAdapter(
                base_adapter=engine,
                store=ProviderBranchStore(Path(root)),
                branch_planner=planner,
            )
            result = await adapter.run(
                _search_request(),
                "run_search_contract",
                _noop_emit,
            )

        assert result.status == "done"
        assert engine.actions == ["observe", "fill_ref"]
        next_state = result.metadata["provider_branch"]["next_state"]
        assert next_state["current_url"] == "https://search.bilibili.com/all?keyword=Amadeus"
        assert next_state["expected_state"] == {
            "url": "https://search.bilibili.com/all?keyword=Amadeus"
        }
        evidence = result.outcome_evidence
        assert evidence is not None
        assert evidence.operation == "search"
        assert evidence.observation_authority == "host"
        assert evidence.expected == next_state["expected_state"]
        assert evidence.observed["url"] == next_state["current_url"]

    asyncio.run(run())


def test_unverified_browser_terminal_cannot_be_reworded_as_success() -> None:
    truth = assess_provider_outcome(
        execution_status="succeeded",
        provider_report="The Amadeus search results are ready.",
        metadata={
            "outcome_evidence": ProviderOutcomeEvidence(
                facet="browser.page_state",
                operation="search",
                observed={
                    "url": "https://www.bilibili.com/",
                    "title": "哔哩哔哩_bilibili",
                },
            ).to_dict()
        },
    )
    assert truth is not None
    assert truth.completeness == "partial"
    assert truth.provider_report_allowed is False
    assert "search results are ready" not in truth.summary.lower()

    note = {
        "provider": "browser",
        "run_id": "run_unverified_search",
        "phase": "Result",
        "summary": truth.summary,
        "metadata": {
            "execution_status": "succeeded",
            "outcome_verdict": truth.to_dict(),
        },
    }
    session = ObserverSession(
        narration_id="run_unverified_search",
        run_id="run_unverified_search",
        session_id="session_1",
        provider="browser",
    )
    session.add_note(note)
    decision = WorkObserverCoordinator()._merge_decision_defaults(
        {
            "action": "final_report",
            "terminal": True,
            "append_to_main_chat": True,
            "speak": True,
            "display_language": "english",
            "display_text": "The Amadeus search results are ready.",
            "main_chat_entry": "The Amadeus search results are ready.",
        },
        session,
        note,
    )
    assert "search results are ready" not in decision["display_text"].lower()
    assert "still needs review" in decision["display_text"].lower()


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all browser search contract tests passed")


if __name__ == "__main__":
    _main()
