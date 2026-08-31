"""render/server.py — 本地静态资产 HTTP 服务器

用途：QWebEngineView 加载 http://127.0.0.1:{port}/render/web/index.html，
同时使所有项目文件（图片、模型等）可从浏览器上下文访问。
"""
import http.server
import json
import mimetypes
import threading
import socket
import sys
import urllib.parse
from pathlib import Path

"""Windows resolves MIME types from HKEY_CLASSES_ROOT, and a polluted
``.js`` -> ``text/plain`` entry there silently overrides the stdlib table.
The browser then refuses to execute the renderer bundle and the wallpaper 
page stays black.  Declare the executable types this server owns so the 
response never depends on client machine state.
"""
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("application/wasm", ".wasm")


class _CORSHandler(http.server.SimpleHTTPRequestHandler):
    """Local asset handler with browser-readable secrets kept out of scope.

    Pages and their assets are served from the same loopback origin, so this
    server must not opt arbitrary internet origins into reading the project
    tree.  The historical class name is retained to avoid import churn.
    """

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def log_message(self, *args):  # 静默日志
        pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def list_directory(self, path):
        self.send_error(404)
        return None


class _QuietThreadingHTTPServer(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class AssetServer:
    """以 root 为 document root 启动本地 HTTP 服务器。

    Parameters
    ----------
    root:       服务器根目录（项目根目录）
    start_port: 首选端口，若被占用则自动递增至 start_port+20
    """

    def __init__(self, root: Path, start_port: int = 17777):
        self.root = Path(root)
        self.start_port = start_port
        self.port: int = -1
        self._server: http.server.HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._static_mounts: dict[str, Path] = {}
        # 动态路由表：路径 → 返回 dict 的 callable（序列化为 JSON 响应）
        self._dynamic_routes: dict[str, object] = {}

    def set_dynamic_route(self, path: str, fn) -> None:
        """注册动态 GET 路由。

        fn() 应返回可 JSON 序列化的对象；优先于静态文件匹配。
        path 必须以 '/' 开头（查询字符串会自动剥离）。
        """
        self._dynamic_routes[path] = fn

    def mount_static(self, prefix: str, root: Path | str) -> None:
        """Serve an additional filesystem root under a URL prefix."""
        clean = "/" + prefix.strip("/") + "/"
        self._static_mounts[clean] = Path(root)

    # ------------------------------------------------------------------

    def start(self) -> int:
        """启动服务器并返回实际监听端口。"""
        handler = _make_handler(self.root, self._dynamic_routes, self._static_mounts)
        for p in range(self.start_port, self.start_port + 20):
            if _port_free(p):
                self._server = _QuietThreadingHTTPServer(("127.0.0.1", p), handler)
                self.port = p
                break
        else:
            raise OSError(f"No free port in [{self.start_port}, {self.start_port+20})")

        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="AssetServer"
        )
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_handler(root: Path, dynamic_routes: dict | None = None, static_mounts: dict | None = None):
    """工厂：创建固定 directory 的 handler 类（避免 os.chdir）。

    dynamic_routes: {path: callable}，callable 无参，返回可 JSON 序列化的对象。
    动态路由优先于静态文件匹配；path 不含查询字符串。
    """
    root_str = str(root)
    routes = dynamic_routes if dynamic_routes is not None else {}
    mounts = static_mounts if static_mounts is not None else {}

    def blocked_path(raw_path: str) -> bool:
        decoded = urllib.parse.unquote(urllib.parse.urlsplit(raw_path).path)
        parts = [part for part in decoded.replace("\\", "/").split("/") if part]
        if any(part in {".", ".."} or part.startswith(".") for part in parts):
            return True
        name = (parts[-1] if parts else "").lower()
        sensitive_markers = (
            "api_key",
            "apikey",
            "auth_token",
            "access_token",
            "credential",
            "password",
            "secret",
        )
        if any(marker in name for marker in sensitive_markers):
            return True
        return Path(name).suffix.lower() in {
            ".db",
            ".key",
            ".pem",
            ".pfx",
            ".p12",
            ".sqlite",
            ".sqlite3",
        }

    class _Handler(_CORSHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=root_str, **kwargs)

        def _host_authorized(self) -> bool:
            raw_host = str(self.headers.get("Host") or "").strip()
            try:
                hostname = urllib.parse.urlsplit("//" + raw_host).hostname
            except ValueError:
                hostname = None
            return str(hostname or "").lower() in {"127.0.0.1", "localhost"}

        def _reject_untrusted_host(self) -> bool:
            if self._host_authorized():
                return False
            self.send_error(421)
            return True

        def do_OPTIONS(self):
            if self._reject_untrusted_host():
                return
            super().do_OPTIONS()

        def do_GET(self):
            if self._reject_untrusted_host():
                return
            # 剥离查询字符串后匹配动态路由
            bare = urllib.parse.urlsplit(self.path).path
            if blocked_path(bare):
                self.send_error(404)
                return
            fn = routes.get(bare)
            if fn is not None:
                try:
                    body = json.dumps(fn(), ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception:
                    self.send_response(500)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                return
            for prefix, mount_root in mounts.items():
                if not bare.startswith(prefix):
                    continue
                rel = urllib.parse.unquote(bare[len(prefix):].lstrip("/"))
                if blocked_path(rel):
                    self.send_error(404)
                    return
                base = Path(mount_root).resolve()
                target = (base / rel).resolve()
                if (base != target and base not in target.parents) or not target.is_file():
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                try:
                    data = target.read_bytes()
                except OSError:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            base = Path(root_str).resolve()
            rel = urllib.parse.unquote(bare.lstrip("/"))
            target = (base / rel).resolve()
            if (
                (base != target and base not in target.parents)
                or not target.is_file()
            ):
                self.send_error(404)
                return
            super().do_GET()

        def do_HEAD(self):
            if self._reject_untrusted_host():
                return
            bare = urllib.parse.urlsplit(self.path).path
            if blocked_path(bare):
                self.send_error(404)
                return
            for prefix, mount_root in mounts.items():
                if not bare.startswith(prefix):
                    continue
                rel = urllib.parse.unquote(bare[len(prefix):].lstrip("/"))
                if blocked_path(rel):
                    self.send_error(404)
                    return
                base = Path(mount_root).resolve()
                target = (base / rel).resolve()
                if (base != target and base not in target.parents) or not target.is_file():
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    mimetypes.guess_type(str(target))[0] or "application/octet-stream",
                )
                self.send_header("Content-Length", str(target.stat().st_size))
                self.end_headers()
                return
            base = Path(root_str).resolve()
            target = (base / urllib.parse.unquote(bare.lstrip("/"))).resolve()
            if (
                (base != target and base not in target.parents)
                or not target.is_file()
            ):
                self.send_error(404)
                return
            super().do_HEAD()

    return _Handler


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
