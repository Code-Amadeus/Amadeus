"""Bounded, rebuildable BC7 cache. Source identity and publication belong to the host."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import struct
import sys
import threading
import time

MIME = "application/x-amadeus-bc7"
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_BODY_BYTES = MAX_FRAME_BYTES + 8192
MAX_DISK_BYTES = 4 * 1024**3
MIN_FREE_BYTES = 1024**3
KEY = re.compile(r"[0-9a-f]{64}\Z")
KTX2 = b"\xabKTX 20\xbb\r\n\x1a\n"
log = logging.getLogger(__name__)


def default_cache_root() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "Amadeus/texture-cache/bc7-v1"


def parse_container(body: bytes, *, key: str, width: int, height: int) -> dict:
    if not 4 < len(body) <= MAX_BODY_BYTES:
        raise ValueError("Invalid texture cache size")
    size = struct.unpack_from("<I", body)[0]
    if not 0 < size <= 4096 or size + 4 >= len(body):
        raise ValueError("Invalid texture cache header")
    meta = json.loads(body[4:4 + size])
    if (not isinstance(meta, dict) or meta.get("version") != 1 or meta.get("key") != key
            or meta.get("width") != width or meta.get("height") != height
            or meta.get("rawBytes") != width * height
            or not KEY.fullmatch(str(meta.get("rawSha256", "")))):
        raise ValueError("Texture cache identity mismatch")
    payload = body[4 + size:]
    if hashlib.sha256(payload).hexdigest() != meta.get("compressedSha256"):
        raise ValueError("Texture cache checksum mismatch")
    return meta


class TextureDiskCache:
    def __init__(self, vendor: Path, root: Path | None = None, *, max_bytes: int = MAX_DISK_BYTES,
                 min_free_bytes: int = MIN_FREE_BYTES):
        self.root = (root or default_cache_root()).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes
        self.token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(b"amadeus-bc7-v1;basis=6;flags=0;")
        for name in ("basis_transcoder.js", "basis_transcoder.wasm"):
            digest.update((vendor / name).read_bytes())
        self._revision = digest.digest()
        self._sources: dict[Path, tuple] = {}
        self._tickets: dict[str, tuple] = {}
        self._entries: OrderedDict[str, int] = OrderedDict()
        self._lock = threading.RLock()
        # Serialize writers, without making reads wait for compression output or fsync.
        self._write_lock = threading.Lock()
        self._last_scan = 0.0
        self._bytes = 0
        self._scan()
        self._prune(0)

    @staticmethod
    def _regular(path: Path) -> bool:
        try:
            info = path.lstat()
            return stat.S_ISREG(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400)
        except OSError:
            return False

    def _scan(self):
        # Only this cache's flat, content-addressed files. Never follow links or recurse.
        found = {}
        for path in self.root.iterdir():
            if (re.fullmatch(r"[0-9a-f]{64}\.[0-9a-f]{16}\.tmp", path.name)
                    and self._regular(path)):
                try:
                    if time.time() - path.stat().st_mtime > 86400:
                        path.unlink()
                except OSError:
                    pass
            if path.suffix != ".bc7" or not KEY.fullmatch(path.stem) or not self._regular(path):
                continue
            try:
                info = path.stat()
            except FileNotFoundError:
                continue
            found[path.stem] = (info.st_mtime_ns, info.st_size)
        current = self._entries
        self._entries = OrderedDict((key, size) for key, (_, size) in sorted(found.items(), key=lambda item: item[1][0])
                                    if key not in current)
        self._entries.update((key, found[key][1]) for key in current if key in found)
        self._bytes = sum(self._entries.values())
        self._last_scan = time.monotonic()

    def _prune(self, incoming: int, *, keep: str | None = None):
        # One finite pass: locked files remain indexed and charged for later attempts.
        if self._bytes + incoming <= self.max_bytes:
            return
        for key, size in list(self._entries.items()):
            if self._bytes + incoming <= self.max_bytes:
                break
            if key == keep:
                continue
            path = self.root / (key + ".bc7")
            try:
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    continue
                path.unlink()  # One validated cache file; never a directory/recursive deletion.
            except FileNotFoundError:
                pass
            except OSError:
                continue
            del self._entries[key]
            self._bytes -= size

    def _record(self, key: str, size: int):
        self._bytes += size - self._entries.pop(key, 0)
        self._entries[key] = size

    def _same_content(self, key: str, body: bytes) -> bool:
        path = self.root / (key + ".bc7")
        if not self._regular(path):
            return False
        try:
            with path.open("rb") as existing:
                if os.fstat(existing.fileno()).st_size != len(body) or existing.read(len(body) + 1) != body:
                    return False
        except OSError:
            return False
        with self._lock:
            self._record(key, len(body))
        return True

    def identify(self, source, path: Path, info) -> tuple[str, int, int] | None:
        stamp = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        with self._lock:
            old = self._sources.get(path)
            if old and old[0] == stamp:
                return old[1]
        source.seek(0)
        header = source.read(80)
        source.seek(0)
        if len(header) != 80 or header[:12] != KTX2:
            return None
        vk, _, width, height, depth, layers, faces, levels, _ = struct.unpack_from("<9I", header, 12)
        if vk != 0 or depth or layers > 1 or faces != 1 or levels != 1 or not (0 < width <= 4096 and 0 < height <= 4096):
            return None  # Original loader owns other formats/layouts.
        digest = hashlib.sha256(self._revision)
        while block := source.read(256 * 1024):
            digest.update(block)
        source.seek(0)
        result = (digest.hexdigest(), (width + 3) & ~3, (height + 3) & ~3)
        with self._lock:
            self._sources[path] = (stamp, result)
            self._tickets[result[0]] = (path, stamp, result[1], result[2])
        return result

    def open(self, key: str):
        path = self.root / (key + ".bc7")
        with self._lock:
            if not self._regular(path):
                return None
            try:
                source = path.open("rb")
                info = os.fstat(source.fileno())
                if not 4 < info.st_size <= MAX_BODY_BYTES:
                    source.close()
                    return None
                if key in self._entries:
                    self._entries.move_to_end(key)
                return source, info
            except OSError:
                return None

    def publish(self, key: str, body: bytes) -> None:
        with self._write_lock:
            self._publish(key, body)

    def _publish(self, key: str, body: bytes) -> None:
        if not KEY.fullmatch(key):
            raise ValueError("Invalid texture cache key")
        with self._lock:
            ticket = self._tickets.get(key)
        if ticket is None:
            raise ValueError("Unknown texture source")
        path, stamp, width, height = ticket
        # Match the serving descriptor's metadata API. On Windows/Python 3.12,
        # path.stat().st_ctime may be creation time while fstat reports change time.
        with path.open("rb") as source:
            info = os.fstat(source.fileno())
        if stamp != (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns):
            raise ValueError("Texture source changed")
        parse_container(body, key=key, width=width, height=height)
        if len(body) > self.max_bytes:
            raise ValueError("Texture exceeds disk cache budget")
        # An identical winner is already complete, even if another reader has it open.
        if self._same_content(key, body):
            return
        # Leave room for the temporary file as well as the permanent free-space floor.
        if shutil.disk_usage(self.root).free < self.min_free_bytes + len(body):
            raise OSError("Texture cache free-space reserve reached")
        temporary = self.root / (key + "." + secrets.token_hex(8) + ".tmp")
        try:
            with temporary.open("xb") as target:
                target.write(body)
                target.flush()
                os.fsync(target.fileno())
            with self._lock:
                if time.monotonic() - self._last_scan > 30:
                    self._scan()
                old_size = self._entries.get(key, 0)
                self._prune(len(body) - old_size, keep=key)
                if self._bytes + len(body) - old_size > self.max_bytes:
                    raise OSError("Texture cache budget could not be reclaimed")
            if shutil.disk_usage(self.root).free < self.min_free_bytes:
                raise OSError("Texture cache free-space reserve reached")
            try:
                os.replace(temporary, self.root / (key + ".bc7"))
            except OSError:
                # Another process may have published the same bytes after our check.
                if self._same_content(key, body):
                    return
                raise  # Sharing violations / full disks are temporary write failures.
            with self._lock:
                self._record(key, len(body))
        finally:
            temporary.unlink(missing_ok=True)
