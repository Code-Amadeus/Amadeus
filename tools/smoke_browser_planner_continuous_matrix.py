"""Real Browser planner matrix against a deterministic local website.

This smoke keeps network/page content deterministic while using the configured
browser-branch LLM and the real Playwright engine. It validates action choice,
session continuity, popup ownership, and conservative no-action behavior.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import http.server
import json
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

from agent_host.adapters.browser_branch import BrowserBranchAdapter  # noqa: E402
from agent_host.provider_runtime import ProviderRuntime  # noqa: E402
from agent_host.provider_types import ProviderRunRequest  # noqa: E402
from server.browser_branch_planner import has_browser_branch_llm_config  # noqa: E402
from server.provider_branch import ProviderBranchStore  # noqa: E402


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:
        pass


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def local_site():
    with tempfile.TemporaryDirectory(prefix="browser_planner_matrix_site_") as tmp:
        root = Path(tmp)
        (root / "index.html").write_text(
            """
            <!doctype html><meta charset="utf-8">
            <title>Planner Matrix Home</title>
            <main>
              <h1>Planner Matrix Home</h1>
              <form action="/results.html" method="get">
                <label for="query">Search this catalog</label>
                <input id="query" name="q" aria-label="Search this catalog" placeholder="Search">
                <button type="submit">Search</button>
              </form>
              <a href="/archive.html">Archive notes</a>
            </main>
            """,
            encoding="utf-8",
        )
        (root / "results.html").write_text(
            """
            <!doctype html><meta charset="utf-8">
            <title>Amadeus Search Results</title>
            <main>
              <h1>Amadeus Search Results</h1>
              <p>Three catalog results are visible.</p>
              <a href="/video-one.html?source=matrix" data-testid="first-video">
                Amadeus architecture overview — primary video result
              </a>
              <a href="/popup-interview.html" target="_blank" data-testid="popup-video">
                Amadeus provider interview — opens in a new tab
              </a>
              <a href="/unrelated.html" data-testid="unrelated-result">
                Unrelated cooking notes
              </a>
            </main>
            """,
            encoding="utf-8",
        )
        (root / "video-one.html").write_text(
            """
            <!doctype html><meta charset="utf-8">
            <title>Amadeus Architecture Video</title>
            <main><h1>Amadeus Architecture Video</h1><p>Primary result detail.</p></main>
            """,
            encoding="utf-8",
        )
        (root / "popup-interview.html").write_text(
            """
            <!doctype html><meta charset="utf-8">
            <title>Amadeus Provider Interview</title>
            <main><h1>Amadeus Provider Interview</h1><p>Popup result detail.</p></main>
            """,
            encoding="utf-8",
        )
        (root / "archive.html").write_text(
            "<!doctype html><meta charset='utf-8'><title>Archive Notes</title>",
            encoding="utf-8",
        )
        (root / "unrelated.html").write_text(
            "<!doctype html><meta charset='utf-8'><title>Cooking Notes</title>",
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


async def run_and_wait(runtime: ProviderRuntime, request: ProviderRunRequest):
    record = await runtime.start(request)
    assert record.task_handle is not None
    await record.task_handle
    assert record.status == "done", record.to_dict()
    return record


def branch_actions(record) -> list[dict[str, Any]]:
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    branch = metadata.get("provider_branch") if isinstance(metadata.get("provider_branch"), dict) else {}
    return [dict(item) for item in (branch.get("actions") or []) if isinstance(item, dict)]


def browser_url(record) -> str:
    return str(record.metadata.get("browser", {}).get("current_url") or "")


def tool_result(record, tool: str) -> dict[str, Any]:
    for event in record.events:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if event.get("type") == "tool.result" and payload.get("tool") == tool:
            return dict(payload)
    return {}


async def branch_turn(
    runtime: ProviderRuntime,
    *,
    session_id: str,
    browser_session_id: str,
    task: str,
    run_name: str,
):
    return await run_and_wait(
        runtime,
        ProviderRunRequest(
            provider="browser",
            task=task,
            mode="observe",
            metadata={
                "source": "llm_delegate",
                "session_id": session_id,
                "provider_branch": True,
                "browser_action": "observe",
                "browser_mode": "observe",
                "browser_session_id": browser_session_id,
                "branch_user_message": task,
                "max_branch_actions": 3,
                "matrix_run_name": run_name,
            },
        ),
    )


async def main() -> None:
    if not has_browser_branch_llm_config():
        print("browser planner continuous matrix skipped: no browser branch LLM config")
        return

    with tempfile.TemporaryDirectory(prefix="browser_planner_matrix_branches_") as branch_root:
        adapter = BrowserBranchAdapter(
            store=ProviderBranchStore(Path(branch_root)),
        )
        runtime = ProviderRuntime()
        runtime.register(adapter)
        session_id = "browser_planner_continuous_matrix"
        records: list[tuple[str, Any]] = []
        try:
            with local_site() as start_url:
                opened = await run_and_wait(
                    runtime,
                    ProviderRunRequest(
                        provider="browser",
                        task=f"Open the planner matrix page: {start_url}",
                        mode="open",
                        metadata={
                            "source": "llm_delegate",
                            "session_id": session_id,
                            "browser_action": "open",
                            "browser_mode": "open",
                            "url": start_url,
                            "max_branch_actions": 0,
                        },
                    ),
                )
                records.append(("open", opened))
                browser_session_id = opened.metadata["browser"]["browser_session_id"]
                assert browser_url(opened).endswith("/index.html"), opened.to_dict()

                searched = await branch_turn(
                    runtime,
                    session_id=session_id,
                    browser_session_id=browser_session_id,
                    task="请在当前页面的站内搜索框搜索 Amadeus。",
                    run_name="search",
                )
                records.append(("search", searched))
                assert [item.get("action") for item in branch_actions(searched)] == ["fill_ref"], searched.to_dict()
                assert "/results.html?q=Amadeus" in browser_url(searched), searched.to_dict()

                opened_first = await branch_turn(
                    runtime,
                    session_id=session_id,
                    browser_session_id=browser_session_id,
                    task=(
                        "观察当前搜索结果。如果存在标题包含 Amadeus architecture overview 的视频，"
                        "打开它；否则只报告当前页面。"
                    ),
                    run_name="open_first",
                )
                records.append(("open_first", opened_first))
                assert [item.get("action") for item in branch_actions(opened_first)] == ["click_ref"], opened_first.to_dict()
                assert "/video-one.html" in browser_url(opened_first), opened_first.to_dict()

                returned = await branch_turn(
                    runtime,
                    session_id=session_id,
                    browser_session_id=browser_session_id,
                    task="返回上一页的搜索结果，不要重新搜索。",
                    run_name="history_back",
                )
                records.append(("history_back", returned))
                assert [item.get("action") for item in branch_actions(returned)] == ["back"], returned.to_dict()
                assert "/results.html?q=Amadeus" in browser_url(returned), returned.to_dict()
                assert tool_result(returned, "browser.back").get("strategy") == "history", returned.to_dict()

                conditional = await branch_turn(
                    runtime,
                    session_id=session_id,
                    browser_session_id=browser_session_id,
                    task=(
                        "检查当前结果。如果存在标题包含 Paxos consensus lecture 的项目就打开；"
                        "如果不存在，只报告当前页面状态，不要搜索或打开其他内容。"
                    ),
                    run_name="conditional_noop",
                )
                records.append(("conditional_noop", conditional))
                mutating = {
                    item.get("action")
                    for item in branch_actions(conditional)
                    if item.get("action") not in {"observe"}
                }
                assert not mutating, conditional.to_dict()
                assert "/results.html?q=Amadeus" in browser_url(conditional), conditional.to_dict()

                popup = await branch_turn(
                    runtime,
                    session_id=session_id,
                    browser_session_id=browser_session_id,
                    task="打开标题为 Amadeus provider interview 的结果；它可能会在新标签页打开。",
                    run_name="popup",
                )
                records.append(("popup", popup))
                assert [item.get("action") for item in branch_actions(popup)] == ["click_ref"], popup.to_dict()
                assert browser_url(popup).endswith("/popup-interview.html"), popup.to_dict()

                popup_back = await branch_turn(
                    runtime,
                    session_id=session_id,
                    browser_session_id=browser_session_id,
                    task="回到打开这个新标签页之前的搜索结果页。",
                    run_name="popup_back",
                )
                records.append(("popup_back", popup_back))
                assert [item.get("action") for item in branch_actions(popup_back)] == ["back"], popup_back.to_dict()
                assert "/results.html?q=Amadeus" in browser_url(popup_back), popup_back.to_dict()
                assert tool_result(popup_back, "browser.back").get("strategy") == "opener", popup_back.to_dict()

                assert all(
                    record.metadata["browser"]["browser_session_id"] == browser_session_id
                    for _, record in records
                ), [(name, record.to_dict()) for name, record in records]

            print("browser planner continuous matrix smoke ok")
            print(
                json.dumps(
                    [
                        {
                            "step": name,
                            "url": browser_url(record),
                            "actions": [item.get("action") for item in branch_actions(record)],
                        }
                        for name, record in records
                    ],
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            await adapter.shutdown()
            await asyncio.sleep(0.1)


if __name__ == "__main__":
    asyncio.run(main())
