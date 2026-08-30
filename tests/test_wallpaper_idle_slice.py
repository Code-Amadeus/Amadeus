from __future__ import annotations

from server.handlers.wallpaper_handler import WallpaperHandler


def configured_handler(projected: dict) -> tuple[WallpaperHandler, list[dict]]:
    calls: list[dict] = []

    class Host:
        @staticmethod
        def set_canvas(payload: dict) -> None:
            calls.append(payload)

    handler = WallpaperHandler()
    handler._wallpaper_host = Host()
    handler._canvas_projector = lambda _payload: dict(projected)
    return handler, calls


def test_empty_canvas_projection_collapses_slice() -> None:
    handler, calls = configured_handler(projected={})

    assert handler._apply_canvas({}) is True
    assert calls[-1] == {"clear": True, "visible": False, "expanded": False}


def test_selected_work_projection_remains_visible() -> None:
    payload = {
        "phase": "Work",
        "title": "Running task",
        "workContext": {"workItemId": "work-1"},
    }
    handler, calls = configured_handler(projected=payload)

    assert handler._apply_canvas({}) is True
    assert calls[-1]["workContext"]["workItemId"] == "work-1"
    assert calls[-1].get("visible") is not False


def test_markdown_work_result_remains_visible() -> None:
    handler, calls = configured_handler(projected={"markdown": "work result"})

    assert handler._apply_canvas({}) is True
    assert calls[-1]["markdown"] == "work result"


def test_permission_projection_remains_visible() -> None:
    payload = {"permissionRequest": {"id": "permission-1"}}
    handler, calls = configured_handler(projected=payload)

    assert handler._apply_canvas({}) is True
    assert calls[-1]["permissionRequest"]["id"] == "permission-1"
