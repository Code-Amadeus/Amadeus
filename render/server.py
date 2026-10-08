"""render/server.py — 本地静态资产 HTTP 服务器

用途：QWebEngineView 加载 http://127.0.0.1:{port}/render/web/index.html，
同时使所有项目文件（图片、模型等）可从浏览器上下文访问。
"""
import datetime
import email.utils
import http.server
import json
import logging
import mimetypes
import os
import threading
import socket
import sys
import time
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

    protocol_version = "HTTP/1.1"

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
    def __init__(self, *args, **kwargs):
        self._connections: set[socket.socket] = set()
        self._connections_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def get_request(self):
        request, address = super().get_request()
        with self._connections_lock:
            self._connections.add(request)
        return request, address

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        finally:
            with self._connections_lock:
                self._connections.discard(request)

    def server_close(self):
        super().server_close()
        with self._connections_lock:
            connections = tuple(self._connections)
        # Closing the listener alone leaves keep-alive handlers waiting for
        # another request. Wake them when this display lifetime ends.
        for request in connections:
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            request.close()

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

    def __init__(self, root: Path, start_port: int = 17777, *, texture_cache: bool = False):
        self.root = Path(root)
        self.start_port = start_port
        self.port: int = -1
        self._server: http.server.HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._static_mounts: dict[str, Path] = {}
        self._static_files: dict[str, Path] = {}
        # 动态路由表：路径 → 返回 dict 的 callable（序列化为 JSON 响应）
        self._dynamic_routes: dict[str, object] = {}
        self._texture_cache_enabled = texture_cache
        self._texture_cache = None
        self._texture_cache_retry_at = 0.0
        self._cache_lock = threading.Lock()

    def _get_texture_cache(self):
        with self._cache_lock:
            if (self._texture_cache_enabled and self._texture_cache is None
                    and time.monotonic() >= self._texture_cache_retry_at):
                from render.texture_cache import TextureDiskCache
                try:
                    self._texture_cache = TextureDiskCache(self.root / "render/web/vendor")
                except OSError:
                    self._texture_cache_retry_at = time.monotonic() + 30
                    logging.getLogger(__name__).warning("BC7 disk cache unavailable; using source textures", exc_info=True)
            return self._texture_cache

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

    def mount_files(self, prefix: str, files: dict[str, Path]) -> None:
        """Replace a mount with only the approved model references or SDK file."""
        clean = "/" + prefix.strip("/") + "/"
        for path in tuple(self._static_files):
            if path.startswith(clean):
                del self._static_files[path]
        for relative, target in files.items():
            self._static_files[clean + relative] = Path(target).resolve()

    # ------------------------------------------------------------------

    def start(self) -> int:
        """启动服务器并返回实际监听端口。"""
        handler = _make_handler(self.root, self._dynamic_routes, self._static_mounts, self._get_texture_cache, self._static_files)
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

