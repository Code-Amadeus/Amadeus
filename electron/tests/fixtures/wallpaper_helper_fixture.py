"""Run the real helper/Session with a synthetic desktop in an isolated profile."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

state = Path(os.environ["AMADEUS_WALLPAPER_TEST_STATE"])
spec = importlib.util.spec_from_file_location("real_wallpaper_helper", os.environ["AMADEUS_WALLPAPER_TEST_HELPER"])
h = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = h
spec.loader.exec_module(h)
os.environ["LOCALAPPDATA"] = str(state / "local")


class FixtureWindows(h.Windows):
    def running(self):
        return self.identity(os.getpid())


class FixtureHost:
    instance = None
    started_process = None

    def __init__(self, windows):
        FixtureHost.instance = self
        self.windows = windows
        self.items = [h.DesktopItem("primary", "old")]
        self.options = h.HostOptions(0, 1, False, False)

    def ensure_running(self):
        pass

    def recover_launch_images(self):
        pass

    def write_project(self, url):
        return "amadeus-managed"

    def primary_monitor(self):
        return "primary"

    def get_options(self):
        return self.options

    def get_wallpapers(self):
        return list(self.items)

    def set_session_options(self):
        pass

    def restore_options(self, previous, images=True):
        self.options = previous

    def set_wallpaper(self, wallpaper, request_settled=None, request_dispatching=None):
        if request_dispatching:
            request_dispatching(self.windows.identity(os.getpid()))
        self.items = [wallpaper]
        if request_settled:
            request_settled()

    def close_project(self, project):
        # Restoration can legitimately outlive the parent's entire exit. The
        # supervisor releases this gate only after observing Electron's exit.
        (state / "cleanup-entered").touch()
        until = time.monotonic() + 30
        while not (state / "cleanup-allow").exists():
            if time.monotonic() >= until:
                raise TimeoutError("test cleanup gate was not released")
            time.sleep(0.01)
        self.items = [item for item in self.items if item.Path != project]


h.Windows, h.LivelyHost = FixtureWindows, FixtureHost
identity = FixtureWindows().identity(os.getpid())
(state / "started.json").write_text(json.dumps(dict(Id=identity.Id, Started=str(identity.Started))), encoding="utf-8")
code = h.main()
if code == 0:
    journal = state / "local/Amadeus/wallpaper/session.json"
    assert not journal.exists()
    assert FixtureHost.instance.items == [h.DesktopItem("primary", "old")]
    (state / "restored.json").write_text(json.dumps(dict(restored=True, journalRemoved=True)), encoding="utf-8")
raise SystemExit(code)
