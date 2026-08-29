from __future__ import annotations

import asyncio
import contextlib
import functools
import http.server
import socket
import socketserver
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_host.provider_runtime import runtime  # noqa: E402
from server.event_bus import bus  # noqa: E402
from server.handlers.provider_handler import ProviderHandler  # noqa: E402
from server.handlers.work_activity_handler import WorkActivityCoordinator  # noqa: E402
from server.protocol import Method  # noqa: E402
from server.work_context import (  # noqa: E402
    augment_system_prompt_with_active_provider_context,
    render_active_provider_context,
)


HIDDEN_DOM_SENTINEL = "RAW_DOM_BRANCH_ONLY_MARKER_MAIN_CONTINUITY"


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def local_site():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "index.html").write_text(
            f"""
            <!doctype html><meta charset="utf-8">
            <title>Continuity Start</title>
            <main>
              <h1>Continuity Start</h1>
              <p>This page simulates the first browser canvas snapshot.</p>
              <p hidden>{HIDDEN_DOM_SENTINEL}</p>
              <form action="/search.html" method="get">
                <label for="site-search">Search</label>
                <input id="site-search" name="q" placeholder="Search" aria-label="Search query">
                <button type="submit">Search</button>
              </form>
              <nav>
                <a href="/archive.html">Archive</a>
                <a href="/next.html">Next section</a>
                <a href="/lab.html">Lab notes</a>
              </nav>
            </main>
            """,
            encoding="utf-8",
        )
        (root / "next.html").write_text(
            """
            <!doctype html><meta charset="utf-8">
            <title>Continuity Detail</title>
            <main>
              <h1>Continuity Detail</h1>
              <p>The same browser session reached the requested detail page.</p>
              <button>Collect evidence</button>
            </main>
            """,
            encoding="utf-8",
        )
        (root / "archive.html").write_text(
            "<!doctype html><meta charset='utf-8'><title>Archive</title>",
            encoding="utf-8",
        )
        (root / "lab.html").write_text(
            "<!doctype html><meta charset='utf-8'><title>Lab Notes</title>",
            encoding="utf-8",
        )
        (root / "search.html").write_text(
            """
            <!doctype html><meta charset="utf-8">
            <title>Search Results</title>
            <main>
              <h1>Search Results</h1>
              <p>The search provider returned results for the submitted query.</p>
            </main>
            """,
            encoding="utf-8",
        )

        port = free_port()
        handler = functools.partial(QuietHandler, directory=str(root))
        server = socketserver.TCPServer(("127.0.0.1", port), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{port}/index.html"
        finally:
            server.shutdown()
            server.server_close()


async def run_provider_and_wait(handler: ProviderHandler, params: dict[str, Any]) -> dict[str, Any]:
    # This smoke predates Provider contract 0.2. Keep its Browser requests
    # explicit so the selector does not correctly reject them as general work.
    params = dict(params)
    params.setdefault(
        "requirements",
        {
            "task_kind": "browser",
            "preferred_provider": "browser",
            "preference_policy": "require",
        },
    )
    response = await handler.run_provider(params)
    run = response["run"]
    record = runtime.get_run(str(run["run_id"]))
    if record is not None and record.task_handle is not None:
        await record.task_handle
        run = record.to_dict()
    return run


async def main() -> None:
    session_id = "main_browser_continuity_session"
    captured: dict[str, list[dict[str, Any]]] = {
        "canvas": [],
        "work_note": [],
        "activity": [],
        "provider_result": [],
    }

    async def capture(method: str, params: dict[str, Any]) -> None:
        key_by_method = {
            Method.WALLPAPER_CANVAS: "canvas",
            Method.CHAT_WORK_NOTE: "work_note",
            Method.WALLPAPER_ACTIVITY: "activity",
            Method.PROVIDER_RESULT: "provider_result",
        }
        key = key_by_method.get(Method(method) if not isinstance(method, Method) else method)
        if key:
            captured[key].append(dict(params or {}))

    for method in (
        Method.WALLPAPER_CANVAS,
        Method.CHAT_WORK_NOTE,
        Method.WALLPAPER_ACTIVITY,
        Method.PROVIDER_RESULT,
    ):
        bus.on(method, capture)

    work_activity = WorkActivityCoordinator()
    work_activity.configure()
    provider = ProviderHandler()

    with local_site() as url:
        first = await run_provider_and_wait(
            provider,
            {
                "provider": "browser",
                "task": f"Open the continuity test page: {url}",
                "mode": "open",
                "metadata": {
                    "source": "llm_delegate",
                    "session_id": session_id,
                    "browser_action": "open",
                    "browser_mode": "open",
                    "url": url,
                    "max_branch_actions": 0,
                },
            },
        )
        assert first["status"] == "done", first
        assert "provider_branch" not in first["metadata"], first
        first_browser = first["metadata"]["browser"]
        browser_session_id = first_browser["browser_session_id"]
        assert browser_session_id, first
        assert first_browser["current_url"].endswith("/index.html"), first

        first_canvas = [item for item in captured["canvas"] if item.get("mode") == "browser"]
        assert first_canvas, captured["canvas"]
        assert first_canvas[-1].get("browserSessionId") == browser_session_id, first_canvas[-1]
        assert first_canvas[-1].get("pageTitle") == "Continuity Start", first_canvas[-1]

        active_context = render_active_provider_context(session_id=session_id)
        assert "Transient active provider context" in active_context, active_context
        assert browser_session_id in active_context, active_context
        assert "Continuity Start" in active_context, active_context
        assert HIDDEN_DOM_SENTINEL not in active_context, active_context

        system_prompt = augment_system_prompt_with_active_provider_context(
            "You are Kurisu. Use provider context only when it helps the next user turn.",
            session_id=session_id,
        )
        assert browser_session_id in system_prompt, system_prompt
        assert "prefer the browser provider/session" in system_prompt, system_prompt
        assert HIDDEN_DOM_SENTINEL not in system_prompt, system_prompt

        second = await run_provider_and_wait(
            provider,
            {
                "provider": "browser",
                "task": "Continue on the current page and open the link titled Next section.",
                "mode": "click_text",
                "metadata": {
                    "source": "llm_delegate",
                    "session_id": session_id,
                    "browser_action": "click_text",
                    "browser_mode": "click_text",
                    "browser_session_id": browser_session_id,
                    "text": "Next section",
                    "max_branch_actions": 3,
                },
            },
        )
        assert second["status"] == "done", second
        second_browser = second["metadata"]["browser"]
        assert second_browser["browser_session_id"] == browser_session_id, second
        assert second_browser["current_url"].endswith("/next.html"), second
        assert second_browser["page_title"] == "Continuity Detail", second

        branch = second["metadata"].get("provider_branch") or {}
        actions = branch.get("actions") or []
        assert any(action.get("action") == "click_ref" for action in actions), branch
        assert branch.get("hidden_message_count", 0) > 0, branch
        assert HIDDEN_DOM_SENTINEL not in second["result"], second["result"]

        branch_store_path = Path(str(branch.get("branch_store_path") or ""))
        assert branch_store_path.exists(), branch
        branch_payload = branch_store_path.read_text(encoding="utf-8")
        assert HIDDEN_DOM_SENTINEL in branch_payload, branch_store_path

        browser_canvases = [item for item in captured["canvas"] if item.get("mode") == "browser"]
        assert browser_canvases[-1].get("browserSessionId") == browser_session_id, browser_canvases[-1]
        assert browser_canvases[-1].get("pageTitle") == "Continuity Detail", browser_canvases[-1]
        assert browser_canvases[-1].get("screenshot", "").startswith("data:image/png;base64,"), browser_canvases[-1]

        reopened = await run_provider_and_wait(
            provider,
            {
                "provider": "browser",
                "task": "Reopen the start page in the same browser session.",
                "mode": "open",
                "metadata": {
                    "source": "llm_delegate",
                    "session_id": session_id,
                    "browser_action": "open",
                    "browser_mode": "open",
                    "browser_session_id": browser_session_id,
                    "url": url,
                    "max_branch_actions": 0,
                },
            },
        )
        assert reopened["status"] == "done", reopened
        assert reopened["metadata"]["browser"]["browser_session_id"] == browser_session_id, reopened
        assert reopened["metadata"]["browser"]["current_url"].endswith("/index.html"), reopened

        searched = await run_provider_and_wait(
            provider,
            {
                "provider": "browser",
                "task": "Type Amadeus in the search box on the current page, then click the Search button.",
                "mode": "click_text",
                "metadata": {
                    "source": "llm_delegate",
                    "session_id": session_id,
                    "browser_action": "click_text",
                    "browser_mode": "click_text",
                    "browser_session_id": browser_session_id,
                    "text": "Search",
                    "max_branch_actions": 3,
                },
            },
        )
        assert searched["status"] == "done", searched
        searched_browser = searched["metadata"]["browser"]
        assert searched_browser["browser_session_id"] == browser_session_id, searched
        assert searched_browser["current_url"].endswith("/search.html?q=Amadeus"), searched
        assert searched_browser["page_title"] == "Search Results", searched
        search_branch = searched["metadata"].get("provider_branch") or {}
        search_actions = search_branch.get("actions") or []
        assert any(action.get("action") == "fill_ref" for action in search_actions), search_branch

        assert any(item.get("activity") == "work" for item in captured["activity"]), captured["activity"]
        assert captured["activity"][-1].get("activity") == "", captured["activity"]
        assert len(captured["provider_result"]) >= 4, captured["provider_result"]

        await runtime.cancel(str(searched["run_id"]))

    adapter = runtime.get_adapter("browser")
    shutdown = getattr(adapter, "shutdown", None)
    if callable(shutdown):
        await shutdown()

    print("main browser continuous scene smoke ok")
    print("browser_session_id:", browser_session_id)
    print("first title:", first_browser.get("page_title") or first_browser.get("title"))
    print("second title:", second_browser.get("page_title") or second_browser.get("title"))
    print("branch actions:", actions)
    print("search title:", searched_browser.get("page_title") or searched_browser.get("title"))
    print("search actions:", search_actions)


if __name__ == "__main__":
    asyncio.run(main())
