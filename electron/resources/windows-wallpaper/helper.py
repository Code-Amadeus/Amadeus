"""Independent Windows wallpaper helper; standard library only, run with python -I.

Lively 2.2.1.0 uses GrpcDotNetNamedPipes, not HTTP/2. The bounded wire adapter
below follows transport.proto at da307b551a9d56b62f5780e91a2d95a699f32430.
No backend imports, background RPC workers, or automatic request retries.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
from dataclasses import asdict, dataclass, replace
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

SUPPORTED_VERSION = "2.2.1.0"
DOTNET_FILETIME_OFFSET = 504911232000000000


class RpcError(OSError):
    def __init__(self, code: int, detail: str):
        self.code, self.detail = code, detail
        super().__init__(f"Lively RPC status {code}: {detail}")


def varint(value: int) -> bytes:
    value &= (1 << 64) - 1
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def read_varint(data: bytes, pos: int = 0) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            raise ValueError("Truncated protobuf varint")
        byte = data[pos]
        pos += 1
        if shift == 63 and byte > 1:
            raise ValueError("Invalid protobuf varint")
        value |= (byte & 127) << shift
        if not byte & 128:
            return value, pos
    raise ValueError("Invalid protobuf varint")


def scalar(number: int, value: int) -> bytes:
    return varint(number << 3) + varint(value) if value else b""


def blob(number: int, value: bytes) -> bytes:
    return varint(number << 3 | 2) + varint(len(value)) + value


def string(number: int, value: str) -> bytes:
    return blob(number, value.encode("utf-8")) if value else b""


@dataclass(frozen=True)
class Field:
    number: int
    wire: int
    value: int | bytes
    raw: bytes


def fields(data: bytes) -> list[Field]:
    """Retain each field's exact encoding, including unknown groups/duplicates."""
    def one(pos: int) -> tuple[Field, int]:
        start = pos
        tag, pos = read_varint(data, pos)
        number, wire = tag >> 3, tag & 7
        if not 0 < number < (1 << 29):
            raise ValueError("Invalid protobuf field number")
        if wire == 0:
            value, pos = read_varint(data, pos)
        elif wire in (1, 5):
            end = pos + (8 if wire == 1 else 4)
            value, pos = data[pos:end], end
        elif wire == 2:
            size, pos = read_varint(data, pos)
            end = pos + size
            value, pos = data[pos:end], end
        elif wire == 3:
            content = pos
            while True:
                closing, after = read_varint(data, pos)
                if closing & 7 == 4:
                    if closing >> 3 != number:
                        raise ValueError("Mismatched protobuf group")
                    value, pos = data[content:pos], after
                    break
                _, pos = one(pos)
        else:
            raise ValueError("Invalid protobuf wire type")
        if pos > len(data):
            raise ValueError("Truncated protobuf field")
        return Field(number, wire, value, data[start:pos]), pos

    result, pos = [], 0
    while pos < len(data):
        field, pos = one(pos)
        result.append(field)
    return result


def value(data: bytes, number: int, default=0, wire: int = 0):
    return next((f.value for f in reversed(fields(data)) if f.number == number and f.wire == wire), default)


def text_value(data: bytes, number: int) -> str:
    return value(data, number, b"", 2).decode("utf-8")


def rewrite_settings(data: bytes, changes: dict[int, int]) -> bytes:
    """Only three owned scalars may change; all other fields remain byte exact."""
    if changes.keys() - {28, 48, 49}:
        raise ValueError("Only browser and image settings may be rewritten")
    result, written = [], set()
    for field in fields(data):
        if field.number in changes and field.wire == 0:
            if field.number not in written:
                result.append(scalar(field.number, changes[field.number]))
                written.add(field.number)
        else:
            result.append(field.raw)
    for number, replacement in changes.items():
        if number not in written:
            result.append(scalar(number, replacement))
    return b"".join(result)


@dataclass(frozen=True)
class ProcessIdentity:
    Id: int
    Started: int


@dataclass(frozen=True)
class DesktopItem:
    Monitor: str
    Path: str


@dataclass(frozen=True)
class HostOptions:
    Arrangement: int
    Browser: int
    DesktopImage: bool
    LockImage: bool


@dataclass(frozen=True)
class Snapshot:
    Project: str
    Monitor: str
    Options: HostOptions
    Wallpapers: list[DesktopItem]
    OwnedProcess: ProcessIdentity | None = None
    Mounted: bool = False
    Phase: str | None = None
    RequestProcess: ProcessIdentity | None = None


def identity_json(item) -> ProcessIdentity | None:
    if item is None:
        return None
    if not isinstance(item, dict) or any(type(item.get(k)) is not int or item[k] <= 0 for k in ("Id", "Started")):
        raise ValueError("Invalid wallpaper process identity")
    return ProcessIdentity(item["Id"], item["Started"])


