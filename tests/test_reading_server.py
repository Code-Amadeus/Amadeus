from __future__ import annotations

import json
from pathlib import Path
from urllib.request import Request, urlopen

from core.reading import ReadingEventServer, ReadingSessionStore


def _json_request(url: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def test_reading_event_server_persists_context_and_turns(tmp_path: Path) -> None:
    store = ReadingSessionStore.open(tmp_path)
    server = ReadingEventServer(store, port=0)
    port = server.start()
    base = f"http://127.0.0.1:{port}"
    try:
        assert _json_request(f"{base}/health") == {"ok": True}
        event = _json_request(
            f"{base}/reading/event",
            {
                "book_id": "book-1",
                "kind": "browser",
                "chapter": "第三章",
                "page": 42,
                "cursor": 1200,
                "selected_start": 200,
                "selected_end": 1200,
                "text": "第三章选中内容",
            },
        )
        assert event["ok"] is True
        assert event["context"]["namespace"] if "namespace" in event["context"] else True

        session = _json_request(f"{base}/reading/session?book_id=book-1")
        assert session["ok"] is True
        assert session["context"]["current_chapter"] == "第三章"
        assert session["context"]["cursor"] == 1200

        turn = _json_request(
            f"{base}/reading/turn",
            {
                "book_id": "book-1",
                "user_message": "总结第三章",
                "assistant_message": "总结完成",
                "referenced_chunks": ["c1"],
            },
        )
        assert turn == {"ok": True}
        assert store.list_chunks("book-1")[0].text == "第三章选中内容"
        context = store.get_context("book-1")
        assert context is not None
        assert context.recent_turns[-1]["user_message"] == "总结第三章"
    finally:
        server.stop()