def _make_handler(root: Path, dynamic_routes: dict | None = None, static_mounts: dict | None = None, texture_cache=None, static_files: dict | None = None):
    """工厂：创建固定 directory 的 handler 类（避免 os.chdir）。

    dynamic_routes: {path: callable}，callable 无参，返回可 JSON 序列化的对象。
    动态路由优先于静态文件匹配；path 不含查询字符串。
    """
    root_str = str(root)
    routes = dynamic_routes if dynamic_routes is not None else {}
    mounts = static_mounts if static_mounts is not None else {}
    files = static_files if static_files is not None else {}

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
            # Dynamic routes retain GET-only semantics; static GET and HEAD
            # share the same opened-file metadata and security boundary.
            bare = urllib.parse.urlsplit(self.path).path
            fn = routes.get(bare)
            if fn is None:
                super().do_GET()
                return
            if self._reject_untrusted_host():
                return
            if blocked_path(bare):
                self.send_error(404)
                return
            try:
                body = json.dumps(fn(), ensure_ascii=False).encode("utf-8")
            except Exception:
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            # Derived writes have a separate, narrow capability. They cannot select
            # a filesystem path, create assets, or use cross-origin ambient access.
            from render.texture_cache import KEY, MAX_BODY_BYTES
            self.close_connection = True
            if self._reject_untrusted_host():
                return
            origin = self.headers.get("Origin", "")
            if origin != f"http://{self.headers.get('Host')}":
                self.send_error(403)
                return
            key = self.path.removeprefix("/_texture-cache/")
            if not self.path.startswith("/_texture-cache/") or not KEY.fullmatch(key):
                self.send_error(404)
                return
            cache = texture_cache() if texture_cache else None
            import secrets
            if cache is None or not secrets.compare_digest(self.headers.get("X-Amadeus-Cache-Token", ""), cache.token):
                self.send_error(403)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if self.headers.get("Transfer-Encoding") or not 4 < size <= MAX_BODY_BYTES:
                    raise ValueError("Invalid body size")
                self.connection.settimeout(10)
                body = self.rfile.read(size)
                if len(body) != size:
                    raise ValueError("Incomplete cache upload")
                cache.publish(key, body)
            except (ValueError, KeyError, TypeError) as error:
                self.send_error(400, str(error))
                return
            except OSError:
                self.send_error(503)
                return
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def send_head(self):
            if self._reject_untrusted_host():
                return None
            bare = urllib.parse.urlsplit(self.path).path
            if blocked_path(bare):
                self.send_error(404)
                return None
            base = Path(root_str).resolve()
            rel = urllib.parse.unquote(bare.lstrip("/"))
            for prefix, mount_root in mounts.items():
                if not bare.startswith(prefix):
                    continue
                rel = urllib.parse.unquote(bare[len(prefix):].lstrip("/"))
                if blocked_path(rel):
                    self.send_error(404)
                    return None
                base = Path(mount_root).resolve()
                break
            else:
                # SimpleHTTPRequestHandler previously rejected this root-file
                # URL even though Path normalizes away its trailing slash.
                if bare.endswith("/"):
                    self.send_error(404)
                    return None
            target = files.get(urllib.parse.unquote(bare), (base / rel).resolve())
            if urllib.parse.unquote(bare) in files:
                base = target.parent
            if (
                (base != target and base not in target.parents)
                or not target.is_file()
            ):
                self.send_error(404)
                return None
            try:
                source = target.open("rb")
            except OSError:
                self.send_error(404)
                return None

            try:
                metadata = os.fstat(source.fileno())
                cache_info = None
                cached = False
                # Original representation remains available for unsupported GPUs
                # and for one explicit repair after a corrupt derived response.
                wants_cache = "application/x-amadeus-bc7" in self.headers.get("Accept", "")
                cache = texture_cache() if texture_cache and wants_cache and target.suffix.lower() == ".ktx2" else None
                if cache is not None:
                    try:
                        identity = cache.identify(source, target, metadata)
                        if identity:
                            cache_info = (identity[0], cache.token)
                            if "source" not in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query):
                                hit = cache.open(identity[0])
                                if hit:
                                    source.close()
                                    source, metadata = hit
                                    cached = True
                    except OSError:
                        source.seek(0)  # A cache read failure must not lose the original representation.
                etag = f'W/"{metadata.st_size:x}-{metadata.st_mtime_ns:x}"'
                not_modified = self._not_modified(metadata, etag)
                self.send_response(304 if not_modified else 200)
                self.send_header("Content-Type", "application/x-amadeus-bc7" if cached else self.guess_type(str(target)))
                if target.suffix.lower() == ".ktx2":
                    self.send_header("Vary", "Accept")
                if cache_info:
                    self.send_header("X-Amadeus-BC7-Key", cache_info[0])
                    self.send_header("X-Amadeus-Cache-Token", cache_info[1])
                # For 304 this is the selected representation's size, not a
                # body length. Both GET and HEAD remain explicitly bodyless.
                self.send_header("Content-Length", str(metadata.st_size))
                self.send_header("ETag", etag)
                self.send_header("Last-Modified", self.date_time_string(metadata.st_mtime))
                self.end_headers()
                if not_modified:
                    source.close()
                    return None
                self._response_size = metadata.st_size
                return source
            except Exception:
                source.close()
                raise

        def _not_modified(self, metadata, etag: str) -> bool:
            condition = self.headers.get("If-None-Match")
            if condition is not None:
                # GET/HEAD use weak comparison, including a client's strong
                # spelling of the same opaque validator. ETag takes precedence
                # even when it does not match the current representation.
                return any(
                    value.strip() == "*"
                    or value.strip().removeprefix("W/") == etag.removeprefix("W/")
                    for value in condition.split(",")
                )
            condition = self.headers.get("If-Modified-Since")
            if condition is None:
                return False
            try:
                modified_since = email.utils.parsedate_to_datetime(condition)
            except (TypeError, IndexError, OverflowError, ValueError):
                return False
            if modified_since.tzinfo is None:
                modified_since = modified_since.replace(tzinfo=datetime.timezone.utc)
            return (
                modified_since.tzinfo is datetime.timezone.utc
                and int(metadata.st_mtime) <= modified_since.timestamp()
            )

        def copyfile(self, source, outputfile):
            # A file can grow or shrink after fstat. Never cross the advertised
            # response boundary, and close on early EOF rather than reuse a
            # connection whose body was shorter than Content-Length.
            remaining = self._response_size
            try:
                while remaining:
                    block = source.read(min(64 * 1024, remaining))
                    if not block:
                        self.close_connection = True
                        return
                    outputfile.write(block)
                    remaining -= len(block)
            except OSError:
                self.close_connection = True
                raise

    return _Handler


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
