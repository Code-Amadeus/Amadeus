import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("companion_layer", Path(__file__).parents[1] / "electron/tools/companion_window_layer.py")
layer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(layer)


class Windows:
    def __init__(self, foreground=20, owner=None, topmost=False, name="Application"):
        self.foreground, self.owner, self.topmost, self.name = foreground, owner or foreground, topmost, name
        self.calls = []

    def GetForegroundWindow(self): return self.foreground
    def IsWindowVisible(self, _): return True
    def GetAncestor(self, *_): return self.owner
    def GetClassNameW(self, _, buffer, __): buffer.value = self.name
    def GetWindowLongPtrW(self, *_): return 8 if self.topmost else 0
    def GetWindow(self, *_): return self.owner
    def SetWindowPos(self, *args): self.calls.append(args); return True


def test_follow_switches_only_companion_behind_latest_app_without_focus_or_geometry_changes():
    windows = Windows(20)
    assert layer.place_behind_active(windows, 10)["ok"]
    assert windows.calls[-1][:2] == (10, 20)
    windows.foreground = windows.owner = 30
    result = layer.place_behind_active(windows, 10)
    assert windows.calls[-1][:2] == (10, 30)
    assert result["foreground"] == 30
    assert all(call[-1] & 0x13 == 0x13 for call in windows.calls)


def test_active_companion_is_left_in_front_and_dialog_owner_group_stays_above_it():
    own = Windows(10)
    assert layer.place_behind_active(own, 10)["mode"] == "companion-active"
    assert own.calls == []
    dialog = Windows(21, owner=20)
    layer.place_behind_active(dialog, 10)
    assert dialog.calls[-1][:2] == (10, 20)


def test_topmost_and_shell_windows_do_not_promote_the_companion_or_reorder_other_windows():
    topmost = Windows(topmost=True)
    layer.place_behind_active(topmost, 10)
    assert topmost.calls[-1][:2] == (10, 0)
    shell = Windows(name="Shell_TrayWnd")
    assert layer.place_behind_active(shell, 10)["mode"] == "shell-active"
    assert shell.calls == []
