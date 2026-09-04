# -*- coding: utf-8 -*-
"""Web wallpaper bridge host for the Amadeus desktop scene.

Wallpaper hosts such as Wallpaper Engine or Lively own desktop integration.
This bridge only serves the existing Pixi wallpaper page and streams
Python-side runtime calls to it.
"""
from __future__ import annotations

import http.server
import hashlib
import json
import logging
import os
import queue
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from config.asset_paths import SPRITEFORGE_RUNTIME_ROOT
from config.settings import WALLPAPER_SFX_GATE_LOG, WALLPAPER_WHEEL_FORWARD
from render.server import AssetServer
from wallpaper.scene_assets import (
    _PROJECT_ROOT,
    _PROJECT_AMBIENT_HIGH,
    _PROJECT_AMBIENT_LOW,
    _PROJECT_SUBTITLE_FRAME,
    _asset_url,
    _crt_bounds_norm,
    _load_crt_config,
    _load_wallpaper_ui_config,
    _prepare_background_asset,
    _prepare_scenario_payload,
)

if TYPE_CHECKING:
    from wallpaper.pointer_wheel_forwarder import PointerWheelForwarder

logger = logging.getLogger(__name__)

_UNSAFE_CANVAS_OPEN_SUFFIXES = frozenset(
    {
        ".appref-ms", ".bat", ".bash", ".chm", ".cmd", ".com", ".cpl",
        ".exe", ".gadget", ".hta", ".htm", ".html", ".jar", ".js", ".jse",
        ".lnk", ".msi", ".msp", ".pl", ".ps1", ".psm1", ".py", ".pyw",
        ".rb", ".reg", ".scr", ".sh", ".svg", ".url", ".vbe", ".vbs",
        ".wsf", ".wsh", ".xhtml", ".zsh",
    }
)

_SPRITEFORGE_RUNTIME_ROOT = SPRITEFORGE_RUNTIME_ROOT

_WALLPAPER_CLIENT_ASSETS = (
    _PROJECT_ROOT / "wallpaper" / "lively" / "index.html",
    _PROJECT_ROOT / "render" / "web" / "wallpaper_engine.html",
    _PROJECT_ROOT / "render" / "web" / "wallpaper_engine_bridge.js",
    _PROJECT_ROOT / "render" / "web" / "electron_slice.html",
    _PROJECT_ROOT / "render" / "web" / "electron_slice_host.js",
    _PROJECT_ROOT / "render" / "web" / "crt_canvas_surface.js",
    _PROJECT_ROOT / "render" / "web" / "wallpaper_scene.js",
    _PROJECT_ROOT / "render" / "web" / "renderer.js",
)


def _wallpaper_asset_revision() -> str:
    """Return a cheap revision for scripts already loaded by a wallpaper page."""
    digest = hashlib.sha256()
    for path in _WALLPAPER_CLIENT_ASSETS:
        try:
            stat = path.stat()
            try:
                relative = path.relative_to(_PROJECT_ROOT).as_posix()
            except ValueError:
                relative = path.as_posix()
            digest.update(f"{relative}:{stat.st_mtime_ns}:{stat.st_size}\n".encode("utf-8"))
        except OSError:
            digest.update(f"missing:{path.name}\n".encode("utf-8"))
    return digest.hexdigest()[:16]


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


