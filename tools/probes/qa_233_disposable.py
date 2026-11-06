"""One-off integration instrument. NEVER run on a personal desktop."""
import ctypes
import atexit
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

if os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted" or os.environ.get("GITHUB_REF") != "refs/heads/codex/qa-233-stock-lively":
    raise SystemExit("Isolated GitHub-hosted QA VM required")
root = Path(__file__).resolve().parents[2]
hp = root / "electron/resources/windows-wallpaper/helper.py"
spec = importlib.util.spec_from_file_location("helper_233", hp)
h = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = h
spec.loader.exec_module(h)
win = h.Windows()
host = h.LivelyHost(win)
journal = Path(os.environ["LOCALAPPDATA"]) / "Amadeus/wallpaper/session.json"
base = Path(os.environ["RUNNER_TEMP"]) / "stock-233"
base.mkdir(exist_ok=False)
exe = root / "electron/node_modules/electron/dist/electron.exe"
external = ["A", "B"]

# Diagnostic run only: add stack locations to the failing helper's report;
# no changes to process queries, timing, registry, or recovery decisions.
source = hp.read_text(encoding="utf-8")
needle = "    except Exception as error:\n        try:\n            session.release_unstarted_host"
assert source.count(needle) == 1
hp.write_text(source.replace(needle, """    except Exception as error:
        import traceback
        def describe(exc):
            return {"type": type(exc).__name__, "winerror": getattr(exc, "winerror", None),
                    "stack": [(f.name, f.lineno) for f in traceback.extract_tb(exc.__traceback__)],
                    "sub": [describe(e) for e in getattr(exc, "exceptions", [])]}
        with (Path(os.environ["RUNNER_TEMP"]) / "stock-233" / "helper-errors.jsonl").open("a", encoding="utf8") as qa_log:
            print(json.dumps(describe(error)), file=qa_log)
        try:
            session.release_unstarted_host"""), encoding="utf-8")


def report(event, **facts):
    print(json.dumps(dict(event=event, **facts), ensure_ascii=True), flush=True)


def drain_traces():
    trace = base / "helper-errors.jsonl"
    if trace.exists():
        for line in trace.read_text(encoding="utf8").splitlines():
            report("helper-error-trace", error=json.loads(line))
        trace.unlink()


atexit.register(drain_traces)

def scenes():
    return sorted(Path(s.Path).name for s in host.get_wallpapers()) if win.running() else []


def desktop_picture():
    buf = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.user32.SystemParametersInfoW(0x73, len(buf), buf, 0):
        raise ctypes.WinError()
    return buf.value


def journal_state():
    if not journal.exists():
        return None
    state = h.Session(host, journal).read()
    return (state.Phase, state.Mounted)


def counts():
    return len(list(win.managed_player_displays()))


def recover():
    p = subprocess.run([sys.executable, "-I", "-S", str(hp), "recover"], capture_output=True, text=True, timeout=110)
    msg = json.loads(p.stdout.strip().splitlines()[-1])
    report("recover", status=msg.get("status"), error=msg.get("error"), pending=journal.exists(), players=counts(),
           running=win.running() is not None, scenes=scenes())
    return p.returncode == 0


def fixture(name):
    directory = base / name
    directory.mkdir(exist_ok=True)
    page = directory / "index.html"
    page.write_text("<html><body>Synthetic wallpaper " + name + "</body></html>", encoding="utf8")
    h.atomic_json(directory / "LivelyInfo.json", dict(AppVersion=h.SUPPORTED_VERSION, Title=name,
                  Author="QA", Desc="Synthetic scene", Type=1, FileName=str(page), IsAbsolutePath=True,
                  Arguments="", Contact="", License="CC0"))
    return h.DesktopItem(host.primary_monitor(), str(directory))


