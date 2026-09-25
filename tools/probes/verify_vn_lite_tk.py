"""Verify the repository-owned VN window, real atlas lifecycle and optional art."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import ctypes
import json
from pathlib import Path
import sys
import time
import tkinter as tk
from urllib.request import Request, urlopen
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.vn_portrait_overlay_lite import overlay_class
from render.companion_pack import CompanionPackError


def pump(overlay, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        overlay.root.update()
        time.sleep(.005)


def post(overlay, path, payload):
    port = overlay._server.server_address[1]
    req = Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(payload).encode(),
                  headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=2) as response:
        assert response.status == 200
    pump(overlay, .08)


def dispose(overlay):
    overlay._server.shutdown()
    overlay._server.server_close()
    overlay.close()
    overlay._thread.join(timeout=2)


def screenshot(overlay, path):
    from server.window_capture import capture_window_frame
    hwnd = ctypes.windll.user32.GetAncestor(overlay.root.winfo_id(), 2)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(capture_window_frame, hwnd)
        while not pending.done():
            pump(overlay, .02)
        with pending.result() as frame:
            frame.save(path)


class ControlFixture:
    """Native widget check only; the real WebSocket/handler contract has separate tests."""
    def __init__(self):
        self.state = {"connected": True, "pending": False, "error": "", "inputs": {
            "session_id": "native-controls", "voice": {"enabled": False, "available": True},
            "vision": {"mode": "off", "available": True}}}
        self.changes = []

    def snapshot(self):
        return deepcopy(self.state)

    def set_inputs(self, session_id, **changes):
        self.changes.append({"session_id": session_id, **changes})
        if "voice" in changes:
            self.state["inputs"]["voice"]["enabled"] = changes["voice"]
        if "vision_mode" in changes:
            self.state["inputs"]["vision"]["mode"] = changes["vision_mode"]
        return True

    def close(self):
        pass


def verify_controls(overlay, output, capture):
    control = ControlFixture()
    overlay._controls = control
    overlay.text_var.set("准备好了，继续故事吧。\n需要我的时候，直接问我就好。")
    pump(overlay, .1)
    assert overlay.root.winfo_width() == overlay._px(470) and overlay.root.winfo_height() == overlay._px(226)
    assert abs(float(overlay.root.attributes("-alpha")) - .88) < .01
    original_position = overlay.root.winfo_x(), overlay.root.winfo_y()

    def hover(inside):
        x, y = original_position
        with patch.object(overlay.root, "winfo_pointerxy", return_value=(x + 200, y + 20) if inside else (x - 5, y - 5)):
            overlay._schedule_hover()
            pump(overlay, .1)
        assert all(bool(button.winfo_ismapped()) == inside for button in overlay._control_buttons.values())

    hover(False)
    if capture:
        screenshot(overlay, output / "card-idle.png")
    hover(True)
    if capture:
        screenshot(overlay, output / "card-hover-off.png")
    for name in ("voice", "vision"):
        button = overlay._control_buttons[name]
        button.event_generate("<Button-1>", x=14, y=14)
        pump(overlay, .1)
    assert control.changes == [{"session_id": "native-controls", "voice": True},
                               {"session_id": "native-controls", "vision_mode": "on_question"}]
    assert (overlay.root.winfo_x(), overlay.root.winfo_y()) == original_position
    if capture:
        screenshot(overlay, output / "card-hover-on.png")
    # Updates from another UI, pending commands and disconnects must govern these same icons.
    control.state["inputs"]["voice"]["enabled"] = False
    control.state["pending"] = True
    pump(overlay, .1)
    overlay._control_buttons["voice"].event_generate("<Button-1>", x=14, y=14)
    assert len(control.changes) == 2
    control.state.update(pending=False, connected=False, error="连接已断开")
    pump(overlay, .1)
    overlay._show_control_hint("voice")
    assert "连接已断开" in overlay._tooltip.cget("text")
    assert overlay.text_var.get().startswith("准备好了"), "control feedback must preserve the current caption"
    hover(False)
    assert not overlay._tooltip.winfo_ismapped()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--screenshots", action="store_true", help="Capture the exact native window on Windows")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    cls = overlay_class()
    overlay = cls(host="127.0.0.1", port=0, lite_dir=ROOT / "assets/companion/kurisu")
    try:
        pump(overlay, .1)
        initial_images = len(overlay.root.tk.call("image", "names"))
        post(overlay, "/reaction", {"source": "vn_playback", "sentence_id": "one", "speaking": True,
                                    "emotion": "thinking", "display_text": "Repository-owned VN companion"})
        first = overlay._lite.spec["url"]
        post(overlay, "/reaction", {"source": "vn_playback", "sentence_id": "one", "speaking": False})
        post(overlay, "/reaction", {"source": "vn_playback", "sentence_id": "two", "speaking": True, "emotion": "thinking"})
        assert overlay._lite.spec["url"] != first
        post(overlay, "/reaction", {"source": "vn_pretranslation", "sentence_id": "one", "display_text": "stale"})
        assert overlay.text_var.get() != "stale"
        post(overlay, "/visibility", {"visible": False})
        assert not overlay.root.winfo_viewable() and overlay._lite.paused and overlay._atlas_timer is None
        post(overlay, "/reaction", {"source": "vn_playback", "sentence_id": "three", "speaking": True,
                                    "emotion": "sad", "text": "A sad beat [EMO preset=sad]"})
        assert overlay._current_emotion == "sad" and overlay.text_var.get() == "A sad beat"
        assert not overlay.root.winfo_viewable(), "incoming reactions must not reopen a hidden window"
        post(overlay, "/visibility", {"visible": True})
        assert overlay.root.winfo_viewable()
        post(overlay, "/reaction", {"source": "vn_playback", "sentence_id": "four", "speaking": True,
                                    "emotion": "surprised"})
        assert overlay._current_emotion == "sided_surprised"
        post(overlay, "/reaction", {"source": "vn_pretranslation", "sentence_id": "four", "display_text": "Long caption for layout verification. " * 15})
        assert overlay.root.winfo_height() >= overlay._caption.winfo_reqheight() + overlay._px(120)
        post(overlay, "/reaction", {"source": "vn_pretranslation", "sentence_id": "four", "display_text": "Short again"})
        assert overlay.root.winfo_height() == overlay._px(226)
        for index in range(16):
            overlay._set_emotion("happy" if index % 2 else "normal", "speaking" if index % 3 else "idle")
        assert len(overlay.root.tk.call("image", "names")) <= initial_images + 1
        assert overlay._lite.resident_bytes <= 16 * 1024**2
        verify_controls(overlay, args.output, args.screenshots)
        player = overlay._lite
    finally:
        dispose(overlay)
    assert player.resident_bytes == 0
    placeholder = cls(host="127.0.0.1", port=0, lite_dir=args.output / "missing-optional-art")
    try:
        pump(placeholder, .1)
        assert placeholder._lite is None
        post(placeholder, "/reaction", {"source": "vn_playback", "sentence_id": "one", "speaking": True, "display_text": "Captions without an art bundle"})
        assert placeholder.text_var.get() == "Captions without an art bundle"
    finally:
        dispose(placeholder)
    corrupt = args.output / "corrupt-installed-art"
    corrupt.mkdir(exist_ok=True)
    (corrupt / "manifest.json").write_text("{", encoding="utf-8")
    try:
        broken = cls(host="127.0.0.1", port=0, lite_dir=corrupt)
    except CompanionPackError:
        pass
    else:
        dispose(broken)
        raise AssertionError("corrupt installed art must fail visibly")
    assert tk._default_root is None, "failed initialization must destroy its Tk root"
    report = {"ok": True, "checks": ["repository-only window", "alternate speaking atlas", "stale subtitle rejected", "sad and surprise aliases select installed art", "hidden window pauses avatar", "caption grows and shrinks with text", "bounded atlas and Tk image memory", "decoded images released", "optional art missing: avatar and captions available", "corrupt installed art fails visibly", "original card dimensions and opacity", "icons appear on hover and hide on leave", "native icon click bindings set session inputs without dragging", "backend updates and pending/disconnected state govern controls", "control errors do not replace captions"], "input_fixture": "synthetic Tk events and hardware-free controls; WebSocket integration tested separately"}
    (args.output / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
