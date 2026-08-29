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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_host.adapters.browser import BrowserAdapter
from agent_host.provider_types import ProviderEvent, ProviderRunRequest


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
            <title>Start Page</title>
            <main>
              <h1>Start Page</h1>
              <p>The first browser snapshot.</p>
              <a href='/next.html'>Next section</a>
            </main>
            """,
            encoding="utf-8",
        )
        (root / "next.html").write_text(
            """
            <!doctype html><meta charset='utf-8'>
            <title>Next Page</title>
            <main>
              <h1>Next Page</h1>
              <p>The browser provider reached the second page in the same session.</p>
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


async def main() -> None:
    adapter = BrowserAdapter()
    events: list[dict] = []

    async def emit(event: ProviderEvent) -> None:
        events.append(event.to_dict())

    with local_site() as url:
        first = await adapter.run(
            ProviderRunRequest(
                provider="browser",
                task=f"Open {url}",
                mode="open",
                metadata={"browser_action": "open", "url": url},
            ),
            "browser_smoke_1",
            emit,
        )
        assert first.status == "done", first.to_dict()
        sid = first.metadata["browser"]["browser_session_id"]
        assert sid, first.to_dict()
        assert first.metadata["browser"]["current_url"].endswith("/index.html"), first.to_dict()

        second = await adapter.run(
            ProviderRunRequest(
                provider="browser",
                task="Click Next section in the current browser page",
                mode="click_text",
                metadata={
                    "browser_action": "click_text",
                    "browser_session_id": sid,
                    "text": "Next section",
                },
            ),
            "browser_smoke_2",
            emit,
        )
        assert second.status == "done", second.to_dict()
        assert second.metadata["browser"]["browser_session_id"] == sid, second.to_dict()
        assert second.metadata["browser"]["current_url"].endswith("/next.html"), second.to_dict()
        assert any(e["type"] == "tool.result" and e["payload"].get("status") == "reused" for e in events), events
        await adapter.cancel("browser_smoke_2")

    print("browser provider continuity smoke ok")


if __name__ == "__main__":
    asyncio.run(main())
