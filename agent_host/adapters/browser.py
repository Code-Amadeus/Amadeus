from __future__ import annotations

import asyncio
import base64
import os
import re
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from agent_host.browser_request_contract import (
    browser_research_query,
    normalize_web_address,
    web_addresses,
)
from agent_host.provider_catalog import BROWSER_MANIFEST
from agent_host.provider_outcome import ProviderOutcomeEvidence
from agent_host.provider_progress import progress_payload
from agent_host.provider_types import (
    EmitProviderEvent,
    ProviderEvent,
    ProviderRunRequest,
    ProviderRunResult,
)


@dataclass(slots=True)
class BrowserSnapshot:
    url: str
    final_url: str
    title: str = ""
    text: str = ""
    excerpt: str = ""
    links: list[dict[str, str]] = field(default_factory=list)
    interaction_refs: list[dict[str, Any]] = field(default_factory=list)
    screenshot: str = ""
    status_code: int | None = None
    navigation_chain: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BrowserSession:
    session_id: str
    browser: Any
    context: Any
    page: Any
    created_at: float
    updated_at: float
    last_url: str = ""
    title: str = ""
    interaction_refs: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Pages displaced by target=_blank / window.open navigations. Browser
    # "back" first uses the active page's history, then returns to its opener
    # when the active tab has no previous entry.
    page_stack: list[Any] = field(default_factory=list)


