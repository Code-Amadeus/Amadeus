from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_host.adapters.browser_branch import (  # noqa: E402
    BrowserBranchAdapter,
    _deterministic_branch_fallback,
    _extract_search_query,
)
from agent_host.provider_types import ProviderEvent, ProviderRunRequest, ProviderRunResult  # noqa: E402
from server.event_bus import bus  # noqa: E402
from server.handlers.work_activity_handler import WorkActivityCoordinator  # noqa: E402
from server.protocol import Method  # noqa: E402
from server.provider_branch import ProviderBranchStore  # noqa: E402


SEARCH_INSTRUCTION = "\u5e2e\u6211\u5728\u8fd9\u4e2a\u9875\u9762\u91cc\u9762\u641c\u7d22\u4e00\u4e0bAmadeus\u3002"
SEARCH_WITH_SITE_INSTRUCTION = "\u8bf7\u5728\u5f53\u524d\u7ef4\u57fa\u767e\u79d1\u9875\u9762\u641c\u7d22 Amadeus\u3002"
SUMMARY_INSTRUCTION = "\u603b\u7ed3\u4e00\u4e0b\u8fd9\u4e2a\u9875\u9762\u7684\u5185\u5bb9\u3002"
CONTINUE_INSTRUCTION = "\u7ee7\u7eed\u3002"


class FakeBrowserEngine:
    provider_id = "browser"
    engine_id = "fake-browser"

    def __init__(self) -> None:
        self.session_id = "browser_semantics_001"
        self.page = "home"
        self.actions: list[dict[str, Any]] = []

    async def run(
        self,
        request: ProviderRunRequest,
        run_id: str,
        emit,
    ) -> ProviderRunResult:
        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        action = str(metadata.get("browser_action") or request.mode or "observe")
        await emit(
            ProviderEvent(
                provider="browser",
                run_id=run_id,
                type="tool.call",
                payload={"tool": f"browser.{action}", "browser_session_id": self.session_id},
            )
        )
        self.actions.append({"action": action, "metadata": dict(metadata), "task": request.task})

        if action == "open":
            self.page = "home"
        elif action == "observe":
            pass
        elif action == "fill_ref":
            ref = str(metadata.get("ref") or "")
            value = str(metadata.get("value") or "")
            if ref != "br_search":
                return ProviderRunResult(status="error", result="bad ref", error="bad_ref")
            if metadata.get("submit"):
                self.page = "search"
            self.actions[-1]["value"] = value
        elif action == "click_ref":
            ref = str(metadata.get("ref") or "")
            if ref == "br_next":
                self.page = "detail"
            elif ref == "br_collect":
                self.page = "collected"
            else:
                return ProviderRunResult(status="error", result="bad ref", error="bad_ref")

        snapshot = self._snapshot()
        await emit(
            ProviderEvent(
                provider="browser",
                run_id=run_id,
                type="artifact.created",
                payload=snapshot,
                metadata={"browser": {"browser_session_id": self.session_id}},
            )
        )
        return ProviderRunResult(
            status="done",
            result=f"Browser fake result: {snapshot['title']}",
            metadata={
                "browser": {
                    "browser_session_id": self.session_id,
                    "current_url": snapshot["url"],
                    "title": snapshot["title"],
                    "page_title": snapshot["title"],
                    "engine": self.engine_id,
                }
            },
        )

    async def inspect_session(self, session_id: str, *, include_dom: bool = True) -> dict[str, Any]:
        snapshot = self._snapshot()
        return {
            "browser_session_id": self.session_id,
            "url": snapshot["url"],
            "title": snapshot["title"],
            "text": snapshot["excerpt"],
            "dom": self._dom() if include_dom else "",
            "interaction_refs": self._refs(),
            "updated_at": 1.0,
        }

    async def cancel(self, run_id: str) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    def _snapshot(self) -> dict[str, Any]:
        title_by_page = {
            "home": "Portal Home",
            "search": "Portal Search Results",
            "detail": "Portal Detail",
            "collected": "Evidence Collected",
        }
        excerpt_by_page = {
            "home": "Home page with a search box, a detail link, and a collect button.",
            "search": "Search results for Amadeus are now visible.",
            "detail": "The requested detail page is open in the same browser session.",
            "collected": "The collect evidence button was clicked.",
        }
        slug = {
            "home": "index.html",
            "search": "search.html?q=Amadeus",
            "detail": "detail.html",
            "collected": "collected.html",
        }[self.page]
        return {
            "artifact_type": "browser.snapshot",
            "browser_session_id": self.session_id,
            "url": f"https://example.local/{slug}",
            "title": title_by_page[self.page],
            "excerpt": excerpt_by_page[self.page],
            "links": [{"title": "Detail link", "url": "https://example.local/detail.html"}],
            "screenshot": "data:image/png;base64,ZmFrZQ==",
            "engine": self.engine_id,
            "status_code": 200,
        }

    def _refs(self) -> list[dict[str, Any]]:
        return [
            {
                "ref": "br_search",
                "kind": "input",
                "role": "textbox",
                "label": "Search query",
                "fillable": True,
            },
            {
                "ref": "br_next",
                "kind": "link",
                "role": "link",
                "label": "Detail link",
                "href": "https://example.local/detail.html",
                "fillable": False,
            },
            {
                "ref": "br_collect",
                "kind": "button",
                "role": "button",
                "label": "Collect evidence",
                "fillable": False,
            },
        ]

    def _dom(self) -> str:
        return """
        <!doctype html><title>Portal Home</title>
        <main>
          <input aria-label="Search query" name="q">
          <a href="/detail.html">Detail link</a>
          <button>Collect evidence</button>
          <section hidden>RAW_DOM_BRANCH_ONLY_SEMANTICS_MARKER</section>
        </main>
        """