class _QuietThreadingHTTPServer(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class _BridgeState:
    def __init__(self):
        self.lock = threading.Lock()
        self.clients: list[queue.Queue[dict]] = []
        self.canvas_clients: list[queue.Queue[dict]] = []
        self.bootstrap_calls: list[dict] = []
        self.bootstrap_keys: dict[str, int] = {}
        self.last_calls: dict[str, dict] = {}
        self.action_token = secrets.token_urlsafe(24)
        self.canvas_action_handler: Callable[[dict], dict] | None = None
        self.browser_action_handler: Callable[[dict], dict] | None = None

    def snapshot(self) -> dict:
        with self.lock:
            calls = list(self.bootstrap_calls)
            calls.extend(self.last_calls.values())
        return {"calls": calls}

    def canvas_snapshot(self) -> dict:
        with self.lock:
            canvas = self.last_calls.get("canvas")
            presentation = self.last_calls.get("canvasPresentation")
            attention = self.last_calls.get("attention")
        return {
            "calls": [item for item in (presentation, canvas, attention) if item]
        }

    def add_client(self) -> queue.Queue[dict]:
        q: queue.Queue[dict] = queue.Queue()
        with self.lock:
            self.clients.append(q)
        return q

    def remove_client(self, q: queue.Queue[dict]) -> None:
        with self.lock:
            try:
                self.clients.remove(q)
            except ValueError:
                pass

    def add_canvas_client(self) -> queue.Queue[dict]:
        q: queue.Queue[dict] = queue.Queue()
        with self.lock:
            self.canvas_clients.append(q)
        return q

    def remove_canvas_client(self, q: queue.Queue[dict]) -> None:
        with self.lock:
            try:
                self.canvas_clients.remove(q)
            except ValueError:
                pass

    def publish(self, event: dict, replay: str | None = None) -> None:
        with self.lock:
            if replay:
                self.last_calls[replay] = event
            clients = list(self.clients)
            canvas_clients = (
                list(self.canvas_clients)
                if str(event.get("method") or "")
                in {"setCanvas", "toggleCanvas", "setCanvasPresentation", "setAttention"}
                else []
            )
        for q in clients + canvas_clients:
            try:
                q.put_nowait(event)
            except Exception:
                pass

    def add_bootstrap(self, event: dict, key: str | None = None) -> None:
        with self.lock:
            changed = True
            if key:
                index = self.bootstrap_keys.get(key)
                if index is None:
                    self.bootstrap_keys[key] = len(self.bootstrap_calls)
                    self.bootstrap_calls.append(event)
                else:
                    previous = self.bootstrap_calls[index]
                    changed = (
                        previous.get("method") != event.get("method")
                        or previous.get("args") != event.get("args")
                    )
                    if changed:
                        self.bootstrap_calls[index] = event
            else:
                self.bootstrap_calls.append(event)
            clients = list(self.clients)
        if not changed:
            return
        for q in clients:
            try:
                q.put_nowait(event)
            except Exception:
                pass


def _open_path_default(path: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _show_path_in_folder(path: Path) -> None:
    target = path if path.is_dir() else path.parent
    if sys.platform.startswith("win"):
        if path.exists() and path.is_file():
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            os.startfile(str(target))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])


def _open_with_dialog(path: Path) -> None:
    if sys.platform.startswith("win"):
        subprocess.Popen(["rundll32.exe", "shell32.dll,OpenAs_RunDLL", str(path)])
    else:
        _open_path_default(path)


def _handle_file_action(payload: dict) -> dict:
    action = str(payload.get("action") or "").strip().lower().replace("-", "_")
    raw_path = str(payload.get("path") or "").strip().strip("\"'")
    if action not in {"open", "folder", "open_with"}:
        return {"ok": False, "error": "unsupported_action"}
    if not raw_path or "://" in raw_path:
        return {"ok": False, "error": "invalid_path"}
    if raw_path.startswith(("\\\\", "//")):
        return {"ok": False, "error": "network_path_not_allowed"}

    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        return {"ok": False, "error": "path_must_be_absolute"}
    shell_suffix = Path(path.name.rstrip(" .")).suffix.lower()
    if action in {"open", "open_with"} and shell_suffix in _UNSAFE_CANVAS_OPEN_SUFFIXES:
        return {"ok": False, "error": "unsafe_file_type", "path": str(path)}
    if action in {"open", "open_with"} and not path.exists():
        return {"ok": False, "error": "path_not_found", "path": str(path)}
    if action == "folder" and not (path.exists() or path.parent.exists()):
        return {"ok": False, "error": "folder_not_found", "path": str(path)}

    try:
        if action == "folder":
            _show_path_in_folder(path)
        elif action == "open_with":
            _open_with_dialog(path)
        else:
            _open_path_default(path)
    except Exception as exc:
        logger.warning("[WallpaperBridge] file action failed: %s", exc)
        return {"ok": False, "error": str(exc), "path": str(path)}

    return {"ok": True, "action": action, "path": str(path)}


def _command_bat_path() -> Path:
    root = Path(tempfile.gettempdir()) / "amadeus-command-chips"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"amadeus_cmd_{int(time.time())}_{secrets.token_hex(3)}.bat"


