"""Observe Windows audio-session metadata without opening a recording stream.

Core Audio session state is evidence of an active audio stream, not proof of a
meeting's UI state. Meeting apps protect both capture and render streams so a
muted microphone does not immediately release the reminder voice lane.
References: learn.microsoft.com/windows/win32/api/audiopolicy/
            learn.microsoft.com/windows/win32/api/audiosessiontypes/
"""
from __future__ import annotations

import asyncio
import ctypes
import logging
import sys
import time
from contextlib import contextmanager
from pathlib import PureWindowsPath
from uuid import UUID

from server.event_bus import bus

logger = logging.getLogger(__name__)


def protected_audio_apps(sessions: list[dict]) -> list[str]:
    protected = set()
    for session in sessions:
        name = PureWindowsPath(session["process"]).name.lower()
        if name == "codex.exe" and session["flow"] == "capture":
            protected.add("Codex 语音输入")
        elif name in {"zoom.exe", "zoomhost.exe"}:
            protected.add("Zoom 音频")
        elif name in {"wemeetapp.exe", "wemeetframework.exe"}:
            protected.add("腾讯会议音频")
    return sorted(protected)


def read_windows_audio_sessions() -> list[dict]:
    if sys.platform != "win32":
        return []
    from ctypes import wintypes as wt

    class GUID(ctypes.Structure):
        _fields_ = [("a", wt.DWORD), ("b", wt.WORD), ("c", wt.WORD), ("d", ctypes.c_ubyte * 8)]

    def guid(value):
        return GUID.from_buffer_copy(UUID(value).bytes_le)

    ole = ctypes.OleDLL("ole32")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel.OpenProcess.restype = wt.HANDLE
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]

    def call(pointer, slot, arguments=(), values=()):
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        result = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *arguments)(table[slot])(pointer, *values)
        if result < 0:
            raise OSError(f"Core Audio HRESULT 0x{result & 0xffffffff:08x}")
        return result

    @contextmanager
    def pointer():
        value = ctypes.c_void_p()
        try:
            yield value
        finally:
            if value.value:
                call(value, 2)  # IUnknown::Release

    pp = ctypes.POINTER(ctypes.c_void_p)
    pg = ctypes.POINTER(GUID)
    manager_iid = guid("77aa99a0-1bd6-484f-8bc7-2c654c9a9b6f")
    control_iid = guid("bfb7ff88-7239-4fc9-8fa2-07c950be9c6d")
    enum_iid = guid("a95664d2-9614-4f35-a746-de8db63617e6")
    enum_class = guid("bcde0395-e52f-467c-8e3d-c4579291692e")
    result = []
    ole.CoInitializeEx(None, 0)
    try:
        with pointer() as enumerator:
            ole.CoCreateInstance(ctypes.byref(enum_class), None, 1, ctypes.byref(enum_iid), ctypes.byref(enumerator))
            for flow, label in ((1, "capture"), (0, "render")):
                with pointer() as devices:
                    call(enumerator, 3, (ctypes.c_int, wt.DWORD, pp), (flow, 1, ctypes.byref(devices)))
                    count = wt.UINT()
                    call(devices, 3, (ctypes.POINTER(wt.UINT),), (ctypes.byref(count),))
                    for index in range(count.value):
                        with pointer() as device, pointer() as manager, pointer() as sessions:
                            call(devices, 4, (wt.UINT, pp), (index, ctypes.byref(device)))
                            call(device, 3, (pg, wt.DWORD, ctypes.c_void_p, pp), (ctypes.byref(manager_iid), 23, None, ctypes.byref(manager)))
                            call(manager, 5, (pp,), (ctypes.byref(sessions),))
                            size = ctypes.c_int()
                            call(sessions, 3, (ctypes.POINTER(ctypes.c_int),), (ctypes.byref(size),))
                            for position in range(size.value):
                                with pointer() as control, pointer() as control2:
                                    call(sessions, 4, (ctypes.c_int, pp), (position, ctypes.byref(control)))
                                    state = ctypes.c_int()
                                    call(control, 3, (ctypes.POINTER(ctypes.c_int),), (ctypes.byref(state),))
                                    if state.value != 1:  # AudioSessionStateActive
                                        continue
                                    call(control, 0, (pg, pp), (ctypes.byref(control_iid), ctypes.byref(control2)))
                                    pid = wt.DWORD()
                                    call(control2, 14, (ctypes.POINTER(wt.DWORD),), (ctypes.byref(pid),))
                                    if not pid.value:
                                        continue
                                    handle = kernel.OpenProcess(0x1000, False, pid.value)
                                    if not handle:
                                        continue  # system/protected or already-exited process
                                    try:
                                        path = ctypes.create_unicode_buffer(32768)
                                        capacity = wt.DWORD(len(path))
                                        if kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(capacity)):
                                            result.append({"pid": pid.value, "process": path.value, "flow": label})
                                    finally:
                                        kernel.CloseHandle(handle)
    finally:
        ole.CoUninitialize()
    return result


class CompanionAudioActivity:
    def __init__(self, on_blocked, probe=read_windows_audio_sessions):
        self.probe = probe
        self.on_blocked = on_blocked
        self.snapshot = {"blocked": False, "note": "", "status": "starting"}
        self.job = None
        self.last_busy = 0.0

    def start(self):
        if self.job is None or self.job.done():
            self.job = asyncio.create_task(self.watch())

    async def refresh(self):
        try:
            apps = protected_audio_apps(await asyncio.to_thread(self.probe))
            if apps:
                self.last_busy = time.monotonic()
            blocked = bool(apps) or (self.snapshot["blocked"] and time.monotonic() - self.last_busy < 1)
            state = {"blocked": blocked, "status": "ok" if sys.platform == "win32" else "unsupported",
                     "note": f"语音等待 · {'、'.join(apps)}" if apps else ("语音即将恢复" if blocked else "")}
        except Exception as exc:
            state = {"blocked": False, "status": "unavailable", "note": "麦克风占用检测不可用"}
            if self.snapshot["status"] != "unavailable":
                logger.warning("Companion audio activity probe unavailable: %s", exc)
        previous = self.snapshot
        self.snapshot = state
        if state["blocked"] and not previous["blocked"]:
            await self.on_blocked()
        if state != previous:
            await bus.emit("companion.audio-activity", state)

    async def watch(self):
        while True:
            await self.refresh()
            await asyncio.sleep(.5)

    async def close(self):
        if self.job:
            self.job.cancel()
            try:
                await self.job
            except asyncio.CancelledError:
                pass