def atomic_json(file: Path, item) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_name(file.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(item, stream, ensure_ascii=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, file)


def same_path(left: str, right: str) -> bool:
    return left.lower() == right.lower()


class Session:
    """The journal owns recovery; process ownership never substitutes for it."""
    def __init__(self, host, journal: Path):
        self.host, self.journal = host, Path(journal)
        self.managed_cleanup_process = None

    def read(self) -> Snapshot:
        item = json.loads(self.journal.read_text(encoding="utf-8-sig"))
        phase = item.get("Phase")
        if phase not in (None, "Preparing", "Pending", "Settled"):
            raise ValueError("Invalid wallpaper recovery phase")
        snapshot = Snapshot(item["Project"], item["Monitor"], HostOptions(**item["Options"]),
                            [DesktopItem(**x) for x in item["Wallpapers"]], identity_json(item.get("OwnedProcess")),
                            item.get("Mounted", False), phase, identity_json(item.get("RequestProcess")))
        if phase == "Pending" and snapshot.RequestProcess is None:
            raise ValueError("Invalid wallpaper recovery journal: Pending requires a request receiver identity.")
        return snapshot

    def save(self, snapshot: Snapshot) -> None:
        atomic_json(self.journal, asdict(snapshot))

    def release_unstarted_host(self, process: ProcessIdentity | None) -> None:
        if process is not None and process != self.managed_cleanup_process and not self.journal.exists():
            self.host.stop_owned_process(process)

    def mount(self, snapshot: Snapshot) -> None:
        if self.journal.exists():
            raise RuntimeError("Recover the previous wallpaper session first.")
        snapshot = replace(snapshot, Mounted=False, Phase="Preparing", RequestProcess=None)
        self.save(snapshot)
        self.managed_cleanup_process = snapshot.OwnedProcess

        def settled():
            nonlocal snapshot
            updated = replace(snapshot, Phase="Settled")
            self.save(updated)
            snapshot = updated

        def dispatching(process):
            nonlocal snapshot
            updated = replace(snapshot, Phase="Pending", RequestProcess=process)
            self.save(updated)
            snapshot = updated

        try:
            self.host.set_session_options()
            self.host.set_wallpaper(DesktopItem(snapshot.Monitor, snapshot.Project), settled, dispatching)
            self.save(replace(snapshot, Mounted=True, Phase="Settled"))
        except Exception as primary:
            if snapshot.Phase == "Pending":
                raise
            try:
                self.restore()
            except Exception as cleanup:
                raise ExceptionGroup("Wallpaper mount and rollback failed.", [primary, cleanup]) from None
            raise

    def run_mounted(self, snapshot: Snapshot, body) -> None:
        self.mount(snapshot)
        try:
            body()
        except Exception as primary:
            try:
                self.restore()
            except Exception as cleanup:
                raise ExceptionGroup("Wallpaper body and cleanup failed.", [primary, cleanup]) from None
            raise
        else:
            self.restore()

    def prepare_recovery(self) -> None:
        if not self.journal.exists():
            return
        snapshot = self.read()
        if snapshot.Phase != "Pending":
            return
        if self.host.is_process_running(snapshot.RequestProcess):
            raise RuntimeError("Wallpaper mount request may still complete in its original Lively process. "
                               "Fully exit Lively, then reopen managed wallpaper or run recover.")
        self.save(replace(snapshot, Mounted=False, Phase="Settled"))

    def restore(self, restarted_process: ProcessIdentity | None = None) -> None:
        self.prepare_recovery()
        if not self.journal.exists():
            return
        snapshot = self.read()
        self.managed_cleanup_process = restarted_process or snapshot.OwnedProcess
        if snapshot.Phase == "Preparing":
            self.host.restore_options(snapshot.Options)
            options = self.host.get_options()
            current = self.host.get_wallpapers()
            changed = options.Arrangement != snapshot.Options.Arrangement or any(
                not any(old.Monitor == x.Monitor and same_path(old.Path, x.Path) for old in snapshot.Wallpapers)
                for x in current)
            if snapshot.OwnedProcess and not changed:
                self.host.stop_owned_process(restarted_process or snapshot.OwnedProcess)
            self.journal.unlink()
            return
        current = self.host.get_wallpapers()
        options = self.host.get_options()
        ours = [x for x in current if same_path(x.Path, snapshot.Project)]
        layout_unchanged = options.Arrangement == snapshot.Options.Arrangement
        affected = {snapshot.Monitor} if snapshot.Options.Arrangement == 0 else {
            snapshot.Monitor, *(x.Monitor for x in snapshot.Wallpapers)}
        restorable = {monitor for monitor in affected if any(x.Monitor == monitor for x in ours) or (
            (not snapshot.Mounted or restarted_process is not None) and all(x.Monitor != monitor for x in current))}
        if snapshot.Options.Arrangement != 0 and ours:
            restorable = affected
        if ours or (snapshot.Phase == "Settled" and not snapshot.Mounted):
            self.save(replace(snapshot, Mounted=False))
            self.host.close_project(snapshot.Project)
        if layout_unchanged:
            self.host.restore_options(snapshot.Options, images=False)
            for wallpaper in snapshot.Wallpapers:
                if wallpaper.Monitor in restorable:
                    self.host.set_wallpaper(wallpaper)
        self.host.restore_options(snapshot.Options)
        after = self.host.get_wallpapers()
        if any(same_path(x.Path, snapshot.Project) and not any(
            old.Monitor == x.Monitor and same_path(old.Path, x.Path) for old in snapshot.Wallpapers) for x in after) or (
            layout_unchanged and any(not any(x.Monitor == old.Monitor and same_path(x.Path, old.Path) for x in after)
                                    for old in snapshot.Wallpapers if old.Monitor in restorable)):
            raise OSError("Wallpaper restoration was not confirmed; recovery journal retained.")
        user_changed = not layout_unchanged or any(not any(
            old.Monitor == x.Monitor and same_path(old.Path, x.Path) for old in snapshot.Wallpapers) for x in after)
        if snapshot.OwnedProcess and not user_changed:
            self.host.stop_owned_process(restarted_process or snapshot.OwnedProcess)
        self.journal.unlink()


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", w.DWORD), ("OffsetHigh", w.DWORD), ("hEvent", w.HANDLE)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
                ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", w.LONG), ("dwFlags", w.DWORD),
                ("szExeFile", w.WCHAR * 260)]