def _make_command_bat(command: str, cwd: str | None = None) -> Path:
    text = str(command or "").strip()
    if not text:
        raise ValueError("empty_command")
    if len(text) > 12000:
        raise ValueError("command_too_long")

    cwd_path: Path | None = None
    if cwd:
        candidate = Path(str(cwd)).expanduser()
        if candidate.is_absolute() and candidate.is_dir():
            cwd_path = candidate

    bat_path = _command_bat_path()
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "title Amadeus command chip",
        "echo [Amadeus] Command chip",
    ]
    if cwd_path is not None:
        lines.append(f'cd /d "{cwd_path}"')
    lines.extend([
        "echo.",
        text,
        "echo.",
        "echo [Amadeus] Command finished with exit code %ERRORLEVEL%.",
    ])
    bat_path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8-sig")
    return bat_path


def _handle_command_action(payload: dict) -> dict:
    action = str(payload.get("action") or "").strip().lower().replace("-", "_")
    command = str(payload.get("command") or "").strip()
    cwd = payload.get("cwd")
    if action != "make_bat":
        return {"ok": False, "error": "unsupported_action"}
    try:
        bat_path = _make_command_bat(command, str(cwd) if cwd else None)
    except Exception as exc:
        logger.warning("[WallpaperBridge] command action failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "action": action, "batPath": str(bat_path)}


def _source_chip_path() -> Path:
    root = Path(tempfile.gettempdir()) / "amadeus-source-chips"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"amadeus_source_{int(time.time())}_{secrets.token_hex(3)}.url"


def _sanitize_source_url(raw_url: str) -> str:
    text = str(raw_url or "").strip()
    if text.lower().startswith("www."):
        text = "https://" + text
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("invalid_url")
    return urllib.parse.urlunparse(parsed)


def _make_source_chip(url: str) -> Path:
    safe_url = _sanitize_source_url(url)
    path = _source_chip_path()
    path.write_text("[InternetShortcut]\r\nURL=" + safe_url + "\r\n", encoding="utf-8-sig")
    return path


def _handle_url_action(payload: dict) -> dict:
    action = str(payload.get("action") or "").strip().lower().replace("-", "_")
    if action not in {"open", "source"}:
        return {"ok": False, "error": "unsupported_action"}
    try:
        url = _sanitize_source_url(str(payload.get("url") or ""))
        if action == "open":
            webbrowser.open(url, new=2, autoraise=True)
            return {"ok": True, "action": action, "url": url}
        source_path = _make_source_chip(url)
        return {"ok": True, "action": action, "url": url, "sourcePath": str(source_path)}
    except Exception as exc:
        logger.warning("[WallpaperBridge] url action failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def _handle_browser_action(state: _BridgeState, payload: dict) -> dict:
    action = str(payload.get("action") or "").strip().lower().replace("-", "_")
    if action not in {"open", "observe", "snapshot", "click_text"}:
        return {"ok": False, "error": "unsupported_action"}
    handler = state.browser_action_handler
    if handler is None:
        return {"ok": False, "error": "browser_action_unavailable"}
    try:
        return handler(payload or {})
    except Exception as exc:
        logger.warning("[WallpaperBridge] browser action failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def _route_canvas_action(state: _BridgeState, target: str, payload: dict) -> dict:
    data = dict(payload or {})
    data["target"] = target
    handler = state.canvas_action_handler
    if handler is not None:
        try:
            result = handler(data)
            result_error = (
                " ".join(str(result.get("error") or "").split())[:240]
                if isinstance(result, dict)
                else ""
            )
            logger.info(
                "[WallpaperBridge] canvas action routed target=%s action=%s "
                "ok=%s error=%s",
                target,
                data.get("action"),
                result.get("ok") if isinstance(result, dict) else None,
                result_error or "-",
            )
            return result
        except Exception as exc:
            logger.warning("[WallpaperBridge] canvas action failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    # Standalone fallback for old bridge-only usage.
    if target == "file":
        return _handle_file_action(data)
    if target == "url":
        return _handle_url_action(data)
    if target == "command":
        return _handle_command_action(data)
    if target == "browser":
        return _handle_browser_action(state, data)
    return {"ok": False, "error": "unsupported_target"}


def _make_bridge_handler(
    state: _BridgeState,
    *,
    allowed_origins: set[str] | frozenset[str] | None = None,
):
    origins = frozenset(str(value).rstrip("/") for value in (allowed_origins or set()))

    class _BridgeHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _headers(self, status=200, content_type="application/json"):
            self.send_response(status)
            origin = str(self.headers.get("Origin") or "").rstrip("/")
            if origin and origin in origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Amadeus-Bridge-Token")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Type", content_type)

        def _origin_authorized(self):
            origin = str(self.headers.get("Origin") or "").rstrip("/")
            # Native health probes and tests do not send Origin. Browser
            # requests do, and must come from this host's exact asset origin.
            return not origin or origin in origins

        def _host_authorized(self):
            raw_host = str(self.headers.get("Host") or "").strip()
            try:
                hostname = urllib.parse.urlsplit("//" + raw_host).hostname
            except ValueError:
                hostname = None
            return str(hostname or "").lower() in {"127.0.0.1", "localhost"}

        def _request_authorization_error(self):
            if not self._host_authorized():
                return "host_not_allowed"
            if not self._origin_authorized():
                return "origin_not_allowed"
            return ""

        def _read_json(self, limit=65536):
            length = min(int(self.headers.get("Content-Length", "0") or 0), limit)
            raw = self.rfile.read(length) if length > 0 else b"{}"
            return json.loads(raw.decode("utf-8", errors="replace"))

        def _json_response(self, payload, status=200):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._headers(status, "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _action_authorized(self):
            return self.headers.get("X-Amadeus-Bridge-Token") == state.action_token

        def _stream_events(self, add_client, remove_client):
            self._headers(200, "text/event-stream; charset=utf-8")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            client = add_client()
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        event = client.get(timeout=15)
                        payload = json.dumps(event, ensure_ascii=False)
                        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                remove_client(client)

        def do_OPTIONS(self):
            auth_error = self._request_authorization_error()
            if auth_error:
                self._json_response({"ok": False, "error": auth_error}, 403)
                return
            self._headers(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            auth_error = self._request_authorization_error()
            if auth_error:
                self._json_response({"ok": False, "error": auth_error}, 403)
                return
            if self.path.startswith("/wallpaper-engine/state") or self.path.startswith("/wallpaper/state"):
                body = json.dumps(state.snapshot(), ensure_ascii=False).encode("utf-8")
                self._headers(200, "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/wallpaper-engine/canvas-state") or self.path.startswith("/wallpaper/canvas-state"):
                body = json.dumps(state.canvas_snapshot(), ensure_ascii=False).encode("utf-8")
                self._headers(200, "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/wallpaper-engine/canvas-events") or self.path.startswith("/wallpaper/canvas-events"):
                self._stream_events(state.add_canvas_client, state.remove_canvas_client)
                return
            if self.path.startswith("/wallpaper-engine/events") or self.path.startswith("/wallpaper/events"):
                self._stream_events(state.add_client, state.remove_client)
                return
            if self.path.startswith("/wallpaper-engine/health") or self.path.startswith("/wallpaper/health"):
                body = b'{"ok":true}'
                self._headers(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._headers(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            auth_error = self._request_authorization_error()
            if auth_error:
                self._json_response({"ok": False, "error": auth_error}, 403)
                return
            if self.path.startswith("/wallpaper-engine/canvas-action") or self.path.startswith("/wallpaper/canvas-action"):
                if not self._action_authorized():
                    logger.warning("[WallpaperBridge] unauthorized canvas action from=%s", self.client_address)
                    self._json_response({"ok": False, "error": "unauthorized"}, 403)
                    return
                try:
                    data = self._read_json()
                    target = str(data.get("target") or data.get("kind") or "").strip().lower().replace("-", "_")
                    result = _route_canvas_action(state, target, data)
                except Exception as exc:
                    logger.warning("[WallpaperBridge] failed to parse canvas action: %s", exc)
                    result = {"ok": False, "error": "bad_request"}
                self._json_response(result, 200 if result.get("ok") else 400)
                return
            if self.path.startswith("/wallpaper-engine/file-action") or self.path.startswith("/wallpaper/file-action"):
                if not self._action_authorized():
                    self._json_response({"ok": False, "error": "unauthorized"}, 403)
                    return
                try:
                    result = _route_canvas_action(state, "file", self._read_json())
                except Exception as exc:
                    logger.warning("[WallpaperBridge] failed to parse file action: %s", exc)
                    result = {"ok": False, "error": "bad_request"}
                self._json_response(result, 200 if result.get("ok") else 400)
                return
            if self.path.startswith("/wallpaper-engine/command-action") or self.path.startswith("/wallpaper/command-action"):
                if not self._action_authorized():
                    self._json_response({"ok": False, "error": "unauthorized"}, 403)
                    return
                try:
                    result = _route_canvas_action(state, "command", self._read_json())
                except Exception as exc:
                    logger.warning("[WallpaperBridge] failed to parse command action: %s", exc)
                    result = {"ok": False, "error": "bad_request"}
                self._json_response(result, 200 if result.get("ok") else 400)
                return
            if self.path.startswith("/wallpaper-engine/url-action") or self.path.startswith("/wallpaper/url-action"):
                if not self._action_authorized():
                    self._json_response({"ok": False, "error": "unauthorized"}, 403)
                    return
                try:
                    result = _route_canvas_action(state, "url", self._read_json())
                except Exception as exc:
                    logger.warning("[WallpaperBridge] failed to parse url action: %s", exc)
                    result = {"ok": False, "error": "bad_request"}
                self._json_response(result, 200 if result.get("ok") else 400)
                return
            if self.path.startswith("/wallpaper-engine/browser-action") or self.path.startswith("/wallpaper/browser-action"):
                if not self._action_authorized():
                    self._json_response({"ok": False, "error": "unauthorized"}, 403)
                    return
                try:
                    result = _route_canvas_action(state, "browser", self._read_json())
                except Exception as exc:
                    logger.warning("[WallpaperBridge] failed to parse browser action: %s", exc)
                    result = {"ok": False, "error": "bad_request"}
                self._json_response(result, 200 if result.get("ok") else 400)
                return
            if self.path.startswith("/wallpaper-engine/client-log") or self.path.startswith("/wallpaper/client-log"):
                try:
                    data = self._read_json()
                    level = str(data.get("level") or "info").lower()
                    event = str(data.get("event") or "unknown")
                    client_id = str(data.get("clientId") or "-")
                    payload = data.get("data")
                    msg = "[WEClient:%s] %s %s", client_id, event, json.dumps(payload, ensure_ascii=False)
                    if event == "keyboard_sfx.gate":
                        if WALLPAPER_SFX_GATE_LOG:
                            logger.info(*msg)
                        else:
                            logger.debug(*msg)
                    elif level in {"error", "critical"}:
                        logger.error(*msg)
                    elif level in {"warn", "warning"}:
                        logger.warning(*msg)
                    elif event.startswith("keyboard_sfx.") or event.startswith("pointer_wheel."):
                        logger.info(*msg)
                    # info-level client logs suppressed (debug only)
                except Exception as exc:
                    logger.warning("[WEClient] failed to parse client log: %s", exc)
                self._headers(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._headers(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return _BridgeHandler


class WallpaperEngineBridgeHost:
    """Host compatible with SpriteForgeAnimator, rendered by a web wallpaper host."""

    def __init__(
        self,
        asset_port: int = 17777,
        bridge_port: int = 17797,
        *,
        slice_host: str = "wallpaper",
    ):
        self._asset_server = AssetServer(_PROJECT_ROOT, start_port=asset_port)
        self._asset_port = -1
        self._bridge_port = -1
        self._bridge_start_port = bridge_port
        self._bridge_server: http.server.ThreadingHTTPServer | None = None
        self._bridge_thread: threading.Thread | None = None
        self._state = _BridgeState()
        self._slice_host = "electron" if str(slice_host).strip().lower() == "electron" else "wallpaper"
        self._slice_bounds = _crt_bounds_norm()
        self._ready = False
        self.on_ready: Optional[Callable[[], None]] = None
        self._background_asset = _prepare_background_asset()
        self._wheel_forwarder: PointerWheelForwarder | None = None

    @property
    def url(self) -> str:
        slice_param = "&sliceHost=electron" if self._slice_host == "electron" else ""
        return (
            f"http://127.0.0.1:{self._asset_port}/render/web/wallpaper_engine.html"
            f"?bridgePort={self._bridge_port}&host=webwallpaper{slice_param}"
        )

    @property
    def asset_port(self) -> int:
        return self._asset_port

    @property
    def bridge_port(self) -> int:
        return self._bridge_port

    @property
    def action_token(self) -> str:
        return self._state.action_token

    @property
    def slice_host(self) -> str:
        return self._slice_host

    @property
    def slice_bounds(self) -> dict[str, float]:
        return dict(self._slice_bounds)

    @property
    def asset_version(self) -> str:
        return _wallpaper_asset_revision()

    @property
    def lively_url(self) -> str:
        slice_param = "&sliceHost=electron" if self._slice_host == "electron" else ""
        return (
            f"http://127.0.0.1:{self._asset_port}/wallpaper/lively/index.html"
            f"?assetPort={self._asset_port}&bridgePort={self._bridge_port}{slice_param}"
        )

    def start(self) -> "WallpaperEngineBridgeHost":
        self._asset_port = self._asset_server.start()
        if _SPRITEFORGE_RUNTIME_ROOT.is_dir():
            self._asset_server.mount_static("/spriteforge", _SPRITEFORGE_RUNTIME_ROOT)
        self._start_bridge_server()
        # 向 asset server 注册动态发现接口，让 JS 客户端在启动时查询实际 bridge 端口。
        # 这样即使 bridge 端口因冲突而偏移，客户端也能正确连接，而不依赖 URL 里的硬编码端口。
        self._asset_server.set_dynamic_route(
            "/wallpaper/bridge-info",
            lambda: {
                "bridgePort": self._bridge_port,
                "assetPort": self._asset_port,
                "bridgeToken": self._state.action_token,
                "assetVersion": _wallpaper_asset_revision(),
                "sliceHost": self._slice_host,
                "sliceBounds": self._slice_bounds,
            },
        )
        self._init_scene()
        if self._slice_host != "electron" and WALLPAPER_WHEEL_FORWARD and sys.platform == "win32":
            try:
                from wallpaper.pointer_wheel_forwarder import PointerWheelForwarder

                self._wheel_forwarder = PointerWheelForwarder(
                    lambda dx, dy: self._state.publish(
                        {"method": "pointerWheel", "args": [{"deltaX": dx, "deltaY": dy}]}
                    )
                ).start()
            except Exception:
                logger.exception("[WallpaperBridge] wheel forwarder failed to start")
                self._wheel_forwarder = None
        self._ready = True
        logger.info("[WallpaperBridge] asset server: http://127.0.0.1:%d/", self._asset_port)
        logger.info("[WallpaperBridge] event bridge: http://127.0.0.1:%d/wallpaper/events", self._bridge_port)
        logger.info("[WallpaperBridge] bridge-info: http://127.0.0.1:%d/wallpaper/bridge-info", self._asset_port)
        logger.info("[WallpaperBridge] web wallpaper URL: %s", self.url)
        logger.info("[WallpaperBridge] Lively URL: %s", self.lively_url)
        if callable(self.on_ready):
            self.on_ready()
        return self

    def stop(self) -> None:
        if self._wheel_forwarder is not None:
            try:
                self._wheel_forwarder.stop()
            except Exception:
                logger.exception("[WallpaperBridge] wheel forwarder failed to stop")
            self._wheel_forwarder = None
        if self._bridge_server:
            self._bridge_server.shutdown()
            self._bridge_server.server_close()
            self._bridge_server = None
        if self._bridge_thread and self._bridge_thread.is_alive():
            self._bridge_thread.join(timeout=1.0)
        self._bridge_thread = None
        self._asset_server.stop()

    def hide(self) -> None:
        pass

    def deleteLater(self) -> None:
        self.stop()

    def _start_bridge_server(self) -> None:
        handler = _make_bridge_handler(
            self._state,
            allowed_origins={
                f"http://127.0.0.1:{self._asset_port}",
                f"http://localhost:{self._asset_port}",
            },
        )
        # 端口固定优先：始终尝试首选端口，保证 WE 里配置的 URL 永远有效。
        # 只有首选端口被其他进程占用时才依次往后找。
        preferred = self._bridge_start_port
        candidates = [preferred] + list(range(preferred + 1, preferred + 20))
        for port in candidates:
            if _port_free(port):
                self._bridge_server = _QuietThreadingHTTPServer(("127.0.0.1", port), handler)
                self._bridge_port = port
                if port != preferred:
                    logger.warning(
                        "[WallpaperEngineBridge] preferred port %d busy, using %d — update WE URL if needed",
                        preferred, port,
                    )
                break
        else:
            raise OSError("No free Wallpaper Engine bridge port")
        self._bridge_thread = threading.Thread(
            target=self._bridge_server.serve_forever,
            daemon=True,
            name="WallpaperEngineBridge",
        )
        self._bridge_thread.start()

    def _event(
        self,
        method: str,
        *args,
        replay: str | None = None,
        bootstrap: bool = False,
        bootstrap_key: str | None = None,
    ) -> None:
        event = {"method": method, "args": list(args), "t": time.time()}
        if bootstrap:
            self._state.add_bootstrap(event, key=bootstrap_key)
        else:
            self._state.publish(event, replay=replay)

    def _init_scene(self) -> None:
        bg_url = _asset_url(self._asset_port, self._background_asset) if self._background_asset is not None else ""
        ui_config = _load_wallpaper_ui_config()
        payload = {
            "backgroundUrl": bg_url,
            "ambientLowUrl": _asset_url(self._asset_port, _PROJECT_AMBIENT_LOW) if _PROJECT_AMBIENT_LOW.exists() else "",
            "ambientDeltaUrl": _asset_url(self._asset_port, _PROJECT_AMBIENT_HIGH) if _PROJECT_AMBIENT_HIGH.exists() else "",
            "subtitleFrameUrl": _asset_url(self._asset_port, _PROJECT_SUBTITLE_FRAME) if _PROJECT_SUBTITLE_FRAME.exists() else "",
            "defaultSubtitleEnabled": bool(ui_config.get("defaultSubtitleEnabled", False)),
            "crtConfig": _load_crt_config(),
            "scenario": _prepare_scenario_payload(self._asset_port),
        }
        self._event("initDesktopScene", payload, bootstrap=True, bootstrap_key="initDesktopScene")

    def set_mode(self, mode: str) -> None:
        self._event("setMode", str(mode), replay="mode")

    def set_activity(self, activity: str) -> None:
        self._event("setActivity", str(activity or ""), replay="activity")

    def set_work_mode(self, enabled: bool) -> None:
        self._event("setWorkMode", bool(enabled), replay="workMode")

    def set_asr_status(self, payload: dict | str) -> None:
        if isinstance(payload, dict):
            status_payload = dict(payload)
        else:
            status_payload = {"status": str(payload or "")}
        self._event("setAsrStatus", status_payload, replay="asrStatus")

    def set_idle_animation(self, playing: bool) -> None:
        self._event("setIdleAnimation", bool(playing), replay="idleAnimation")

    def set_emotion(self, emotion: str) -> None:
        self._event("setEmotion", str(emotion), replay="emotion")

    def set_speaking(self, speaking: bool) -> None:
        self._event("setSpeaking", bool(speaking), replay="speaking")

    def set_mouth_value(self, value: float) -> None:
        self._event("setMouth", max(0.0, min(1.0, float(value))))

    def set_subtitle(self, text: str) -> None:
        self._event("setSubtitle", text, replay="subtitle")

    def set_canvas_presentation(self, profile: dict) -> None:
        self._event(
            "setCanvasPresentation",
            dict(profile or {}),
            replay="canvasPresentation",
        )

    def set_default_subtitle_enabled(self, enabled: bool) -> None:
        self._event("setDefaultSubtitleEnabled", bool(enabled), replay="defaultSubtitle")

    def load_sprite_frames(self, emotion: str, frame_urls: list[str]) -> None:
        emotion_key = str(emotion)
        self._event(
            "loadSpriteFrames",
            emotion_key,
            self._rewrite_urls(frame_urls),
            bootstrap=True,
            bootstrap_key=f"loadSpriteFrames:{emotion_key}",
        )

    def load_sprite_clip_frames(
        self,
        emotion: str,
        in_frame_urls: list[str],
        loop_frame_urls: list[str],
        out_frame_urls: list[str],
    ) -> None:
        self._event(
            "loadSpriteClipFrames",
            str(emotion),
            self._rewrite_urls(in_frame_urls),
            self._rewrite_urls(loop_frame_urls),
            self._rewrite_urls(out_frame_urls),
            bootstrap=True,
            bootstrap_key=f"loadSpriteClipFrames:{emotion}",
        )

    def set_sprite_clip_config(self, emotion: str, config: dict) -> None:
        emotion_key = str(emotion)
        self._event(
            "setSpriteClipConfig",
            emotion_key,
            config or {},
            bootstrap=True,
            bootstrap_key=f"setSpriteClipConfig:{emotion_key}",
        )

    def set_idle_frame_interval_ms(self, emotion: str, interval_ms: int) -> None:
        emotion_key = str(emotion)
        self._event(
            "setIdleFrameIntervalMs",
            emotion_key,
            int(interval_ms),
            bootstrap=True,
            bootstrap_key=f"setIdleFrameIntervalMs:{emotion_key}",
        )

    def load_mouth_config(self, emotion: str, config: dict) -> None:
        emotion_key = str(emotion)
        self._event(
            "loadMouthConfig",
            emotion_key,
            self._rewrite_payload(config or {}),
            bootstrap=True,
            bootstrap_key=f"loadMouthConfig:{emotion_key}",
        )

    def load_spriteforge_graph(self, payload: dict) -> None:
        self._event(
            "loadSpriteForgeGraph",
            self._rewrite_payload(payload or {}),
            bootstrap=True,
            bootstrap_key="loadSpriteForgeGraph",
        )

    def trigger_spriteforge_intent(self, label: str, options: dict | None = None) -> None:
        self._event("triggerSpriteForgeIntent", str(label or ""), dict(options or {}))

    def release_spriteforge(self, options: dict | None = None) -> None:
        self._event(
            "releaseSpriteForge",
            dict(options or {}),
            replay="spriteforgeRelease",
        )

    def set_canvas(self, payload: dict) -> None:
        self._event("setCanvas", self._rewrite_canvas_payload(payload or {}), replay="canvas")

    def set_attention(self, payload: dict) -> None:
        self._event("setAttention", dict(payload or {}), replay="attention")

    def toggle_canvas(self) -> None:
        self._event("toggleCanvas")

    def set_canvas_action_handler(self, handler: Callable[[dict], dict] | None) -> None:
        self._state.canvas_action_handler = handler

    def set_browser_action_handler(self, handler: Callable[[dict], dict] | None) -> None:
        self._state.browser_action_handler = handler

    def hold_sprite_frame(self, frame_index: int | None = None) -> None:
        self._event("holdSpriteFrame", frame_index, replay="hold")

    def clear_sprite_hold(self) -> None:
        self._event("clearSpriteHold", replay="hold")

    def _rewrite_urls(self, urls: list[str]) -> list[str]:
        return [self._rewrite_asset_string(url) for url in urls]

    def _rewrite_payload(self, value):
        if isinstance(value, dict):
            return {k: self._rewrite_payload(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._rewrite_payload(v) for v in value]
        if isinstance(value, str):
            return self._rewrite_asset_string(value)
        return value

    def _rewrite_canvas_payload(self, value, *, field: str = ""):
        """Rewrite render assets without turning authority/path data into URLs."""
        semantic_path_fields = {
            "cwd",
            "file",
            "path",
            "scope",
            "scope_paths",
            "scopePaths",
            "source_path",
            "staging_root",
            "target_path",
            "target_root",
            "temporary_path",
            "workspacePath",
            "workspace_path",
            "workspaceFocusPath",
            "workspace_focus_path",
        }
        if field in semantic_path_fields:
            return value
        if isinstance(value, dict):
            return {
                key: self._rewrite_canvas_payload(item, field=str(key))
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._rewrite_canvas_payload(item, field=field) for item in value]
        if isinstance(value, str):
            return self._rewrite_asset_string(value)
        return value

    def _rewrite_asset_string(self, value: str) -> str:
        path = self._local_asset_path(value)
        if path is None:
            return value
        return self._http_asset_url(path) or value

    def _local_asset_path(self, value: str) -> Path | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            if text.startswith("file://"):
                parsed = urllib.parse.urlparse(text)
                return Path(urllib.request.url2pathname(parsed.path)).resolve()
            path = Path(text)
            if path.is_absolute() and path.exists():
                return path.resolve()
        except Exception:
            return None
        return None

    def _http_asset_url(self, path: Path) -> str | None:
        roots = (
            (_PROJECT_ROOT.resolve(), ""),
            (_SPRITEFORGE_RUNTIME_ROOT.resolve(), "spriteforge"),
        )
        for root, prefix in roots:
            try:
                rel = path.resolve().relative_to(root)
            except ValueError:
                continue
            url_path = urllib.parse.quote(rel.as_posix(), safe="/")
            if prefix:
                url_path = f"{prefix}/{url_path}"
            return f"http://127.0.0.1:{self._asset_port}/{url_path}"
        return None
