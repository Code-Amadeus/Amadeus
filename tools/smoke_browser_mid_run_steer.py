"""Real Playwright smoke for ChatHandler-to-Browser mid-run steering.

The page is local and deterministic. The first planner call is held open while
two user turns enter through ChatHandler. The stale turn and stale plan must
never surface; the same provider run opens only the newest target.
"""

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
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_host.adapters.browser_branch import BrowserBranchAdapter  # noqa: E402
from agent_host.provider_runtime import ProviderRuntime  # noqa: E402
from agent_host.provider_types import ProviderRunRequest, ProviderSteerRequest  # noqa: E402
from server.event_bus import bus  # noqa: E402
from server.handlers.chat_handler import ChatHandler  # noqa: E402
from server.interaction_branch import (  # noqa: E402
    InteractionBranchCoordinator,
    InteractionBranchState,
)
from server.protocol import Method  # noqa: E402
from server.provider_branch import ProviderBranchStore  # noqa: E402


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:
        pass


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def _local_site():
    with tempfile.TemporaryDirectory(prefix="browser_steer_site_") as tmp:
        root = Path(tmp)
        (root / "index.html").write_text(
            """
            <!doctype html><meta charset="utf-8"><title>Steer Home</title>
            <main>
              <h1>Steer Home</h1>
              <a href="/old.html">Old target</a>
              <a href="/new.html">Newest target</a>
            </main>
            """,
            encoding="utf-8",
        )
        (root / "old.html").write_text(
            "<!doctype html><meta charset='utf-8'><title>Old Target</title>",
            encoding="utf-8",
        )
        (root / "new.html").write_text(
            "<!doctype html><meta charset='utf-8'><title>Newest Target</title>",
            encoding="utf-8",
        )
        port = _free_port()
        server = socketserver.TCPServer(
            ("127.0.0.1", port),
            functools.partial(_QuietHandler, directory=str(root)),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{port}/index.html"
        finally:
            server.shutdown()
            server.server_close()


def _ref_for(context: dict[str, Any], label: str) -> str:
    for item in context.get("interaction_refs") or []:
        if isinstance(item, dict) and label.lower() in str(item.get("label") or "").lower():
            return str(item.get("ref") or "")
    raise AssertionError(f"missing interaction ref: {label}")


async def _wait_for_steer(record, revision: int) -> None:
    for _ in range(400):
        if any(
            item.get("type") == "run.status"
            and item.get("payload", {}).get("stage") == "steer_queued"
            and int(item.get("payload", {}).get("revision") or 0) == revision
            for item in record.events
        ):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"steer revision {revision} was not queued")


