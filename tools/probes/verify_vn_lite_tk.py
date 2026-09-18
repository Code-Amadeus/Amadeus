"""Native verification: original Tk shell unchanged, only the avatar uses Lite atlases."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.vn_portrait_overlay_lite import load_legacy, overlay_class


def pump(overlay, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        overlay.root.update()
        time.sleep(.005)


def shell(overlay):
    return {"size": [overlay.card_width, overlay.card_height], "alpha": overlay.root.attributes("-alpha"),
            "avatar_position": overlay.avatar_label.place_info(), "text_position": overlay.text_label.place_info(),
            "caption_font": str(overlay.text_label.cget("font")), "caption_bg": overlay.text_label.cget("bg"),
            "caption_fg": overlay.text_label.cget("fg"), "canvas_bg": overlay.frame.cget("bg")}


def dispose(overlay):
    overlay.server.shutdown()
    overlay.server.server_close()
    for timer in overlay.root.tk.call('after', 'info'):
        overlay.root.after_cancel(timer)
    overlay.root.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-helper", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    legacy = load_legacy(args.legacy_helper)
    cls = overlay_class(legacy)
    methods = ("_layout_card", "_start_drag", "_drag", "_scan_tick", "_tick", "_poll_queue", "_start_http_server")
    assert all(getattr(cls, name) is getattr(legacy.PortraitOverlayTk, name) for name in methods)
    common = dict(images_dir=ROOT / "assets/images", cache_dir=legacy.DEFAULT_CACHE_DIR,
                  host="127.0.0.1", port=0, x=60, y=80, crop_side_ratio=.74, crop_y_ratio=.035)
    original = legacy.PortraitOverlayTk(**common)
    try:
        pump(original, .1)
        before = shell(original)
    finally:
        dispose(original)
    overlay = cls(**common, lite_dir=ROOT / "assets/companion/kurisu")
    try:
        pump(overlay, .1)
        after = shell(overlay)
        # Widget paths are interpreter-local identities, not presentation differences.
        for snapshot in (before, after):
            for key in ("avatar_position", "text_position"):
                snapshot[key].pop("in", None)
        assert before == after, (before, after)
        assert overlay._lite and not overlay._photo_frames and not overlay._projection_sources
        initial_images = len(overlay.root.tk.call('image', 'names'))
        overlay.apply_reaction({"source": "vn_playback", "sentence_id": "1", "speaking": True,
                                "emotion": "thinking", "display_text": "保留原来的 VN 外框，只更新头像绘制。"})
        first_variant = overlay._lite.spec["url"]
        draws = overlay._lite.draws
        pump(overlay, .85)
        speech_draws = overlay._lite.draws - draws
        assert speech_draws >= 15, speech_draws
        assert overlay._lite.resident_bytes <= 16 * 1024**2
        overlay.apply_reaction({"source": "vn_pretranslation", "sentence_id": "1", "display_text": "字幕保持，外框不改。"})
        assert overlay._current_state == "speaking"
        overlay.apply_reaction({"source": "vn_playback", "sentence_id": "1", "speaking": False})
        pump(overlay, .15)
        assert overlay._current_emotion == "sided_thinking"
        pump(overlay, .3)
        assert overlay._current_emotion == "normal" and overlay._current_state == "idle"
        assert overlay.text_var.get() == "字幕保持，外框不改。"
        overlay.apply_reaction({"source": "vn_playback", "sentence_id": "2", "speaking": True, "emotion": "thinking"})
        assert overlay._lite.spec["url"] != first_variant
        overlay.apply_reaction({"source": "vn_playback", "sentence_id": "1", "speaking": False})
        assert overlay._current_state == "speaking"
        for n in range(30):
            overlay._set_emotion("happy" if n % 2 else "normal", "speaking" if n % 3 else "idle")
        assert len(overlay.root.tk.call('image', 'names')) <= initial_images + 1
        assert overlay._lite.resident_bytes <= 16 * 1024**2 and len(overlay._lite.entries) <= 2
        overlay.root.withdraw(); pump(overlay, .05)
        paused = overlay._lite.draws
        pump(overlay, .3)
        assert overlay._lite.draws == paused and overlay._atlas_timer is None
        overlay.root.deiconify(); pump(overlay, .15)
        assert not overlay._lite.paused
        player = overlay._lite
    finally:
        dispose(overlay)
    assert player.resident_bytes == 0 and not player.entries
    fallback = cls(**common, lite_dir=args.output / "no-pack")
    try:
        assert fallback._lite is None and fallback._photo_frames
    finally:
        dispose(fallback)
    static = cls(**common, lite_dir=ROOT / "assets/companion/kurisu", static_idle=True)
    try:
        pump(static, .1)
        draws = static._lite.draws
        pump(static, .4)
        assert static._lite.draws == draws and static._atlas_timer is None
    finally:
        dispose(static)
    report = {"ok": True, "unchanged_shell_methods": list(methods), "shell": after,
              "speech_draws_in_850ms": speech_draws, "first_thinking_variant": first_variant,
              "checks": ["original shell layout and styling", "24fps source-duration playback", "thinking alternate",
                         "caption-only event", "350ms neutral return", "stale stop ignored", "bounded atlases and Tk images",
                         "hidden avatar paused", "decoded images released", "missing pack uses original renderer", "static idle has no avatar timer"]}
    (args.output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
