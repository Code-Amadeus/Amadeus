from __future__ import annotations

import asyncio
import contextlib
import functools
import http.server
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_host.adapters.browser import BrowserAdapter
from agent_host.provider_types import ProviderEvent, ProviderRunRequest
from render.spriteforge_intent import RANDOM_TRIGGER_ROUTES
from server.ai_os_schema import work_note_payload, work_signal
from server.canvas_action_router import CanvasActionRouter
from server.event_bus import bus
from server.handlers.work_activity_handler import WorkActivityCoordinator
from server.protocol import Method
from server.work_observer import WorkObserverCoordinator


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass


class LocalServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True


@contextlib.contextmanager
def local_site():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "index.html").write_text(
            """
            <!doctype html><meta charset="utf-8">
            <title>Start Page</title>
            <main>
              <h1>Start Page</h1>
              <p>The first browser snapshot for the AI OS workflow contract.</p>
              <a href="/next.html">Next section</a>
            </main>
            """,
            encoding="utf-8",
        )
        (root / "next.html").write_text(
            """
            <!doctype html><meta charset="utf-8">
            <title>Next Page</title>
            <main>
              <h1>Next Page</h1>
              <p>The browser provider reached the second page in the same session.</p>
              <button>Collect evidence</button>
            </main>
            """,
            encoding="utf-8",
        )
        handler = functools.partial(QuietHandler, directory=str(root))
        server = LocalServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = int(server.server_address[1])
            yield f"http://127.0.0.1:{port}/index.html"
        finally:
            server.shutdown()
            server.server_close()


