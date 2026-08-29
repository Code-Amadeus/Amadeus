# -*- coding: utf-8 -*-
"""Pure asset helpers shared by wallpaper hosts.

This module intentionally has no PyQt imports. Electron/Lively wallpaper mode
can use it without pulling in the legacy desktop WebEngine host.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from config.asset_paths import (
    AUDIO_ROOT,
    IMAGE_ROOT,
    PREVIEW_ROOT,
    PROJECT_ROOT,
    SCENARIO_RUNTIME_ROOT,
    SCENARIO_SOURCE_ROOT,
)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = PROJECT_ROOT
_WALLPAPER_DIR = _PROJECT_ROOT / "wallpaper"
_CFG_PATH = _WALLPAPER_DIR / "crt_config.json"
_BRIDGE_CFG_PATH = _WALLPAPER_DIR / "we_bridge_config.json"
_RUNTIME_BG = _PROJECT_ROOT / "runtime" / "wallpaper" / "runtime_wallpaper_bg.png"
_PROJECT_WALLPAPER = IMAGE_ROOT / "amadeus_desktop_wallpaper.png"
_PROJECT_AMBIENT_LOW = IMAGE_ROOT / "amadeus_ambient_low_blend.png"
_PROJECT_AMBIENT_HIGH = IMAGE_ROOT / "amadeus_ambient_high_blend.png"
_PROJECT_SUBTITLE_FRAME = IMAGE_ROOT / "subtitle_frame_big.png"
_PROJECT_KEYBOARD_SFX = AUDIO_ROOT / "sfx" / "computer_use_keyboard_loop.wav"

_DEFAULT_SCENARIO_SOURCE_ROOT = SCENARIO_SOURCE_ROOT
_SCENARIO_SOURCE_ROOT = Path(os.environ.get("AMADEUS_SCENARIO_ROOT", str(_DEFAULT_SCENARIO_SOURCE_ROOT)))
_RUNTIME_SCENARIOS = SCENARIO_RUNTIME_ROOT
_SCENARIO_GRAPH_NAME = "scenario_graph.json"
_SCENARIO_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
_SCENARIO_VIDEO_EXTS = {".mp4", ".webm", ".mov"}
_SCENARIO_BACKPLATE_NAME = "scenario_backplate.png"
_SCENARIO_BACKPLATE_CANDIDATES = [
    "BG_empty.png",
    "bg_empty.png",
    "scene_backplate.png",
    "scene_base.png",
    "scene_empty.png",
    "crt_bg.png",
    "crt_bg_empty.png",
    "crt_empty.png",
    "crt_default.png",
    "lab_empty.png",
    "background.png",
    "base.png",
    "empty.png",
    "default.png",
    "scenario_backplate.png",
]
_SCENARIO_DEFAULT_SOURCE_CROP_NORM = [
    113 / 1398,
    119 / 1125,
    1182 / 1398,
    902 / 1125,
]


def _load_crt_config() -> dict:
    try:
        return json.loads(_CFG_PATH.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        logger.warning("[WallpaperAssets] failed to load CRT config: %s", exc)
        return {
            "img_size": [1672, 941],
            "crt_corners": [[556, 629], [560, 199], [1127, 204], [1132, 630]],
            "scanline_alpha": 32,
            "vignette_alpha": 110,
        }


def _crt_bounds_norm(config: dict | None = None) -> dict[str, float]:
    """Project the authored CRT polygon to a host-independent normalized box."""
    value = config if isinstance(config, dict) else _load_crt_config()
    image_size = value.get("img_size") or [1672, 941]
    source_width = max(1.0, float(image_size[0] or 1672))
    source_height = max(1.0, float(image_size[1] or 941))
    points = value.get("crt_polygon") or value.get("crt_corners") or []
    valid = [point for point in points if isinstance(point, (list, tuple)) and len(point) >= 2]
    if len(valid) < 3:
        valid = [[556, 199], [1132, 199], [1132, 630], [556, 630]]
    xs = [float(point[0]) / source_width for point in valid]
    ys = [float(point[1]) / source_height for point in valid]
    left = min(xs)
    top = min(ys)
    return {
        "x": left,
        "y": top,
        "width": max(xs) - left,
        "height": max(ys) - top,
    }


def _load_wallpaper_ui_config() -> dict:
    try:
        data = json.loads(_BRIDGE_CFG_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _resolve_wallpaper_source() -> Path:
    raw = os.environ.get("AMADEUS_WALLPAPER_PATH", "").strip()
    candidates = [Path(raw)] if raw else [_PROJECT_WALLPAPER]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _is_inside_project(path: Path) -> bool:
    try:
        path.resolve().relative_to(_PROJECT_ROOT)
        return True
    except ValueError:
        return False


def _asset_url(port: int, path: Path) -> str:
    rel = path.resolve().relative_to(_PROJECT_ROOT).as_posix()
    return f"http://127.0.0.1:{port}/{rel}"


def _prepare_background_asset() -> Optional[Path]:
    src = _resolve_wallpaper_source()
    if not src.exists():
        logger.warning("[WallpaperAssets] wallpaper image not found: %s", src)
        return None
    if _is_inside_project(src):
        return src
    try:
        _RUNTIME_BG.parent.mkdir(parents=True, exist_ok=True)
        if (not _RUNTIME_BG.exists()) or src.stat().st_mtime > _RUNTIME_BG.stat().st_mtime:
            shutil.copyfile(src, _RUNTIME_BG)
        return _RUNTIME_BG
    except Exception as exc:
        logger.warning("[WallpaperAssets] failed to prepare wallpaper image: %s", exc)
        return None


def _copy_scenario_pack(src_root: Path, dst_root: Path) -> Optional[Path]:
    graph = src_root / _SCENARIO_GRAPH_NAME
    if not graph.exists():
        return None
    try:
        dst_root.mkdir(parents=True, exist_ok=True)
        for src in src_root.rglob("*"):
            if not src.is_file():
                continue
            try:
                rel = src.relative_to(src_root)
            except ValueError:
                continue
            dst = dst_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if (not dst.exists()) or src.stat().st_mtime > dst.stat().st_mtime or src.stat().st_size != dst.stat().st_size:
                shutil.copy2(src, dst)
        return dst_root / _SCENARIO_GRAPH_NAME
    except Exception as exc:
        logger.warning("[WallpaperAssets] failed to prepare scenario assets: %s", exc)
        return None


def _scenario_manifest_fps(path: Path) -> float:
    candidates = []
    if path.is_dir():
        candidates.extend([path / "spriteforge_clip.json", path.parent / "spriteforge_clip.json"])
    else:
        candidates.extend([path.with_suffix("") / "spriteforge_clip.json", path.parent / "spriteforge_clip.json"])
    for manifest in candidates:
        if not manifest.exists():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8-sig"))
            fps = float(data.get("fps") or data.get("source_fps") or 0)
            if fps > 0:
                return fps
        except Exception:
            continue
    return 30.0


def _frame_urls(port: int, directory: Path) -> list[str]:
    frames = sorted(directory.glob("*.png"), key=lambda p: p.name.lower())
    return [_asset_url(port, frame) for frame in frames if frame.is_file()]


def _first_frame_sequence_dir(path: Path) -> Optional[Path]:
    candidates = []
    if path.is_dir():
        candidates.extend([path, path / "loop"])
    else:
        candidates.extend([
            path.with_suffix("") / "loop",
            path.parent / f"{path.stem}_loop" / "loop",
        ])
    for candidate in candidates:
        if candidate.exists() and any(candidate.glob("*.png")):
            return candidate
    return None


def _scenario_frames_payload(port: int, directory: Path) -> dict:
    return {"type": "frames", "frames": _frame_urls(port, directory), "fps": _scenario_manifest_fps(directory)}


def _scenario_resource_payload(port: int, runtime_root: Path, resource: str) -> dict:
    rel = Path(str(resource or "").replace("\\", "/"))
    path = (runtime_root / rel).resolve()
    frame_dir = _first_frame_sequence_dir(path)
    if frame_dir is not None:
        return _scenario_frames_payload(port, frame_dir)
    if path.is_file():
        suffix = path.suffix.lower()
        if suffix in _SCENARIO_IMAGE_EXTS:
            return {"type": "image", "url": _asset_url(port, path)}
        if suffix in _SCENARIO_VIDEO_EXTS:
            return {"type": "video", "url": _asset_url(port, path)}
    return {"type": "missing", "resource": str(resource or "")}


def _copy_backplate_source(src: Path, dst: Path) -> Optional[Path]:
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if (not dst.exists()) or src.stat().st_mtime > dst.stat().st_mtime or src.stat().st_size != dst.stat().st_size:
            shutil.copy2(src, dst)
        return dst
    except Exception as exc:
        logger.warning("[WallpaperAssets] failed to copy scenario backplate: %s", exc)
        return None


def _find_explicit_scenario_backplate(src_root: Path, runtime_root: Path) -> tuple[Optional[Path], Optional[list[float]]]:
    raw = os.environ.get("AMADEUS_SCENARIO_BACKPLATE_PATH", "").strip()
    if raw:
        src = Path(raw)
        if src.exists():
            dst = runtime_root / _SCENARIO_BACKPLATE_NAME
            copied = _copy_backplate_source(src, dst)
            if copied:
                return copied, _SCENARIO_DEFAULT_SOURCE_CROP_NORM

    for name in _SCENARIO_BACKPLATE_CANDIDATES:
        source_candidate = src_root / name
        if source_candidate.exists():
            copied = _copy_backplate_source(source_candidate, runtime_root / _SCENARIO_BACKPLATE_NAME)
            if copied:
                return copied, _SCENARIO_DEFAULT_SOURCE_CROP_NORM
        candidate = runtime_root / name
        if candidate.exists() and name.lower() != _SCENARIO_BACKPLATE_NAME.lower():
            return candidate, _SCENARIO_DEFAULT_SOURCE_CROP_NORM

    generated = runtime_root / _SCENARIO_BACKPLATE_NAME
    if generated.exists():
        return generated, _SCENARIO_DEFAULT_SOURCE_CROP_NORM

    legacy_crop = PREVIEW_ROOT / "wallpaper" / "crt_bg_crop.png"
    if legacy_crop.exists():
        return legacy_crop, None
    return None, None


def _prepare_scenario_backplate(src_root: Path, runtime_root: Path) -> tuple[Optional[Path], Optional[list[float]]]:
    explicit, crop_norm = _find_explicit_scenario_backplate(src_root, runtime_root)
    if explicit is not None:
        return explicit, crop_norm

    out_path = runtime_root / _SCENARIO_BACKPLATE_NAME
    try:
        from PIL import Image
        import numpy as np
    except Exception as exc:
        if out_path.exists():
            return out_path, _SCENARIO_DEFAULT_SOURCE_CROP_NORM
        logger.warning("[WallpaperAssets] scenario backplate unavailable: %s", exc)
        return None, None

    candidates = [
        path for path in runtime_root.rglob("*.png")
        if path.name != _SCENARIO_BACKPLATE_NAME and "loop" not in {part.lower() for part in path.parts}
    ]
    if not candidates:
        return (out_path, _SCENARIO_DEFAULT_SOURCE_CROP_NORM) if out_path.exists() else (None, None)

    newest = max((path.stat().st_mtime for path in candidates), default=0)
    if out_path.exists() and out_path.stat().st_mtime >= newest:
        return out_path, _SCENARIO_DEFAULT_SOURCE_CROP_NORM

    opened = []
    try:
        sizes: dict[tuple[int, int], list[Path]] = {}
        for path in candidates:
            with Image.open(path) as image:
                sizes.setdefault(image.size, []).append(path)
        paths = max(sizes.values(), key=len)
        if len(paths) < 2:
            return (out_path, _SCENARIO_DEFAULT_SOURCE_CROP_NORM) if out_path.exists() else (None, None)

        arrays = []
        for path in paths:
            image = Image.open(path).convert("RGB")
            opened.append(image)
            arrays.append(np.asarray(image, dtype=np.uint8))
        median = np.median(np.stack(arrays, axis=0), axis=0).astype(np.uint8)
        Image.fromarray(median, "RGB").save(out_path)
        logger.info("[WallpaperAssets] scenario backplate generated: %s from %d images", out_path, len(paths))
        return out_path, _SCENARIO_DEFAULT_SOURCE_CROP_NORM
    except Exception as exc:
        logger.warning("[WallpaperAssets] failed to generate scenario backplate: %s", exc)
        return (out_path, _SCENARIO_DEFAULT_SOURCE_CROP_NORM) if out_path.exists() else (None, None)
    finally:
        for image in opened:
            try:
                image.close()
            except Exception:
                pass


def _prepare_scenario_payload(port: int) -> dict:
    src_root = _SCENARIO_SOURCE_ROOT
    runtime_graph = _RUNTIME_SCENARIOS / _SCENARIO_GRAPH_NAME
    if runtime_graph.exists():
        graph_path = runtime_graph
        backplate_source_root = _RUNTIME_SCENARIOS
        logger.info("[WallpaperAssets] using local scenario runtime pack: %s", _RUNTIME_SCENARIOS)
    elif src_root.exists():
        graph_path = _copy_scenario_pack(src_root, _RUNTIME_SCENARIOS)
        backplate_source_root = src_root
    else:
        graph_path = None
        backplate_source_root = _RUNTIME_SCENARIOS
    if graph_path is None or not graph_path.exists():
        return {"enabled": False, "reason": f"scenario graph not found: local={runtime_graph}, source={src_root}"}
    try:
        graph = json.loads(graph_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        logger.warning("[WallpaperAssets] failed to load scenario graph: %s", exc)
        return {"enabled": False, "reason": "scenario graph parse failed"}
    if (
        not isinstance(graph, dict)
        or not isinstance(graph.get("nodes"), list)
        or not isinstance(graph.get("edges"), list)
        or any(not isinstance(node, dict) for node in graph["nodes"])
        or any(not isinstance(edge, dict) for edge in graph["edges"])
    ):
        logger.warning("[WallpaperAssets] scenario graph has an invalid shape: %s", graph_path)
        return {"enabled": False, "reason": "scenario graph has an invalid shape"}

    node_resources = {}
    for node in graph.get("nodes", []):
        node_id = str(node.get("id") or "")
        if not node_id:
            continue
        node_resources[node_id] = _scenario_resource_payload(
            port,
            _RUNTIME_SCENARIOS,
            str(node.get("resource") or ""),
        )
    backplate, backplate_crop_norm = _prepare_scenario_backplate(backplate_source_root, _RUNTIME_SCENARIOS)

    return {
        "enabled": True,
        "inactivitySeconds": float(os.environ.get("AMADEUS_SCENARIO_IDLE_SECONDS", "60")),
        "staticHoldSeconds": float(os.environ.get("AMADEUS_SCENARIO_STATIC_SECONDS", "15")),
        "enableFramePlayback": os.environ.get("AMADEUS_SCENARIO_FRAME_PLAYBACK", "1").strip().lower()
        not in {"0", "false", "no", "off"},
        "graph": graph,
        "resources": node_resources,
        "activities": {
            "work": {
                "entryLabel": "computer use",
                "labels": ["computer use"],
                "resourceHints": ["computer_use/computer_uses_mastered", "computer_uses_mastered"],
                "sceneIds": ["computer_use"],
                "keyboardSfxLabels": ["computer use"],
                "keyboardSfxResourceHints": ["computer_use/computer_uses_mastered", "computer_uses_mastered"],
                "holdDuringSpeech": True,
                "stayWithinActivity": True,
            },
        },
        "keyboardSfxUrl": _asset_url(port, _PROJECT_KEYBOARD_SFX) if _PROJECT_KEYBOARD_SFX.exists() else "",
        "backplateUrl": _asset_url(port, backplate) if backplate else "",
        "backplateCropNorm": backplate_crop_norm,
        "placementMode": "crt_screen",
        "sourceCropNorm": _SCENARIO_DEFAULT_SOURCE_CROP_NORM,
    }
