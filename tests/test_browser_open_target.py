"""An explicit browser open command must navigate to a deterministic target."""

from __future__ import annotations

import asyncio
import base64
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.adapters.browser import BrowserAdapter, BrowserSession, BrowserSnapshot
from agent_host.browser_request_contract import (
    browser_research_query,
    normalize_delegate_browser_request,
)
from agent_host.provider_types import ProviderRunRequest


class _FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"


def _adapter() -> tuple[BrowserAdapter, dict[str, object]]:
    adapter = BrowserAdapter()
    now = time.time()
    session = BrowserSession(
        session_id="browser_test",
        browser=None,
        context=None,
        page=_FakePage(),
        created_at=now,
        updated_at=now,
    )
    calls: dict[str, object] = {
        "sessions": 0,
        "opened": [],
        "searched": [],
    }

    async def _get_or_create_session(session_id, chat_session_id, run_id, emit):
        calls["sessions"] = int(calls["sessions"]) + 1
        return session

    async def _open_and_capture(sess, url, run_id, emit, *, index=1, timeout_ms=0):
        opened = calls["opened"]
        assert isinstance(opened, list)
        opened.append(url)
        sess.page.url = url
        sess.last_url = url
        sess.title = "哔哩哔哩_bilibili"
        return BrowserSnapshot(url=url, final_url=url, title=sess.title)

    async def _search(sess, query, run_id, emit, *, max_results=3, timeout_ms=0):
        searched = calls["searched"]
        assert isinstance(searched, list)
        searched.append(query)
        return []

    adapter._get_or_create_session = _get_or_create_session  # type: ignore[assignment]
    adapter._open_and_capture = _open_and_capture  # type: ignore[assignment]
    adapter._search_with_playwright = _search  # type: ignore[assignment]
    return adapter, calls


async def _noop_emit(_event) -> None:
    return None


def test_open_extracts_bare_domain_without_searching() -> None:
    async def run() -> None:
        adapter, calls = _adapter()
        result = await adapter.run(
            ProviderRunRequest(
                provider="browser",
                task="打开哔哩哔哩网站（bilibili.com）",
                mode="open",
                metadata={
                    "source": "llm_delegate",
                    "browser_action": "open",
                    "session_id": "session_1",
                },
            ),
            "run_open_bilibili",
            _noop_emit,
        )

        assert result.status == "done"
        assert calls["opened"] == ["https://bilibili.com"]
        assert calls["searched"] == []
        assert result.metadata["browser"]["expected_state"] == {
            "url": "https://bilibili.com"
        }

    asyncio.run(run())


def test_open_normalizes_explicit_bare_url_attribute() -> None:
    async def run() -> None:
        adapter, calls = _adapter()
        result = await adapter.run(
            ProviderRunRequest(
                provider="browser",
                task="open the requested site",
                mode="open",
                metadata={
                    "browser_action": "open",
                    "url": "bilibili.com",
                },
            ),
            "run_explicit_bare_url",
            _noop_emit,
        )

        assert result.status == "done"
        assert calls["opened"] == ["https://bilibili.com"]
        assert calls["searched"] == []

    asyncio.run(run())


def test_open_without_address_fails_before_browser_launch_or_search() -> None:
    async def run() -> None:
        adapter, calls = _adapter()
        result = await adapter.run(
            ProviderRunRequest(
                provider="browser",
                task="打开那个网站",
                mode="open",
                metadata={"browser_action": "open"},
            ),
            "run_missing_target",
            _noop_emit,
        )

        assert result.status == "error"
        assert result.error == "missing_open_target"
        assert calls["sessions"] == 0
        assert calls["opened"] == []
        assert calls["searched"] == []

    asyncio.run(run())


def test_explicit_url_in_task_is_promoted_to_a_structured_open_target() -> None:
    normalized = normalize_delegate_browser_request(
        "Open https://en.wikipedia.org/wiki/Kurisu_Makise and report the page.",
        "open",
    )
    assert normalized.action == "open"
    assert normalized.parameters == {
        "url": "https://en.wikipedia.org/wiki/Kurisu_Makise"
    }
    assert normalized.audit["target_source"] == "task"


def test_addressless_open_with_find_intent_lowers_to_research() -> None:
    normalized = normalize_delegate_browser_request(
        "Open Wikipedia and search for the Kurisu Makise page.",
        "open",
    )
    assert normalized.action == ""
    assert normalized.parameters == {}
    assert normalized.audit == {
        "status": "lowered",
        "from_action": "open",
        "to_mode": "research",
        "reason": "addressless_open_with_search_intent",
    }


def test_explicit_visible_browser_search_opens_a_search_results_page() -> None:
    normalized = normalize_delegate_browser_request(
        "Search the web for Beijing weather.",
        "open",
        {"_host_source_user_text": "打开浏览器搜索北京天气"},
    )
    assert normalized.action == "open"
    assert normalized.parameters == {
        "url": "https://www.bing.com/search?q=%E5%8C%97%E4%BA%AC%E5%A4%A9%E6%B0%94"
    }
    assert normalized.audit == {
        "status": "canonical",
        "action": "open",
        "target_source": "visible_browser_query",
        "reason": "explicit_visible_browser_search",
    }


def test_vague_addressless_open_remains_a_strict_contract_error() -> None:
    normalized = normalize_delegate_browser_request("打开那个网站", "open")
    assert normalized.action == "open"
    assert normalized.parameters == {}
    assert normalized.audit == {}


def test_bing_search_redirect_is_normalized_to_the_destination() -> None:
    adapter = BrowserAdapter()
    target = "https://en.wikipedia.org/wiki/Kurisu_Makise"
    encoded = base64.urlsafe_b64encode(target.encode("utf-8")).decode("ascii").rstrip("=")
    redirect = f"https://www.bing.com/ck/a?u=a1{encoded}&ntb=1"
    assert adapter._normalize_search_href(redirect) == target


def test_compound_open_and_find_uses_a_search_query_not_command_prose() -> None:
    query = browser_research_query(
        "Open Wikipedia and search for 'Kurisu Makise' (牧瀬紅莉栖) page."
    )
    assert query == "Kurisu Makise (牧瀬紅莉栖) Wikipedia"


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all browser open target tests passed")


if __name__ == "__main__":
    _main()
