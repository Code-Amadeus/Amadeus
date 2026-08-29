"""Win32 desktop-layer placement shared by wallpaper-capable window hosts."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import sys
import time


_MIN_LIVELY_WIDTH = 320
_MIN_LIVELY_HEIGHT = 180


def _user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowExW.restype = wintypes.HWND
    user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.EnumChildWindows.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.LPARAM]
    user32.EnumChildWindows.restype = wintypes.BOOL
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = wintypes.LPARAM
    user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
    user32.SetParent.restype = wintypes.HWND
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    return user32


def _process_image_name(process_id: int) -> str:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(process_id))
    if not process:
        return ""
    try:
        capacity = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(capacity.value)
        if not kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(capacity)):
            return ""
        return buffer.value
    finally:
        kernel32.CloseHandle(process)


def _usable_lively_window(*, visible: bool, class_name: str, rect: wintypes.RECT) -> bool:
    """Reject Lively helper/place-holder HWNDs that are not the wallpaper surface."""
    width = max(0, int(rect.right) - int(rect.left))
    height = max(0, int(rect.bottom) - int(rect.top))
    return (
        visible
        and class_name.startswith("WindowsForms10.Window")
        and width >= _MIN_LIVELY_WIDTH
        and height >= _MIN_LIVELY_HEIGHT
    )


def _rect_intersection_area(left: wintypes.RECT, right: wintypes.RECT) -> int:
    width = max(0, min(left.right, right.right) - max(left.left, right.left))
    height = max(0, min(left.bottom, right.bottom) - max(left.top, right.top))
    return width * height


def _window_class(user32, hwnd: int) -> str:
    class_name = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(hwnd, class_name, len(class_name))
    return class_name.value


def find_lively_wallpaper_target(reference_hwnd: int = 0) -> tuple[int, int]:
    """Find a visible Lively surface and its real desktop parent.

    Lively can temporarily leave hidden 1x1/14x14 top-level helper windows while
    its real wallpaper HWND is parented under WorkerW/Progman. Both the current
    child-window form and older bottom-ranked top-level form are supported.
    Hidden helpers are not valid Z-order anchors.
    """
    if sys.platform != "win32":
        return 0, 0
    user32 = _user32()
    candidates: list[tuple[int, int, int, int, int]] = []
    process_names: dict[int, str] = {}
    seen: set[int] = set()
    reference_rect = wintypes.RECT()
    has_reference_rect = bool(
        reference_hwnd
        and user32.IsWindow(int(reference_hwnd))
        and user32.GetWindowRect(int(reference_hwnd), ctypes.byref(reference_rect))
    )

    def _consider(hwnd) -> None:
        hwnd_value = int(hwnd)
        if not hwnd_value or hwnd_value in seen or hwnd_value == int(reference_hwnd or 0):
            return
        seen.add(hwnd_value)
        class_name = _window_class(user32, hwnd)
        if not class_name.startswith("WindowsForms10.Window"):
            return
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return
        if not _usable_lively_window(
            visible=bool(user32.IsWindowVisible(hwnd)),
            class_name=class_name,
            rect=rect,
        ):
            return
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        pid = int(process_id.value)
        image = process_names.get(pid)
        if image is None:
            image = _process_image_name(pid).replace("\\", "/").rsplit("/", 1)[-1].lower()
            process_names[pid] = image
        if image != "lively.player.webview2.exe":
            return
        area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
        overlap = _rect_intersection_area(rect, reference_rect) if has_reference_rect else 0
        parent = int(user32.GetParent(hwnd) or 0)
        parent_class = _window_class(user32, parent) if parent else ""
        if parent_class in {"Progman", "WorkerW"}:
            placement_rank = 2
        elif not parent:
            placement_rank = 1
        else:
            placement_rank = 0
        candidates.append((placement_rank, overlap, area, hwnd_value, parent))

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _visit_child(hwnd, _):
        _consider(hwnd)
        return True

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _visit_top(hwnd, _):
        _consider(hwnd)
        user32.EnumChildWindows(hwnd, _visit_child, 0)
        return True

    user32.EnumWindows(_visit_top, 0)
    selected = max(candidates, default=(0, 0, 0, 0, 0))
    return selected[3], selected[4]


def find_lively_wallpaper_window(reference_hwnd: int = 0) -> int:
    """Compatibility wrapper returning only the selected surface HWND."""
    return find_lively_wallpaper_target(reference_hwnd)[0]


def _ensure_desktop_owned_window(user32, hwnd: int, owner: int) -> tuple[bool, str]:
    """Keep Chromium top-level while binding its lifetime to the desktop."""
    GWL_STYLE = -16
    GWLP_HWNDPARENT = -8
    GW_OWNER = 4
    WS_CHILD = 0x40000000
    WS_POPUP = 0x80000000
    changed = False
    style = int(user32.GetWindowLongPtrW(int(hwnd), GWL_STYLE))
    desired_style = (style | WS_POPUP) & ~WS_CHILD
    if desired_style != style:
        ctypes.set_last_error(0)
        user32.SetWindowLongPtrW(int(hwnd), GWL_STYLE, desired_style)
        if ctypes.get_last_error():
            return False, f"set-style-failed:{ctypes.get_last_error()}"
        changed = True
    if style & WS_CHILD:
        ctypes.set_last_error(0)
        user32.SetParent(int(hwnd), 0)
        if ctypes.get_last_error():
            return False, f"set-parent-failed:{ctypes.get_last_error()}"
        changed = True
    current_owner = int(user32.GetWindow(int(hwnd), GW_OWNER) or 0)
    if current_owner != int(owner):
        ctypes.set_last_error(0)
        user32.SetWindowLongPtrW(int(hwnd), GWLP_HWNDPARENT, int(owner))
        if ctypes.get_last_error():
            return False, f"set-owner-failed:{ctypes.get_last_error()}"
        changed = True
    final_style = int(user32.GetWindowLongPtrW(int(hwnd), GWL_STYLE))
    final_owner = int(user32.GetWindow(int(hwnd), GW_OWNER) or 0)
    if final_style & WS_CHILD or final_owner != int(owner):
        return False, "desktop-owner-postcondition-failed"
    return True, "changed" if changed else "unchanged"


def find_wallpaper_parent() -> tuple[int, str]:
    """Return the desktop host behind SHELLDLL_DefView and normal app windows."""
    if sys.platform != "win32":
        return 0, "unsupported-platform"

    user32 = _user32()
    progman = user32.FindWindowW("Progman", None)
    if progman:
        # Ask Explorer to materialize the WorkerW wallpaper layer.
        user32.SendMessageW(progman, 0x052C, 0, 0)

    icon_host = ctypes.c_size_t(0)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _find_icon_host(hwnd, _):
        shell_view = user32.FindWindowExW(hwnd, None, "SHELLDLL_DefView", None)
        if shell_view:
            icon_host.value = hwnd
            return False
        return True

    user32.EnumWindows(_find_icon_host, 0)
    if icon_host.value:
        worker = user32.FindWindowExW(None, icon_host.value, "WorkerW", None)
        if worker:
            return int(worker), "workerw-behind-icons"
    if progman:
        return int(progman), "progman-fallback"
    return 0, "not-found"


def _place_above_desktop_anchor(
    user32,
    hwnd: int,
    anchor: int,
    mode: str,
) -> tuple[bool, str, bool]:
    """Place top-level Slice immediately above the wallpaper's top-level host."""
    if not anchor or not user32.IsWindow(int(anchor)):
        return False, "desktop-anchor-missing", False
    rect = wintypes.RECT()
    if not user32.GetWindowRect(int(hwnd), ctypes.byref(rect)):
        return False, "slice-rect-unavailable", False
    desktop_owner = int(user32.FindWindowW("Progman", None) or anchor)
    top_level_ok, top_level_reason = _ensure_desktop_owned_window(
        user32,
        int(hwnd),
        desktop_owner,
    )
    if not top_level_ok:
        return False, top_level_reason, False
    native_frame_changed = top_level_reason == "changed"
    GW_HWNDPREV = 3
    previous = int(user32.GetWindow(int(anchor), GW_HWNDPREV) or 0)
    if previous == int(hwnd):
        return True, mode, native_frame_changed
    SWP_NOACTIVATE = 0x0010
    SWP_FRAMECHANGED = 0x0020
    if user32.SetWindowPos(
        int(hwnd),
        previous,
        rect.left,
        rect.top,
        max(1, rect.right - rect.left),
        max(1, rect.bottom - rect.top),
        SWP_NOACTIVATE | SWP_FRAMECHANGED,
    ):
        return True, mode, True
    return False, f"desktop-z-order-failed:{ctypes.get_last_error()}", False


