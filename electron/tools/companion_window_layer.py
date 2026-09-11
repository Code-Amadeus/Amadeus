"""Keep the Companion immediately behind the active application's owner group.

Only reorders the supplied Companion HWND. No focus changes, input injection,
process discovery, or alteration of any other application's window settings.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import sys
import time


def place_behind_active(api, target: int) -> dict:
    foreground = api.GetForegroundWindow()
    if not foreground or not api.IsWindowVisible(foreground):
        return {"mode": "no-active-window"}
    owner = api.GetAncestor(foreground, 3) or foreground  # GA_ROOTOWNER
    if owner == target or foreground == target:
        return {"mode": "companion-active"}
    name = ctypes.create_unicode_buffer(128)
    api.GetClassNameW(owner, name, len(name))
    if name.value in {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}:
        return {"mode": "shell-active"}
    # Normal windows remain below system/topmost surfaces. If the active owner
    # is topmost, move only within the normal band, never promote the Companion.
    topmost = bool(api.GetWindowLongPtrW(owner, -20) & 0x8)
    after = 0 if topmost else owner  # HWND_TOP in the normal band
    ok = bool(api.SetWindowPos(target, after, 0, 0, 0, 0, 0x0010 | 0x0001 | 0x0002 | 0x0200))
    return {"mode": "behind-active", "ok": ok, "target": target, "anchor": int(owner),
            "previous": int(api.GetWindow(target, 3) or 0), "foreground": int(api.GetForegroundWindow() or 0)}


def watch(target: int) -> None:
    api = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                      wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
    signatures = {
        "GetForegroundWindow": ([], wintypes.HWND),
        "GetAncestor": ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
        "GetWindow": ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
        "GetWindowLongPtrW": ([wintypes.HWND, ctypes.c_int], ctypes.c_ssize_t),
        "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        "IsWindow": ([wintypes.HWND], wintypes.BOOL),
        "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
        "SetWindowPos": ([wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL),
        "SetWinEventHook": ([wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, callback_type,
                             wintypes.DWORD, wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
        "UnhookWinEvent": ([wintypes.HANDLE], wintypes.BOOL),
        "PeekMessageW": ([ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT], wintypes.BOOL),
        "TranslateMessage": ([ctypes.POINTER(wintypes.MSG)], wintypes.BOOL),
        "DispatchMessageW": ([ctypes.POINTER(wintypes.MSG)], ctypes.c_ssize_t),
    }
    for name, (args, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = args, result
    previous = None

    def reconcile():
        nonlocal previous
        result = place_behind_active(api, target)
        if result != previous:
            print(json.dumps(result), flush=True)
            previous = result

    @callback_type
    def changed(_hook, _event, _window, _object, _child, _thread, _time):
        try:
            reconcile()
        except Exception as error:
            print(json.dumps({"mode": "error", "error": str(error)}), flush=True)

    # EVENT_SYSTEM_FOREGROUND; out-of-context callbacks run on this message loop.
    hook = api.SetWinEventHook(3, 3, None, changed, 0, 0, 0)
    if not hook:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        reconcile()
        message = wintypes.MSG()
        while api.IsWindow(target):
            while api.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                api.TranslateMessage(ctypes.byref(message))
                api.DispatchMessageW(ctypes.byref(message))
            time.sleep(.025)
    finally:
        api.UnhookWinEvent(hook)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", type=int, required=True)
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("Companion window following requires Windows")
    watch(args.window)
