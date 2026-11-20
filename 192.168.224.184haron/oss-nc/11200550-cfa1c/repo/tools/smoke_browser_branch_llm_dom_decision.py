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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_host.adapters.browser_branch import BrowserBranchAdapter  # noqa: E402
from agent_host.provider_runtime import ProviderRuntime  # noqa: E402
from agent_host.provider_types import ProviderRunRequest  # noqa: E402
from server.browser_branch_planner import has_browser_branch_llm_config  # noqa: E402


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
            <!doctype html><meta charset="utf-8">
            <title>Branch Planner Test Portal</title>
            <main>
              <h1>Branch Planner Test Portal</h1>
              <p>Choose the item requested by the user. Do not choose unrelated cards.</p>
              <nav>
                <a href="/archive.html" data-testid="archive-link">Archive notes</a>
                <a href="/past-chaldea.html" data-testid="target-video">
                  FGO mainline video - Past Chaldea recap
                </a>
                <a href="/lab.html" data-testid="lab-link">Unrelated lab memo</a>
              </nav>
              <form action="/search.html" method="get">
                <label for="q">Search box</label>
                <input id="q" name="q" placeholder="Search">
                <button type="submit">Search</button>
              </form>
              <section hidden>RAW_DOM_BRANCH_ONLY_MARKER</section>
            </main>
            """,
            encoding="utf-8",
        )
        (root / "past-chaldea.html").write_text(
            """
            <!doctype html><meta charset="utf-8">
            <title>Past Chaldea Recap</title>
            <main><h1>Past Chaldea Recap</h1><p>The requested video detail page.</p></main>
            """,
            encoding="utf-8",
        )
        (root / "archive.html").write_text(
            "<!doctype html><meta charset='utf-8'><title>Archive Notes</title><h1>Archive Notes</h1>",
            encoding="utf-8",
        )
        (root / "lab.html").write_text(
            "<!doctype html><meta charset='utf-8'><title>Lab Memo</title><h1>Lab Memo</h1>",
            encoding="utf-8",
        )
        (root / "search.html").write_text(
            "<!doctype html><meta charset='utf-8'><title>Search Page</title><h1>Search Page</h1>",
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
    if not has_browser_branch_llm_config():
        print("browser branch llm dom decision smoke skipped: no DeepSeek/OpenAI key configured")
        return

    adapter = BrowserBranchAdapter()
    runtime = ProviderRuntime()
    runtime.register(adapter)
    try:
        with local_site() as url:
            record = await runtime.start(
                ProviderRunRequest(
                    provider="browser",
                    task="请阅读当前页面 DOM，点开标题包含 Past Chaldea 的 FGO 视频，不要点 Archive 或 Lab。",
                    mode="open",
                    metadata={
                        "source": "llm_delegate",
                        "session_id": "main_chat_session_llm_dom",
                        "provider_branch": True,
                        "browser_action": "open",
                        "browser_mode": "open",
                        "url": url,
                        "max_branch_actions": 2,
                    },
                )
            )
            assert record.task_handle is not None
            await record.task_handle

        branch_meta = record.metadata.get("provider_branch") if isinstance(record.metadata, dict) else {}
        branch_text = json.dumps(branch_meta, ensure_ascii=False)
        assert record.status == "done", record.to_dict()
        assert record.metadata["browser"]["page_title"] == "Past Chaldea Recap", record.to_dict()
        assert record.metadata["browser"]["current_url"].endswith("/past-chaldea.html"), record.to_dict()
        assert "click_ref" in branch_text, branch_text
        assert "RAW_DOM_BRANCH_ONLY_MARKER" not in record.result

        branch_file = Path(branch_meta["branch_store_path"])
        persisted = json.loads(branch_file.read_text(encoding="utf-8"))
        persisted_text = json.dumps(persisted, ensure_ascii=False)
        assert "RAW_DOM_BRANCH_ONLY_MARKER" in persisted_text

        print("browser branch llm dom decision smoke ok")
        print("record result:", record.result)
        print("browser next state:", record.metadata["browser"])
        print("branch actions:", branch_meta.get("actions"))
    finally:
        await adapter.shutdown()
        await asyncio.sleep(0.1)


if __name__ == "__main__":
    asyncio.run(main())
