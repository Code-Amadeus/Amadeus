"""Isolated wallpaper bridge for texture probes; no backend, messages, or models."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import platform
import socket

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["WALLPAPER_WHEEL_FORWARD"] = "false"

import psutil
from config.asset_paths import SPRITEFORGE_RUNTIME_ROOT
from render.spriteforge_animator import SpriteForgeAnimator
import wallpaper.wallpaper_engine_bridge as bridge_module


def unused_port() -> int:
    # The product servers own the final bind. This process never connects to an
    # existing server; a collision makes them bind another free port.
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return candidate.getsockname()[1]


def pack_metadata(animator: SpriteForgeAnimator) -> dict:
    pack = animator._character_pack
    if pack is None:
        return {"available": False}
    files = {}
    for name in ("runtime_manifest.json", "graph_config.json", "spriteforge_mouth_config.json"):
        source = SPRITEFORGE_RUNTIME_ROOT / name
        if source.is_file():
            files[name] = hashlib.sha256(source.read_bytes()).hexdigest()
    return {"available": True, "id": pack.manifest["id"], "version": pack.manifest["version"],
            "format": pack.manifest["format"], "sourceSha256": files,
            "clips": len(pack.clip_paths), "frames": sum(map(len, pack.clip_paths.values()))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    scenario = {}
    prepare_scenario = bridge_module._prepare_scenario_payload

    def probe_scenario(port):
        payload = prepare_scenario(port) if args.scenario else {"enabled": False, "reason": "not requested by probe"}
        scenario.update(payload)
        return payload

    bridge_module._prepare_scenario_payload = probe_scenario
    asset_port = unused_port()
    bridge_port = unused_port()
    while abs(bridge_port - asset_port) < 20:
        bridge_port = unused_port()
    host = bridge_module.WallpaperEngineBridgeHost(asset_port=asset_port, bridge_port=bridge_port, slice_host="electron")
    animator = SpriteForgeAnimator(host)
    try:
        host.start()
        available = animator.start()

        host.set_canvas_action_handler(lambda payload: {"ok": False, "error": "probe_only"})
        work_nodes = [node for node in scenario.get("graph", {}).get("nodes", [])
                      if str(node.get("label", "")).lower() == "computer use"
                      or "computer_uses_mastered" in str(node.get("resource", "")).lower()]
        resources = scenario.get("resources", {})
        usable_work_nodes = []
        for node in work_nodes:
            resource = resources.get(str(node.get("id", "")), {})
            if resource.get("type") == "image" and resource.get("url"):
                usable_work_nodes.append(node)
            elif (resource.get("type") == "frames" and resource.get("frames")
                  and scenario.get("enableFramePlayback")):
                usable_work_nodes.append(node)
        print(json.dumps({"ready": True, "url": host.url, "assetPort": host.asset_port,
                          "bridgePort": host.bridge_port, "assetVersion": host.asset_version,
                          "characterAvailable": available, "pack": pack_metadata(animator), "pid": os.getpid(),
                          "scenario": {"available": bool(scenario.get("enabled")), "workActivityAvailable": bool(usable_work_nodes),
                                       "framePlayback": bool(scenario.get("enableFramePlayback")),
                                       "reason": ("not requested" if not args.scenario else
                                                  "scenario pack unavailable" if not scenario.get("enabled") else
                                                  "" if usable_work_nodes else "work activity resource unavailable or unsupported")},
                          "settings": {"graphicsProfile": host.graphics_profile, "renderMaxFps": host.render_max_fps,
                                       "renderMaxResolution": host.render_max_resolution,
                                       "renderTextureSampling": host.render_texture_sampling},
                          "environment": {"python": platform.python_version(), "platform": platform.system(),
                                          "release": platform.release(), "architecture": platform.machine(),
                                          "psutil": psutil.__version__}}), flush=True)
        process = psutil.Process()
        for line in sys.stdin:
            request = json.loads(line)
            command = request["command"]
            if command == "stop":
                break
            try:
                if command == "sample":
                    info = process.memory_info()
                    private = getattr(info, "private", None)
                    source = "memory_info.private"
                    if private is None:
                        private = getattr(process.memory_full_info(), "uss", None)
                        source = "memory_full_info.uss" if private is not None else "unavailable"
                    cpu = process.cpu_times()
                    result = {"pid": os.getpid(), "rssBytes": info.rss, "privateBytes": private,
                              "privateSource": source, "cpuSeconds": cpu.user + cpu.system}
                elif command == "speaking":
                    host.set_speaking(request["active"] is True)
                    result = {"ok": True}
                elif command == "trigger":
                    host.trigger_spriteforge_intent(str(request["label"]))
                    result = {"ok": True}
                elif command == "idle":
                    host.release_spriteforge()
                    result = {"ok": True}
                elif command == "companion":
                    host.set_companion_active(request["active"] is True)
                    result = {"ok": True}
                elif command == "scenario":
                    host.set_activity("work" if request["active"] is True else "")
                    result = {"ok": True}
                else:
                    raise ValueError("unsupported probe command")
                print(json.dumps({"id": request["id"], "result": result}), flush=True)
            except Exception as exc:
                print(json.dumps({"id": request["id"], "error": str(exc)}), flush=True)
    finally:
        animator.stop()
        host.stop()
        bridge_module._prepare_scenario_payload = prepare_scenario


if __name__ == "__main__":
    main()
