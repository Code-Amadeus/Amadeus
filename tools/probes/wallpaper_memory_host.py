"""Isolated real wallpaper bridge for the renderer memory probe (no voice/LLM)."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["WALLPAPER_WHEEL_FORWARD"] = "false"

import psutil
from render.spriteforge_animator import SpriteForgeAnimator
from wallpaper.wallpaper_engine_bridge import WallpaperEngineBridgeHost


def main():
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    host = WallpaperEngineBridgeHost(asset_port=18777, bridge_port=18797, slice_host="electron")
    animator = SpriteForgeAnimator(host)
    try:
        host.start()
        available = animator.start()

        def action(payload):
            if payload.get("target") == "presentation" and payload.get("action") == "companion":
                host.set_companion_active(payload.get("active") is True)
                return {"ok": True}
            return {"ok": False, "error": "probe_only"}

        host.set_canvas_action_handler(action)
        print(json.dumps({"ready": True, "url": host.url, "assetPort": host.asset_port,
                          "bridgePort": host.bridge_port, "assetVersion": host.asset_version,
                          "characterAvailable": available, "pid": os.getpid()}), flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            command = request["command"]
            if command == "stop":
                break
            if command == "sample":
                info = psutil.Process().memory_info()
                result = {"rss": info.rss, "private": getattr(info, "private", None)}
            elif command == "speaking":
                host.set_speaking(request["active"])
                host.set_subtitle("用于测量头像与字幕渲染的固定测试文本。")
                result = True
            elif command == "emotion":
                host.set_emotion(request["value"])
                result = True
            else:
                raise ValueError(command)
            print(json.dumps({"id": request["id"], "result": result}), flush=True)
    finally:
        animator.stop()
        host.stop()


if __name__ == "__main__":
    main()