def branch_context(latest_instruction: str, *, pending_goal: str = "") -> dict[str, Any]:
    context: dict[str, Any] = {
        "user_message": (
            "Continue the active browser interaction branch.\n"
            f"Latest user instruction: {latest_instruction}\n"
            f"Pending branch goal: {pending_goal}"
        ),
        "request_metadata": {"pending_goal": pending_goal},
        "branch_transcript": [{"role": "user", "content": pending_goal}],
        "interaction_refs": FakeBrowserEngine()._refs(),
    }
    return context


async def run_branch_with_planner(planner_action: dict[str, Any], *, task: str) -> ProviderRunResult:
    fake = FakeBrowserEngine()

    async def planner(context: dict[str, Any]) -> dict[str, Any]:
        return {
            "assistant_message": "",
            "actions": [planner_action],
            "final_report": "Done.",
            "compact_digest": "Browser branch completed one deterministic test action.",
            "reason": "test fixture",
            "confidence": 1.0,
            "planner": "fixture",
        }

    with tempfile.TemporaryDirectory() as tmp:
        adapter = BrowserBranchAdapter(
            base_adapter=fake,
            store=ProviderBranchStore(Path(tmp)),
            branch_planner=planner,
        )
        events: list[dict[str, Any]] = []

        async def emit(event: ProviderEvent) -> None:
            events.append(event.to_dict())

        result = await adapter.run(
            ProviderRunRequest(
                provider="browser",
                task=task,
                mode="click_ref",
                metadata={
                    "source": "llm_delegate",
                    "session_id": "semantics_session",
                    "browser_action": "click_ref",
                    "browser_session_id": fake.session_id,
                    "max_branch_actions": 3,
                },
            ),
            "browser_semantics_run",
            emit,
        )
        assert result.metadata.get("provider_branch", {}).get("hidden_message_count", 0) > 0, result.to_dict()
        assert "RAW_DOM_BRANCH_ONLY_SEMANTICS_MARKER" not in result.result, result.result
        assert events, "branch should emit provider events"
        return result