def _attach_window_to_desktop(hwnd: int) -> tuple[bool, str, bool]:
    """Place an existing window on the currently available wallpaper layer."""
    if sys.platform != "win32" or int(hwnd or 0) <= 0:
        return False, "invalid-window", False
    user32 = _user32()
    if not user32.IsWindow(int(hwnd)):
        return False, "window-closed", False

    lively, lively_parent = find_lively_wallpaper_target(int(hwnd))
    if lively:
        # Lively has shipped both a bottom-ranked top-level HWND and a child
        # under Progman/WorkerW. In the child form, anchor to its top-level
        # parent instead of parenting Chromium into the desktop tree; a child
        # transparent BrowserWindow is not reliably DirectComposition-backed.
        anchor = int(lively_parent or lively)
        return _place_above_desktop_anchor(
            user32,
            int(hwnd),
            anchor,
            "above-lively-below-apps",
        )

    parent, mode = find_wallpaper_parent()
    if not parent:
        return False, mode, False
    return _place_above_desktop_anchor(user32, int(hwnd), parent, mode)


def attach_window_to_desktop(hwnd: int) -> tuple[bool, str]:
    """Place an existing window on the currently available wallpaper layer."""
    ok, mode, _changed = _attach_window_to_desktop(hwnd)
    return ok, mode


def watch_window_on_desktop(
    hwnd: int,
    *,
    interval_s: float = 1.0,
    emit=print,
) -> None:
    """Continuously reconcile Slice with Lively/WorkerW availability."""
    if sys.platform != "win32":
        emit(json.dumps({"ok": False, "mode": "unsupported-platform"}))
        return
    user32 = _user32()
    last_result: tuple[bool, str] | None = None
    while user32.IsWindow(int(hwnd)):
        ok, mode, reconciled = _attach_window_to_desktop(int(hwnd))
        result = (ok, mode)
        if result != last_result or reconciled:
            emit(
                json.dumps({"ok": ok, "mode": mode, "reconciled": reconciled}),
                flush=True,
            )
            last_result = result
        time.sleep(max(0.2, float(interval_s)))


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--attach", type=int, required=True)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    if args.watch:
        watch_window_on_desktop(args.attach, interval_s=args.interval)
        return 0
    ok, mode = attach_window_to_desktop(args.attach)
    print(json.dumps({"ok": ok, "mode": mode}))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