async def main() -> None:
    planner_started = asyncio.Event()
    release_stale_planner = asyncio.Event()
    planner_calls: list[str] = []

    async def planner(context: dict[str, Any]) -> dict[str, Any]:
        instruction = str(context.get("latest_user_instruction") or "")
        planner_calls.append(instruction)
        if len(planner_calls) == 1:
            planner_started.set()
            await release_stale_planner.wait()
            return {
                "actions": [
                    {
                        "action": "click_ref",
                        "ref": _ref_for(context, "Old target"),
                        "task": "Open the old target",
                    }
                ],
                "final_report": "Opened the old target.",
            }
        return {
            "actions": [
                {
                    "action": "click_ref",
                    "ref": _ref_for(context, "Newest target"),
                    "task": "Open the newest target",
                }
            ],
            "final_report": "Opened the newest target.",
        }

    with tempfile.TemporaryDirectory(prefix="browser_steer_branches_") as branch_root:
        adapter = BrowserBranchAdapter(
            store=ProviderBranchStore(Path(branch_root)),
            branch_planner=planner,
        )
        runtime = ProviderRuntime()
        runtime.register(adapter)
        try:
            with _local_site() as start_url:
                opened = await runtime.start(
                    ProviderRunRequest(
                        provider="browser",
                        task="Open the steer test page",
                        mode="open",
                        metadata={
                            "browser_action": "open",
                            "url": start_url,
                            "session_id": "browser-mid-run-steer-smoke",
                            "max_branch_actions": 0,
                        },
                    )
                )
                assert opened.task_handle is not None
                await opened.task_handle
                assert opened.status == "done", opened.to_dict()
                browser_session_id = opened.metadata["browser"]["browser_session_id"]

                record = await runtime.start(
                    ProviderRunRequest(
                        provider="browser",
                        task="Open the old target",
                        mode="observe",
                        metadata={
                            "source": "llm_delegate",
                            "provider_branch": True,
                            "browser_action": "observe",
                            "browser_session_id": browser_session_id,
                            "session_id": "browser-mid-run-steer-smoke",
                            "branch_user_message": "Open the old target",
                        },
                    )
                )
                await asyncio.wait_for(planner_started.wait(), timeout=10.0)
                provider_runs: list[dict[str, Any]] = []

                async def provider_run(params):
                    provider_runs.append(dict(params))
                    raise AssertionError("active Browser run was duplicated")

                async def provider_steer(params):
                    return await runtime.steer(
                        str(params.get("run_id") or ""),
                        ProviderSteerRequest(
                            task=str(params.get("task") or ""),
                            revision=int(params.get("revision") or 0),
                            metadata=dict(params.get("metadata") or {}),
                        ),
                    )

                coordinator = InteractionBranchCoordinator(
                    provider_run=provider_run,
                    provider_steer=provider_steer,
                    root=Path(branch_root) / "interaction",
                )
                active_branch = InteractionBranchState(
                    # In production run.created establishes this identity before
                    # ChatHandler can route a continuation. The smoke attaches
                    # after the held run has started, so mirror that identity.
                    branch_id=record.run_id,
                    parent_session_id="browser-mid-run-steer-smoke",
                    provider="browser",
                    status="active",
                    goal="Open a target on the local test page",
                    browser_session_id=browser_session_id,
                    title="Steer Home",
                    url=start_url,
                    active_run_id=record.run_id,
                )
                coordinator._active_by_session[active_branch.parent_session_id] = active_branch

                unexpected_llm: list[str] = []

                async def stream_llm(text, **_kwargs):
                    unexpected_llm.append(str(text))
                    raise AssertionError("same-site continuation reached the main LLM")

                spoken: list[dict[str, Any]] = []

                async def voice_sink(payload):
                    spoken.append(dict(payload))

                handler = ChatHandler()
                handler.configure(
                    stream_llm,
                    asyncio.Queue(),
                    interaction_branch_router=coordinator.try_route_user_message,
                    assistant_voice_sink=voice_sink,
                )
                epoch = 0

                def open_turn(**_kwargs):
                    nonlocal epoch
                    epoch += 1
                    return {"chat_epoch": epoch}

                handler._open_turn = open_turn  # type: ignore[method-assign]
                completed: list[dict[str, Any]] = []
                tokens: list[dict[str, Any]] = []

                async def capture_complete(_method, params):
                    completed.append(dict(params))

                async def capture_token(_method, params):
                    tokens.append(dict(params))

                async def visible(_turn_id: str) -> bool:
                    return True

                bus.on(Method.CHAT_COMPLETE, capture_complete)
                bus.on(Method.CHAT_TOKEN, capture_token)
                try:
                    import agent_host.provider_runtime as runtime_module

                    base_url = start_url.rsplit("/", 1)[0]
                    with (
                        patch.object(runtime_module, "runtime", runtime),
                        patch.object(
                            ChatHandler,
                            "_turn_allows_visible_emit",
                            new=staticmethod(visible),
                        ),
                        patch.object(
                            ChatHandler,
                            "_save_direct_turn",
                            new=staticmethod(lambda **_kwargs: None),
                        ),
                        patch.object(
                            ChatHandler,
                            "_notify_coordinator_finished",
                            new=staticmethod(lambda *_args, **_kwargs: None),
                        ),
                        patch("core.session_manager.set_current_session_id"),
                        patch(
                            "core.chat_runtime.get_chat_runtime",
                            return_value=SimpleNamespace(enable_conversation=False),
                        ),
                    ):
                        await handler.send_text(
                            f"Open {base_url}/old.html",
                            session_id="browser-mid-run-steer-smoke",
                            turn_id="steer-turn-1",
                        )
                        first_task = handler._stream_task
                        assert first_task is not None
                        await _wait_for_steer(record, 1)
                        await handler.send_text(
                            f"Open {base_url}/new.html",
                            session_id="browser-mid-run-steer-smoke",
                            turn_id="steer-turn-2",
                        )
                        second_task = handler._stream_task
                        assert second_task is not None
                        await _wait_for_steer(record, 2)
                        release_stale_planner.set()
                        await asyncio.wait_for(
                            asyncio.gather(first_task, second_task),
                            timeout=30.0,
                        )
                        await asyncio.sleep(0)
                finally:
                    bus.off(Method.CHAT_COMPLETE, capture_complete)
                    bus.off(Method.CHAT_TOKEN, capture_token)

                assert record.status == "done", record.to_dict()

                browser = record.metadata.get("browser") or {}
                branch = record.metadata.get("provider_branch") or {}
                actions = branch.get("actions") or []
                stages = [
                    item.get("payload", {}).get("stage")
                    for item in record.events
                    if item.get("type") == "run.status"
                ]
                assert str(browser.get("current_url") or "").endswith("/new.html"), record.to_dict()
                assert len(actions) == 1 and actions[0].get("instruction_revision") == 2, actions
                assert "steer_queued" in stages and "steer_applied" in stages, stages
                assert len(planner_calls) == 2 and planner_calls[-1].endswith("/new.html"), planner_calls
                assert provider_runs == [] and unexpected_llm == []
                assert [item.get("turn_id") for item in completed] == ["steer-turn-2"]
                completion_text = str(completed[0].get("full_text") or "").lower()
                assert "newest target" in completion_text, completed
                assert "conflict" not in completion_text, completed
                assert tokens and {item.get("turn_id") for item in tokens} == {"steer-turn-2"}
                assert len(spoken) == 1
                print(
                    {
                        "run_id": record.run_id,
                        "planner_calls": planner_calls,
                        "actions": actions,
                        "current_url": browser.get("current_url"),
                        "steer_stages": [stage for stage in stages if stage],
                        "chat_completions": completed,
                        "spoken_count": len(spoken),
                    }
                )
        finally:
            await adapter.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
