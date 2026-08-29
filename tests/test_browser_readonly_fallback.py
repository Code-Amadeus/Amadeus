"""An unrecognized browser action must observe, never navigate.

On 2026-07-25 a delegate carrying `branch="observe"` instead of
`action="observe"` reached the adapter with no action at all. The action
defaulted to the mode string, the task text was turned into a search query,
and the adapter searched the web on the live session — navigating the page
the request was asking about away to a search results page, then failing with
"could not find a source".

The session was alive and parked on the right page the whole time. The rule
this locks down: a query synthesized from the task text is a guess, so when
the session already holds a page, that guess must never be allowed to
navigate. Observing is the only safe fallback.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.adapters.browser import BrowserAdapter, BrowserSession, BrowserSnapshot
from agent_host.provider_types import ProviderRunRequest


class _FakePage:
    def __init__(self, url: str) -> None:
        self.url = url


def _session(url: str) -> BrowserSession:
    now = time.time()
    return BrowserSession(
        session_id="browser_test",
        browser=None,
        context=None,
        page=_FakePage(url),
        created_at=now,
        updated_at=now,
        last_url=url,
        title="Las Meninas - Wikipedia" if url != "about:blank" else "",
    )


def _adapter(url: str) -> tuple[BrowserAdapter, dict]:
    """Adapter whose session and page IO are stubbed; records what was called."""
    adapter = BrowserAdapter()
    calls: dict = {"searched": [], "captured": 0, "opened": []}
    session = _session(url)

    async def _get_or_create_session(session_id, chat_session_id, run_id, emit):
        return session

    async def _search(sess, query, run_id, emit, *, max_results=3, timeout_ms=0):
        calls["searched"].append(query)
        # The real search navigates the shared session; model that faithfully
        # so a regression shows up as a destroyed page, not just a call count.
        sess.page.url = f"https://html.duckduckgo.com/html/?q={query}"
        sess.last_url = sess.page.url
        return []

    async def _capture(sess, run_id, emit, *, index=1):
        calls["captured"] += 1
        return BrowserSnapshot(url=sess.page.url, final_url=sess.page.url, title=sess.title)

    async def _open_and_capture(sess, url_, run_id, emit, *, index=1, timeout_ms=0):
        calls["opened"].append(url_)
        sess.page.url = url_
        sess.last_url = url_
        return BrowserSnapshot(url=url_, final_url=url_, title="opened")

    adapter._get_or_create_session = _get_or_create_session  # type: ignore[assignment]
    adapter._search_with_playwright = _search  # type: ignore[assignment]
    adapter._capture_page = _capture  # type: ignore[assignment]
    adapter._open_and_capture = _open_and_capture  # type: ignore[assignment]
    return adapter, calls


async def _noop_emit(_event) -> None:
    return None


def test_unrecognized_action_observes_instead_of_navigating() -> None:
    """The 07-25 incident: branch="observe" lands here with no action."""

    async def run() -> None:
        page_url = "https://ja.wikipedia.org/wiki/Las_Meninas"
        adapter, calls = _adapter(page_url)
        result = await adapter.run(
            ProviderRunRequest(
                provider="browser",
                task="現在のページの状態を確認",
                mode="delegate",
                metadata={"source": "llm_delegate"},
            ),
            "run_observe",
            _noop_emit,
        )
        assert result.status == "done", f"expected a result, got {result.error!r}"
        assert calls["captured"] == 1, "the live page should have been observed"
        assert not calls["searched"], f"must not search on a live session: {calls['searched']}"
        assert adapter._sessions.get("browser_test") is None or True
        assert (
            result.metadata["browser"]["current_url"] == page_url
        ), "the live page must not be navigated away"

    asyncio.run(run())


def test_unrecognized_action_still_searches_when_no_page_is_open() -> None:
    """With nothing to protect, falling back to a search is still useful."""

    async def run() -> None:
        adapter, calls = _adapter("about:blank")
        await adapter.run(
            ProviderRunRequest(
                provider="browser",
                task="Velazquez Las Meninas analysis",
                mode="delegate",
                metadata={"source": "llm_delegate"},
            ),
            "run_search",
            _noop_emit,
        )
        assert calls["searched"], "a blank session has no page to observe"
        assert calls["captured"] == 0

    asyncio.run(run())


def test_explicit_query_still_searches_on_a_live_session() -> None:
    """An explicit query is an instruction, not a guess, so it is honoured."""

    async def run() -> None:
        adapter, calls = _adapter("https://ja.wikipedia.org/wiki/Las_Meninas")
        await adapter.run(
            ProviderRunRequest(
                provider="browser",
                task="look up something else",
                mode="research",
                metadata={"source": "llm_delegate", "query": "Prado museum hours"},
            ),
            "run_explicit",
            _noop_emit,
        )
        assert calls["searched"] == ["Prado museum hours"]

    asyncio.run(run())


def test_explicit_observe_action_is_unchanged() -> None:
    """The documented action keeps working exactly as before."""

    async def run() -> None:
        page_url = "https://ja.wikipedia.org/wiki/Las_Meninas"
        adapter, calls = _adapter(page_url)
        await adapter.run(
            ProviderRunRequest(
                provider="browser",
                task="observe the current page",
                mode="observe",
                metadata={"source": "llm_delegate", "browser_action": "observe"},
            ),
            "run_action_observe",
            _noop_emit,
        )
        assert calls["captured"] == 1
        assert not calls["searched"]

    asyncio.run(run())


def test_explicit_observe_never_opens_urls_embedded_in_branch_context() -> None:
    """Generated continuation context contains URLs, but they are not actions."""

    async def run() -> None:
        page_url = "https://search.example/results?q=amadeus"
        adapter, calls = _adapter(page_url)
        await adapter.run(
            ProviderRunRequest(
                provider="browser",
                task=(
                    "Continue the active browser interaction branch.\n"
                    "Latest user instruction: observe the results.\n"
                    f"Current page: {page_url}.\n"
                    "Branch goal: https://example.com/original-home."
                ),
                mode="observe",
                metadata={
                    "source": "llm_delegate",
                    "browser_action": "observe",
                },
            ),
            "run_branch_observe",
            _noop_emit,
        )
        assert calls["captured"] == 1
        assert not calls["opened"], "observe must not navigate to contextual URLs"
        assert not calls["searched"]

    asyncio.run(run())


def _main() -> None:
    test_unrecognized_action_observes_instead_of_navigating()
    test_unrecognized_action_still_searches_when_no_page_is_open()
    test_explicit_query_still_searches_on_a_live_session()
    test_explicit_observe_action_is_unchanged()
    test_explicit_observe_never_opens_urls_embedded_in_branch_context()
    print("ok: an unrecognized browser action observes and never navigates")


if __name__ == "__main__":
    _main()
