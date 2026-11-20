"""Opt-in reversible fresh-settings probe on installed Lively, not clean-machine qualification."""
import importlib.util
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("wallpaper_helper_probe", ROOT / "electron/resources/windows-wallpaper/helper.py")
h = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = h
spec.loader.exec_module(h)


def main():
    windows = h.Windows()
    host = h.LivelyHost(windows)
    journal = host.data_directory.parent / "Amadeus/wallpaper/session.json"
    if journal.exists():
        raise RuntimeError("Resolve the active wallpaper session before the cold-start probe.")
    original = windows.running()
    baseline = host.get_wallpapers() if original else []
    backup = ROOT / "build/windows-wallpaper" / ("first-run-backup-" + str(time.time_ns()))
    backup.mkdir(parents=True)
    saved = {}
    for name in ("Settings.json", "WallpaperLayout.json"):
        file = host.data_directory / name
        saved[name] = file.read_bytes() if file.exists() else None
        if saved[name] is not None:
            (backup / name).write_bytes(saved[name])
    try:
        if original:
            host.stop_owned_process(original)
        (host.data_directory / "Settings.json").unlink(missing_ok=True)
        (host.data_directory / "WallpaperLayout.json").write_text("[]")
        h.LivelyHost.prepare_first_run(host.data_directory)
        fresh = host.data_directory / "Settings.json"
        configuration = json.loads(fresh.read_text())
        configuration["WallpaperDir"] = str(backup / "test-library")
        h.atomic_json(fresh, configuration)
        prepared = h.LivelyHost(windows)
        prepared.ensure_running()
        time.sleep(3)
        # Toolhelp's same-session process list is queried separately for the UI.
        import subprocess
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "@(Get-Process Lively.UI.WinUI -ErrorAction SilentlyContinue | Where-Object SessionId -eq "
            "(Get-Process -Id $PID).SessionId).Count"], capture_output=True, text=True, check=True, timeout=15)
        assert int(result.stdout.strip()) == 0, "Fresh launch opened Lively UI"
        assert prepared.get_options().Browser == 1
        configuration = json.loads(fresh.read_text(encoding="utf-8-sig"))
        assert not configuration["IsFirstRun"] and not configuration["Startup"]
        project = backup / "test-library/wallpapers/amadeus-cold-start-test"
        project.mkdir(parents=True, exist_ok=True)
        page = project / "index.html"
        page.write_text("<!doctype html><body style='background:#102436;color:white'>Amadeus startup experiment</body>")
        h.atomic_json(project / "LivelyInfo.json", dict(Title="Amadeus startup experiment", Type=1,
                                                       FileName=str(page), IsAbsolutePath=True))
        session = h.Session(prepared, backup / "test-session.json")
        session.mount(h.Snapshot(str(project), prepared.primary_monitor(), prepared.get_options(),
                                 prepared.get_wallpapers(), prepared.started_process))
        session.restore()
        assert windows.running() is None, "Newly owned Lively process was not released"
        h.atomic_json(backup.parent / "first-run-experiment.json", dict(passed=True, wizardVisible=False, webView2=True,
                      independentAutostart=False, ownedHostReleased=True, kind="fresh-settings-on-installed-Lively"))
        print("PASS fresh settings: no UI, WebView2, no independent autostart, owned host released", flush=True)
    finally:
        running = windows.running()
        if running:
            host.stop_owned_process(running)
        for name, content in saved.items():
            file = host.data_directory / name
            if content is None:
                file.unlink(missing_ok=True)
            else:
                file.write_bytes(content)
        if original:
            host.ensure_running()
            until = time.monotonic() + 30
            while host.get_wallpapers() != baseline and time.monotonic() < until:
                time.sleep(0.2)
            assert host.get_wallpapers() == baseline, "Original Lively layout did not restore"
        print("Original settings, layout and running state restored.", flush=True)


if __name__ == "__main__":
    main()
