"""Browser navigation actions preserve session and page-observation identity."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.adapters.browser import BrowserAdapter, BrowserSession, BrowserSnapshot
from agent_host.adapters.browser_branch import BrowserBranchAdapter
from agent_host.browser_interaction_policy import BrowserInteractionPolicy
from agent_host.provider_types import ProviderEvent, ProviderRunRequest, ProviderRunResult
from config import settings
from server.browser_branch_planner import _normalize_decision
from server.provider_branch import ProviderBranchStore


class _FakePage:
    def __init__(self, url: str, *, back_url: str = "") -> None:
        self.url = url
        self.back_url = back_url
        self.closed = False

    async def go_back(self, **_kwargs: Any) -> None:
        if self.back_url:
            self.url = self.back_url
            self.back_url = ""

    async def close(self) -> None:
        self.closed = True

    def is_closed(self) -> bool:
        return self.closed


class _FakeCapturedPage(_FakePage):
    async def title(self) -> str:
        return "Captured page"

    async def content(self) -> str:
        return "<html><body>Captured page text</body></html>"

    def locator(self, _selector: str):
        return self

    async def inner_text(self, **_kwargs: Any) -> str:
        return "Captured page text"

    async def screenshot(self, **_kwargs: Any) -> bytes:
        return b"png"

    async def evaluate(self, _script: str, _limit: int) -> list[dict[str, Any]]:
        return []


class _VisibleLaunchContext:
    async def new_page(self) -> _FakePage:
        return _FakePage("about:blank")


class _VisibleLaunchBrowser:
    def __init__(self, *, headless: bool) -> None:
        self.headless = headless

    async def new_context(self, **_kwargs: Any) -> _VisibleLaunchContext:
        return _VisibleLaunchContext()


class _VisibleLaunchChromium:
    def __init__(self) -> None:
        self.browser: _VisibleLaunchBrowser | None = None
        self.launch_options: dict[str, Any] = {}

    async def launch(self, **options: Any) -> _VisibleLaunchBrowser:
        self.launch_options = dict(options)
        headless = bool(options.get("headless", True))
        self.browser = _VisibleLaunchBrowser(headless=headless)
        return self.browser


class _VisibleLaunchPlaywright:
    def __init__(self) -> None:
        self.chromium = _VisibleLaunchChromium()


def test_desktop_visible_browser_setting_launches_a_windowed_session() -> None:
    async def run() -> None:
        playwright = _VisibleLaunchPlaywright()
        with (
            patch.object(settings, "AMADEUS_BROWSER_VISIBLE", True, create=True),
            patch.object(settings, "AMADEUS_BROWSER_CHANNEL", "", create=True),
        ):
            adapter = BrowserAdapter()
            adapter._playwright = playwright
            session = await adapter._get_or_create_session(
                "",
                "chat-visible-browser",
                "run-visible-browser",
                lambda _event: asyncio.sleep(0),
            )

        assert session.browser is playwright.chromium.browser
        assert session.browser.headless is False
        assert playwright.chromium.launch_options == {"headless": False}

    asyncio.run(run())


def test_configured_browser_channel_is_used_for_the_visible_session() -> None:
    async def run() -> None:
        playwright = _VisibleLaunchPlaywright()
        with (
            patch.object(settings, "AMADEUS_BROWSER_VISIBLE", True, create=True),
            patch.object(settings, "AMADEUS_BROWSER_CHANNEL", "msedge", create=True),
        ):
            adapter = BrowserAdapter()
            adapter._playwright = playwright
            await adapter._get_or_create_session(
                "",
                "chat-edge-browser",
                "run-edge-browser",
                lambda _event: asyncio.sleep(0),
            )

        assert playwright.chromium.launch_options == {
            "headless": False,
            "channel": "msedge",
        }

    asyncio.run(run())


def _session(page: _FakePage, *, stack: list[_FakePage] | None = None) -> BrowserSession:
    now = time.time()
    return BrowserSession(
        session_id="browser_navigation_test",
        browser=None,
        context=None,
        page=page,
        created_at=now,
        updated_at=now,
        last_url=page.url,
        page_stack=list(stack or []),
    )


async def _run_back(session: BrowserSession) -> tuple[ProviderRunResult, list[dict[str, Any]]]:
    adapter = BrowserAdapter()
    events: list[dict[str, Any]] = []

    async def get_session(_sid, _chat_sid, _run_id, _emit):
        return session

    async def ready(_page, *, timeout_ms):
        return None

    async def capture(sess, _run_id, _emit, *, index=1):
        sess.title = "Previous Page"
        return BrowserSnapshot(
            url=sess.page.url,
            final_url=sess.page.url,
            title=sess.title,
        )

    async def emit(event: ProviderEvent) -> None:
        events.append(event.to_dict())

    adapter._get_or_create_session = get_session  # type: ignore[assignment]
    adapter._wait_for_page_ready = ready  # type: ignore[assignment]
    adapter._capture_page = capture  # type: ignore[assignment]
    result = await adapter.run(
        ProviderRunRequest(
            provider="browser",
            task="Return to the previous page.",
            mode="back",
            metadata={
                "browser_action": "back",
                "browser_session_id": session.session_id,
            },
        ),
        "browser_back_test",
        emit,
    )
    return result, events


def test_snapshot_is_canvas_evidence_not_premature_outcome_progress() -> None:
    async def run() -> None:
        adapter = BrowserAdapter()
        events: list[ProviderEvent] = []

        async def emit(event: ProviderEvent) -> None:
            events.append(event)

        snapshot = await adapter._capture_page(
            _session(_FakeCapturedPage("https://example.test/missing")),
            "browser_snapshot_test",
            emit,
            index=1,
            response_status=404,
            navigation_chain=["https://example.test/missing"],
        )

        assert snapshot.status_code == 404
        assert [event.type for event in events] == ["artifact.created"]
        assert events[0].payload["artifact_type"] == "browser.snapshot"

    asyncio.run(run())


def test_back_uses_same_tab_history_first() -> None:
    async def run() -> None:
        session = _session(
            _FakePage(
                "https://example.test/detail",
                back_url="https://example.test/results",
            )
        )
        result, events = await _run_back(session)
        assert result.status == "done", result.to_dict()
        assert result.metadata["browser"]["current_url"].endswith("/results")
        tool_result = next(
            item for item in events
            if item["type"] == "tool.result" and item["payload"].get("tool") == "browser.back"
        )
        assert tool_result["payload"]["strategy"] == "history"

    asyncio.run(run())


def test_back_returns_to_opener_when_popup_has_no_history() -> None:
    async def run() -> None:
        opener = _FakePage("https://example.test/results")
        popup = _FakePage("https://example.test/popup")
        session = _session(popup, stack=[opener])
        result, events = await _run_back(session)
        assert result.status == "done", result.to_dict()
        assert session.page is opener
        assert popup.closed is True
        tool_result = next(
            item for item in events
            if item["type"] == "tool.result" and item["payload"].get("tool") == "browser.back"
        )
        assert tool_result["payload"]["strategy"] == "opener"

    asyncio.run(run())


def test_planner_and_policy_accept_structured_back() -> None:
    decision = _normalize_decision(
        {
            "actions": [{"action": "back"}],
            "final_report": "前のページに戻ったわ。",
        },
        {"interaction_refs": []},
    )
    assert decision["actions"] == [
        {"action": "back", "task": "Return to previous browser page"}
    ]
    confirmed = _normalize_decision(
        {
            "actions": [],
            "goal_satisfied": True,
            "final_report": "The observed page is already the requested destination.",
        },
        {"interaction_refs": []},
    )
    assert confirmed["goal_satisfied"] is True

    request = ProviderRunRequest(
        provider="browser",
        task="Return to the previous page.",
        mode="back",
        metadata={
            "source": "llm_delegate",
            "browser_action": "back",
            "browser_session_id": "browser_1",
        },
    )
    policy = BrowserInteractionPolicy().decide(request)
    assert policy.use_branch is True
    assert policy.entry_kind == "dom_branch_observe_first"


class _PageEpochEngine:
    provider_id = "browser"
    engine_id = "page-epoch-test"

    def __init__(self) -> None:
        self.page = "results"
        self.session_id = "browser_epoch_test"
        self.executed: list[str] = []

    async def run(self, request, _run_id, _emit) -> ProviderRunResult:
        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        action = str(metadata.get("browser_action") or request.mode or "observe")
        if action == "click_ref":
            ref = str(metadata.get("ref") or "")
            self.executed.append(ref)
            if self.page == "results" and ref == "br_1":
                self.page = "detail"
            elif self.page == "detail" and ref == "br_2":
                self.page = "destructive"
        state = await self.inspect_session(self.session_id, include_dom=False)
        return ProviderRunResult(
            status="done",
            result="ok",
            metadata={
                "browser": {
                    "browser_session_id": self.session_id,
                    "current_url": state["url"],
                    "title": state["title"],
                }
            },
        )

    async def inspect_session(self, _session_id: str, *, include_dom: bool = True) -> dict[str, Any]:
        states = {
            "results": {
                "url": "https://example.test/results",
                "title": "Results",
                "refs": [
                    {"ref": "br_1", "kind": "link", "role": "link", "label": "First result", "href": "/detail"},
                    {"ref": "br_2", "kind": "link", "role": "link", "label": "Second result", "href": "/second"},
                ],
            },
            "detail": {
                "url": "https://example.test/detail",
                "title": "Detail",
                "refs": [
                    {"ref": "br_1", "kind": "link", "role": "link", "label": "Home", "href": "/"},
                    {"ref": "br_2", "kind": "button", "role": "button", "label": "Delete item"},
                ],
            },
            "destructive": {
                "url": "https://example.test/deleted",
                "title": "Deleted",
                "refs": [],
            },
        }
        state = states[self.page]
        return {
            "browser_session_id": self.session_id,
            "url": state["url"],
            "title": state["title"],
            "text": state["title"],
            "dom": f"<html><title>{state['title']}</title></html>" if include_dom else "",
            "interaction_refs": list(state["refs"]),
        }

    async def cancel(self, _run_id: str) -> None:
        return None

    async def shutdown(self) -> None:
        return None


def test_navigation_replans_instead_of_replaying_actions_from_the_previous_page() -> None:
    async def run() -> None:
        engine = _PageEpochEngine()
        planner_calls = 0

        async def planner(_context):
            nonlocal planner_calls
            planner_calls += 1
            if planner_calls > 1:
                return {
                    "actions": [],
                    "goal_satisfied": False,
                    "final_report": "The second target is not available on this page.",
                    "compact_digest": "replanned after navigation",
                }
            return {
                "actions": [
                    {"action": "click_ref", "ref": "br_1", "task": "Open first result"},
                    {"action": "click_ref", "ref": "br_2", "task": "Open second result"},
                ],
                "final_report": "Opened both results.",
                "compact_digest": "two actions planned from one page",
            }

        async def emit(_event):
            return None

        with tempfile.TemporaryDirectory(prefix="browser_epoch_") as root:
            adapter = BrowserBranchAdapter(
                base_adapter=engine,
                store=ProviderBranchStore(Path(root)),
                branch_planner=planner,
            )
            result = await adapter.run(
                ProviderRunRequest(
                    provider="browser",
                    task="Open the first and second result.",
                    mode="observe",
                    metadata={
                        "source": "llm_delegate",
                        "provider_branch": True,
                        "browser_action": "observe",
                        "browser_session_id": engine.session_id,
                        "max_branch_actions": 3,
                    },
                ),
                "browser_epoch_run",
                emit,
            )

        branch = result.metadata["provider_branch"]
        assert planner_calls == 2
        assert engine.executed == ["br_1"]
        assert engine.page == "detail"
        assert len(branch["actions"]) == 1
        assert branch["risks"][0]["deferred_action_count"] == 1
        assert branch["next_state"]["expected_state"] == {}

    asyncio.run(run())


def test_opening_search_results_replans_before_terminal_completion() -> None:
    async def run() -> None:
        engine = _PageEpochEngine()
        planner_pages: list[str] = []

        async def planner(context):
            title = str(context.get("page", {}).get("title") or "")
            planner_pages.append(title)
            if len(planner_pages) == 1:
                return {
                    "actions": [
                        {
                            "action": "open",
                            "url": "https://example.test/results",
                            "task": "Open search results",
                        }
                    ],
                    "final_report": "Opened the result page.",
                }
            return {
                "actions": [
                    {"action": "click_ref", "ref": "br_1", "task": "Open first result"}
                ],
                "final_report": "Opened the requested detail page.",
                "compact_digest": "search result followed to detail",
            }

        async def emit(_event):
            return None

        with tempfile.TemporaryDirectory(prefix="browser_search_replan_") as root:
            adapter = BrowserBranchAdapter(
                base_adapter=engine,
                store=ProviderBranchStore(Path(root)),
                branch_planner=planner,
            )
            result = await adapter.run(
                ProviderRunRequest(
                    provider="browser",
                    task="Open the Paxos page.",
                    mode="observe",
                    metadata={
                        "source": "llm_delegate",
                        "source_user_text": "打开第一个搜索结果",
                        "provider_branch": True,
                        "browser_action": "observe",
                        "browser_session_id": engine.session_id,
                        "max_branch_actions": 3,
                    },
                ),
                "browser_search_replan_run",
                emit,
            )

        branch = result.metadata["provider_branch"]
        assert planner_pages == ["Results", "Results"]
        assert engine.executed == ["br_1"]
        assert engine.page == "detail"
        assert [item["action"] for item in branch["actions"]] == [
            "open",
            "click_ref",
        ]
        assert branch["visible_messages"][0]["content"] == "打开第一个搜索结果"
        assert branch["next_state"]["expected_state"] == {
            "url": "https://example.test/detail"
        }

    asyncio.run(run())


def test_back_action_exports_host_observed_expected_state() -> None:
    async def run() -> None:
        engine = _PageEpochEngine()
        engine.page = "detail"

        async def planner(_context):
            return {
                "actions": [{"action": "back", "task": "Return to results"}],
                "final_report": "前の検索結果に戻ったわ。",
                "compact_digest": "returned to results",
            }

        original_run = engine.run

        async def run_with_back(request, run_id, emit):
            metadata = request.metadata if isinstance(request.metadata, dict) else {}
            if str(metadata.get("browser_action") or request.mode) == "back":
                engine.page = "results"
            return await original_run(request, run_id, emit)

        engine.run = run_with_back  # type: ignore[method-assign]

        async def emit(_event):
            return None

        with tempfile.TemporaryDirectory(prefix="browser_back_expected_") as root:
            adapter = BrowserBranchAdapter(
                base_adapter=engine,
                store=ProviderBranchStore(Path(root)),
                branch_planner=planner,
            )
            result = await adapter.run(
                ProviderRunRequest(
                    provider="browser",
                    task="Return to results.",
                    mode="observe",
                    metadata={
                        "source": "llm_delegate",
                        "provider_branch": True,
                        "browser_action": "observe",
                        "browser_session_id": engine.session_id,
                    },
                ),
                "browser_back_expected_run",
                emit,
            )

        expected = {"url": "https://example.test/results"}
        assert result.metadata["provider_branch"]["next_state"]["expected_state"] == expected
        assert result.metadata["provider_branch"]["actions"][0]["expected_state"] == expected

    asyncio.run(run())


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all browser navigation-action tests passed")


if __name__ == "__main__":
    _main()