def actual(case, expected, shared=None):
    assert not journal.exists()
    work = base / case
    work.mkdir()
    env = os.environ.copy()
    env.update(QA233_DIR=str(work), AMADEUS_PYTHON=sys.executable, AMADEUS_E2E_NO_TTS="1",
               VTS_ENABLED="0", VTS_HEARTBEAT_ENABLED="0", VTS_RECONNECT_ENABLED="0")
    env.pop("ELECTRON_RUN_AS_NODE", None)
    before_pic = desktop_picture()
    options = host.get_options() if shared else None
    reported_false_empty = False
    saw_player = False
    passed = False
    observed = None
    with (work / "electron.log").open("w", encoding="utf8") as log:
        child = subprocess.Popen([str(exe), str(root / "electron/scripts/qa-233-disposable.cjs")], env=env,
                                 stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            deadline = time.monotonic() + 100
            while time.monotonic() < deadline:
                state = journal_state()
                players = counts()
                saw_player |= bool(players)
                try:
                    rows = scenes()
                except OSError:
                    rows = None
                if players and rows is not None and "amadeus-managed" not in rows and state and state[0] == "Settled":
                    reported_false_empty = True
                now = (state, players, rows, (work / "slice-loaded").exists())
                if now != observed:
                    report("observation", case=case, journal=state, players=players, scenes=rows, slice=now[-1])
                    observed = now
                if state == ("Settled", True) and players and rows is not None and "amadeus-managed" in rows and now[-1]:
                    break
                if child.poll() is not None or (work / "errors").exists():
                    raise RuntimeError("product startup failed")
                time.sleep(.25)
            else:
                raise RuntimeError("actual desktop never confirmed managed scene, player and loaded Slice")
            if case == "user-change":
                host.set_wallpaper(fixture("B"))
                assert scenes() == ["B"]
            if case == "stock-close-negative":
                # The verified stock Close RPC loads LivelyInfo.json before
                # closing. Temporarily withhold only OUR generated project's
                # metadata on this clean VM, not any user's project or DLL.
                project = Path(h.Session(host, journal).read().Project).resolve()
                assert project.name == "amadeus-managed" and project.parent.name == "wallpapers"
                metadata, held = project / "LivelyInfo.json", project / "LivelyInfo.QA233-held"
                assert json.loads(metadata.read_text(encoding="utf8"))["Author"] == "Amadeus" and not held.exists()
                metadata.rename(held)
                try:
                    (work / "quit").touch()
                    child.wait(70)
                    report("stock-close-withheld", journal=journal.exists(), players=counts(), scenes=scenes(),
                           host_running=win.running() is not None)
                    assert journal.exists() and counts() > 0 and win.running() is not None
                    assert not recover() and journal.exists() and counts() > 0
                finally:
                    held.rename(metadata)
                assert recover()
            if case == "helper-loss":
                # Match the exact helper launched by this Electron process.
                command = ("@(Get-CimInstance Win32_Process -Filter 'ParentProcessId = " + str(child.pid) +
                           "' | Where-Object { $_.Name -match '^python(w)?[.]exe$' -and "
                           "$_.CommandLine.Contains('windows-wallpaper') -and $_.CommandLine.Contains('helper.py') } "
                           "| Select-Object -ExpandProperty ProcessId) | ConvertTo-Json -Compress")
                ids = json.loads(subprocess.check_output(["powershell", "-NoProfile", "-Command", command], text=True))
                pid = ids[0] if isinstance(ids, list) and len(ids) == 1 else ids
                assert isinstance(pid, int)
                # Terminate only the disposable test's helper and its venv child.
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=True, stdout=subprocess.DEVNULL)
                assert journal.exists()
            (work / "quit").touch()
            assert child.wait(60) == 0
            if case == "helper-loss":
                assert journal.exists()
                assert recover()
            assert not journal.exists() and counts() == 0
            assert win.running() == shared and scenes() == expected
            if shared:
                assert host.get_options() == options
            assert desktop_picture() == before_pic
            passed = True
        finally:
            (work / "quit").touch(exist_ok=True)
            if child.poll() is None:
                try:
                    child.wait(40)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(10)
            report("result", case=case, passed=passed, saw_managed_process=saw_player,
                   observed_player_missing_from_report=reported_false_empty, pending=journal.exists(),
                   player_count=counts(), host_running=win.running() is not None)
            if not passed:
                # Emit only known diagnostics from this entirely synthetic profile.
                for file in [work / "errors", work / "electron.log"]:
                    if file.exists():
                        for line in file.read_text(encoding="utf8", errors="replace").splitlines()[-200:]:
                            if any(t in line.lower() for t in ("qa233_stack", "traceback", "error:", "failed", "not found", "timed out", "exception")) and not any(t in line.lower() for t in ("token", "api_key", "bearer", "authorization")):
                                report("diagnostic", message=line[:1200])


assert not win.running() and not journal.exists()
report("desktop", shell=bool(ctypes.windll.user32.GetShellWindow()), monitors=ctypes.windll.user32.GetSystemMetrics(80))
failures = []
groups = {
    "owned": ("empty-owned", "empty-owned-repeat", "empty-owned-third", "helper-loss"),
    "shared": ("shared-original", "user-change"),
    "negative": ("stock-close-negative",),
}
for name in groups[os.environ["QA233_GROUP"]]:
    shared = None
    try:
        if name in ("shared-original", "user-change"):
            host.ensure_running()
            shared = host.started_process
            assert shared and win.running() == shared
            host.set_wallpaper(fixture("A"))
            assert scenes() == ["A"]
        actual(name, ["B"] if name == "user-change" else ["A"] if shared else [], shared)
    except Exception as error:
        failures.append(name)
        report("case-failure", case=name, error=type(error).__name__, detail=str(error)[:220])
    finally:
        if journal.exists() and not recover():
            snap = h.Session(host, journal).read()
            if snap.Phase == "Pending" and snap.OwnedProcess and win.running() == snap.OwnedProcess == snap.RequestProcess:
                report("pending-operator-recovery", original_receiver_positively_identified=True)
                host.stop_owned_process(snap.OwnedProcess)
                assert not win.is_running(snap.OwnedProcess)
                assert recover()
            else:
                raise RuntimeError("Unproven cleanup; stop this disposable VM")
        if shared and win.running() == shared and not journal.exists():
            for n in external:
                if (base / n).exists():
                    host.close_project(str(base / n))
            host.stop_owned_process(shared)
    drain_traces()
    if win.running() or journal.exists():
        raise SystemExit("Unexpected state after recovery; stopping isolated qualification")
report("finish", failed=failures)
raise SystemExit(bool(failures))