class Windows:
    def __init__(self):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateFileW": (w.HANDLE, [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]),
            "CloseHandle": (w.BOOL, [w.HANDLE]),
            "CreateEventW": (w.HANDLE, [w.LPVOID, w.BOOL, w.BOOL, w.LPCWSTR]),
            "WaitForSingleObject": (w.DWORD, [w.HANDLE, w.DWORD]),
            "ReadFile": (w.BOOL, [w.HANDLE, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(OVERLAPPED)]),
            "WriteFile": (w.BOOL, [w.HANDLE, w.LPCVOID, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(OVERLAPPED)]),
            "GetOverlappedResult": (w.BOOL, [w.HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(w.DWORD), w.BOOL]),
            "CancelIoEx": (w.BOOL, [w.HANDLE, ctypes.POINTER(OVERLAPPED)]),
            "SetNamedPipeHandleState": (w.BOOL, [w.HANDLE, ctypes.POINTER(w.DWORD), w.LPVOID, w.LPVOID]),
            "GetNamedPipeServerProcessId": (w.BOOL, [w.HANDLE, ctypes.POINTER(w.ULONG)]),
            "OpenProcess": (w.HANDLE, [w.DWORD, w.BOOL, w.DWORD]),
            "GetProcessTimes": (w.BOOL, [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4),
            "ProcessIdToSessionId": (w.BOOL, [w.DWORD, ctypes.POINTER(w.DWORD)]),
            "QueryFullProcessImageNameW": (w.BOOL, [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]),
            "CreateToolhelp32Snapshot": (w.HANDLE, [w.DWORD, w.DWORD]),
            "Process32FirstW": (w.BOOL, [w.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]),
            "Process32NextW": (w.BOOL, [w.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]),
        }
        for name, (result, args) in signatures.items():
            function = getattr(self.api, name)
            function.restype, function.argtypes = result, args

    def check(self, result):
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    def close(self, handle):
        self.api.CloseHandle(handle)

    def open_process(self, pid):
        handle = self.api.OpenProcess(0x1000 | 0x100000, False, pid)
        if not handle and ctypes.get_last_error() == 87:
            return None
        return self.check(handle)

    def identity(self, pid: int) -> ProcessIdentity:
        handle = self.open_process(pid)
        if handle is None:
            raise ProcessLookupError("Wallpaper process exited during identity lookup")
        try:
            started, exited, kernel, user = (w.FILETIME() for _ in range(4))
            self.check(self.api.GetProcessTimes(handle, ctypes.byref(started), ctypes.byref(exited),
                                                ctypes.byref(kernel), ctypes.byref(user)))
            ticks = (started.dwHighDateTime << 32) | started.dwLowDateTime
            return ProcessIdentity(pid, ticks + DOTNET_FILETIME_OFFSET)
        finally:
            self.close(handle)

    def is_running(self, identity: ProcessIdentity) -> bool:
        handle = self.open_process(identity.Id)
        if handle is None:
            return False
        try:
            state = self.api.WaitForSingleObject(handle, 0)
            if state == 0:
                return False
            if state != 258:
                raise ctypes.WinError(ctypes.get_last_error())
            # Read from the same process object; a reused PID cannot change it.
            started, exited, kernel, user = (w.FILETIME() for _ in range(4))
            self.check(self.api.GetProcessTimes(handle, ctypes.byref(started), ctypes.byref(exited),
                                                ctypes.byref(kernel), ctypes.byref(user)))
            return ((started.dwHighDateTime << 32) | started.dwLowDateTime) + DOTNET_FILETIME_OFFSET == identity.Started
        finally:
            self.close(handle)

    def receiver(self, pipe) -> ProcessIdentity:
        pid = w.ULONG()
        self.check(self.api.GetNamedPipeServerProcessId(pipe, ctypes.byref(pid)))
        return self.identity(pid.value)

    def username(self) -> str:
        api = ctypes.WinDLL("advapi32", use_last_error=True)
        api.GetUserNameW.argtypes = [w.LPWSTR, ctypes.POINTER(w.DWORD)]
        api.GetUserNameW.restype = w.BOOL
        size, buffer = w.DWORD(257), ctypes.create_unicode_buffer(257)
        self.check(api.GetUserNameW(buffer, ctypes.byref(size)))
        return buffer.value

    def running(self) -> ProcessIdentity | None:
        current = w.DWORD()
        self.check(self.api.ProcessIdToSessionId(os.getpid(), ctypes.byref(current)))
        snapshot = self.api.CreateToolhelp32Snapshot(2, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            found = self.api.Process32FirstW(snapshot, ctypes.byref(entry))
            while found:
                if entry.szExeFile.lower() == "lively.exe":
                    session = w.DWORD()
                    self.check(self.api.ProcessIdToSessionId(entry.th32ProcessID, ctypes.byref(session)))
                    if session.value == current.value:
                        try:
                            return self.identity(entry.th32ProcessID)
                        except ProcessLookupError:
                            pass
                found = self.api.Process32NextW(snapshot, ctypes.byref(entry))
            if ctypes.get_last_error() != 18:
                raise ctypes.WinError(ctypes.get_last_error())
            return None
        finally:
            self.close(snapshot)

    def executable(self, pid: int) -> str:
        handle = self.open_process(pid)
        if handle is None:
            raise ProcessLookupError("Lively exited")
        try:
            size, buffer = w.DWORD(32768), ctypes.create_unicode_buffer(32768)
            self.check(self.api.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)))
            return buffer.value
        finally:
            self.close(handle)

    def lock(self, file: Path):
        file.parent.mkdir(parents=True, exist_ok=True)
        handle = self.api.CreateFileW(str(file), 0xC0000000, 0, None, 4, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle


class Budget:
    def __init__(self, seconds: float, cancel: threading.Event | None = None):
        self.until = time.monotonic() + seconds
        self.utc_ns = time.time_ns() + int(seconds * 1_000_000_000)
        self.cancel = cancel

    def check(self):
        if time.monotonic() >= self.until:
            raise RpcError(4, "Deadline exceeded")
        if self.cancel is not None and self.cancel.is_set():
            raise RpcError(1, "Cancelled")


class Pipe:
    """Overlapped IO is cancelled and drained before its buffers are released."""
    def __init__(self, windows: Windows, name: str, budget: Budget):
        self.windows, self.handle = windows, None
        while True:
            budget.check()
            handle = windows.api.CreateFileW("\\\\.\\pipe\\" + name, 0xC0000000, 0, None, 3, 0x40000000, None)
            if handle != ctypes.c_void_p(-1).value:
                self.handle = handle
                break
            error = ctypes.get_last_error()
            if error not in (2, 231):
                raise ctypes.WinError(error)
            time.sleep(min(0.02, max(0, budget.until - time.monotonic())))
        try:
            budget.check()
            mode = w.DWORD(2)
            windows.check(windows.api.SetNamedPipeHandleState(handle, ctypes.byref(mode), None, None))
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.handle is not None:
            self.windows.close(self.handle)
            self.handle = None

    def receiver(self):
        return self.windows.receiver(self.handle)

    def io(self, data: bytes | None, budget: Budget) -> tuple[bytes, bool]:
        budget.check()
        api = self.windows.api
        event = self.windows.check(api.CreateEventW(None, True, False, None))
        overlap = OVERLAPPED(hEvent=event)
        buffer = ctypes.create_string_buffer(data, len(data)) if data is not None else ctypes.create_string_buffer(16384)
        count = w.DWORD()
        try:
            operation = api.WriteFile if data is not None else api.ReadFile
            success = operation(self.handle, buffer, len(buffer), ctypes.byref(count), ctypes.byref(overlap))
            error = 0 if success else ctypes.get_last_error()
            if error == 234:
                # With overlapped message reads, ERROR_MORE_DATA can complete
                # immediately without filling lpNumberOfBytesRead. The result
                # API owns the actual byte count for this partial message.
                api.GetOverlappedResult(self.handle, ctypes.byref(overlap), ctypes.byref(count), False)
            if error == 997:
                try:
                    while True:
                        budget.check()
                        state = api.WaitForSingleObject(event, max(1, min(25, int((budget.until - time.monotonic()) * 1000))))
                        if state == 0:
                            break
                        if state != 258:
                            raise ctypes.WinError(ctypes.get_last_error())
                    success = api.GetOverlappedResult(self.handle, ctypes.byref(overlap), ctypes.byref(count), False)
                    error = 0 if success else ctypes.get_last_error()
                except BaseException:
                    # Windows retains the OVERLAPPED/buffer until completion, even
                    # after CancelIoEx. No detached work can later dispatch a Set.
                    api.CancelIoEx(self.handle, ctypes.byref(overlap))
                    api.GetOverlappedResult(self.handle, ctypes.byref(overlap), ctypes.byref(count), True)
                    raise
            if error not in (0, 234):
                raise ctypes.WinError(error)
            if data is not None and count.value != len(data):
                raise OSError("Incomplete named-pipe write")
            if data is None and count.value == 0:
                raise EOFError("Lively closed the RPC pipe before trailers")
            return buffer.raw[:count.value], error != 234
        finally:
            self.windows.close(event)

    def write(self, data: bytes, budget: Budget):
        self.io(data, budget)

    def read(self, budget: Budget) -> bytes:
        chunks = []
        while True:
            data, complete = self.io(None, budget)
            chunks.append(data)
            if complete:
                return b"".join(chunks)


def transport_message(number: int, payload: bytes | int) -> bytes:
    message = scalar(number, payload) if isinstance(payload, int) else blob(number, payload)
    return varint(len(message)) + message


class Rpc:
    def __init__(self, windows: Windows, name: str | None = None, pipe_factory=Pipe):
        self.windows = windows
        self.name = name or "Grpc_LIVELY:DESKTOPWALLPAPERSYSTEM" + windows.username()
        self.pipe_factory = pipe_factory

    def call(self, service: str, method: str, payload: bytes = b"", *, timeout: float = 15,
             dispatching=None, cancel: threading.Event | None = None, streaming: bool = False) -> list[bytes]:
        budget = Budget(timeout, cancel)
        pipe = self.pipe_factory(self.windows, self.name, budget)
        sent = False
        try:
            budget.check()
            if dispatching is not None:
                identity = pipe.receiver()
                budget.check()
                # Runs on this call's thread. A deadline cannot return while the
                # journal callback is entered; no background serializer survives.
                dispatching(identity)
            budget.check()
            seconds, nanos = divmod(budget.utc_ns, 1_000_000_000)
            init = string(1, f"/Lively.Grpc.Common.Proto.{service}.{service}Service/{method}") + blob(
                2, scalar(1, seconds) + scalar(2, nanos))
            packet = (transport_message(1, init) + transport_message(2, b"")
                      + transport_message(3, scalar(1, len(payload)) + scalar(2, 1)) + payload)
            sent = True  # A partially written request is also uncertain.
            pipe.write(packet, budget)
            result = []
            while True:
                packet, pos = pipe.read(budget), 0
                while pos < len(packet):
                    size, pos = read_varint(packet, pos)
                    end = pos + size
                    if end > len(packet):
                        raise ValueError("Truncated Lively transport message")
                    message, pos = fields(packet[pos:end]), end
                    if len(message) != 1:
                        raise ValueError("Invalid Lively transport envelope")
                    field = message[0]
                    if field.number == 3 and field.wire == 2:
                        length = value(field.value, 1)
                        if value(field.value, 2):
                            end = pos + length
                            if end > len(packet):
                                raise ValueError("Truncated inline Lively payload")
                            result.append(packet[pos:end])
                            pos = end
                        else:
                            separate = pipe.read(budget)
                            if len(separate) != length:
                                raise ValueError("Invalid separate Lively payload length")
                            result.append(separate)
                    elif field.number == 5 and field.wire == 2:
                        code, detail = value(field.value, 2), text_value(field.value, 3)
                        if code:
                            raise RpcError(code, detail)
                        if not streaming and len(result) != 1:
                            raise ValueError("Expected one unary Lively response payload")
                        return result
                    elif field.number == 4 and field.value == 1:
                        raise RpcError(1, "Server cancelled")
                    elif field.number not in (2, 4):
                        raise ValueError("Unexpected Lively transport message")
        except BaseException:
            if sent:
                try:
                    pipe.write(transport_message(4, 1), Budget(0.05))
                except Exception:
                    pass  # Preserve the original error; cancel is not exit proof.
            raise
        finally:
            pipe.close()


class LivelyHost:
    def __init__(self, windows: Windows, data_directory: Path | None = None, rpc: Rpc | None = None):
        self.windows = windows
        self.data_directory = data_directory or Path(os.environ["LOCALAPPDATA"]) / "Lively Wallpaper"
        self.rpc = rpc or Rpc(windows)
        self.started_process = None

    @property
    def launch_backup(self):
        return self.data_directory / "Amadeus.launch-settings.json"

    def is_process_running(self, identity):
        return self.windows.is_running(identity)

    def find_executable(self) -> str | None:
        import winreg
        running = self.windows.running()
        if running:
            return self.windows.executable(running.Id)
        uninstall = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                root = winreg.OpenKey(hive, uninstall)
            except FileNotFoundError:
                continue
            with root:
                for index in range(winreg.QueryInfoKey(root)[0]):
                    with winreg.OpenKey(root, winreg.EnumKey(root, index)) as key:
                        try:
                            name = winreg.QueryValueEx(key, "DisplayName")[0]
                            if not name.startswith("Lively Wallpaper"):
                                continue
                            executable = Path(winreg.QueryValueEx(key, "InstallLocation")[0]) / "Lively.exe"
                            if executable.is_file():
                                return str(executable)
                        except FileNotFoundError:
                            continue
        for directory in (Path(os.environ["LOCALAPPDATA"]) / "Programs", Path(os.environ["PROGRAMFILES"])):
            executable = directory / "Lively Wallpaper" / "Lively.exe"
            if executable.is_file():
                return str(executable)
        return None

    @staticmethod
    def check_version(version):
        if version != SUPPORTED_VERSION:
            raise RuntimeError(f"Managed wallpaper supports Lively {SUPPORTED_VERSION}; found {version}. "
                               "Existing installation was not changed.")

    @staticmethod
    def file_version(file: str) -> str:
        api = ctypes.WinDLL("version", use_last_error=True)
        api.GetFileVersionInfoSizeW.argtypes = [w.LPCWSTR, ctypes.POINTER(w.DWORD)]
        api.GetFileVersionInfoSizeW.restype = w.DWORD
        api.GetFileVersionInfoW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID]
        api.GetFileVersionInfoW.restype = w.BOOL
        api.VerQueryValueW.argtypes = [w.LPCVOID, w.LPCWSTR, ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.UINT)]
        api.VerQueryValueW.restype = w.BOOL
        unused = w.DWORD()
        size = api.GetFileVersionInfoSizeW(file, ctypes.byref(unused))
        if not size:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(size)
        if not api.GetFileVersionInfoW(file, 0, size, buffer):
            raise ctypes.WinError(ctypes.get_last_error())
        pointer, length = w.LPVOID(), w.UINT()
        if not api.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)) or length.value < 16:
            raise OSError("Could not read Lively file version")
        signature, _, high, low = struct.unpack("<4I", ctypes.string_at(pointer, 16))
        if signature != 0xFEEF04BD:
            raise ValueError("Invalid Lively version resource")
        return f"{high >> 16}.{high & 65535}.{low >> 16}.{low & 65535}"

    @staticmethod
    def prepare_first_run(directory: Path) -> bool:
        directory.mkdir(parents=True, exist_ok=True)
        file = directory / "Settings.json"
        try:
            with file.open("x", encoding="utf-8") as stream:
                json.dump(dict(AppVersion=SUPPORTED_VERSION, IsFirstRun=False, Startup=False, WebBrowser=1,
                               SysTrayIcon=False, DesktopAutoWallpaper=False, LockScreenAutoWallpaper=False), stream)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            return False
        return True

    def prepare_launch_images(self):
        if self.launch_backup.exists():
            return
        file = self.data_directory / "Settings.json"
        settings = json.loads(file.read_text(encoding="utf-8-sig"))
        previous = dict(Desktop=settings.get("DesktopAutoWallpaper", False), Lock=settings.get("LockScreenAutoWallpaper", False))
        if not any(previous.values()):
            return
        with self.launch_backup.open("x", encoding="utf-8") as stream:
            json.dump(previous, stream)
            stream.flush()
            os.fsync(stream.fileno())
        settings.update(DesktopAutoWallpaper=False, LockScreenAutoWallpaper=False)
        atomic_json(file, settings)

    def get_settings(self) -> bytes:
        return self.rpc.call("Settings", "GetSettings")[0]

    def set_settings(self, settings: bytes):
        self.rpc.call("Settings", "SetSettings", settings)

    def recover_launch_images(self):
        if not self.launch_backup.exists():
            return
        previous = json.loads(self.launch_backup.read_text(encoding="utf-8-sig"))
        if self.windows.running():
            self.set_settings(rewrite_settings(self.get_settings(), {49: int(previous["Desktop"]), 48: int(previous["Lock"])}))
        else:
            file = self.data_directory / "Settings.json"
            settings = json.loads(file.read_text(encoding="utf-8-sig"))
            settings.update(DesktopAutoWallpaper=previous["Desktop"], LockScreenAutoWallpaper=previous["Lock"])
            atomic_json(file, settings)
        self.launch_backup.unlink()

    def ensure_running(self):
        try:
            self.start_and_wait()
        finally:
            self.recover_launch_images()

    def start_and_wait(self):
        expected = None
        if self.windows.running() is None:
            executable = self.find_executable()
            if not executable:
                raise RuntimeError("Lively Wallpaper is missing. Run the Windows wallpaper setup.")
            self.check_version(self.file_version(executable))
            self.prepare_first_run(self.data_directory)
            self.prepare_launch_images()
            layout = self.data_directory / "WallpaperLayout.json"
            if layout.exists():
                expected = [DesktopItem(x["LivelyScreen"]["DeviceId"], x["LivelyInfoPath"])
                            for x in json.loads(layout.read_text(encoding="utf-8-sig"))]
            process = subprocess.Popen([executable], creationflags=subprocess.CREATE_NO_WINDOW)
            self.started_process = self.windows.identity(process.pid)
        until, last = time.monotonic() + 45, None
        while time.monotonic() < until:
            try:
                stats = self.rpc.call("Desktop", "GetCoreStats", timeout=2)[0]
                self.check_version(text_value(stats, 2))
                if value(stats, 3):
                    connected = {x.Monitor for x, _ in self.get_screens()}
                    current = self.get_wallpapers()
                    if expected is None or all(any(x.Monitor == old.Monitor and same_path(x.Path, old.Path) for x in current)
                                               for old in expected if old.Monitor in connected):
                        return
            except RpcError as error:
                last = error
            time.sleep(0.2)
        raise TimeoutError("Lively did not become ready.") from last

    def get_options(self) -> HostOptions:
        settings = self.get_settings()
        return HostOptions(value(settings, 16), value(settings, 28), bool(value(settings, 49)), bool(value(settings, 48)))

    def set_session_options(self):
        settings = self.get_settings()
        if value(settings, 28) != 1 or value(settings, 48) or value(settings, 49):
            self.set_settings(rewrite_settings(settings, {28: 1, 48: 0, 49: 0}))

    def restore_options(self, previous: HostOptions, images=True):
        settings = self.get_settings()
        changes = {}
        if value(settings, 28) == 1 and previous.Browser != 1:
            changes[28] = previous.Browser
        if images:
            if not value(settings, 49) and previous.DesktopImage:
                changes[49] = 1
            if not value(settings, 48) and previous.LockImage:
                changes[48] = 1
        if changes:
            self.set_settings(rewrite_settings(settings, changes))

    def get_wallpapers(self) -> list[DesktopItem]:
        return [DesktopItem(text_value(value(item, 2, b"", 2), 1), text_value(item, 1))
                for item in self.rpc.call("Desktop", "GetWallpapers", streaming=True)]

    def get_screens(self):
        screens = self.rpc.call("Display", "GetScreens")[0]
        return [(DesktopItem(text_value(f.value, 1), ""), bool(value(f.value, 6)))
                for f in fields(screens) if f.number == 1 and f.wire == 2]

    def primary_monitor(self):
        primary = [screen.Monitor for screen, is_primary in self.get_screens() if is_primary]
        if len(primary) != 1:
            raise ValueError("Expected exactly one primary Lively display")
        return primary[0]

    def write_project(self, url):
        directory = Path(text_value(self.get_settings(), 33)) / "wallpapers" / "amadeus-managed"
        atomic_json(directory / "LivelyInfo.json", dict(AppVersion=SUPPORTED_VERSION, Title="Amadeus", Desc="Managed by Amadeus",
                    Author="Amadeus", Type=3, FileName=url, IsAbsolutePath=True, Arguments="", Contact="", License="AGPL-3.0"))
        return str(directory)

    def set_wallpaper(self, wallpaper, request_settled=None, request_dispatching=None):
        self.rpc.call("Desktop", "SetWallpaper", string(1, wallpaper.Path) + string(2, wallpaper.Monitor),
                      dispatching=request_dispatching)
        if request_settled:
            request_settled()
        until = time.monotonic() + 30
        while time.monotonic() < until:
            if any(same_path(x.Path, wallpaper.Path) and x.Monitor == wallpaper.Monitor for x in self.get_wallpapers()):
                return
            time.sleep(0.2)
        raise TimeoutError("Lively did not confirm the wallpaper mount.")

    def close_project(self, project):
        self.rpc.call("Desktop", "CloseWallpaperLibrary", string(2, project))

    def screenshot(self, file):
        self.rpc.call("Desktop", "TakeScreenshot", string(1, self.primary_monitor()) + string(2, str(file.resolve())))
        if not file.is_file():
            raise OSError("The Lively player did not produce a screenshot.")

    def stop_owned_process(self, identity):
        if not self.windows.is_running(identity):
            return

        def receiver(actual):
            if actual != identity:
                raise RuntimeError("Lively shutdown pipe belongs to another process; shutdown blocked.")
        try:
            self.rpc.call("Commands", "AutomationCommand", b"".join(string(1, x) for x in ("app", "--shutdown", "true")),
                          dispatching=receiver)
        except (OSError, EOFError):
            if not self.windows.is_running(identity):
                return
            raise
        until = time.monotonic() + 15
        while self.windows.is_running(identity):
            if time.monotonic() >= until:
                raise TimeoutError("Owned Lively process did not exit.")
            time.sleep(0.05)


