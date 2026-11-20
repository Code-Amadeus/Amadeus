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
from core.session_manager import ConversationHistory  # noqa: E402
from server.provider_branch import ProviderBranchStore, apply_branch_merge_to_history  # noqa: E402


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
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
            """
            <!doctype html><meta charset='utf-8'>
            <title>Kurisu Video Portal</title>
            <main>
              <h1>Kurisu Video Portal</h1>
              <form action="/results.html" method="get">
                <label for="q">Search box</label>
                <input id="q" name="q" placeholder="Search">
                <button type="submit">Search</button>
              </form>
              <section aria-label="Recommended videos">
                <a href="/video.html" class="video-card" data-testid="fgo-video">
                  FGO mainline video - Past Chaldea recap
                </a>
                <a href="/other.html" class="video-card">Unrelated lab notes</a>
              </section>
              <script>window.hiddenDomMarker = "RAW_DOM_BRANCH_ONLY_MARKER";</script>
            </main>
            """,
            encoding="utf-8",
        )
        (root / "video.html").write_text(
            """
            <!doctype html><meta charset='utf-8'>
            <title>FGO Video Detail</title>
            <main><h1>FGO Video Detail</h1><p>Video detail page opened.</p></main>
            """,
            encoding="utf-8",
        )
        (root / "other.html").write_text(
            "<!doctype html><meta charset='utf-8'><title>Other Page</title><h1>Other Page</h1>",
            encoding="utf-8",
        )
        (root / "results.html").write_text(
            "<!doctype html><meta charset='utf-8'><title>Search Results</title><h1>Search Results</h1>",
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


async def main() -> None:
    decisions: list[dict[str, Any]] = []

    async def branch_planner(context: dict[str, Any]) -> dict[str, Any]:
        hidden_dom = str(context.get("hidden_context", {}).get("dom") or "")
        assert "RAW_DOM_BRANCH_ONLY_MARKER" in hidden_dom
        refs = list(context.get("interaction_refs") or [])
        target = next(
            (
                item
                for item in refs
                if item.get("kind") == "link" and "FGO mainline video" in str(item.get("label") or "")
            ),
            None,
        )
        assert target, refs
        decision = {
            "assistant_message": "我在浏览器分支里看到了当前页面的可操作对象，会点开 FGO 视频卡片。",
            "actions": [
                {
                    "action": "click_ref",
                    "ref": target["ref"],
                    "label": target.get("label"),
                    "task": "Open the FGO video card from the current browser page.",
                }
            ],
            "final_report": "我已经点开当前页面里的 FGO 视频卡片，canvas 可以继续展示视频详情页。",
            "compact_digest": "Browser branch used full DOM plus interaction refs to choose and open the FGO video card.",
        }
        decisions.append(decision)
        return decision

    with tempfile.TemporaryDirectory() as tmp:
        adapter = BrowserBranchAdapter(
            store=ProviderBranchStore(Path(tmp) / "branches"),
            branch_planner=branch_planner,
        )
        runtime = ProviderRuntime()
        runtime.register(adapter)

        with local_site() as url:
            record = await runtime.start(
                ProviderRunRequest(
                    provider="browser",
                    task="点开当前页面里的 FGO 视频。",
                    mode="open",
                    metadata={
                        "source": "llm_delegate",
                        "session_id": "main_chat_session_real_flow",
                        "browser_action": "open",
                        "browser_mode": "open",
                        "url": url,
                    },
                )
            )
            assert record.task_handle is not None
            await record.task_handle

        assert decisions, "branch planner should run with real page refs"
        assert record.status == "done", record.to_dict()
        assert record.result == "我已经点开当前页面里的 FGO 视频卡片，canvas 可以继续展示视频详情页。"
        assert record.metadata["browser"]["page_title"] == "FGO Video Detail", record.metadata
        assert record.metadata["browser"]["current_url"].endswith("/video.html"), record.metadata

        event_types = [item.get("type") for item in record.events]
        assert "run.created" in event_types, event_types
        assert "run.status" in event_types, event_types
        assert "artifact.created" in event_types, event_types
        assert "run.finished" in event_types, event_types

        branch_meta = record.metadata["provider_branch"]
        assert branch_meta["branch_id"] == record.run_id, branch_meta
        assert branch_meta["status"] == "done", branch_meta
        assert branch_meta["actions"] and branch_meta["actions"][0]["action"] == "click_ref", branch_meta
        assert branch_meta["hidden_message_count"] >= 4, branch_meta

        branch_file = Path(branch_meta["branch_store_path"])
        persisted = json.loads(branch_file.read_text(encoding="utf-8"))
        persisted_text = json.dumps(persisted, ensure_ascii=False)
        assert "RAW_DOM_BRANCH_ONLY_MARKER" in persisted_text
        assert "browser_branch_decision" in persisted_text
        assert "click_ref" in persisted_text

        merge_for_history = {
            "branch_id": branch_meta["branch_id"],
            "parent_session_id": "main_chat_session_real_flow",
            "provider": "browser",
            "status": branch_meta["status"],
            "visible_messages": branch_meta["visible_messages"],
            "hidden_message_count": branch_meta["hidden_message_count"],
            "branch_store_path": branch_meta["branch_store_path"],
            "final_report": branch_meta["final_report"],
            "compact_digest": branch_meta["compact_digest"],
            "artifacts": [],
            "actions": branch_meta["actions"],
            "risks": branch_meta["risks"],
            "next_state": branch_meta["next_state"],
            "created_at": record.created_at,
            "closed_at": record.updated_at,
        }
        history = ConversationHistory(max_rounds=20)
        history.add_user("打开这个视频站。")
        history.add_assistant("页面已经显示在 CRT canvas。")
        apply_branch_merge_to_history(merge_for_history, history)
        transcript = "\n".join(item["content"] for item in history.dialog)
        assert "点开当前页面里的 FGO 视频" in transcript
        assert "我在浏览器分支里看到了当前页面的可操作对象" in transcript
        assert "我已经点开当前页面里的 FGO 视频卡片" in transcript
        assert "RAW_DOM_BRANCH_ONLY_MARKER" not in transcript
        assert "browser_branch_decision" not in transcript

        await adapter.shutdown()
        await asyncio.sleep(0.1)

        print("browser branch provider-runtime flow smoke ok")
        print("record run id:", record.run_id)
        print("record result:", record.result)
        print("browser next state:", record.metadata["browser"])
        print("hidden messages:", branch_meta["hidden_message_count"])
        print("event types:", event_types)


if __name__ == "__main__":
    asyncio.run(main())
