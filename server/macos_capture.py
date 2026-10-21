"""Native macOS helper lifecycle and unique window identity for all VN capture paths."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
from threading import Lock
from typing import Any

_build_lock = Lock()


def ensure_macos_helper() -> Path:
    root = Path(__file__).resolve().parents[1]
    binary = root / "build/bin/mac_screen_ocr"
    # The script owns freshness checks. Serialize callers so a list/preview/start
    # cannot execute a binary while another request is replacing it.
    with _build_lock:
        proc = subprocess.run(
            [str(root / "tools/build_mac_screen_ocr.sh")],
            capture_output=True, text=True, timeout=120, check=False,
        )
        if proc.returncode or not binary.is_file():
            raise RuntimeError(f"Native OCR helper build failed: {(proc.stderr or proc.stdout).strip()[-500:]}")
    return binary


def list_macos_capture_windows() -> list[dict[str, Any]]:
    binary = ensure_macos_helper()
    proc = subprocess.run([str(binary), "--list-windows"], capture_output=True,
                          text=True, timeout=15, check=False)
    if proc.returncode:
        raise RuntimeError(f"Window enumeration failed: {proc.stderr.strip()[-500:]}")
    windows = json.loads(proc.stdout)
    if not isinstance(windows, list):
        raise RuntimeError("Invalid native window list.")
    return windows


def resolve_macos_window(windows: list[dict[str, Any]], *, window_id: int | None = None,
                         pid: int | None = None, window_name: str = "",
                         app_name: str = "") -> dict[str, Any]:
    """An explicit ID never falls back. Name selectors must identify one window."""
    if window_id:
        matches = [w for w in windows if w["id"] == window_id and (not pid or w["pid"] == pid)]
    else:
        if not (pid or window_name or app_name):
            raise RuntimeError("Choose an open game window before capturing.")
        matches = [w for w in windows
                   if (not pid or w["pid"] == pid)
                   and (not window_name or window_name.casefold() in w["title"].casefold())
                   and (not app_name or app_name.casefold() in w["app"].casefold())]
    if len(matches) != 1:
        raise RuntimeError("The game window is missing or ambiguous. Select its window again.")
    return dict(matches[0])
