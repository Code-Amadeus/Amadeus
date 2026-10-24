"""Desktop-owned cleanup evidence; this module never terminates a process.

Install the tracking Job before eager runtime imports. A clean receipt requires
successful Host teardown and a drained non-breakaway Job. A crash leaves the
desktop's pre-launch registration unresolved, even after every PID has exited.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

LAUNCH_ENV = "AMADEUS_BACKEND_CLEANUP_LAUNCH"
SCHEMA = "amadeus.backend-cleanup.v1"


def _ordinary_path(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("cleanup path must be absolute")
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if item.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("cleanup path cannot traverse a reparse point")


def _atomic_json(path: Path, value: dict) -> None:
    _ordinary_path(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    # The desktop creates the receipt directory before registering the launch.
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    _ordinary_path(path)
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        kernel.MoveFileExW.restype = wintypes.BOOL
        if not kernel.MoveFileExW(str(temporary), str(path), 0x8):  # WRITE_THROUGH, never replace
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.link(temporary, path)
        temporary.unlink()
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class TrackingJob:
    """A Windows containment counter, with no termination or breakaway flags.

    Nested-job accounting includes MCP/Provider child jobs. Closing this handle
    cannot kill their processes. External HTTP daemons are never assigned.
    """

    def __init__(self, name: str, *, kernel=None, assign=True):
        if kernel is None:
            if os.name != "nt":
                raise OSError("cleanup proof requires Windows Job accounting")
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel.CreateJobObjectW.restype = wintypes.HANDLE
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                        ctypes.c_void_p, wintypes.DWORD,
                                                        ctypes.c_void_p]
            kernel.QueryInformationJobObject.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel = kernel
        self.name = name
        if os.name == "nt":
            ctypes.set_last_error(0)
        self.handle = kernel.CreateJobObjectW(None, name)
        if not self.handle:
            raise OSError("cannot create backend tracking Job")
        if kernel is not None and os.name == "nt" and ctypes.get_last_error() == 183:
            self.close()
            raise OSError("backend tracking Job identity already exists")
        # A new Job has zero limits: no KILL_ON_JOB_CLOSE, BREAKAWAY_OK or
        # SILENT_BREAKAWAY_OK. Never call SetInformationJobObject here.
        if assign:
            self.assign_current()

    def assign_current(self) -> None:
        if not self.kernel.AssignProcessToJobObject(self.handle, self.kernel.GetCurrentProcess()):
            self.close()
            raise OSError("cannot assign backend tracking Job")

    def only_backend_remains(self) -> bool:
        try:
            return self.active_processes() == 1
        except OSError:
            return False

    def active_processes(self) -> int:
        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_int64) for name in (
                "TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime",
                "ThisPeriodTotalKernelTime")] + [(name, wintypes.DWORD) for name in (
                "TotalPageFaultCount", "TotalProcesses", "ActiveProcesses",
                "TotalTerminatedProcesses")]
        information = Accounting()
        if not self.handle or not self.kernel.QueryInformationJobObject(
                self.handle, 1, ctypes.byref(information), ctypes.sizeof(information), None):
            raise OSError("backend tracking Job accounting is unavailable")
        return information.ActiveProcesses

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _creation_identity() -> str:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    times = [wintypes.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(kernel.GetCurrentProcess(), *(ctypes.byref(item) for item in times)):
        raise OSError("backend creation identity is unavailable")
    return str(times[0].dwLowDateTime | times[0].dwHighDateTime << 32)


class BackendCleanup:
    def __init__(self, launch: dict, *, job_factory=None,
                 identity_factory=_creation_identity, observer_factory=None):
        self.launch = launch
        self.job = None
        self.creation = None
        self.runtime_settled = False
        self.failure: str | None = None
        try:
            owner = launch["owner"]
            if (launch.get("schema") != SCHEMA or not isinstance(owner, dict)
                    or set(owner) != {"id", "nonce", "project_root", "python_command"}
                    or not re.fullmatch(r"[0-9a-f]{32}", owner["id"])
                    or not isinstance(owner["nonce"], str) or len(owner["nonce"]) < 16
                    or Path(owner["project_root"]).resolve() != Path(__file__).resolve().parents[1]
                    or not isinstance(owner["python_command"], str) or not owner["python_command"]):
                raise ValueError("invalid desktop cleanup launch identity")
            nonce = os.environ.get("AMADEUS_BACKEND_INSTANCE_NONCE")
            if nonce is not None and nonce != owner["nonce"]:
                raise ValueError("cleanup launch does not match authenticated desktop")
            command = shutil.which(owner["python_command"]) or owner["python_command"]
            if Path(command).resolve() != Path(sys.executable).resolve():
                raise ValueError("cleanup launch Python executable does not match")
            state_path = Path(launch["state_path"])
            _ordinary_path(state_path)
            self.receipt = Path(str(state_path) + ".receipts") / f"{owner['id']}.json"
            self.approval = self.receipt.with_suffix(".approval.json")
            _ordinary_path(self.receipt)
            index = json.loads(state_path.read_text(encoding="utf-8"))
            if index.get("schema") != SCHEMA or owner not in index.get("owners", []):
                raise ValueError("cleanup launch is not registered by desktop")
            self.job_name = f"Global\\AmadeusBackendCleanup-{owner['id']}"
            self.creation = identity_factory()
            self.job = (job_factory or (lambda name: TrackingJob(name, assign=False)))(self.job_name)
            # The observer starts outside the Job. It keeps a query handle after
            # the backend exits: named Jobs cannot be reopened reliably once
            # their last handle closes, even when descendants are still alive.
            (observer_factory or _start_observer)(self)
            self.job.assign_current()
        except Exception as exc:
            # Ordinary backend startup may continue; unknown containment never
            # authorizes package replacement and is visible in its pending owner.
            self.failure = type(exc).__name__

    def record_runtime_cleanup(self, *, providers_settled: bool, work_settled: bool,
                               teardown_succeeded: bool) -> None:
        self.runtime_settled = (providers_settled is True and work_settled is True
                                and teardown_succeeded is True)

    def finish(self) -> bool:
        if (self.failure or not self.runtime_settled or self.job is None
                or not self.creation or not self.job.only_backend_remains()):
            return False
        _atomic_json(self.approval, {
            "schema": SCHEMA, "owner": self.launch["owner"], "state": "settled",
            "process": {"pid": os.getpid(), "created": self.creation},
            "job_name": self.job_name,
            "proof": "windows-job-drained", "providers_settled": True,
            "work_settled": True,
        })
        # Keep the non-inheritable tracking handle open until process exit.
        # Nothing after this call may launch another child.
        return True


def begin_from_environment() -> BackendCleanup | None:
    raw = os.environ.pop(LAUNCH_ENV, "")
    if not raw:
        return None
    if len(raw) > 64 * 1024:
        raise ValueError("cleanup launch exceeds limit")
    return BackendCleanup(json.loads(raw))


def _start_observer(proof: BackendCleanup) -> None:
    request = {"launch": proof.launch, "process": {"pid": os.getpid(), "created": proof.creation},
               "job_name": proof.job_name}
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() in {"SYSTEMROOT", "WINDIR"}}
    child = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--observe",
                              json.dumps(request, separators=(",", ":"))],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             env=environment, **({"creationflags": subprocess.CREATE_NO_WINDOW}
                                                if os.name == "nt" else {}))
    ready = proof.receipt.with_suffix(".observer-ready.json")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if ready.exists():
            _ordinary_path(ready)
            identity = json.loads(ready.read_text(encoding="utf-8"))
            if identity.get("job_name") != proof.job_name or identity.get("owner") != proof.launch["owner"]:
                raise ValueError("cleanup observer identity mismatch")
            return
        if child.poll() is not None:
            raise OSError("cleanup observer exited before its query handle was ready")
        time.sleep(.01)
    raise TimeoutError("cleanup observer did not become ready")


def _observe(request: dict) -> None:
    """No application imports, credentials, provider work or process termination."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenJobObjectW.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                wintypes.DWORD, ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    process = request["process"]
    owner = request["launch"]["owner"]
    receipt = Path(str(request["launch"]["state_path"]) + ".receipts") / f"{owner['id']}.json"
    _ordinary_path(receipt)
    job = TrackingJob.__new__(TrackingJob)
    job.kernel = kernel
    job.handle = kernel.OpenJobObjectW(4, False, request["job_name"])  # QUERY only
    root = kernel.OpenProcess(0x100000 | 0x1000, False, process["pid"])  # wait + identity
    try:
        if not job.handle or not root:
            raise OSError("cleanup observer cannot acquire lifetime handles")
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(root, *(ctypes.byref(item) for item in times)):
            raise OSError("cleanup observer cannot identify backend")
        created = str(times[0].dwLowDateTime | times[0].dwHighDateTime << 32)
        if created != process["created"]:
            raise ValueError("cleanup observer backend creation identity mismatch")
        _atomic_json(receipt.with_suffix(".observer-ready.json"), {
            "owner": owner, "job_name": request["job_name"],
        })
        if kernel.WaitForSingleObject(root, 0xFFFFFFFF) != 0:
            raise OSError("cleanup observer could not observe backend exit")
        approval_path = receipt.with_suffix(".approval.json")
        if not approval_path.exists():
            _atomic_json(receipt.with_suffix(".failed.json"), {
                "schema": SCHEMA, "owner": owner, "reason": "backend_cleanup_approval_missing",
            })
            return  # Crash/failed teardown: registration remains unresolved.
        _ordinary_path(approval_path)
        approval = json.loads(approval_path.read_text(encoding="utf-8"))
        if (approval.get("schema") != SCHEMA or approval.get("state") != "settled"
                or approval.get("owner") != owner or approval.get("process") != process
                or approval.get("job_name") != request["job_name"]
                or approval.get("proof") != "windows-job-drained"
                or approval.get("providers_settled") is not True
                or approval.get("work_settled") is not True):
            raise ValueError("cleanup observer approval mismatch")
        deadline = time.monotonic() + 30
        while job.active_processes() != 0:
            if time.monotonic() >= deadline:
                raise TimeoutError("owned descendants survived backend cleanup")
            time.sleep(.05)
        _atomic_json(receipt, {**approval, "observer": {
            "pid": os.getpid(), "created": _creation_identity(),
        }})
    except Exception as exc:
        _atomic_json(receipt.with_suffix(".failed.json"), {"schema": SCHEMA, "owner": owner,
                                                       "reason": type(exc).__name__})
        raise
    finally:
        job.close()
        if root:
            kernel.CloseHandle(root)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--observe" or os.name != "nt":
        raise SystemExit(2)
    _observe(json.loads(sys.argv[2]))
