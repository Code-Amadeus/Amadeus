"""Loopback HTTP adapter for reader extensions and plugins."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import traceback
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .session import ReadingSessionStore


_MAX_BODY = 32 * 1024 * 1024


class ReadingEventServer:
    """Small localhost-only event receiver; the Host owns process lifecycle."""

    def __init__(
        self,
        store: ReadingSessionStore,
        *,
        host: str = "127.0.0.1",
        port: int = 17878,
        comic_handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ):
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("reading adapter must bind to a loopback address")
        self.store = store
        self.comic_handler = comic_handler
        self.host = host
        self.port = int(port)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def bound_port(self) -> int:
        if self._server is None:
            return self.port
        return int(self._server.server_address[1])

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> int:
        if self._server is not None:
            return self.bound_port
        store = self.store
        comic_handler = self.comic_handler

        class Handler(BaseHTTPRequestHandler):
            server_version = "AmadeusReadingAdapter/1.0"

            def log_message(self, *_args: object) -> None:
                return

            def _reply(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(int(status))
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json_body(self) -> dict[str, Any]:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError as error:
                    raise ValueError("invalid content length") from error
                if not 0 < length <= _MAX_BODY:
                    raise ValueError("invalid request size")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("expected a JSON object")
                return payload

            def do_GET(self) -> None:
                target = urlsplit(self.path)
                if target.path == "/health":
                    self._reply(HTTPStatus.OK, {"ok": True})
                    return
                if target.path == "/reading/session":
                    book_id = str(parse_qs(target.query).get("book_id", [""])[0]).strip()
                    if not book_id:
                        self._reply(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "book_id is required"})
                        return
                    context = store.get_context(book_id)
                    if context is None:
                        self._reply(HTTPStatus.NOT_FOUND, {"ok": False, "error": "session not found"})
                        return
                    self._reply(HTTPStatus.OK, {"ok": True, "context": context.to_dict()})
                    return
                self._reply(HTTPStatus.NOT_FOUND, {"ok": False, "error": "unknown endpoint"})

            def do_POST(self) -> None:
                target = urlsplit(self.path)
                try:
                    payload = self._json_body()
                    if target.path == "/reading/event":
                        context = store.update_from_event(payload)
                        self._reply(HTTPStatus.OK, {"ok": True, "context": context.to_dict()})
                        return
                    if target.path == "/comic/chapter":
                        if comic_handler is None:
                            raise ValueError("comic chapter handler is unavailable")
                        result = comic_handler(payload)
                        self._reply(HTTPStatus.OK, result if isinstance(result, dict) else {"ok": True})
                        return
                    if target.path == "/reading/turn":
                        book_id = str(payload.get("book_id") or "").strip()
                        if not book_id:
                            raise ValueError("book_id is required")
                        chunks = payload.get("referenced_chunks") or ()
                        if not isinstance(chunks, list):
                            raise ValueError("referenced_chunks must be a list")
                        store.append_turn(
                            book_id,
                            user_message=str(payload.get("user_message") or ""),
                            assistant_message=str(payload.get("assistant_message") or ""),
                            selected_excerpt=str(payload.get("selected_excerpt") or ""),
                            referenced_chunks=[str(item) for item in chunks],
                        )
                        self._reply(HTTPStatus.OK, {"ok": True})
                        return
                except Exception as error:
                    traceback.print_exc()
                    self._reply(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(error)})
                    return
                self._reply(HTTPStatus.NOT_FOUND, {"ok": False, "error": "unknown endpoint"})

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="reading-event-server",
            daemon=True,
        )
        self._thread.start()
        return self.bound_port

    def stop(self) -> None:
        server = self._server
        self._server = None
        if server is None:
            return
        server.shutdown()
        server.server_close()