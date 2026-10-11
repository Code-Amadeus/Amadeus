"""Owned child-process lifetime containment, including abrupt Host exit on Windows."""
from __future__ import annotations

import ctypes
import os
import signal
import subprocess
from ctypes import wintypes


class ProcessTree:
    def __init__(self, process: subprocess.Popen) -> None:
        self.process = process
        self.job = None
        if os.name != "nt":
            return
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        job = kernel.CreateJobObjectW(None, None)
        if not job:
            process.kill()
            raise OSError("cannot create extension lifetime job")
        limits = Extended()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(job, wintypes.HANDLE(process._handle)):
            kernel.CloseHandle(job)
            process.kill()
            raise OSError("cannot contain extension process tree")
        self.job, self.kernel = job, kernel

    def resume(self) -> None:
        """Resume only after Job assignment; no startup hook can escape the tree."""
        if os.name != "nt":
            return
        kernel = self.kernel

        class ThreadEntry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                        ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD)]

        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenThread.restype = wintypes.HANDLE
        kernel.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel.ResumeThread.restype = wintypes.DWORD
        snapshot = kernel.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise OSError("cannot inspect suspended extension thread")
        entry = ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        resumed = False
        try:
            more = kernel.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.th32OwnerProcessID == self.process.pid:
                    handle = kernel.OpenThread(0x2, False, entry.th32ThreadID)  # THREAD_SUSPEND_RESUME
                    if not handle:
                        raise OSError("cannot resume extension process")
                    try:
                        if kernel.ResumeThread(handle) == 0xFFFFFFFF:
                            raise OSError("cannot resume extension process")
                        resumed = True
                    finally:
                        kernel.CloseHandle(handle)
                more = kernel.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            kernel.CloseHandle(snapshot)
        if not resumed:
            raise OSError("suspended extension process has no thread")

    def terminate(self) -> None:
        if self.job is not None:
            self.kernel.CloseHandle(self.job)
            self.job = None
        elif os.name == "nt":
            if self.process.poll() is None:
                self.process.kill()
        else:
            # A direct child may exit gracefully while its owned descendants live.
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)
