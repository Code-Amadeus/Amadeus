"""One frame from an exact HWND via Windows Graphics Capture.

Only the selected window surface is captured. Occluding apps and companion
windows must never enter it; errors never fall back to the desktop.
"""
from __future__ import annotations

import ctypes
import io
from pathlib import Path
import subprocess
import sys
from threading import Event


def capture_window_frame(hwnd: int, *, timeout: float = 8, preserve_alpha: bool = False):
    """Isolate the native capture lifetime and faults from the long-running host."""
    from PIL import Image
    import psutil

    if not hwnd:
        raise RuntimeError("The game window is unavailable.")
    args = [sys.executable, str(Path(__file__).resolve()), str(hwnd)]
    if preserve_alpha:
        args.append("--preserve-alpha")
    process = subprocess.Popen(args,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        data, error = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # Windows venv launchers can own a Python child. End the whole worker,
        # including that child, rather than leaking a native capture session.
        try:
            children = psutil.Process(process.pid).children(recursive=True)
        except psutil.NoSuchProcess:
            children = []
        for child in reversed(children):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        process.kill()
        process.communicate()
        raise RuntimeError("Game-window capture timed out. Restore the game and try again.") from exc
    if process.returncode:
        detail = error.decode("utf-8", errors="replace").strip()[-500:]
        raise RuntimeError(f"Game-window capture failed ({process.returncode}). {detail}")
    with Image.open(io.BytesIO(data)) as image:
        return image.convert("RGBA" if preserve_alpha else "RGB")


def _capture_frame(hwnd: int, *, timeout: float = 5, preserve_alpha: bool = False):
    from PIL import Image
    from windows_capture import WindowsCapture

    if not hwnd or ctypes.windll.user32.IsIconic(ctypes.c_void_p(hwnd)):
        raise RuntimeError("The game window is minimized or unavailable. Restore it before capturing.")
    capture = WindowsCapture(window_hwnd=hwnd, cursor_capture=False, secondary_window=False)
    ready = Event()
    result = {}

    @capture.event
    def on_frame_arrived(frame, control):
        try:
            # Copy before releasing the native frame buffer.
            pixels = frame.frame_buffer
            if preserve_alpha:
                # WGC supplies premultiplied BGRA. UI previews must retain alpha
                # and undo premultiplication instead of turning transparent edges black.
                with Image.frombytes("RGBa", (pixels.shape[1], pixels.shape[0]), pixels[:, :, [2, 1, 0, 3]].tobytes()) as rgba:
                    result["image"] = rgba.convert("RGBA")
            else:
                result["image"] = Image.fromarray(pixels[:, :, 2::-1].copy())
        except Exception as exc:
            result["error"] = exc
        finally:
            control.stop()
            ready.set()

    @capture.event
    def on_closed():
        ready.set()

    control = capture.start_free_threaded()
    try:
        if not ready.wait(timeout):
            raise RuntimeError("The game window did not provide a frame in time. Restore it and try again.")
        if "error" in result:
            raise RuntimeError("The game frame could not be read.") from result["error"]
        if "image" not in result:
            raise RuntimeError("The game window closed before a frame was available.")
        return result["image"]
    finally:
        control.stop()


if __name__ == "__main__":
    try:
        _capture_frame(int(sys.argv[1]), preserve_alpha="--preserve-alpha" in sys.argv[2:]).save(sys.stdout.buffer, format="PNG")
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