async def main() -> None:
    captured: dict[str, list[dict[str, Any]]] = {
        "canvas": [],
        "activity": [],
        "work_note": [],
        "observer": [],
        "sprite_intent": [],
        "sprite_release": [],
        "chat_token": [],
        "chat_complete": [],
        "subtitle": [],
    }

    async def capture(method: str, params: dict[str, Any]) -> None:
        key_by_method = {
            Method.WALLPAPER_CANVAS: "canvas",
            Method.WALLPAPER_ACTIVITY: "activity",
            Method.CHAT_WORK_NOTE: "work_note",
            Method.CHAT_OBSERVER_DECISION: "observer",
            Method.RENDER_SPRITEFORGE_INTENT: "sprite_intent",
            Method.RENDER_SPRITEFORGE_RELEASE: "sprite_release",
            Method.CHAT_TOKEN: "chat_token",
            Method.CHAT_COMPLETE: "chat_complete",
            Method.RENDER_SUBTITLE: "subtitle",
        }
        key = key_by_method.get(Method(method) if not isinstance(method, Method) else method)
        if key:
            captured[key].append(dict(params or {}))

    for method in (
        Method.WALLPAPER_CANVAS,
        Method.WALLPAPER_ACTIVITY,
        Method.CHAT_WORK_NOTE,
        Method.CHAT_OBSERVER_DECISION,
        Method.RENDER_SPRITEFORGE_INTENT,
        Method.RENDER_SPRITEFORGE_RELEASE,
        Method.CHAT_TOKEN,
        Method.CHAT_COMPLETE,
        Method.RENDER_SUBTITLE,
    ):
        bus.on(method, capture)

    work_activity = WorkActivityCoordinator()
    work_activity.configure()

    output_busy = {"value": True}
    appended: list[dict[str, Any]] = []
    narrated: list[dict[str, Any]] = []
    narration_flushed = asyncio.Event()
    observer_llm_calls: list[dict[str, Any]] = []

    def capture_narration(payload: dict[str, Any]) -> None:
        narrated.append(dict(payload))
        narration_flushed.set()

    async def observer_llm(*, note: dict, notes: list[dict], recent_chat: list[dict]) -> dict:
        observer_llm_calls.append({"note": dict(note), "notes": list(notes), "recent_chat": list(recent_chat)})
        if str(note.get("title") or "") == "Browser interpretation":
            return {
                "action": "progress_note",
                "terminal": False,
                "append_to_main_chat": False,
                "speak": True,
                "display_text": "I kept the browser session open and reached the next page. The canvas has the current page preview.",
                "reason": "meaningful browser progress deserves a short low-priority narration",
            }
        return {
            "action": "silent",
            "terminal": False,
            "append_to_main_chat": False,
            "speak": False,
            "display_text": "",
            "reason": "raw browser mechanics stay on the canvas",
        }

    observer = WorkObserverCoordinator()
    observer.configure(
        is_chat_busy=lambda: False,
        is_tts_busy=lambda: output_busy["value"],
        append_to_main_chat=lambda decision: appended.append(dict(decision)),
        narrate=capture_narration,
        get_recent_chat=lambda session_id: [{"role": "user", "content": "Open this site and keep working on the current page."}],
        observer_llm=observer_llm,
    )

    async def fast_wait_for_output_idle() -> None:
        while output_busy["value"]:
            await asyncio.sleep(0.01)

    observer._wait_for_output_idle = fast_wait_for_output_idle  # type: ignore[method-assign]

    adapter = BrowserAdapter()
    run_index = 0
    browser_runs: list[dict[str, Any]] = []
    injected_interpretation = False

    async def run_browser(params: dict[str, Any]) -> dict[str, Any]:
        nonlocal run_index, injected_interpretation
        run_index += 1
        metadata = dict(params.get("metadata") if isinstance(params.get("metadata"), dict) else {})
        request = ProviderRunRequest(
            provider="browser",
            task=str(params.get("task") or "Browser workflow contract"),
            cwd=params.get("cwd"),
            mode=str(params.get("mode") or metadata.get("browser_action") or "open"),
            metadata=metadata,
        )
        run_id = f"browser_contract_{run_index}"

        async def emit_event(event: ProviderEvent) -> None:
            nonlocal injected_interpretation
            data = event.to_dict()
            data.setdefault("metadata", metadata)
            await bus.emit(Method.PROVIDER_EVENT, data)
            payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
            if (
                not injected_interpretation
                and data.get("type") == "artifact.created"
                and payload.get("artifact_type") == "browser.snapshot"
                and str(payload.get("title") or "") == "Next Page"
            ):
                injected_interpretation = True
                await bus.emit(
                    Method.CHAT_WORK_NOTE,
                    work_note_payload(
                        source="observer_fixture",
                        provider="browser",
                        run_id=run_id,
                        session_id=str(metadata.get("session_id") or ""),
                        phase="Work",
                        title="Browser interpretation",
                        summary="The page changed through a canvas action, and the same browser session is still active.",
                        signals=[
                            work_signal(
                                kind="report",
                                label="report",
                                text="Same browser session reached the second page and remains available for follow-up actions.",
                                importance="important",
                            )
                        ],
                        importance="important",
                        observer_policy="auto",
                        metadata=metadata,
                        speak=False,
                    ),
                )

        await bus.emit(
            Method.PROVIDER_EVENT,
            {
                "provider": "browser",
                "run_id": run_id,
                "type": "run.created",
                "payload": {"task": request.task, "cwd": request.cwd, "mode": request.mode},
                "metadata": metadata,
            },
        )
        await bus.emit(
            Method.PROVIDER_EVENT,
            {
                "provider": "browser",
                "run_id": run_id,
                "type": "run.status",
                "payload": {"status": "running"},
                "metadata": metadata,
            },
        )
        result = await adapter.run(request, run_id, emit_event)
        terminal_type = {
            "done": "run.finished",
            "error": "run.failed",
            "cancelled": "run.cancelled",
        }.get(result.status, "run.finished")
        await bus.emit(
            Method.PROVIDER_EVENT,
            {
                "provider": "browser",
                "run_id": run_id,
                "type": terminal_type,
                "payload": {"status": result.status, "result": result.result, "error": result.error},
                "metadata": result.metadata,
            },
        )
        record = {
            "provider": "browser",
            "run_id": run_id,
            "task": request.task,
            "cwd": request.cwd,
            "status": result.status,
            "result": result.result,
            "error": result.error,
            "metadata": result.metadata,
        }
        await bus.emit(Method.PROVIDER_RESULT, record)
        browser_runs.append(record)
        return record

    router = CanvasActionRouter(provider_run=run_browser)

    with local_site() as url:
        first = await run_browser(
            {
                "provider": "browser",
                "task": f"Open {url}",
                "mode": "open",
                "metadata": {"browser_action": "open", "url": url, "session_id": "browser_contract_session"},
            }
        )
        assert first["status"] == "done", first
        sid = first["metadata"]["browser"]["browser_session_id"]
        assert sid, first
        assert first["metadata"]["browser"]["current_url"].endswith("/index.html"), first

        action_result = await router.route(
            {
                "target": "browser",
                "action": "click_text",
                "text": "Next section",
                "browserSessionId": sid,
            }
        )
        assert action_result["ok"] is True, action_result
        second = browser_runs[-1]
        assert second["status"] == "done", second
        assert second["metadata"]["browser"]["browser_session_id"] == sid, second
        assert second["metadata"]["browser"]["current_url"].endswith("/next.html"), second

        browser_canvas = [item for item in captured["canvas"] if item.get("mode") == "browser"]
        assert browser_canvas, captured["canvas"]
        latest_browser = browser_canvas[-1]
        assert latest_browser.get("browserSessionId") == sid, latest_browser
        assert latest_browser.get("pageTitle") == "Next Page", latest_browser
        assert latest_browser.get("screenshot", "").startswith("data:image/png;base64,"), latest_browser
        assert any(str(link.get("title") or "") == "Next section" for link in browser_canvas[0].get("links") or []), browser_canvas[0]

        assert any(item.get("activity") == "work" for item in captured["activity"]), captured["activity"]
        assert captured["activity"][-1].get("activity") == "", captured["activity"]
        assert any(
            item.get("semantic_label") == "work"
            and item.get("label") in RANDOM_TRIGGER_ROUTES["thinking"]
            for item in captured["sprite_intent"]
        ), captured["sprite_intent"]
        assert any(
            item.get("source") == "work_activity" and item.get("run_id") == second["run_id"]
            for item in captured["sprite_release"]
        ), captured["sprite_release"]

        assert not captured["chat_token"], "provider/browser facts must not stream as main chat tokens"
        assert not captured["chat_complete"], "provider/browser facts must not complete a main chat turn"
        assert not captured["subtitle"], "provider/browser facts must not directly write subtitles"
        assert not appended, "progress narration must not append to main chat history"
        assert not narrated, "observer narration should wait behind busy TTS"

        output_busy["value"] = False
        assert observer._queue is not None
        await observer._queue.join()
        await asyncio.wait_for(narration_flushed.wait(), timeout=5.0)
        assert len(narrated) == 1, narrated
        assert narrated[0]["source"] == "work_observer", narrated
        assert narrated[0]["action"] in {"progress_note", "speak"}, narrated
        assert narrated[0]["terminal"] is False, narrated
        assert not appended, "non-terminal observer narration should stay out of main chat history"
        assert observer_llm_calls and observer_llm_calls[-1]["recent_chat"], observer_llm_calls

        await adapter.cancel(second["run_id"])

    if observer._worker is not None:
        observer._worker.cancel()
        try:
            await observer._worker
        except asyncio.CancelledError:
            pass
    print("ai os browser workflow contract smoke ok")


if __name__ == "__main__":
    asyncio.run(main())
