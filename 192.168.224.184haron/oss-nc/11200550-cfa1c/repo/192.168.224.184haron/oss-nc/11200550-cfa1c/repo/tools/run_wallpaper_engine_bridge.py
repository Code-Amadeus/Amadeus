# -*- coding: utf-8 -*-
"""Diagnostic launcher for the current web Wallpaper Engine bridge.

Set Wallpaper Engine to open the printed URL.  This process keeps serving
assets and streaming runtime state while the normal app drives the host.
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from render.spriteforge_animator import SpriteForgeAnimator
from wallpaper.wallpaper_engine_bridge import WallpaperEngineBridgeHost


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s - %(message)s",
    )
    host = WallpaperEngineBridgeHost()
    animator_holder = {"animator": None}

    def _on_ready():
        animator = SpriteForgeAnimator(host)
        animator.start()
        animator_holder["animator"] = animator

    host.on_ready = _on_ready
    host.start()
    print("")
    print("Web wallpaper URL:")
    print(host.url)
    print("")
    print("Lively URL:")
    print(host.lively_url)
    print("")
    print("Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        animator = animator_holder.get("animator")
        if animator is not None:
            animator.stop()
        host.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
