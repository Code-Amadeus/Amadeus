"""Opt-in Windows capture regression using two overlapping test-owned windows."""
import json
from pathlib import Path
import sys
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from server.visual_runtime import list_capture_windows
from server.window_capture import capture_window_frame


def run():
    target = tk.Tk()
    target.title("VN capture regression target")
    target.configure(background="#125a97")
    target.geometry("320x220+80+80")
    cover = tk.Toplevel(target)
    cover.title("VN capture regression occluder")
    cover.configure(background="#e42837")
    cover.geometry("320x220+80+80")
    cover.attributes("-topmost", True)
    output = ROOT / "output/diagnostics/vn-window-capture"
    output.mkdir(parents=True, exist_ok=True)
    try:
        target.update()
        candidates = [w for w in list_capture_windows(120) if w["title"] == target.title()]
        assert len(candidates) == 1
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(capture_window_frame, int(candidates[0]["hwnd"], 16))
            # Keep the test-owned UI responsive while WGC starts and captures.
            while not future.done():
                target.update()
                target.after(10)
            frame = future.result()
        frame.save(output / "occluded-target.png")
        assert frame.getpixel((160, 110)) == (18, 90, 151), "Occluding window contaminated the target capture"
        result = {"target_color": list(frame.getpixel((160, 110))), "occluder_color": [228, 40, 55], "passed": True}
        (output / "report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result))
    finally:
        target.destroy()


if __name__ == "__main__":
    run()