def error_detail(error: BaseException) -> str:
    if isinstance(error, BaseExceptionGroup):
        return str(error) + ": " + "; ".join(error_detail(x) for x in error.exceptions)
    return str(error)


def report(item):
    try:
        # Raw output has no buffered data for Python to flush again at shutdown
        # after the parent's stdout reader has disappeared.
        pending = memoryview((json.dumps(item, ensure_ascii=True) + "\n").encode("ascii"))
        while pending:
            pending = pending[os.write(sys.stdout.fileno(), pending):]
    except OSError:
        pass  # Parent death closes stdout too; restoration must still finish.


def main(args=None) -> int:
    if sys.platform != "win32":
        raise RuntimeError("Windows wallpaper helper only.")
    args = list(sys.argv[1:] if args is None else args)
    windows = Windows()
    host = LivelyHost(windows)
    state = Path(os.environ["LOCALAPPDATA"]) / "Amadeus" / "wallpaper"
    journal = state / "session.json"
    session = Session(host, journal)
    lock = None
    try:
        command = args[0] if args else "inspect"
        if command == "screenshot" and len(args) == 2:
            host.screenshot(Path(args[1]))
            report(dict(screenshot=str(Path(args[1]).resolve())))
            return 0
        if command == "inspect":
            running = windows.running()
            report(dict(executable=host.find_executable(), running=running is not None, recoveryPending=journal.exists(),
                        wallpapers=[asdict(x) for x in host.get_wallpapers()] if running else None,
                        options=asdict(host.get_options()) if running else None))
            return 0
        if command == "prepare":
            report(dict(prepared=host.prepare_first_run(host.data_directory)))
            return 0
        if command not in ("run", "recover"):
            raise ValueError("Expected inspect, prepare, run or recover.")
        if command == "run":
            url = urlsplit(args[1]) if len(args) == 2 else None
            if url is None or url.scheme != "http" or url.hostname != "127.0.0.1" or url.path != "/wallpaper/lively/index.html":
                raise ValueError("Expected the local Amadeus wallpaper wrapper URL.")
        lock = windows.lock(state / "session.lock")
        if command == "recover" and not journal.exists():
            host.recover_launch_images()
            report(dict(status="clean"))
            return 0
        session.prepare_recovery()
        host.ensure_running()
        session.restore(host.started_process)
        if command == "recover":
            report(dict(status="restored"))
            return 0
        host.ensure_running()
        snapshot = Snapshot(host.write_project(args[1]), host.primary_monitor(), host.get_options(),
                            host.get_wallpapers(), host.started_process)

        def body():
            report(dict(status="mounted"))
            lively = windows.running()
            if lively is None:
                raise OSError("Lively exited after mounting.")
            stopped = threading.Event()
            def read_input():
                try:
                    # A daemon blocked in TextIOWrapper.readline holds Python's
                    # buffered-reader lock during host-exit finalization. Raw IO
                    # lets the helper exit even while its parent still has stdin.
                    while (byte := os.read(sys.stdin.fileno(), 1)) and byte != b"\n":
                        pass
                finally:
                    stopped.set()
            threading.Thread(target=read_input, daemon=True).start()
            while not stopped.wait(0.1):
                if not windows.is_running(lively):
                    raise OSError("Lively exited unexpectedly; the wallpaper recovery journal is retained.")
        session.run_mounted(snapshot, body)
        report(dict(status="restored"))
        return 0
    except Exception as error:
        try:
            session.release_unstarted_host(host.started_process)
        except Exception as cleanup:
            try:
                print(error_detail(cleanup), file=sys.stderr, flush=True)
            except OSError:
                pass
        report(dict(status="error", error=error_detail(error), recoveryPending=journal.exists()))
        return 1
    finally:
        if lock is not None:
            windows.close(lock)


if __name__ == "__main__":
    raise SystemExit(main())