async def assert_canvas_prefers_latest_metadata_snapshot() -> None:
    captured: list[dict[str, Any]] = []

    async def capture(method: str, params: dict[str, Any]) -> None:
        if method == Method.WALLPAPER_CANVAS:
            captured.append(dict(params or {}))

    bus.on(Method.WALLPAPER_CANVAS, capture)
    coordinator = WorkActivityCoordinator()
    coordinator.configure()
    run_id = "canvas_latest_snapshot_semantics"
    old_snapshot = {
        "artifact_type": "browser.snapshot",
        "browser_session_id": "browser_semantics_001",
        "url": "https://example.local/old.html",
        "title": "Old Preview",
        "excerpt": "Old preview should be replaced by final metadata.",
        "screenshot": "data:image/png;base64,b2xk",
    }
    new_snapshot = {
        "artifact_type": "browser.snapshot",
        "browser_session_id": "browser_semantics_001",
        "url": "https://example.local/new.html",
        "title": "New Final",
        "excerpt": "New final snapshot should win.",
        "screenshot": "data:image/png;base64,bmV3",
    }
    await bus.emit(
        Method.PROVIDER_EVENT,
        {
            "provider": "browser",
            "run_id": run_id,
            "type": "artifact.created",
            "payload": old_snapshot,
            "metadata": {"browser": {"browser_session_id": "browser_semantics_001"}},
        },
    )
    await bus.emit(
        Method.PROVIDER_RESULT,
        {
            "provider": "browser",
            "run_id": run_id,
            "status": "done",
            "result": "final",
            "metadata": {
                "browser": {"browser_session_id": "browser_semantics_001"},
                "provider_branch": {"artifacts": [old_snapshot, new_snapshot]},
            },
        },
    )
    browser_canvases = [item for item in captured if item.get("mode") == "browser"]
    assert browser_canvases, captured
    assert browser_canvases[-1].get("pageTitle") == "New Final", browser_canvases[-1]
    assert browser_canvases[-1].get("url") == "https://example.local/new.html", browser_canvases[-1]


async def main() -> None:
    search_ctx = branch_context(SEARCH_INSTRUCTION)
    search_with_site_ctx = branch_context(SEARCH_WITH_SITE_INSTRUCTION)
    summary_ctx = branch_context(SUMMARY_INSTRUCTION, pending_goal=SEARCH_INSTRUCTION)
    passive_ctx = branch_context(CONTINUE_INSTRUCTION, pending_goal="search Amadeus on this page")

    assert _extract_search_query(search_ctx) == "Amadeus"
    assert _deterministic_branch_fallback(search_ctx)
    assert _extract_search_query(search_with_site_ctx) == "Amadeus"
    assert _deterministic_branch_fallback(search_with_site_ctx)
    assert _extract_search_query(summary_ctx) == ""
    assert _deterministic_branch_fallback(summary_ctx) is None
    assert _extract_search_query(passive_ctx) == ""
    assert _deterministic_branch_fallback(passive_ctx) is None

    search_result = await run_branch_with_planner(
        {"action": "fill_ref", "ref": "br_search", "value": "Amadeus", "submit": True},
        task=SEARCH_INSTRUCTION,
    )
    assert search_result.status == "done", search_result.to_dict()
    assert search_result.metadata["browser"]["page_title"] == "Portal Search Results", search_result.to_dict()
    assert any(
        item.get("action") == "fill_ref"
        for item in search_result.metadata["provider_branch"]["actions"]
    ), search_result.metadata["provider_branch"]

    click_result = await run_branch_with_planner(
        {"action": "click_ref", "ref": "br_next"},
        task="Open the current page detail link.",
    )
    assert click_result.status == "done", click_result.to_dict()
    assert click_result.metadata["browser"]["page_title"] == "Portal Detail", click_result.to_dict()
    assert any(
        item.get("action") == "click_ref"
        for item in click_result.metadata["provider_branch"]["actions"]
    ), click_result.metadata["provider_branch"]

    await assert_canvas_prefers_latest_metadata_snapshot()

    print("browser runtime semantics matrix smoke ok")


if __name__ == "__main__":
    asyncio.run(main())