class BrowserAdapter:
    """Playwright-backed browser execution provider.

    The browser is a stateful provider, not a standalone agent. Amadeus owns
    intent, permission, narration, and canvas routing. This adapter owns the
    browser session and emits observable facts: search results, opened pages,
    screenshots, page excerpts, and source links.
    """

    provider_id = "browser"
    engine_id = "playwright"
    manifest = BROWSER_MANIFEST

    def __init__(self) -> None:
        # Both hold Playwright objects once started. Declared bare, their type
        # was fixed as None, which made every later assignment and use an error.
        self._playwright_manager: Any = None
        self._playwright: Any = None
        self._sessions: dict[str, BrowserSession] = {}
        self._run_to_session: dict[str, str] = {}
        self._chat_to_session: dict[str, str] = {}
        self._session_ttl_s = max(60.0, float(os.environ.get("AMADEUS_BROWSER_SESSION_TTL_SECONDS", "900")))

    async def run(
        self,
        request: ProviderRunRequest,
        run_id: str,
        emit: EmitProviderEvent,
    ) -> ProviderRunResult:
        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        mode = str(metadata.get("browser_mode") or request.mode or "research").strip().lower()
        action = str(metadata.get("browser_action") or metadata.get("action") or mode).strip().lower()
        operation = BROWSER_MANIFEST.capabilities.operation(action)
        max_pages = self._bounded_int(metadata.get("max_pages"), default=3, low=1, high=5)
        timeout_ms = self._bounded_int(metadata.get("timeout_ms"), default=18000, low=4000, high=60000)
        query = str(metadata.get("query") or "").strip()
        urls = self._urls_from_request(
            request,
            metadata,
            allow_bare_domain=action == "open",
        )
        expected_state = (
            {"url": urls[-1]}
            if action == "open" and urls
            else {}
        )
        session_id = str(
            metadata.get("browser_session_id")
            or metadata.get("browserSessionId")
            or ""
        ).strip()
        chat_session_id = str(metadata.get("session_id") or metadata.get("chat_session_id") or "").strip()
        ephemeral = bool(metadata.get("ephemeral", False))

        if action in {"close", "close_session"}:
            if not session_id and chat_session_id:
                session_id = self._chat_to_session.get(chat_session_id, "")
            if not session_id:
                return ProviderRunResult(status="error", result="No browser session id was provided.", error="missing_browser_session_id")
            await self._close_session(session_id)
            return ProviderRunResult(
                status="done",
                result=f"Closed browser session `{session_id}`.",
                metadata={"browser": {"browser_session_id": session_id, "closed": True}},
            )

        declared_action = str(
            metadata.get("browser_action") or metadata.get("action") or ""
        ).strip().lower()
        atomic_actions = BROWSER_MANIFEST.capabilities.atomic_operation_ids()
        if declared_action and declared_action not in atomic_actions:
            # Semantic actions such as ``search`` must be lowered by the
            # branch policy into structured atomic actions first.  Unknown
            # explicit actions are contract errors, not observations: silently
            # capturing the current page made a no-op look like successful
            # execution in the Work ledger and Slice.
            return ProviderRunResult(
                status="error",
                result=(
                    "Browser provider could not start: unsupported atomic action "
                    f"{declared_action!r}."
                ),
                error=f"unsupported_browser_action:{declared_action}",
                metadata={
                    "browser": {
                        "chat_session_id": chat_session_id,
                        "mode": mode,
                        "action": action,
                        "query": query,
                        "expected_state": {},
                    }
                },
            )

        # ``open`` is a navigation command, not an underspecified research
        # request.  Searching for the task text when its target cannot be
        # resolved changes both the action and the page, and previously turned
        # ``open bilibili.com`` into a DuckDuckGo search.  Reject the malformed
        # command before launching a browser so the host can report the actual
        # missing target without implying that anything was opened.
        if action == "open" and not urls:
            return ProviderRunResult(
                status="error",
                result="Browser provider could not start: open requires an explicit web address.",
                error="missing_open_target",
                metadata={
                    "browser": {
                        "chat_session_id": chat_session_id,
                        "mode": mode,
                        "action": action,
                        "query": query,
                        "expected_state": {},
                    }
                },
            )

        # A query derived from the task text is a guess, not an instruction:
        # nothing in the request actually asked to search the web.
        query_synthesized = False
        if not urls and not query and action not in {
            "observe",
            "snapshot",
            "extract",
            "click_text",
            "click_ref",
            "fill_ref",
            "back",
        }:
            query = self._task_as_query(request.task)
            query_synthesized = bool(query)

        try:
            session = await self._get_or_create_session(session_id, chat_session_id, run_id, emit)
        except Exception as exc:
            return ProviderRunResult(
                status="error",
                result="Playwright is not available in the Amadeus runtime environment.",
                error=f"playwright_unavailable: {exc}",
                metadata={
                    "browser": {
                        "mode": mode,
                        "action": action,
                        "query": query,
                        "expected_state": expected_state,
                    }
                },
            )

        self._run_to_session[run_id] = session.session_id
        snapshots: list[BrowserSnapshot] = []
        try:
            if action == "click_ref":
                ref = str(metadata.get("ref") or metadata.get("action_ref") or metadata.get("target_ref") or "").strip()
                if not ref:
                    raise ValueError("click_ref requires metadata.ref")
                await self._click_ref(session, ref, run_id, emit, timeout_ms=timeout_ms)
                snapshots.append(await self._capture_page(session, run_id, emit, index=1))
            elif action == "fill_ref":
                ref = str(metadata.get("ref") or metadata.get("action_ref") or metadata.get("target_ref") or "").strip()
                value = str(metadata.get("value") or metadata.get("input") or metadata.get("text") or metadata.get("query") or "").strip()
                submit = self._truthy(metadata.get("submit"))
                if not ref:
                    raise ValueError("fill_ref requires metadata.ref")
                if not value:
                    raise ValueError("fill_ref requires metadata.value")
                await self._fill_ref(session, ref, value, run_id, emit, timeout_ms=timeout_ms, submit=submit)
                snapshots.append(await self._capture_page(session, run_id, emit, index=1))
            elif action == "click_text":
                text = str(metadata.get("text") or metadata.get("selector_text") or metadata.get("label") or "").strip()
                if not text:
                    raise ValueError("click_text requires metadata.text")
                await self._click_text(session, text, run_id, emit, timeout_ms=timeout_ms, task=request.task)
                snapshots.append(await self._capture_page(session, run_id, emit, index=1))
            elif action == "back":
                await self._go_back(session, run_id, emit, timeout_ms=timeout_ms)
                snapshots.append(await self._capture_page(session, run_id, emit, index=1))
            elif action in {"observe", "snapshot", "extract"}:
                # Observation is a read of the live session, never a navigation
                # request. Branch continuation tasks intentionally contain the
                # current URL, original goal URLs, and compact history as
                # context. Treating those URLs as destinations destroys the
                # page the planner was asked to inspect and can leave it on an
                # older URL from the branch goal.
                snapshots.append(await self._capture_page(session, run_id, emit, index=1))
            else:
                # An unrecognized or defaulted action must never navigate a
                # session that already holds a page. "Check the current page"
                # arrives here with no action and a synthesized query, and
                # searching for that query would destroy the very page the
                # request is about. Observing is the only safe fallback, and
                # it is usually what such a request meant.
                observing = query_synthesized
                live_page = bool(session.page.url and session.page.url != "about:blank")
                if not urls and query and not (observing and live_page):
                    urls = await self._search_with_playwright(
                        session,
                        query,
                        run_id,
                        emit,
                        max_results=max_pages,
                        timeout_ms=timeout_ms,
                    )
                if not urls and observing and live_page:
                    snapshots.append(await self._capture_page(session, run_id, emit, index=1))
                elif urls:
                    for index, url in enumerate(urls[:max_pages], start=1):
                        snapshots.append(
                            await self._open_and_capture(
                                session,
                                url,
                                run_id,
                                emit,
                                index=index,
                                timeout_ms=timeout_ms,
                            )
                        )
                else:
                    raise ValueError(f"Browser provider could not find a source for: {query or request.task}")
        except Exception as exc:
            return ProviderRunResult(
                status="error",
                result=f"Browser provider failed: {exc}",
                error=str(exc),
                metadata={
                    "browser": {
                        "browser_session_id": session.session_id,
                        "chat_session_id": chat_session_id,
                        "mode": mode,
                        "action": action,
                        "query": query,
                        "current_url": session.last_url,
                        "expected_state": expected_state,
                    }
                },
            )
        finally:
            if ephemeral:
                await self._close_session(session.session_id)

        markdown = self._result_markdown(request.task, query=query, snapshots=snapshots)
        result_metadata = {
            "browser": {
                "browser_session_id": session.session_id,
                "chat_session_id": chat_session_id,
                "mode": mode,
                "action": action,
                "query": query,
                "urls": [item.final_url or item.url for item in snapshots],
                "engine": "playwright",
                "snapshot_count": len(snapshots),
                "current_url": session.last_url,
                "title": session.title,
                "expected_state": expected_state,
                "ephemeral": ephemeral,
            }
        }
        outcome_evidence = None
        if operation is not None and operation.outcome_facet:
            outcome_evidence = ProviderOutcomeEvidence(
                facet=operation.outcome_facet,
                operation=operation.operation_id,
                expected=dict(expected_state),
                observed={
                    "url": session.last_url,
                    "title": session.title,
                    "text": snapshots[-1].excerpt if snapshots else "",
                    "navigation_chain": (
                        list(snapshots[-1].navigation_chain)
                        if snapshots
                        else []
                    ),
                },
            )
        return ProviderRunResult(
            status="done",
            result=markdown,
            metadata=result_metadata,
            outcome_evidence=outcome_evidence,
        )

    async def cancel(self, run_id: str) -> None:
        session_id = self._run_to_session.pop(run_id, "")
        if session_id:
            await self._close_session(session_id)

    @staticmethod
    def _navigation_chain(response: Any) -> list[str]:
        """Return the browser-observed request chain for one navigation."""
        if response is None:
            return []
        try:
            request = response.request
            reverse_chain: list[str] = []
            while request is not None:
                url = str(getattr(request, "url", "") or "").strip()
                if url:
                    reverse_chain.append(url)
                request = getattr(request, "redirected_from", None)
            return list(reversed(reverse_chain))
        except Exception:
            return []

    @staticmethod
    def _append_navigation_url(chain: list[str], value: object) -> None:
        url = str(value or "").strip()
        if url and (not chain or chain[-1] != url):
            chain.append(url)

    async def shutdown(self) -> None:
        """Close all browser sessions and stop the Playwright manager."""
        for session_id in list(self._sessions):
            await self._close_session(session_id)
        await self._stop_playwright()

    async def inspect_session(self, session_id: str, *, include_dom: bool = True) -> dict[str, Any]:
        """Return high-detail session state for a provider branch.

        This is deliberately not emitted to the main chat. Browser branches can
        use it as hidden context for precise page decisions, then merge only a
        compact user-visible result back to the main conversation.
        """
        session = self._sessions.get(str(session_id or ""))
        if session is None:
            raise RuntimeError(f"Unknown browser session {session_id!r}.")
        page = session.page
        url = str(page.url or session.last_url or "about:blank")
        title = self._clean_text(await page.title())
        dom = await page.content() if include_dom else ""
        text = ""
        try:
            text = self._clean_text(await page.locator("body").inner_text(timeout=2500))
        except Exception:
            pass
        return {
            "browser_session_id": session.session_id,
            "url": url,
            "title": title or session.title,
            "dom": dom,
            "text": text,
            "interaction_refs": [dict(item) for item in session.interaction_refs.values()],
            "updated_at": session.updated_at,
        }

    async def _ensure_playwright(self) -> Any:
        if self._playwright is not None:
            return self._playwright
        from playwright.async_api import async_playwright

        self._playwright_manager = async_playwright()
        self._playwright = await self._playwright_manager.start()
        return self._playwright

    async def _get_or_create_session(
        self,
        requested_session_id: str,
        chat_session_id: str,
        run_id: str,
        emit: EmitProviderEvent,
    ) -> BrowserSession:
        await self._prune_sessions()
        if not requested_session_id and chat_session_id:
            requested_session_id = self._chat_to_session.get(chat_session_id, "")
        if requested_session_id and requested_session_id in self._sessions:
            session = self._sessions[requested_session_id]
            session.updated_at = time.time()
            if chat_session_id:
                self._chat_to_session[chat_session_id] = session.session_id
            await emit(
                ProviderEvent(
                    provider=self.provider_id,
                    run_id=run_id,
                    type="tool.result",
                    payload={"tool": "browser.session", "status": "reused", "browser_session_id": session.session_id},
                )
            )
            return session

        playwright = await self._ensure_playwright()
        session_id = requested_session_id or f"browser_{secrets.token_hex(6)}"
        if chat_session_id:
            self._chat_to_session[chat_session_id] = session_id
        await emit(
            ProviderEvent(
                provider=self.provider_id,
                run_id=run_id,
                type="tool.call",
                payload={"tool": "browser.launch", "engine": "chromium", "browser_session_id": session_id},
            )
        )
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="AmadeusBrowserProvider/0.1 local-runtime",
            ignore_https_errors=True,
        )
        page = await context.new_page()
        now = time.time()
        session = BrowserSession(
            session_id=session_id,
            browser=browser,
            context=context,
            page=page,
            created_at=now,
            updated_at=now,
        )
        self._sessions[session_id] = session
        await emit(
            ProviderEvent(
                provider=self.provider_id,
                run_id=run_id,
                type="tool.result",
                payload={"tool": "browser.launch", "engine": "chromium", "status": "ready", "browser_session_id": session_id},
            )
        )
        return session

    async def _search_with_playwright(
        self,
        session: BrowserSession,
        query: str,
        run_id: str,
        emit: EmitProviderEvent,
        *,
        max_results: int,
        timeout_ms: int,
    ) -> list[str]:
        results: list[dict[str, str]] = []
        search_engines = (
            (
                "duckduckgo",
                f"https://duckduckgo.com/html/?q={quote_plus(query)}",
                "a.result__a",
            ),
            (
                "bing",
                f"https://www.bing.com/search?q={quote_plus(query)}",
                "li.b_algo h2 a[href]",
            ),
        )
        for engine, search_url, selector in search_engines:
            await emit(
                ProviderEvent(
                    provider=self.provider_id,
                    run_id=run_id,
                    type="tool.call",
                    payload={
                        "tool": "browser.search",
                        "engine": engine,
                        "query": query,
                        "browser_session_id": session.session_id,
                    },
                )
            )
            response = await session.page.goto(
                search_url,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            session.last_url = str(session.page.url or search_url)
            session.updated_at = time.time()
            html = await session.page.content()
            soup = BeautifulSoup(html or "", "html.parser")
            engine_results: list[dict[str, str]] = []
            for anchor in soup.select(selector):
                href = str(anchor.get("href") or "").strip()
                label = self._clean_text(anchor.get_text(" ", strip=True))
                normalized = self._normalize_search_href(href)
                if not normalized or not label:
                    continue
                if any(item["url"] == normalized for item in engine_results):
                    continue
                engine_results.append({"title": label[:120], "url": normalized})
                if len(engine_results) >= max_results:
                    break
            await emit(
                ProviderEvent(
                    provider=self.provider_id,
                    run_id=run_id,
                    type="tool.result",
                    payload={
                        "tool": "browser.search",
                        "engine": engine,
                        "query": query,
                        "status_code": response.status if response else None,
                        "status": "ok" if engine_results else "no_results",
                        "results": engine_results,
                        "browser_session_id": session.session_id,
                    },
                )
            )
            if engine_results:
                results = engine_results
                break
        if results:
            await emit(
                ProviderEvent(
                    provider=self.provider_id,
                    run_id=run_id,
                    type="semantic.progress",
                    payload={
                        **progress_payload(
                            "validation",
                            f"Found {len(results)} browser candidate source(s) for: {query}",
                            source="browser_provider:search",
                            explicit=True,
                            verified=True,
                        ),
                        "browser_session_id": session.session_id,
                    },
                )
            )
        return [item["url"] for item in results[:max_results]]

    async def _click_text(
        self,
        session: BrowserSession,
        text: str,
        run_id: str,
        emit: EmitProviderEvent,
        *,
        timeout_ms: int,
        task: str = "",
    ) -> None:
        await emit(
            ProviderEvent(
                provider=self.provider_id,
                run_id=run_id,
                type="tool.call",
                payload={"tool": "browser.click_text", "text": text, "browser_session_id": session.session_id},
            )
        )
        strategy = await self._click_or_focus_control(
            session.page,
            text,
            task=task,
            timeout_ms=timeout_ms,
        )
        try:
            await session.page.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 5000))
        except Exception:
            pass
        session.last_url = str(session.page.url or session.last_url)
        session.updated_at = time.time()
        await emit(
            ProviderEvent(
                provider=self.provider_id,
                run_id=run_id,
                type="tool.result",
                payload={
                    "tool": "browser.click_text",
                    "text": text,
                    "url": session.last_url,
                    "strategy": strategy,
                    "browser_session_id": session.session_id,
                },
            )
        )

    async def _click_ref(
        self,
        session: BrowserSession,
        ref: str,
        run_id: str,
        emit: EmitProviderEvent,
        *,
        timeout_ms: int,
    ) -> None:
        await emit(
            ProviderEvent(
                provider=self.provider_id,
                run_id=run_id,
                type="tool.call",
                payload={"tool": "browser.click_ref", "ref": ref, "browser_session_id": session.session_id},
            )
        )
        action_ref = session.interaction_refs.get(ref)
        if not action_ref:
            raise RuntimeError(f"Unknown browser interaction ref {ref!r}. Observe the page again before continuing.")
        strategy = await self._activate_action_ref(session, action_ref, timeout_ms=timeout_ms)
        try:
            await session.page.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 5000))
        except Exception:
            pass
        session.last_url = str(session.page.url or session.last_url)
        session.updated_at = time.time()
        await emit(
            ProviderEvent(
                provider=self.provider_id,
                run_id=run_id,
                type="tool.result",
                payload={
                    "tool": "browser.click_ref",
                    "ref": ref,
                    "strategy": strategy,
                    "url": session.last_url,
                    "browser_session_id": session.session_id,
                },
            )
        )

    async def _fill_ref(
        self,
        session: BrowserSession,
        ref: str,
        value: str,
        run_id: str,
        emit: EmitProviderEvent,
        *,
        timeout_ms: int,
        submit: bool,
    ) -> None:
        await emit(
            ProviderEvent(
                provider=self.provider_id,
                run_id=run_id,
                type="tool.call",
                payload={"tool": "browser.fill_ref", "ref": ref, "submit": submit, "browser_session_id": session.session_id},
            )
        )

        action_ref = session.interaction_refs.get(ref)
        if not action_ref:
            raise RuntimeError(f"Unknown browser interaction ref {ref!r}. Observe the page again before continuing.")
        selector = str(action_ref.get("selector") or "")
        if not selector:
            raise RuntimeError(f"Browser interaction ref {ref!r} is not fillable.")
        locator = session.page.locator(selector).first
        await locator.fill(value, timeout=timeout_ms)
        strategy = "fill"
        if submit:
            strategy = await self._submit_filled_control(session, selector, timeout_ms=timeout_ms)
        await self._wait_for_page_ready(session.page, timeout_ms=timeout_ms)
        session.last_url = str(session.page.url or session.last_url)
        session.updated_at = time.time()
        await emit(
            ProviderEvent(
                provider=self.provider_id,
                run_id=run_id,
                type="tool.result",
                payload={
                    "tool": "browser.fill_ref",
                    "ref": ref,
                    "submit": submit,
                    "strategy": strategy,
                    "url": session.last_url,
                    "browser_session_id": session.session_id,
                },
            )
        )

    async def _go_back(
        self,
        session: BrowserSession,
        run_id: str,
        emit: EmitProviderEvent,
        *,
        timeout_ms: int,
    ) -> None:
        from_url = str(session.page.url or session.last_url or "")
        await emit(
            ProviderEvent(
                provider=self.provider_id,
                run_id=run_id,
                type="tool.call",
                payload={
                    "tool": "browser.back",
                    "from_url": from_url,
                    "browser_session_id": session.session_id,
                },
            )
        )

        strategy = ""
        with suppress(Exception):
            await session.page.go_back(
                wait_until="domcontentloaded",
                timeout=max(1000, min(timeout_ms, 6000)),
            )
            await self._wait_for_page_ready(session.page, timeout_ms=timeout_ms)
        if self._url_changed(from_url, str(session.page.url or "")):
            strategy = "history"

        if not strategy:
            while session.page_stack:
                previous_page = session.page_stack.pop()
                try:
                    if previous_page is None or bool(previous_page.is_closed()):
                        continue
                except Exception:
                    if previous_page is None:
                        continue
                current_page = session.page
                session.page = previous_page
                with suppress(Exception):
                    if current_page is not previous_page:
                        await current_page.close()
                await self._wait_for_page_ready(session.page, timeout_ms=timeout_ms)
                strategy = "opener"
                break

        if not strategy:
            raise RuntimeError("The current browser page has no previous page to return to.")

        session.last_url = str(session.page.url or session.last_url)
        session.updated_at = time.time()
        await emit(
            ProviderEvent(
                provider=self.provider_id,
                run_id=run_id,
                type="tool.result",
                payload={
                    "tool": "browser.back",
                    "from_url": from_url,
                    "url": session.last_url,
                    "strategy": strategy,
                    "browser_session_id": session.session_id,
                },
            )
        )

    async def _activate_action_ref(self, session: BrowserSession, action_ref: dict[str, Any], *, timeout_ms: int) -> str:
        selector = str(action_ref.get("selector") or "")
        href = str(action_ref.get("href") or "")
        if selector:
            strategy = await self._click_locator_and_follow_popup(
                session,
                session.page.locator(selector).first,
                timeout_ms=max(1200, min(timeout_ms, 5000)),
            )
            if strategy:
                return f"selector:{selector}:{strategy}"
        bbox = action_ref.get("bbox") if isinstance(action_ref.get("bbox"), dict) else {}
        try:
            x = float(bbox.get("x", 0)) + float(bbox.get("width", 0)) / 2
            y = float(bbox.get("y", 0)) + float(bbox.get("height", 0)) / 2
            if x > 0 and y > 0:
                strategy = await self._perform_page_action(
                    session,
                    lambda: session.page.mouse.click(x, y),
                    timeout_ms=max(1200, min(timeout_ms, 5000)),
                )
                return f"bbox:{strategy}"
        except Exception:
            pass
        if href:
            await session.page.goto(href, wait_until="domcontentloaded", timeout=timeout_ms)
            return "href"
        raise RuntimeError(f"Could not activate browser interaction ref {action_ref.get('ref')!r}.")

    async def _submit_filled_control(self, session: BrowserSession, selector: str, *, timeout_ms: int) -> str:
        before_url = str(session.page.url or "")
        locator = session.page.locator(selector).first
        try:
            enter_strategy = await self._perform_page_action(
                session,
                lambda: locator.press("Enter", timeout=min(timeout_ms, 5000)),
                timeout_ms=max(1200, min(timeout_ms, 6000)),
            )
        except Exception:
            enter_strategy = "enter-failed"
        if enter_strategy == "popup" or self._url_changed(before_url, str(session.page.url or "")):
            return f"fill+enter:{enter_strategy}"

        submit_handle = await self._nearest_submit_control(session.page, selector)
        try:
            if submit_handle is not None:
                click_strategy = await self._perform_page_action(
                    session,
                    lambda: submit_handle.click(timeout=max(1200, min(timeout_ms, 5000))),
                    timeout_ms=max(1200, min(timeout_ms, 6000)),
                )
                if click_strategy == "popup" or self._url_changed(before_url, str(session.page.url or "")):
                    return f"fill+submit-control:{click_strategy}"
        finally:
            with suppress(Exception):
                if submit_handle is not None:
                    await submit_handle.dispose()

        form_strategy = await self._request_submit_nearest_form(session, selector, timeout_ms=timeout_ms)
        if form_strategy:
            return f"fill+{form_strategy}"
        return f"fill+enter:{enter_strategy}"

    async def _click_locator_and_follow_popup(self, session: BrowserSession, locator: Any, *, timeout_ms: int) -> str:
        try:
            return await self._perform_page_action(
                session,
                lambda: locator.click(timeout=timeout_ms),
                timeout_ms=timeout_ms,
            )
        except Exception:
            return ""

    async def _perform_page_action(self, session: BrowserSession, action: Any, *, timeout_ms: int) -> str:
        page = session.page
        popup_task = asyncio.create_task(page.wait_for_event("popup", timeout=max(1000, min(timeout_ms, 6000))))
        try:
            await action()
        except Exception:
            if not popup_task.done():
                popup_task.cancel()
            with suppress(BaseException):
                await popup_task
            raise
        finally:
            if popup_task.done():
                with suppress(BaseException):
                    popup_task.exception()
        popup = None
        with suppress(Exception):
            popup = await popup_task
        if popup is not None:
            if not session.page_stack or session.page_stack[-1] is not page:
                session.page_stack.append(page)
            session.page = popup
            await self._wait_for_page_ready(session.page, timeout_ms=timeout_ms)
            return "popup"
        with suppress(Exception):
            if not popup_task.done():
                popup_task.cancel()
        await self._wait_for_page_ready(session.page, timeout_ms=timeout_ms)
        return "same-page"

    async def _wait_for_page_ready(self, page: Any, *, timeout_ms: int) -> None:
        with suppress(Exception):
            await page.wait_for_load_state("domcontentloaded", timeout=max(1000, min(timeout_ms, 5000)))
        with suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=max(1000, min(timeout_ms, 3000)))

    async def _nearest_submit_control(self, page: Any, selector: str) -> Any | None:
        try:
            return await page.evaluate_handle(
                """
                (selector) => {
                  const input = document.querySelector(selector);
                  if (!input) return null;
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect && rect.width > 1 && rect.height > 1 &&
                      style.visibility !== "hidden" && style.display !== "none" &&
                      Number(style.opacity || "1") !== 0;
                  };
                  const textOf = (el) => [
                    el.innerText,
                    el.textContent,
                    el.value,
                    el.getAttribute("aria-label"),
                    el.getAttribute("title"),
                    el.getAttribute("placeholder"),
                    el.getAttribute("name"),
                    el.getAttribute("id"),
                    el.className
                  ].filter(Boolean).join(" ").toLowerCase();
                  const inputRect = input.getBoundingClientRect();
                  const roots = [];
                  if (input.form) roots.push(input.form);
                  let node = input.parentElement;
                  for (let i = 0; node && i < 7; i += 1, node = node.parentElement) {
                    roots.push(node);
                  }
                  const seen = new Set();
                  const candidates = [];
                  for (const root of roots) {
                    for (const el of root.querySelectorAll(
                      'button,a,[role="button"],input[type="submit"],input[type="button"],[onclick]'
                    )) {
                      if (seen.has(el) || el === input || !visible(el)) continue;
                      seen.add(el);
                      const rect = el.getBoundingClientRect();
                      const dx = Math.abs((rect.left + rect.width / 2) - (inputRect.left + inputRect.width / 2));
                      const dy = Math.abs((rect.top + rect.height / 2) - (inputRect.top + inputRect.height / 2));
                      const label = textOf(el);
                      let score = 120 - dx / 8 - dy / 4;
                      if (el.type === "submit") score += 80;
                      if (/search|submit|go|find|搜索|搜|查询|检索/.test(label)) score += 90;
                      if (rect.left >= inputRect.right - 12) score += 25;
                      candidates.push({ el, score });
                    }
                  }
                  candidates.sort((a, b) => b.score - a.score);
                  return candidates[0] ? candidates[0].el : null;
                }
                """,
                selector,
            )
        except Exception:
            return None

    async def _request_submit_nearest_form(self, session: BrowserSession, selector: str, *, timeout_ms: int) -> str:
        before_url = str(session.page.url or "")

        async def request_submit() -> None:
            await session.page.evaluate(
                """
                (selector) => {
                  const input = document.querySelector(selector);
                  const form = input && input.closest("form");
                  if (!form) return false;
                  if (typeof form.requestSubmit === "function") {
                    form.requestSubmit();
                  } else {
                    form.submit();
                  }
                  return true;
                }
                """,
                selector,
            )

        try:
            strategy = await self._perform_page_action(
                session,
                request_submit,
                timeout_ms=max(1200, min(timeout_ms, 6000)),
            )
            if strategy == "popup" or self._url_changed(before_url, str(session.page.url or "")):
                return f"form-submit:{strategy}"
        except Exception:
            return ""
        return ""

    @staticmethod
    def _url_changed(before: str, after: str) -> bool:
        before_clean = before.strip().rstrip("/")
        after_clean = after.strip().rstrip("/")
        return bool(after_clean and after_clean != before_clean)

    async def _click_or_focus_control(self, page: Any, text: str, *, task: str, timeout_ms: int) -> str:
        short_timeout = max(1200, min(timeout_ms, 3500))
        if self._looks_like_search_focus(text, task):
            for selector in (
                "input[type='search']",
                "input[name='q']",
                "textarea[name='q']",
                "input[aria-label*='search' i]",
                "input[placeholder*='search' i]",
                "input[title*='search' i]",
                "input[aria-label*='検索']",
                "input[placeholder*='検索']",
                "input[aria-label*='搜索']",
                "input[placeholder*='搜索']",
            ):
                if await self._try_click_locator(page.locator(selector).first, timeout_ms=short_timeout):
                    return f"search-control:{selector}"

        lookup_attempts = (
            ("label", lambda: page.get_by_label(text, exact=False).first),
            ("placeholder", lambda: page.get_by_placeholder(text, exact=False).first),
            ("button", lambda: page.get_by_role("button", name=re.compile(re.escape(text), re.I)).first),
            ("link", lambda: page.get_by_role("link", name=re.compile(re.escape(text), re.I)).first),
            ("text", lambda: page.get_by_text(text, exact=False).first),
        )
        for strategy, factory in lookup_attempts:
            if await self._try_click_locator(factory(), timeout_ms=short_timeout):
                return strategy

        dom_strategy = await self._click_dom_attribute_match(page, text, timeout_ms=short_timeout)
        if dom_strategy:
            return dom_strategy

        raise RuntimeError(
            f"Could not click or focus a visible browser control matching {text!r}. "
            f"Try observing the page first or use a more specific visible label."
        )

    async def _try_click_locator(self, locator_factory: Any, *, timeout_ms: int) -> bool:
        try:
            locator = locator_factory() if callable(locator_factory) else locator_factory
            await locator.click(timeout=timeout_ms)
            return True
        except Exception:
            return False

    async def _click_dom_attribute_match(self, page: Any, text: str, *, timeout_ms: int) -> str:
        handle = None
        try:
            handle = await page.evaluate_handle(
                """
                (needle) => {
                  const wanted = String(needle || '').trim().toLowerCase();
                  if (!wanted) return null;
                  const candidates = Array.from(document.querySelectorAll(
                    'a,button,input,textarea,select,[role="button"],[role="link"],[aria-label],[title]'
                  ));
                  const textOf = (el) => [
                    el.innerText,
                    el.textContent,
                    el.value,
                    el.getAttribute('aria-label'),
                    el.getAttribute('title'),
                    el.getAttribute('placeholder'),
                    el.getAttribute('name'),
                    el.getAttribute('id')
                  ].filter(Boolean).join(' ').trim().toLowerCase();
                  return candidates.find((el) => textOf(el).includes(wanted)) || null;
                }
                """,
                text,
            )
            element = handle.as_element() if handle else None
            if not element:
                return ""
            await element.click(timeout=timeout_ms)
            return "dom-attribute"
        except Exception:
            return ""
        finally:
            try:
                if handle is not None:
                    await handle.dispose()
            except Exception:
                pass

    @staticmethod
    def _looks_like_search_focus(text: str, task: str) -> bool:
        haystack = f"{text} {task}".strip().lower()
        if not haystack:
            return False
        return any(
            token in haystack
            for token in (
                "search box",
                "search input",
                "search field",
                "検索ボックス",
                "検索欄",
                "検索窓",
                "搜索框",
                "搜索栏",
                "search",
                "検索",
                "搜索",
            )
        )

    async def _open_and_capture(
        self,
        session: BrowserSession,
        url: str,
        run_id: str,
        emit: EmitProviderEvent,
        *,
        index: int,
        timeout_ms: int,
    ) -> BrowserSnapshot:
        await emit(
            ProviderEvent(
                provider=self.provider_id,
                run_id=run_id,
                type="tool.call",
                payload={"tool": "browser.open", "url": url, "index": index, "browser_session_id": session.session_id},
            )
        )
        navigation_chain = [str(url)]

        def _record_main_frame(frame: Any) -> None:
            try:
                if frame == session.page.main_frame:
                    self._append_navigation_url(navigation_chain, frame.url)
            except Exception:
                pass

        session.page.on("framenavigated", _record_main_frame)
        try:
            response = await session.page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            for item in self._navigation_chain(response):
                self._append_navigation_url(navigation_chain, item)
            try:
                await session.page.wait_for_load_state(
                    "networkidle",
                    timeout=min(timeout_ms, 5000),
                )
            except Exception:
                pass
            self._append_navigation_url(navigation_chain, session.page.url)
        finally:
            session.page.remove_listener("framenavigated", _record_main_frame)
        snapshot = await self._capture_page(
            session,
            run_id,
            emit,
            index=index,
            response_status=response.status if response else None,
            navigation_chain=navigation_chain,
        )
        await emit(
            ProviderEvent(
                provider=self.provider_id,
                run_id=run_id,
                type="tool.result",
                payload={
                    "tool": "browser.open",
                    "url": snapshot.final_url,
                    "title": snapshot.title,
                    "status_code": snapshot.status_code,
                    "browser_session_id": session.session_id,
                },
            )
        )
        return snapshot

    async def _capture_page(
        self,
        session: BrowserSession,
        run_id: str,
        emit: EmitProviderEvent,
        *,
        index: int,
        response_status: int | None = None,
        navigation_chain: list[str] | None = None,
    ) -> BrowserSnapshot:
        final_url = str(session.page.url or session.last_url or "about:blank")
        title = self._clean_text(await session.page.title())
        html = await session.page.content()
        parsed = self._parse_html(html, final_url)
        text = parsed.text
        try:
            body_text = await session.page.locator("body").inner_text(timeout=2500)
            text = self._clean_text(body_text) or text
        except Exception:
            pass
        screenshot_bytes = await session.page.screenshot(type="png", full_page=False)
        interaction_refs = await self._extract_interaction_refs(session.page)
        snapshot = BrowserSnapshot(
            url=final_url,
            final_url=final_url,
            title=title or parsed.title,
            text=text,
            excerpt=self._excerpt(text),
            links=parsed.links,
            interaction_refs=interaction_refs,
            screenshot="data:image/png;base64," + base64.b64encode(screenshot_bytes).decode("ascii"),
            status_code=response_status,
            navigation_chain=list(navigation_chain or []),
        )
        session.last_url = final_url
        session.title = snapshot.title
        session.interaction_refs = {str(item.get("ref")): item for item in interaction_refs if item.get("ref")}
        session.updated_at = time.time()
        await emit(
            ProviderEvent(
                provider=self.provider_id,
                run_id=run_id,
                type="artifact.created",
                payload={
                    "artifact_type": "browser.snapshot",
                    "browser_session_id": session.session_id,
                    "url": snapshot.final_url,
                    "title": snapshot.title or self._host_label(snapshot.final_url),
                    "excerpt": snapshot.excerpt,
                    "links": snapshot.links[:6],
                    "interaction_refs": snapshot.interaction_refs[:16],
                    "screenshot": snapshot.screenshot,
                    "engine": "playwright",
                    "status_code": snapshot.status_code,
                },
            )
        )
        return snapshot

    async def _extract_interaction_refs(self, page: Any, *, limit: int = 36) -> list[dict[str, Any]]:
        try:
            raw_refs = await page.evaluate(
                """
                (limit) => {
                  const cssEscape = (value) => {
                    if (window.CSS && CSS.escape) return CSS.escape(String(value));
                    return String(value).replace(/[^a-zA-Z0-9_-]/g, "\\\\$&");
                  };
                  const textOf = (el) => [
                    el.innerText,
                    el.textContent,
                    el.value,
                    el.getAttribute("aria-label"),
                    el.getAttribute("title"),
                    el.getAttribute("placeholder"),
                    el.getAttribute("alt"),
                    el.getAttribute("name")
                  ].filter(Boolean).join(" ").replace(/\\s+/g, " ").trim();
                  const selectorFor = (el) => {
                    const tag = el.tagName.toLowerCase();
                    const id = el.getAttribute("id");
                    if (id) return `${tag}#${cssEscape(id)}`;
                    const testId = el.getAttribute("data-testid") || el.getAttribute("data-test");
                    if (testId) return `${tag}[data-testid="${cssEscape(testId)}"],${tag}[data-test="${cssEscape(testId)}"]`;
                    const name = el.getAttribute("name");
                    if (name) return `${tag}[name="${cssEscape(name)}"]`;
                    const aria = el.getAttribute("aria-label");
                    if (aria) return `${tag}[aria-label="${cssEscape(aria)}"]`;
                    const href = el.getAttribute("href");
                    if (tag === "a" && href) return `a[href="${cssEscape(href)}"]`;
                    const index = Array.from(document.querySelectorAll(tag)).indexOf(el) + 1;
                    return `${tag}:nth-of-type(${Math.max(1, index)})`;
                  };
                  const roleOf = (el) => {
                    const explicit = el.getAttribute("role");
                    if (explicit) return explicit;
                    const tag = el.tagName.toLowerCase();
                    if (tag === "a") return "link";
                    if (tag === "button") return "button";
                    if (tag === "input" || tag === "textarea") return "textbox";
                    if (tag === "select") return "combobox";
                    if (tag === "video") return "video";
                    return tag;
                  };
                  const nodes = Array.from(document.querySelectorAll(
                    'a[href],button,input,textarea,select,video,[role="button"],[role="link"],[onclick]'
                  ));
                  const refs = [];
                  for (const el of nodes) {
                    const rect = el.getBoundingClientRect();
                    if (!rect || rect.width <= 1 || rect.height <= 1) continue;
                    const style = window.getComputedStyle(el);
                    if (style.visibility === "hidden" || style.display === "none" || Number(style.opacity || "1") === 0) continue;
                    if (rect.bottom < 0 || rect.right < 0 || rect.top > window.innerHeight || rect.left > window.innerWidth) continue;
                    const label = textOf(el);
                    const tag = el.tagName.toLowerCase();
                    const href = el.href || el.getAttribute("href") || "";
                    const kind =
                      tag === "input" || tag === "textarea" || tag === "select" ? "input" :
                      tag === "video" ? "video" :
                      tag === "a" ? "link" :
                      "control";
                    if (!label && !href && kind !== "input" && kind !== "video") continue;
                    refs.push({
                      ref: `br_${refs.length + 1}`,
                      kind,
                      role: roleOf(el),
                      label: label.slice(0, 120) || href.slice(0, 120) || kind,
                      href: href || "",
                      selector: selectorFor(el),
                      fillable: kind === "input",
                      bbox: {
                        x: Math.round(rect.left),
                        y: Math.round(rect.top),
                        width: Math.round(rect.width),
                        height: Math.round(rect.height)
                      }
                    });
                    if (refs.length >= limit) break;
                  }
                  return refs;
                }
                """,
                limit,
            )
        except Exception:
            return []
        if not isinstance(raw_refs, list):
            return []
        refs: list[dict[str, Any]] = []
        for item in raw_refs:
            if not isinstance(item, dict):
                continue
            ref = str(item.get("ref") or "").strip()
            selector = str(item.get("selector") or "").strip()
            if not ref or not selector:
                continue
            refs.append(
                {
                    "ref": ref,
                    "kind": self._clean_text(str(item.get("kind") or "control"))[:40],
                    "role": self._clean_text(str(item.get("role") or ""))[:40],
                    "label": self._clean_text(str(item.get("label") or ""))[:140],
                    "href": str(item.get("href") or "").strip()[:500],
                    "selector": selector[:500],
                    "fillable": bool(item.get("fillable")),
                    "bbox": dict(item.get("bbox") or {}) if isinstance(item.get("bbox"), dict) else {},
                }
            )
        return refs[:limit]

    def _parse_html(self, html: str, base_url: str) -> BrowserSnapshot:
        soup = BeautifulSoup(html or "", "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        title = self._clean_text(soup.title.get_text(" ", strip=True) if soup.title else "")
        text = self._clean_text(soup.get_text(" ", strip=True))
        links: list[dict[str, str]] = []
        for anchor in soup.select("a[href]"):
            href = str(anchor.get("href") or "").strip()
            label = self._clean_text(anchor.get_text(" ", strip=True))
            if not href or not label:
                continue
            absolute = urljoin(base_url, href)
            if not absolute.startswith(("http://", "https://")):
                continue
            if any(item["url"] == absolute for item in links):
                continue
            links.append({"title": label[:90], "url": absolute})
            if len(links) >= 12:
                break
        return BrowserSnapshot(url=base_url, final_url=base_url, title=title, text=text, links=links)

    async def _prune_sessions(self) -> None:
        now = time.time()
        for session_id, session in list(self._sessions.items()):
            if now - session.updated_at > self._session_ttl_s:
                await self._close_session(session_id)

    async def _close_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return
        for run_id, sid in list(self._run_to_session.items()):
            if sid == session_id:
                self._run_to_session.pop(run_id, None)
        for chat_session_id, sid in list(self._chat_to_session.items()):
            if sid == session_id:
                self._chat_to_session.pop(chat_session_id, None)
        try:
            await session.context.close()
        except Exception:
            pass
        try:
            await session.browser.close()
        except Exception:
            pass
        if not self._sessions:
            await self._stop_playwright()

    async def _stop_playwright(self) -> None:
        playwright = self._playwright
        manager = self._playwright_manager
        self._playwright_manager = None
        self._playwright = None
        if playwright is not None and hasattr(playwright, "stop"):
            try:
                await playwright.stop()
                return
            except Exception:
                pass
        if manager is None:
            return
        try:
            await manager.stop()
        except Exception:
            pass

    def _urls_from_request(
        self,
        request: ProviderRunRequest,
        metadata: dict[str, Any],
        *,
        allow_bare_domain: bool = False,
    ) -> list[str]:
        urls: list[str] = []
        raw_urls = metadata.get("urls")
        if isinstance(raw_urls, list):
            urls.extend(str(item) for item in raw_urls)
        if metadata.get("url"):
            urls.append(str(metadata.get("url")))
        urls.extend(self._extract_urls(request.task))
        normalized: list[str] = []
        for url in urls:
            fixed = self._normalize_url(url, allow_bare_domain=allow_bare_domain)
            if fixed and fixed not in normalized:
                normalized.append(fixed)
        if allow_bare_domain and not normalized:
            for domain in self._extract_bare_domains(request.task):
                fixed = self._normalize_url(domain, allow_bare_domain=True)
                if fixed and fixed not in normalized:
                    normalized.append(fixed)
        return normalized

    def _extract_urls(self, text: str) -> list[str]:
        return web_addresses(text)

    def _extract_bare_domains(self, text: str) -> list[str]:
        """Extract address-like bare domains only for an explicit open action.

        This deliberately does not run for research/search requests: a domain
        mentioned in prose is not necessarily the requested navigation target.
        """

        return web_addresses(text, allow_bare_domain=True)

    def _normalize_url(self, url: str, *, allow_bare_domain: bool = False) -> str:
        return normalize_web_address(url, allow_bare_domain=allow_bare_domain)

    def _normalize_search_href(self, href: str) -> str:
        if not href:
            return ""
        if href.startswith("//"):
            href = "https:" + href
        if href.startswith("/l/") or "duckduckgo.com/l/" in href:
            query = parse_qs(urlparse(href).query)
            href = unquote(query.get("uddg", [""])[0])
        elif "bing.com/ck/a" in href:
            query = parse_qs(urlparse(href).query)
            encoded = str(query.get("u", [""])[0])
            if encoded.startswith("a1"):
                try:
                    payload = encoded[2:]
                    payload += "=" * (-len(payload) % 4)
                    href = base64.urlsafe_b64decode(payload).decode("utf-8")
                except (UnicodeDecodeError, ValueError):
                    pass
        return self._normalize_url(href)

    def _task_as_query(self, task: str) -> str:
        return browser_research_query(task)

    def _result_markdown(self, task: str, *, query: str, snapshots: list[BrowserSnapshot]) -> str:
        lines = ["### Browser result", f"Task: {self._clean_text(task)[:180]}"]
        if query:
            lines.append(f"Query: `{query}`")
        lines.append("")
        if not snapshots:
            lines.append("No browser sources were collected.")
            return "\n".join(lines)
        for index, item in enumerate(snapshots, start=1):
            title = item.title or self._host_label(item.final_url or item.url) or f"Source {index}"
            lines.append(f"{index}. [{title}]({item.final_url or item.url})")
            if item.excerpt:
                lines.append(f"   {item.excerpt}")
        return "\n".join(lines)

    def _host_label(self, url: str) -> str:
        try:
            return urlparse(url).netloc.removeprefix("www.")
        except Exception:
            return ""

    def _excerpt(self, text: str, limit: int = 320) -> str:
        cleaned = self._clean_text(text)
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: limit - 3].rstrip() + "..."

    def _clean_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "")).strip()

    def _bounded_int(self, value: Any, *, default: int, low: int, high: int) -> int:
        try:
            number = int(value)
        except Exception:
            number = default
        return max(low, min(high, number))

    @staticmethod
    def _truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "submit"}
