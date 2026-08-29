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

from agent_host.adapters.browser import BrowserAdapter  # noqa: E402
from agent_host.provider_types import ProviderEvent, ProviderRunRequest  # noqa: E402


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
            <title>Action Ref Start</title>
            <main>
              <h1>Action Ref Start</h1>
              <form action="/results.html" method="get">
                <label for="q">Search box</label>
                <input id="q" name="q" placeholder="Search">
                <button type="submit">Search</button>
              </form>
              <a href="/video.html" class="video-card">FGO mainline video</a>
            </main>
            """,
            encoding="utf-8",
        )
        (root / "video.html").write_text(
            "<!doctype html><meta charset='utf-8'><title>Video Page</title><h1>Video Page</h1>",
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
            "browser_refs_1",
            emit,
        )
        assert first.status == "done", first.to_dict()
        sid = first.metadata["browser"]["browser_session_id"]
        refs = adapter._sessions[sid].interaction_refs  # smoke-level contract check
        assert refs, events
        link_ref = next((ref for ref, item in refs.items() if "FGO mainline video" in item.get("label", "")), "")
        input_ref = next((ref for ref, item in refs.items() if item.get("fillable")), "")
        assert link_ref, refs
        assert input_ref, refs

        clicked = await adapter.run(
            ProviderRunRequest(
                provider="browser",
                task="Open the FGO video card by ref",
                mode="click_ref",
                metadata={"browser_action": "click_ref", "browser_session_id": sid, "ref": link_ref},
            ),
            "browser_refs_2",
            emit,
        )
        assert clicked.status == "done", clicked.to_dict()
        assert clicked.metadata["browser"]["current_url"].endswith("/video.html"), clicked.to_dict()

        reopened = await adapter.run(
            ProviderRunRequest(
                provider="browser",
                task=f"Reopen {url}",
                mode="open",
                metadata={"browser_action": "open", "browser_session_id": sid, "url": url},
            ),
            "browser_refs_3",
            emit,
        )
        assert reopened.status == "done", reopened.to_dict()
        input_ref = next((ref for ref, item in adapter._sessions[sid].interaction_refs.items() if item.get("fillable")), "")
        assert input_ref, adapter._sessions[sid].interaction_refs

        filled = await adapter.run(
            ProviderRunRequest(
                provider="browser",
                task="Fill the search box by ref",
                mode="fill_ref",
                metadata={
                    "browser_action": "fill_ref",
                    "browser_session_id": sid,
                    "ref": input_ref,
                    "value": "paxos",
                    "submit": True,
                },
            ),
            "browser_refs_4",
            emit,
        )
        assert filled.status == "done", filled.to_dict()
        assert "/results.html" in filled.metadata["browser"]["current_url"], filled.to_dict()

        await adapter.cancel("browser_refs_4")

    print("browser interaction refs smoke ok")


if __name__ == "__main__":
    asyncio.run(main())
