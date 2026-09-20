"""Build an optional Amadeus KTX2 character pack from a SpriteForge workspace.

The source workspace is authoring input. The resulting package contains only a
runtime manifest, a path-free graph, a runtime mouth configuration, and KTX2
textures. Use ``--move-textures`` only after a successful dry run; source
textures are removed only after the staged package has been completely copied
and validated.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "assets" / "spriteforge" / "runtime" / "kurisu"
PACK_FORMAT = "amadeus.spriteforge.character-pack.v1"
DEFAULT_FPS = 24
PHASE_PRIORITY = ("loop", "in", "out")
FRAME_DIR_NAMES = (
    "frames_alpha_fx_snapghost_200c_tailfix",
    "frames_alpha_fx_snapghost_200c",
    "frames_alpha_fx_snapghost_72",
    "frames_alpha_2x_gmfss_refbottom_trans_blend",
    "frames_alpha_2x_gmfss_aligned",
    "frames_alpha_2x_gmfss",
    "frames_alpha_2x_lerp",
    "frames_alpha",
    "frames",
)
DEFAULT_FRAME_DIR = "frames_alpha_2x_gmfss"
FAST_TRANSITION_LABELS = {
    "thinking_trans",
    "thinking_to_serious",
    "thinking_to_key_point",
    "serious_to_thinking",
    "shy_trans",
    "surprise_trans",
    "angry_trans",
}
SNAPGHOST_TRANSITION_LABELS = {
    "thinking_trans",
    "thinking_to_serious",
    "thinking_to_key_point",
    "serious_to_thinking",
}
FRAME_DIR_BY_LABEL = {
    "idle_closed_eye": "frames_alpha_2x_gmfss_aligned",
    "sad_trans": "frames_alpha",
    "trans_standby": "frames_alpha_fx_snapghost_200c_tailfix",
    **{
        label: "frames_alpha"
        for label in FAST_TRANSITION_LABELS - SNAPGHOST_TRANSITION_LABELS
    },
    **{label: "frames_alpha_fx_snapghost_72" for label in SNAPGHOST_TRANSITION_LABELS},
    "thinking_trans": "frames_alpha_2x_gmfss",
    "thinking_to_serious": "frames_alpha_2x_gmfss",
    "thinking_to_key_point": "frames_alpha_2x_gmfss",
    "serious_to_thinking": "frames_alpha_2x_gmfss",
}
ROOT_OVERRIDE_BY_LABEL = {
    "trans_standby": Path("projects")
    / "kurisu_front_to_side_4s_1777652179404"
    / "frames_alpha_fx_snapghost_200c_tailfix"
    / "idle",
}
FPS_MULTIPLIER_BY_LABEL = {
    "trans_smile": 4,
    "sad_trans": 4,
    "speaking_trans": 2,
    "closed_eye_trans": 2,
    **{label: 4 for label in FAST_TRANSITION_LABELS},
    **{label: 2 for label in SNAPGHOST_TRANSITION_LABELS},
}
FPS_OVERRIDE_BY_LABEL = {"trans_standby": 48}
ONCE_THEN_HOLD_LABELS = {
    "trans_standby",
    "speaking_trans",
    "closed_eye_trans",
    "thinking_trans",
    "thinking_to_serious",
    "thinking_to_key_point",
    "serious_to_thinking",
    "trans_smile",
    "sad_trans",
    "shy_trans",
    "surprise_trans",
    "angry_trans",
}
POST_EMOTION_ROOTS = {
    "smile": Path("projects") / "kurisu_smile_loop" / "frames_alpha_2x_gmfss" / "idle",
    "sad": Path("projects") / "kurisu_sad_idle_loop" / "frames_alpha_2x_gmfss" / "idle",
}
PREFER_OWN_CLOSED_FRAME = {"key_point_speaking", "thinking_speaking2"}


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _replace_frame_dir(root: Path, frame_dir: str) -> Path:
    parts = list(root.parts)
    for index, part in enumerate(parts):
        if part in FRAME_DIR_NAMES:
            parts[index] = frame_dir
            return Path(*parts)
    raise ValueError(f"cannot identify frame variant in {root}")


def _asset_layout(root: Path) -> tuple[Path, Path]:
    current = root
    while current != current.parent:
        if current.name in FRAME_DIR_NAMES:
            return current.parent, root.relative_to(current)
        current = current.parent
    raise ValueError(f"cannot identify project root for {root}")


def _select_clip(root: Path) -> tuple[str, Path]:
    for phase in PHASE_PRIORITY:
        candidate = root / phase
        if candidate.is_dir() and next(candidate.glob("*.png"), None) is not None:
            return phase, candidate
    if root.is_dir() and next(root.glob("*.png"), None) is not None:
        return "flat", root
    raise ValueError(f"clip has no PNG authoring frames: {root}")


def _sidecar(source: Path, suffix: str) -> Path:
    parts = list(source.parts)
    for index, part in enumerate(parts):
        if part in FRAME_DIR_NAMES:
            parts[index] = f"{part}{suffix}"
            return Path(*parts).with_suffix(".ktx2")
    raise ValueError(f"cannot derive KTX2 sidecar from {source}")


def _detect_fps(project_dir: Path, ffprobe: Path) -> int:
    if not ffprobe.is_file():
        return DEFAULT_FPS
    downloads = project_dir / "downloads"
    videos = [] if not downloads.is_dir() else [
        *downloads.glob("*.mp4"),
        *downloads.glob("*.mov"),
    ]
    if not videos:
        return DEFAULT_FPS
    try:
        result = subprocess.run(
            [
                str(ffprobe),
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_streams",
                str(videos[0]),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=8,
        )
        for stream in json.loads(result.stdout).get("streams", []):
            rate = str(stream.get("r_frame_rate") or "")
            if "/" not in rate:
                continue
            numerator, denominator = rate.split("/", 1)
            fps = round(int(numerator) / max(int(denominator), 1))
            if 10 <= fps <= 120:
                return fps
    except Exception:
        pass
    return DEFAULT_FPS


def _clip_spec(
    *,
    workspace: Path,
    label: str,
    source_root: Path,
    suffix: str,
    ffprobe: Path,
) -> dict[str, Any]:
    frame_dir = source_root.parent.name
    phase, clip_dir = _select_clip(source_root)
    png_frames = sorted(clip_dir.glob("*.png"))
    project_dir, state_relative = _asset_layout(source_root)
    fps = _detect_fps(project_dir, ffprobe)
    if frame_dir != "frames_alpha":
        original = project_dir / "frames_alpha" / state_relative / phase
        if original.is_dir():
            original_count = sum(1 for _ in original.glob("*.png"))
            if original_count > 0:
                interpolation = len(png_frames) // original_count
                if interpolation > 1:
                    fps *= interpolation
    fps *= FPS_MULTIPLIER_BY_LABEL.get(label, 1)
    fps = FPS_OVERRIDE_BY_LABEL.get(label, fps)
    textures = [_sidecar(frame, suffix) for frame in png_frames]
    missing = [path for path in textures if not path.is_file() or path.stat().st_size <= 0]
    if missing:
        raise ValueError(f"{label}: {len(missing)} KTX2 sidecars are missing; first={missing[0]}")
    return {
        "label": label,
        "phase": phase,
        "frameIntervalMs": round(1000 / fps),
        "loopMode": "once_then_hold" if label in ONCE_THEN_HOLD_LABELS else "loop",
        "sourceTextures": textures,
    }


def _safe_label(label: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "_", label).strip("._")
    if not value:
        raise ValueError(f"invalid label: {label!r}")
    return value


def _authoring_path(workspace: Path, raw_root: object, phase: object, name: object) -> Path:
    root_text = str(raw_root or "").strip().replace("\\", "/")
    frame_name = str(name or "").strip()
    if not root_text or not frame_name:
        raise ValueError("mouth profile has no closed source frame")
    parts = [part for part in root_text.split("/") if part]
    if parts and parts[0].casefold() == workspace.name.casefold():
        root = workspace.parent.joinpath(*parts)
    else:
        root = workspace.joinpath(*parts)
    phase_text = str(phase or "").strip()
    if phase_text and phase_text != "flat":
        root /= phase_text
    return root / frame_name


def _mouth_runtime_config(
    workspace: Path,
    raw: dict[str, Any],
    suffix: str,
) -> tuple[dict[str, Any], dict[str, tuple[Path, dict[str, Any]]]]:
    expressions: dict[str, Any] = {}
    for name, raw_expression in (raw.get("expressions") or {}).items():
        if not isinstance(raw_expression, dict):
            continue
        expressions[str(name)] = {
            key: value
            for key, value in raw_expression.items()
            if key != "speaking_frames"
        }

    profiles: dict[str, Any] = {}
    overlays: dict[str, tuple[Path, dict[str, Any]]] = {}
    for raw_label, raw_profile in (raw.get("profiles") or {}).items():
        label = str(raw_label)
        if not isinstance(raw_profile, dict):
            continue
        profile = {
            key: value
            for key, value in raw_profile.items()
            if key not in {"root", "phase", "frame_names", "closed_source", "frame_count"}
        }
        names = raw_profile.get("frame_names") or []
        anchors = raw_profile.get("anchor_track") or []
        try:
            closed_index = int(raw_profile.get("closed_frame_idx", 0))
        except (TypeError, ValueError):
            closed_index = 0
        closed_source = raw_profile.get("closed_source")
        use_closed_source = (
            isinstance(closed_source, dict) and label not in PREFER_OWN_CLOSED_FRAME
        )
        if use_closed_source:
            assert isinstance(closed_source, dict)
            source = _authoring_path(
                workspace,
                closed_source.get("root"),
                closed_source.get("phase"),
                closed_source.get("frame_name"),
            )
            anchor = closed_source.get("anchor")
            anchor = dict(anchor) if isinstance(anchor, dict) else {}
        else:
            if not isinstance(names, list) or not 0 <= closed_index < len(names):
                raise ValueError(f"mouth profile {label!r} has no valid closed frame")
            source = _authoring_path(
                workspace,
                raw_profile.get("root"),
                raw_profile.get("phase"),
                names[closed_index],
            )
            raw_anchor = anchors[closed_index] if 0 <= closed_index < len(anchors) else {}
            anchor = dict(raw_anchor) if isinstance(raw_anchor, dict) else {}
        texture = _sidecar(source, suffix)
        if (not texture.is_file() or texture.stat().st_size <= 0) and use_closed_source:
            if not isinstance(names, list) or not 0 <= closed_index < len(names):
                raise ValueError(f"mouth profile {label!r} has no valid own closed frame")
            source = _authoring_path(
                workspace,
                raw_profile.get("root"),
                raw_profile.get("phase"),
                names[closed_index],
            )
            raw_anchor = anchors[closed_index] if 0 <= closed_index < len(anchors) else {}
            anchor = dict(raw_anchor) if isinstance(raw_anchor, dict) else {}
            texture = _sidecar(source, suffix)
        if not texture.is_file() or texture.stat().st_size <= 0:
            raise ValueError(f"mouth profile {label!r} is missing KTX2 overlay: {texture}")
        runtime_anchor = {
            "cx": anchor.get("cx", raw_profile.get("cx", 0.0)),
            "cy": anchor.get("cy", raw_profile.get("cy", 0.0)),
            "width": anchor.get("width", raw_profile.get("width", 40.0)),
            "height": anchor.get("height", raw_profile.get("height", 20.0)),
        }
        profile["runtime_overlay_anchor"] = runtime_anchor
        profiles[label] = profile
        overlays[label] = (texture, runtime_anchor)

    runtime = {
        "version": raw.get("version"),
        "canvas_size": raw.get("canvas_size"),
        "profile_kind": "runtime_ktx2",
        "expressions": expressions,
        "profiles": profiles,
    }
    return runtime, overlays


def _runtime_graph(raw: dict[str, Any]) -> dict[str, Any]:
    nodes = []
    for raw_node in raw.get("nodes", []):
        if not isinstance(raw_node, dict):
            continue
        node = {"id": raw_node.get("id"), "label": raw_node.get("label")}
        if raw_node.get("isRoot"):
            node["isRoot"] = True
        nodes.append(node)
    edges = [
        {
            "id": edge.get("id"),
            "from": edge.get("from"),
            "to": edge.get("to"),
            "prob": edge.get("prob"),
        }
        for edge in raw.get("edges", [])
        if isinstance(edge, dict)
    ]
    return {"nodes": nodes, "edges": edges}


def build_plan(
    workspace: Path,
    *,
    quality: int,
    zcmp: int,
    ffprobe: Path,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    graph_path = workspace / "graph_config.json"
    mouth_path = workspace / "spriteforge_mouth_config.json"
    graph = _read_json(graph_path)
    mouth = _read_json(mouth_path)
    suffix = f"_ktx2_uastc_q{quality}_z{zcmp}"

    clip_specs: list[dict[str, Any]] = []
    for node in graph.get("nodes", []):
        if not isinstance(node, dict):
            continue
        label = str(node.get("label") or "").strip()
        root_text = str(node.get("root") or "").strip().replace("\\", "/")
        if not label or not root_text:
            raise ValueError("graph nodes require label and root")
        override = ROOT_OVERRIDE_BY_LABEL.get(label)
        if override is not None:
            source_root = workspace / override
        else:
            source_root = workspace.joinpath(*[part for part in root_text.split("/") if part])
            source_root = _replace_frame_dir(
                source_root,
                FRAME_DIR_BY_LABEL.get(label, DEFAULT_FRAME_DIR),
            )
        clip_specs.append(
            _clip_spec(
                workspace=workspace,
                label=label,
                source_root=source_root,
                suffix=suffix,
                ffprobe=ffprobe,
            )
        )
    for label, relative_root in POST_EMOTION_ROOTS.items():
        clip_specs.append(
            _clip_spec(
                workspace=workspace,
                label=label,
                source_root=workspace / relative_root,
                suffix=suffix,
                ffprobe=ffprobe,
            )
        )

    runtime_mouth, mouth_sources = _mouth_runtime_config(workspace, mouth, suffix)
    source_to_relative: dict[Path, Path] = {}
    clip_entries: dict[str, Any] = {}
    for spec in clip_specs:
        label = str(spec["label"])
        phase = str(spec["phase"])
        frame_paths = []
        for source in spec["sourceTextures"]:
            source = Path(source).resolve()
            relative = source_to_relative.get(source)
            if relative is None:
                relative = Path("textures") / _safe_label(label) / _safe_label(phase) / source.name
                source_to_relative[source] = relative
            frame_paths.append(relative.as_posix())
        clip_entries[label] = {
            "phase": phase,
            "frameIntervalMs": int(spec["frameIntervalMs"]),
            "loopMode": str(spec["loopMode"]),
            "frames": frame_paths,
        }

    mouth_entries: dict[str, list[str]] = {}
    for label, (raw_source, _anchor) in mouth_sources.items():
        source = raw_source.resolve()
        relative = source_to_relative.get(source)
        if relative is None:
            relative = Path("textures") / "mouth" / _safe_label(label) / source.name
            source_to_relative[source] = relative
        mouth_entries[label] = [relative.as_posix()]

    texture_bytes = sum(path.stat().st_size for path in source_to_relative)
    manifest = {
        "format": PACK_FORMAT,
        "id": "kurisu",
        "displayName": "Kurisu",
        "version": "2026.08.27",
        "textureFormat": "ktx2",
        "graph": "graph_config.json",
        "mouthConfig": "spriteforge_mouth_config.json",
        "quality": quality,
        "zcmp": zcmp,
        "clips": clip_entries,
        "mouthOverlays": mouth_entries,
        "clipCount": len(clip_entries),
        "frameCount": sum(len(clip["frames"]) for clip in clip_entries.values()),
        "textureCount": len(source_to_relative),
        "textureBytes": texture_bytes,
    }
    return {
        "manifest": manifest,
        "graph": _runtime_graph(graph),
        "mouth": runtime_mouth,
        "textures": source_to_relative,
    }


def _write_package(plan: dict[str, Any], output: Path, *, move_textures: bool) -> None:
    output = output.resolve()
    staging = output.parent / f".{output.name}.staging"
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")
    if staging.exists():
        raise FileExistsError(f"staging directory already exists: {staging}")
    staging.mkdir(parents=True)
    try:
        textures: dict[Path, Path] = plan["textures"]
        for source, relative in textures.items():
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            if destination.stat().st_size != source.stat().st_size:
                raise OSError(f"copied texture size mismatch: {source}")
        (staging / "graph_config.json").write_text(
            json.dumps(plan["graph"], indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (staging / "spriteforge_mouth_config.json").write_text(
            json.dumps(plan["mouth"], indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (staging / "runtime_manifest.json").write_text(
            json.dumps(plan["manifest"], indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        copied = list(staging.rglob("*.ktx2"))
        if len(copied) != int(plan["manifest"]["textureCount"]):
            raise OSError("staged texture count does not match manifest")
        staging.replace(output)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    if move_textures:
        for source in plan["textures"]:
            source.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--quality", type=int, default=4)
    parser.add_argument("--zcmp", type=int, default=18)
    parser.add_argument("--ffprobe", type=Path, default=PROJECT_ROOT / "ffprobe.exe")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--move-textures", action="store_true")
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        raise SystemExit(f"SpriteForge workspace not found: {workspace}")
    plan = build_plan(
        workspace,
        quality=max(0, int(args.quality)),
        zcmp=max(0, int(args.zcmp)),
        ffprobe=args.ffprobe.resolve(),
    )
    manifest = plan["manifest"]
    print(
        "character pack plan: "
        f"clips={manifest['clipCount']} frames={manifest['frameCount']} "
        f"textures={manifest['textureCount']} bytes={manifest['textureBytes']}"
    )
    if args.dry_run:
        for label, clip in manifest["clips"].items():
            print(
                f"{label:<24} frames={len(clip['frames']):4d} "
                f"interval={clip['frameIntervalMs']:3d}ms phase={clip['phase']}"
            )
        return 0
    _write_package(plan, args.output, move_textures=bool(args.move_textures))
    print(f"character pack written: {args.output.resolve()}")
    if args.move_textures:
        print("source runtime textures removed after validated package copy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
