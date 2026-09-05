from __future__ import annotations

import asyncio
import http.client
import http.server
import json
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from render.server import AssetServer
from server.canvas_action_router import CanvasActionRouter
from wallpaper.wallpaper_engine_bridge import (
    _BridgeState,
    _handle_file_action,
    _make_bridge_handler,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
    finally:
        connection.close()


def test_asset_server_does_not_expose_project_secrets_or_cors() -> None:
    with tempfile.TemporaryDirectory(prefix="asset_server_security_") as temp:
        root = Path(temp)
        (root / "public.js").write_text("window.assetLoaded = true;", encoding="utf-8")
        (root / ".env").write_text("SECRET=do-not-serve", encoding="utf-8")
        (root / "GEMINI_API_KEY.txt").write_text("do-not-serve", encoding="utf-8")
        (root / "nested").mkdir()
        (root / "nested" / "asset.png").write_bytes(b"png")
        mounted = root / "mounted"
        mounted.mkdir()
        (mounted / "frame.png").write_bytes(b"frame")

        server = AssetServer(root, start_port=_free_port())
        server.mount_static("/external", mounted)
        port = server.start()
        try:
            status, headers, body = _request(
                port,
                "GET",
                "/public.js",
                headers={"Origin": "https://evil.example"},
            )
            assert status == 200
            assert body == b"window.assetLoaded = true;"
            assert "access-control-allow-origin" not in headers
            assert headers.get("x-content-type-options") == "nosniff"

            rebound, _, rebound_body = _request(
                port,
                "GET",
                "/public.js",
                headers={"Host": "attacker.example"},
            )
            assert rebound == 421
            assert b"window.assetLoaded" not in rebound_body

            mounted_head, mounted_headers, mounted_body = _request(
                port,
                "HEAD",
                "/external/frame.png",
            )
            assert mounted_head == 200
            assert mounted_headers.get("content-length") == str(len(b"frame"))
            assert mounted_body == b""

            for path in ("/.env", "/GEMINI_API_KEY.txt", "/nested/"):
                blocked, _, blocked_body = _request(port, "GET", path)
                assert blocked == 404, path
                assert b"SECRET=do-not-serve" not in blocked_body
        finally:
            server.stop()


def test_wallpaper_bridge_requires_exact_asset_origin() -> None:
    state = _BridgeState()
    allowed_origin = "http://127.0.0.1:18999"
    handler = _make_bridge_handler(state, allowed_origins={allowed_origin})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, headers, body = _request(
            port,
            "GET",
            "/wallpaper/state",
            headers={"Origin": "https://evil.example"},
        )
        assert status == 403
        assert "access-control-allow-origin" not in headers
        assert json.loads(body)["error"] == "origin_not_allowed"

        rebound_status, _, rebound_body = _request(
            port,
            "GET",
            "/wallpaper/state",
            headers={"Host": "attacker.example"},
        )
        assert rebound_status == 403
        assert json.loads(rebound_body)["error"] == "host_not_allowed"

        status, headers, _ = _request(
            port,
            "OPTIONS",
            "/wallpaper/canvas-action",
            headers={
                "Origin": allowed_origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-Amadeus-Bridge-Token, Content-Type",
            },
        )
        assert status == 200
        assert headers.get("access-control-allow-origin") == allowed_origin
        assert headers.get("access-control-allow-origin") != "*"

        payload = json.dumps(
            {"target": "command", "action": "run_terminal", "command": "whoami"}
        ).encode("utf-8")
        status, _, body = _request(
            port,
            "POST",
            "/wallpaper/canvas-action",
            headers={
                "Origin": allowed_origin,
                "Content-Type": "application/json",
                "X-Amadeus-Bridge-Token": state.action_token,
            },
            body=payload,
        )
        assert status == 400
        assert json.loads(body) == {"ok": False, "error": "unsupported_action"}

        state.chat_submit_handler = lambda payload: {
            "ok": True,
            "turn_id": "wallpaper-turn",
            "text": payload["text"],
        }
        chat_payload = json.dumps({"text": "hello from keyboard"}).encode("utf-8")
        blocked_chat, _, blocked_chat_body = _request(
            port,
            "POST",
            "/wallpaper/chat-action",
            headers={"Origin": allowed_origin, "Content-Type": "application/json"},
            body=chat_payload,
        )
        assert blocked_chat == 403
        assert json.loads(blocked_chat_body) == {"ok": False, "error": "unauthorized"}

        chat_status, _, chat_body = _request(
            port,
            "POST",
            "/wallpaper/chat-action",
            headers={
                "Origin": allowed_origin,
                "Content-Type": "application/json",
                "X-Amadeus-Bridge-Token": state.action_token,
            },
            body=chat_payload,
        )
        assert chat_status == 200
        assert json.loads(chat_body) == {
            "ok": True,
            "turn_id": "wallpaper-turn",
            "text": "hello from keyboard",
        }

        native_status, _, _ = _request(port, "GET", "/wallpaper/health")
        assert native_status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_canvas_router_never_executes_rendered_command_text() -> None:
    async def run() -> None:
        result = await CanvasActionRouter().route(
            {"target": "command", "action": "run_terminal", "command": "whoami"}
        )
        assert result == {"ok": False, "error": "unsupported_action"}

        with tempfile.TemporaryDirectory(prefix="canvas_file_guard_") as temp:
            executable = Path(temp) / "provider-rendered.cmd"
            executable.write_text("whoami\n", encoding="utf-8")
            blocked = await CanvasActionRouter().route(
                {"target": "file", "action": "open", "path": str(executable)}
            )
            assert blocked["ok"] is False
            assert blocked["error"] == "unsafe_file_type"
            fallback = _handle_file_action(
                {"action": "open", "path": str(executable)}
            )
            assert fallback["ok"] is False
            assert fallback["error"] == "unsafe_file_type"
            trailing_dot = await CanvasActionRouter().route(
                {
                    "target": "file",
                    "action": "open",
                    "path": str(executable) + ".",
                }
            )
            assert trailing_dot["ok"] is False
            assert trailing_dot["error"] == "unsafe_file_type"

        network = await CanvasActionRouter().route(
            {
                "target": "file",
                "action": "open",
                "path": r"\\attacker.invalid\share\payload.txt",
            }
        )
        assert network == {"ok": False, "error": "network_path_not_allowed"}

    asyncio.run(run())


def _main() -> None:
    test_asset_server_does_not_expose_project_secrets_or_cors()
    test_wallpaper_bridge_requires_exact_asset_origin()
    test_canvas_router_never_executes_rendered_command_text()
    print("ok: local asset and wallpaper bridges reject cross-site authority")


if __name__ == "__main__":
    _main()
