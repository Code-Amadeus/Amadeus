"""Forward desktop mouse-wheel input to the wallpaper page.

Wallpaper Engine and Lively forward cursor position and clicks into the
wallpaper's CEF page, but mouse wheel events never reach it -- the desktop
swallows them. This module installs a global WH_MOUSE_LL hook and republishes
wheel deltas over the bridge SSE stream as ``pointerWheel`` calls, gated so a
delta is only forwarded while the window under the cursor is the desktop
layer (WorkerW / Progman). The page synthesizes a DOM WheelEvent at its last
known cursor position, so no screen-to-page coordinate mapping is needed.

The hook callback stays minimal (class-name gate + deque append); publishing
happens on a separate drain thread so global input latency is never tied to
bridge clients.
"""

from __future__ import annotations

import collections
import ctypes
import ctypes.wintypes as wintypes
import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)

_WH_MOUSE_LL = 14
_WM_MOUSEWHEEL = 0x020A
_WM_MOUSEHWHEEL = 0x020E
_WM_QUIT = 0x0012
_GA_ROOT = 2

# Chrome reports roughly 100 CSS px per 120-unit wheel notch; match that so
# the page-side scroll handler sees familiar magnitudes.
_PX_PER_NOTCH = 100.0

_DESKTOP_CLASSES = {"WorkerW", "Progman"}


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


_HOOK_PROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
)


class PointerWheelForwarder:
    """Global wheel hook that publishes deltas through a callback."""

    def __init__(self, publish: Callable[[float, float], None]):
        self._publish = publish
        # Private instance: shared ctypes.windll caches prototypes globally,
        # and every argtype here matters -- on x64 the default int conversion
        # truncates LPARAM/HHOOK and raises OverflowError inside the hook.
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        u = self._user32
        u.SetWindowsHookExW.restype = ctypes.c_void_p
        u.SetWindowsHookExW.argtypes = [
            ctypes.c_int, _HOOK_PROC, wintypes.HINSTANCE, wintypes.DWORD,
        ]
        u.UnhookWindowsHookEx.restype = wintypes.BOOL
        u.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        u.CallNextHookEx.restype = ctypes.c_ssize_t
        u.CallNextHookEx.argtypes = [
            ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM,
        ]
        u.WindowFromPoint.restype = ctypes.c_void_p
        u.WindowFromPoint.argtypes = [wintypes.POINT]
        u.GetAncestor.restype = ctypes.c_void_p
        u.GetAncestor.argtypes = [ctypes.c_void_p, wintypes.UINT]
        u.GetClassNameW.restype = ctypes.c_int
        u.GetClassNameW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR, ctypes.c_int]
        u.GetMessageW.restype = wintypes.BOOL
        u.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), ctypes.c_void_p, wintypes.UINT, wintypes.UINT,
        ]
        u.TranslateMessage.restype = wintypes.BOOL
        u.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        u.DispatchMessageW.restype = ctypes.c_ssize_t
        u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        u.PostThreadMessageW.restype = wintypes.BOOL
        u.PostThreadMessageW.argtypes = [
            wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        ]
        self._hook = None
        self._hook_proc = None  # keep a reference so ctypes does not GC it
        self._hook_thread_id = 0
        self._thread: threading.Thread | None = None
        self._drain_thread: threading.Thread | None = None
        self._pending: collections.deque[tuple[float, float]] = collections.deque(maxlen=256)
        self._wake = threading.Event()
        self._stopping = False
        self._class_buf = ctypes.create_unicode_buffer(64)
        self._seen = 0
        self._gated = 0
        self._forwarded = 0

    # -- gate ---------------------------------------------------------------

    def _cursor_over_desktop(self, pt: wintypes.POINT) -> bool:
        hwnd = self._user32.WindowFromPoint(pt)
        if not hwnd:
            return False
        root = self._user32.GetAncestor(hwnd, _GA_ROOT) or hwnd
        length = self._user32.GetClassNameW(root, self._class_buf, 64)
        if length <= 0:
            return False
        allowed = self._class_buf.value in _DESKTOP_CLASSES
        if not allowed:
            self._gated += 1
            # Bounded diagnostic: the first few rejections name the class so a
            # wallpaper host that wraps the desktop in its own window is
            # discoverable from server.log instead of silently eating wheel.
            if self._gated <= 5 or self._gated % 200 == 0:
                logger.info(
                    "[PointerWheel] wheel gated: cursor root class=%r (gated=%d forwarded=%d)",
                    self._class_buf.value, self._gated, self._forwarded,
                )
        return allowed

    # -- hook thread --------------------------------------------------------

    def _on_mouse(self, n_code, w_param, l_param):
        if n_code >= 0 and w_param in (_WM_MOUSEWHEEL, _WM_MOUSEHWHEEL):
            self._seen += 1
            info = ctypes.cast(l_param, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
            if self._cursor_over_desktop(info.pt):
                self._forwarded += 1
                if self._forwarded <= 5 or self._forwarded % 200 == 0:
                    logger.info(
                        "[PointerWheel] wheel forwarded (seen=%d gated=%d forwarded=%d)",
                        self._seen, self._gated, self._forwarded,
                    )
                # mouseData high word is a signed wheel delta in 120-unit notches.
                raw = ctypes.c_short((info.mouseData >> 16) & 0xFFFF).value
                px = (raw / 120.0) * _PX_PER_NOTCH
                if w_param == _WM_MOUSEWHEEL:
                    # Natural DOM direction: wheel-up (positive raw) scrolls up
                    # (negative deltaY).
                    self._pending.append((0.0, -px))
                else:
                    self._pending.append((px, 0.0))
                self._wake.set()
        return self._user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _run_hook(self):
        kernel32 = ctypes.windll.kernel32
        self._hook_thread_id = kernel32.GetCurrentThreadId()
        self._hook_proc = _HOOK_PROC(self._on_mouse)
        self._hook = self._user32.SetWindowsHookExW(_WH_MOUSE_LL, self._hook_proc, None, 0)
        if not self._hook:
            logger.warning(
                "[PointerWheel] SetWindowsHookExW failed (error=%d); wheel forwarding disabled",
                ctypes.get_last_error(),
            )
            return
        logger.info("[PointerWheel] global wheel hook installed")
        msg = wintypes.MSG()
        while self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            self._user32.TranslateMessage(ctypes.byref(msg))
            self._user32.DispatchMessageW(ctypes.byref(msg))
        self._user32.UnhookWindowsHookEx(self._hook)
        self._hook = None

    # -- drain thread -------------------------------------------------------

    def _run_drain(self):
        while not self._stopping:
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            while self._pending:
                try:
                    dx, dy = self._pending.popleft()
                except IndexError:
                    break
                try:
                    self._publish(dx, dy)
                except Exception:
                    logger.exception("[PointerWheel] publish failed")

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> "PointerWheelForwarder":
        if self._thread is not None:
            return self
        self._stopping = False
        self._thread = threading.Thread(
            target=self._run_hook, name="wallpaper-wheel-hook", daemon=True
        )
        self._drain_thread = threading.Thread(
            target=self._run_drain, name="wallpaper-wheel-drain", daemon=True
        )
        self._thread.start()
        self._drain_thread.start()
        return self

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._hook_thread_id:
            self._user32.PostThreadMessageW(self._hook_thread_id, _WM_QUIT, 0, 0)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        if self._drain_thread and self._drain_thread.is_alive():
            self._drain_thread.join(timeout=1.0)
        self._thread = None
        self._drain_thread = None
