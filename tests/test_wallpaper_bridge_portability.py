from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(sys.platform == "win32", reason="Regression only affects non-Windows imports")
def test_wallpaper_bridge_import_does_not_load_windows_pointer_api() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import wallpaper.wallpaper_engine_bridge"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
