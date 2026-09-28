import pytest
from PIL import Image

from server import visual_runtime


def test_general_capture_keeps_its_own_image_size_settings(monkeypatch) -> None:
    monkeypatch.setattr(visual_runtime, "_config", visual_runtime.VisionConfig(max_long_side=320, jpeg_quality=35))
    monkeypatch.setattr(visual_runtime, "_capture_image", lambda _scope: (
        Image.new("RGB", (1600, 900)), {"left": 0, "top": 0, "width": 1600, "height": 900}, "full_screen",
    ))
    result = visual_runtime.capture_visual_context()
    assert (result["frame"]["width"], result["frame"]["height"]) == (320, 180)


def test_disabling_visual_context_preserves_the_selected_mode_for_reenable() -> None:
    original = visual_runtime.get_config()
    try:
        visual_runtime.set_config({
            "vision_enabled": True,
            "vision_mode": "watching",
        })
        visual_runtime.set_config({"vision_enabled": False})
        disabled = visual_runtime.get_config()
        assert disabled["enabled"] is False
        assert disabled["mode"] == "watching"

        visual_runtime.set_config({"vision_enabled": True})
        enabled = visual_runtime.get_config()
        assert enabled["enabled"] is True
        assert enabled["mode"] == "watching"
    finally:
        visual_runtime.set_config({
            "vision_enabled": original["enabled"],
            "vision_mode": original["mode"],
            "vision_scope": original["scope"],
            "vision_provider": original["provider"],
            "vision_max_long_side": original["max_long_side"],
            "vision_jpeg_quality": original["jpeg_quality"],
            "vision_region": original["region"],
            "vision_window_handle": original["window_handle"],
        })


def test_selected_window_scope_never_expands_to_full_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(visual_runtime, "_configured_window_rect", lambda: None)
    with pytest.raises(RuntimeError, match="selected vision window"):
        visual_runtime._resolve_capture_region(
            "selected_window",
            {"left": 0, "top": 0, "width": 1920, "height": 1080},
        )


def test_region_scope_never_expands_to_full_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(visual_runtime._config, "region", "")
    with pytest.raises(RuntimeError, match="Vision region"):
        visual_runtime._resolve_capture_region(
            "region",
            {"left": 0, "top": 0, "width": 1920, "height": 1080},
        )
