from __future__ import annotations

from ctypes import wintypes
import json

from wallpaper import windows_desktop_layer as desktop_layer


def _rect(left: int, top: int, right: int, bottom: int) -> wintypes.RECT:
    return wintypes.RECT(left, top, right, bottom)


def test_lively_anchor_rejects_hidden_and_placeholder_windows() -> None:
    assert not desktop_layer._usable_lively_window(
        visible=False,
        class_name="WindowsForms10.Window.0.app.0.test",
        rect=_rect(0, 0, 3840, 2160),
    )
    assert not desktop_layer._usable_lively_window(
        visible=True,
        class_name="WindowsForms10.Window.0.app.0.test",
        rect=_rect(0, 0, 14, 14),
    )
    assert desktop_layer._usable_lively_window(
        visible=True,
        class_name="WindowsForms10.Window.0.app.0.test",
        rect=_rect(0, 0, 3840, 2160),
    )


def test_lively_anchor_requires_the_wallpaper_window_class() -> None:
    assert not desktop_layer._usable_lively_window(
        visible=True,
        class_name="GDI+ Hook Window Class",
        rect=_rect(0, 0, 3840, 2160),
    )


def test_desktop_owner_transition_requires_the_postcondition() -> None:
    class User32:
        parent = 99
        style = 0x10000000
        owner = 0

        @classmethod
        def GetParent(cls, _hwnd: int) -> int:
            return cls.parent

        @classmethod
        def GetWindowLongPtrW(cls, _hwnd: int, _index: int) -> int:
            return cls.style

        @classmethod
        def SetWindowLongPtrW(cls, _hwnd: int, _index: int, style: int) -> int:
            cls.style = style
            return 0

        @staticmethod
        def SetParent(_hwnd: int, _parent: int) -> int:
            return 0

        @classmethod
        def GetWindow(cls, _hwnd: int, _command: int) -> int:
            return cls.owner

    ok, reason = desktop_layer._ensure_desktop_owned_window(User32(), 42, 77)
    assert not ok
    assert reason == "desktop-owner-postcondition-failed"


def test_desktop_owner_transition_restores_popup_style_and_owner() -> None:
    class User32:
        parent = 99
        style = 0x50000000
        owner = 0

        @classmethod
        def GetParent(cls, _hwnd: int) -> int:
            return cls.parent

        @classmethod
        def GetWindowLongPtrW(cls, _hwnd: int, _index: int) -> int:
            return cls.style

        @classmethod
        def SetParent(cls, _hwnd: int, _parent: int) -> int:
            cls.parent = 0
            return 0

        @classmethod
        def GetWindow(cls, _hwnd: int, _command: int) -> int:
            return cls.owner

        @classmethod
        def SetWindowLongPtrW(cls, _hwnd: int, index: int, value: int) -> int:
            if index == -16:
                cls.style = value
            if index == -8:
                cls.owner = value
            return 0

    user32 = User32()
    assert desktop_layer._ensure_desktop_owned_window(user32, 42, 77)[0]
    assert user32.style & 0x80000000
    assert not user32.style & 0x40000000
    assert user32.owner == 77


def test_desktop_watch_reconciles_when_the_available_layer_changes(monkeypatch) -> None:
    alive = iter((True, True, True, False))
    placements = iter(
        (
            (True, "workerw-behind-icons", False),
            (True, "above-lively-below-apps", True),
            (True, "workerw-behind-icons", False),
        )
    )
    emitted: list[dict[str, object]] = []

    class User32:
        @staticmethod
        def IsWindow(_hwnd: int) -> bool:
            return next(alive)

    monkeypatch.setattr(desktop_layer.sys, "platform", "win32")
    monkeypatch.setattr(desktop_layer, "_user32", lambda: User32())
    monkeypatch.setattr(desktop_layer, "_attach_window_to_desktop", lambda _hwnd: next(placements))
    monkeypatch.setattr(desktop_layer.time, "sleep", lambda _seconds: None)

    desktop_layer.watch_window_on_desktop(
        42,
        interval_s=0.2,
        emit=lambda message, **_kwargs: emitted.append(json.loads(message)),
    )

    assert emitted == [
        {"ok": True, "mode": "workerw-behind-icons", "reconciled": False},
        {"ok": True, "mode": "above-lively-below-apps", "reconciled": True},
        {"ok": True, "mode": "workerw-behind-icons", "reconciled": False},
    ]


def test_desktop_watch_reemits_same_layer_only_when_native_frame_changed(monkeypatch) -> None:
    alive = iter((True, True, True, False))
    placements = iter(
        (
            (True, "above-lively-below-apps", False),
            (True, "above-lively-below-apps", True),
            (True, "above-lively-below-apps", False),
        )
    )
    emitted: list[dict[str, object]] = []

    class User32:
        @staticmethod
        def IsWindow(_hwnd: int) -> bool:
            return next(alive)

    monkeypatch.setattr(desktop_layer.sys, "platform", "win32")
    monkeypatch.setattr(desktop_layer, "_user32", lambda: User32())
    monkeypatch.setattr(desktop_layer, "_attach_window_to_desktop", lambda _hwnd: next(placements))
    monkeypatch.setattr(desktop_layer.time, "sleep", lambda _seconds: None)

    desktop_layer.watch_window_on_desktop(
        42,
        interval_s=0.2,
        emit=lambda message, **_kwargs: emitted.append(json.loads(message)),
    )

    assert emitted == [
        {"ok": True, "mode": "above-lively-below-apps", "reconciled": False},
        {"ok": True, "mode": "above-lively-below-apps", "reconciled": True},
    ]


if __name__ == "__main__":
    class _MonkeyPatch:
        def __init__(self) -> None:
            self._undo: list[tuple[object, str, object]] = []

        def setattr(self, obj, name: str, value) -> None:
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self) -> None:
            for obj, name, value in reversed(self._undo):
                setattr(obj, name, value)

    test_lively_anchor_rejects_hidden_and_placeholder_windows()
    test_lively_anchor_requires_the_wallpaper_window_class()
    test_desktop_owner_transition_requires_the_postcondition()
    test_desktop_owner_transition_restores_popup_style_and_owner()
    for test in (
        test_desktop_watch_reconciles_when_the_available_layer_changes,
        test_desktop_watch_reemits_same_layer_only_when_native_frame_changed,
    ):
        patch = _MonkeyPatch()
        try:
            test(patch)
        finally:
            patch.undo()
    print("ok: Windows desktop Slice placement and reconciliation contracts")
