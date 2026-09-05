"""Retired application entry point.

The desktop application is owned by Electron and the Python backend lives in
``server.app``. This stub is intentionally import-safe so obsolete launchers
fail with a clear migration message instead of initializing legacy runtimes.
"""

from __future__ import annotations


_DEPRECATION_MESSAGE = """\
[DEPRECATED] main.py 的 PyQt 桌面 GUI 入口已退役。
  - 桌面端：启动 Electron 应用（run_electron_utf8.bat）
  - 本地 CUDA 语音 profile：run_electron_utf8.bat
  - 后端：  uv run --locked --no-sync python -m server.app [--port 17777]
旧 PyQt 代码已隔离至 legacy/pyqt（见 DEPRECATED_FILES.md）。"""


def main() -> int:
    print(_DEPRECATION_MESSAGE)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
