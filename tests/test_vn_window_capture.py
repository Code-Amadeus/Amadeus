"""Window-only capture: ownership, buffer lifetime and failure cleanup."""
from types import SimpleNamespace
from unittest.mock import Mock
import sys

import numpy as np
import pytest

from server import window_capture


def capture_fixture(monkeypatch, mode):
    control = Mock()
    pixels = np.array([[[15, 45, 120, 255]]], dtype=np.uint8)
    class Capture:
        def __init__(self, **kwargs):
            self.options = kwargs
            captures.append(self)
        def event(self, handler):
            setattr(self, handler.__name__, handler)
            return handler
        def start_free_threaded(self):
            if mode == "frame":
                self.on_frame_arrived(SimpleNamespace(frame_buffer=pixels), Mock())
                pixels[:] = 0  # Native frame storage is no longer ours.
            elif mode == "closed":
                self.on_closed()
            return control
    captures = []
    monkeypatch.setitem(sys.modules, "windows_capture", SimpleNamespace(WindowsCapture=Capture))
    monkeypatch.setattr(window_capture.ctypes, "windll", SimpleNamespace(user32=SimpleNamespace(IsIconic=lambda _: False)), raising=False)
    return captures, control


def test_exact_window_and_detached_frame(monkeypatch):
    captures, control = capture_fixture(monkeypatch, "frame")
    frame = window_capture._capture_frame(123)
    assert captures[0].options == {"window_hwnd": 123, "cursor_capture": False, "secondary_window": False}
    assert frame.getpixel((0, 0)) == (120, 45, 15)
    control.stop.assert_called_once()


@pytest.mark.parametrize("mode,message", [("closed", "closed"), ("timeout", "in time")])
def test_capture_failure_stops_capture_without_screen_fallback(monkeypatch, mode, message):
    captures, control = capture_fixture(monkeypatch, mode)
    with pytest.raises(RuntimeError, match=message):
        window_capture._capture_frame(123, timeout=0)
    assert len(captures) == 1
    control.stop.assert_called_once()


def test_capture_rejects_absent_handle_before_native_start(monkeypatch):
    captures, _ = capture_fixture(monkeypatch, "frame")
    with pytest.raises(RuntimeError, match="unavailable"):
        window_capture.capture_window_frame(0)
    assert captures == []


def test_native_process_crash_is_a_capture_error(monkeypatch):
    process = Mock(returncode=0xC0000005)
    process.communicate.return_value = (b"", b"")
    monkeypatch.setattr(window_capture.subprocess, "Popen", Mock(return_value=process))
    with pytest.raises(RuntimeError, match="Game-window capture failed"):
        window_capture.capture_window_frame(123)


def test_timed_out_capture_cleans_up_its_worker_tree(monkeypatch):
    process = Mock(pid=123, returncode=-1)
    process.communicate.side_effect = [window_capture.subprocess.TimeoutExpired("capture", 8), (b"", b"")]
    monkeypatch.setattr(window_capture.subprocess, "Popen", Mock(return_value=process))
    child = Mock()
    monkeypatch.setattr("psutil.Process", Mock(return_value=Mock(children=Mock(return_value=[child]))))
    with pytest.raises(RuntimeError, match="timed out"):
        window_capture.capture_window_frame(123)
    child.kill.assert_called_once()
    process.kill.assert_called_once()


def test_host_audio_probe_rejects_silent_failure_chunk(tmp_path):
    import json
    from tools.probes.verify_vn_host_io import verify_audio

    path = tmp_path / "test.npz"
    meta = np.array(json.dumps({"processed_text": "test"}))
    np.savez(path, sr=24000, audio=np.zeros(16000), meta_json=meta)
    with pytest.raises(AssertionError, match="only silence"):
        verify_audio(tmp_path, "test", 0)
    np.savez(path, sr=24000, audio=np.array([0.0, 0.1, -0.1]), meta_json=meta)
    assert verify_audio(tmp_path, "test", 0)["peak"] == .1
