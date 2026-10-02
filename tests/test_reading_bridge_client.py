"""The browser reading client and the loopback adapter must not drift apart.

`browser-extension/amadeus-reading-bridge` is the shipped client for
`core.reading.ReadingEventServer`. These tests pin the boundary between them
without a browser: the allowed request surface, the payload the service worker
builds, and the same payload posted to a real adapter.

Run from the worktree root:

    python -m pytest tests/test_reading_bridge_client.py -q
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from core.reading import ReadingEventServer, ReadingSessionStore

CLIENT_DIR = Path(__file__).resolve().parents[1] / "browser-extension" / "amadeus-reading-bridge"
ADAPTER_PORT = 17878


def _client_source(name: str) -> str:
    path = CLIENT_DIR / name
    assert path.is_file(), f"missing client file: {path}"
    return path.read_text(encoding="utf-8")


@pytest.fixture()
def adapter(tmp_path):
    store = ReadingSessionStore.open(tmp_path / "reading")
    server = ReadingEventServer(store, port=0)
    port = server.start()
    try:
        yield store, port
    finally:
        server.stop()


def _post(port: int, path: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def test_manifest_grants_only_the_adapter_origin_and_the_send_entry_points():
    manifest = json.loads(_client_source("manifest.json"))
    assert manifest["manifest_version"] == 3
    assert manifest["host_permissions"] == [f"http://127.0.0.1:{ADAPTER_PORT}/*"]
    assert set(manifest["permissions"]) == {"contextMenus", "storage", "tabs", "activeTab"}
    # Three ways in: context menu, keyboard shortcut, popup button.
    assert "contextMenus" in manifest["permissions"]
    assert manifest["commands"]["send-selection"]["suggested_key"]["default"] == "Ctrl+Shift+Y"
    assert manifest["action"]["default_popup"] == "popup.html"
    assert manifest["background"]["service_worker"] == "background.js"


def test_service_worker_targets_the_adapter_and_carries_no_credential():
    source = _client_source("background.js")
    assert f"http://127.0.0.1:{ADAPTER_PORT}/reading/event" in source
    # The adapter is loopback-only and unauthenticated; a client must not invent
    # a token header, and must never ship a secret in readable source.
    assert "X-Amadeus-Token" not in source
    assert not re.search(r"(?i)(api[_-]?key|secret|token)\s*[:=]\s*[\"'][^\"']{8,}", source)


def test_payload_matches_the_reading_selection_contract():
    source = _client_source("background.js")
    payload_block = source.split("const payload = {", 1)[1].split("};", 1)[0]
    for field in (
        'type: "reading.selection"',
        'app: "browser"',
        "book_id:",
        "chapter:",
        "cursor:",
        "selected_start:",
        "selected_end:",
        "text",
    ):
        assert field in payload_block, f"payload is missing {field}"


def test_auto_reading_uses_the_start_entrypoint_and_chunks():
    background = _client_source("background.js")
    content = _client_source("content.js")
    popup = _client_source("popup.html")
    assert "AMADEUS_START_READING" in background
    assert "AMADEUS_CAPTURE_READING" in background
    assert "AMADEUS_CAPTURE_READING" in content
    assert "spoiler_cursor:" in background
    assert "chunks" in background
    assert 'id="start"' in popup


def test_auto_reading_payload_keeps_the_next_chapter_behind_the_spoiler_cursor(adapter):
    store, port = adapter
    first = "第一章正文。" * 20
    second = "第二章正文。" * 20
    payload = {
        "type": "reading.selection",
        "app": "browser",
        "kind": "web",
        "book_id": "browser:auto-chapters",
        "chapter": "第一章",
        "cursor": len(first),
        "spoiler_cursor": len(first),
        "selected_start": 0,
        "selected_end": len(first),
        "text": first,
        "chunks": [
            {
                "id": "chapter-0-0-" + str(len(first)),
                "chapter": "第一章",
                "start_offset": 0,
                "end_offset": len(first),
                "text": first,
                "page": None,
            },
            {
                "id": "chapter-1-" + str(len(first)) + "-" + str(len(first) + len(second)),
                "chapter": "第二章",
                "start_offset": len(first),
                "end_offset": len(first) + len(second),
                "text": second,
                "page": None,
            },
        ],
    }
    status, body = _post(port, "/reading/event", payload)
    assert status == 200 and body["ok"] is True
    context = store.get_context("browser:auto-chapters")
    assert context.cursor == len(first)
    assert context.spoiler_cursor == len(first)
    chunks = store.list_chunks("browser:auto-chapters")
    assert len(chunks) == 2
    assert chunks[0].chapter == "第一章"
    assert chunks[1].chapter == "第二章"


def test_known_novel_sites_have_explicit_adapters() -> None:
    source = _client_source("content.js")
    assert "wenku8.net" in source
    assert "linovelib.com" in source
    assert "next_page" in source
    assert "#acontent" in source
    assert "chapterUrls" in source
    assert "isIndexPage" in source


def test_the_content_script_does_not_report_a_pixel_offset_as_a_page():
    """scrollY is a pixel offset; the old client sent it as a page number."""
    source = _client_source("content.js")
    capture_block = source.split("sendResponse({", 1)[1].split("});", 1)[0]
    assert "page: null" in capture_block
    assert "scrollY:" not in capture_block
    assert "page: Math" not in capture_block and "page: window" not in capture_block


def test_omitting_the_page_clears_it_rather_than_keeping_the_old_one(adapter):
    """Documents a real adapter semantic the client has to live with.

    `ReadingSessionStore.update_from_event` reads the page with
    `event.get("page")`, so an absent key is indistinguishable from an explicit
    null and the stored page becomes None. The generic web client therefore has
    no page number on sites it cannot measure; a reader-specific client can
    supply one.
    """
    store, port = adapter
    book = "browser:page-probe"
    _post(port, "/reading/event", {"book_id": book, "chapter": "第一章", "page": 42, "cursor": 10, "text": "a"})
    assert store.get_context(book).current_page == 42

    # The client omits `page` when it has no real page number.
    _post(port, "/reading/event", {"book_id": book, "chapter": "第二章", "cursor": 20, "text": "b"})
    context = store.get_context(book)
    assert context.current_chapter == "第二章"
    assert context.current_page is None, "an omitted page is an unknown page, not the previous one"

    # A client that knows the page keeps it.
    _post(port, "/reading/event", {"book_id": book, "chapter": "第二章", "page": 43, "cursor": 30, "text": "c"})
    assert store.get_context(book).current_page == 43


def test_the_service_worker_payload_reaches_the_adapter(adapter):
    store, port = adapter
    text = "她推开了那扇门。"
    payload = {
        "type": "reading.selection",
        "app": "browser",
        "book_id": "browser:deadbeef",
        "chapter": "第三章",
        "page": 42,
        "cursor": 40 + len(text),
        "selected_start": 40,
        "selected_end": 40 + len(text),
        "text": text,
    }
    status, body = _post(port, "/reading/event", payload)
    assert status == 200 and body["ok"] is True

    context = store.get_context("browser:deadbeef")
    assert context is not None
    assert context.current_chapter == "第三章"
    assert context.current_page == 42
    assert context.cursor == 40 + len(text)
    assert context.selected_start == 40
    assert context.selected_end == 40 + len(text)

    chunks = store.list_chunks("browser:deadbeef")
    assert len(chunks) == 1
    assert chunks[0].text == text
    assert chunks[0].chapter == "第三章"


def test_an_empty_selection_is_rejected_by_the_adapter(adapter):
    """The client blocks this first; the adapter must not store a blank chunk."""
    store, port = adapter
    status, body = _post(port, "/reading/event", {"book_id": "browser:empty", "text": "", "cursor": 10})
    assert status == 200
    assert body["ok"] is True
    assert store.list_chunks("browser:empty") == []

    status, body = _post(port, "/reading/event", {"text": "no book"})
    assert status == 400 and body["ok"] is False


def test_the_adapter_answers_no_cors_preflight(adapter):
    """Documents why the client is an extension and not a page bookmarklet.

    A page fetch with a JSON content type is preflighted; the adapter has no
    OPTIONS handler, so a plain web page cannot post here. The extension works
    because its host permission exempts it from CORS.
    """
    _store, port = adapter
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/reading/event",
        method="OPTIONS",
        headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status = response.status
            headers = response.headers
    except urllib.error.HTTPError as error:
        status = error.code
        headers = error.headers
    assert status >= 400
    assert headers.get("Access-Control-Allow-Origin") is None
