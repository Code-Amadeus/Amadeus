"""Run the overlay-constructor regression without pytest.

`tests/test_vn_overlay_window_contract.py` needs tkinter, which the project
venv lacks; `D:\\python` has tkinter but not pytest's tomli dependency. This
script executes the same construction path with both flags so the fix for the
`companion_controls` NameError is verified in this environment too.

    D:\\python\\python.exe tools/probes/verify_overlay_constructor.py
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import render.vn_overlay_controls as controls_module  # noqa: E402
from render.vn_overlay_window import PortraitOverlayTk  # noqa: E402


def run_case(companion: bool) -> None:
    captured: dict[str, object] = {}

    class FakeControls:
        def __init__(self, url, *, companion=False):
            captured["url"] = url
            captured["companion"] = companion

        def snapshot(self):
            return {"connected": False, "inputs": {}, "pending": False, "error": ""}

        def close(self):
            captured["closed"] = True

    original = controls_module.VNOverlayControls
    controls_module.VNOverlayControls = FakeControls

    root = MagicMock()
    root.winfo_fpixels.return_value = 96
    root.after.return_value = 1
    try:
        with patch("render.vn_overlay_window.tk.Tk", return_value=root), \
             patch("render.vn_overlay_window.tk.Canvas", return_value=MagicMock()), \
             patch("render.vn_overlay_window.tk.Label", return_value=MagicMock()), \
             patch("render.vn_overlay_window.tk.Button", return_value=MagicMock()), \
             patch("render.vn_overlay_window.tk.StringVar", return_value=MagicMock()):
            shell = PortraitOverlayTk(
                port=0,
                backend_url="ws://127.0.0.1:17777/ws",
                companion_controls=companion,
            )
    finally:
        controls_module.VNOverlayControls = original

    try:
        assert captured["url"] == "ws://127.0.0.1:17777/ws", captured
        assert captured["companion"] is companion, captured
        assert shell._companion_controls_mode is companion
        port = shell._server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
            health = json.loads(response.read().decode("utf-8"))
        assert health["companion_controls"] is companion, health
        print(f"[ok  ] companion_controls={companion}: constructed, wired, health reports {health['companion_controls']}")
    finally:
        shell._server.shutdown()
        shell._server.server_close()
        shell._thread.join(timeout=2)


if __name__ == "__main__":
    run_case(False)
    run_case(True)
    print("overlay constructor wiring verified")
