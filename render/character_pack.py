"""Validation and status for the optional SpriteForge runtime character pack."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.asset_paths import SPRITEFORGE_RUNTIME_MANIFEST, SPRITEFORGE_RUNTIME_ROOT


CHARACTER_PACK_FORMAT = "amadeus.spriteforge.character-pack.v1"


class CharacterPackError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CharacterPack:
    root: Path
    manifest: dict[str, Any]
    graph: dict[str, Any]
    mouth_config: dict[str, Any]
    clip_paths: dict[str, tuple[Path, ...]]
    mouth_overlay_paths: dict[str, tuple[Path, ...]]


def _read_json(path: Path, *, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CharacterPackError(code, f"missing {path.name}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CharacterPackError(code, f"cannot read {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise CharacterPackError(code, f"{path.name} must contain a JSON object")
    return value


def _asset_path(root: Path, value: object, *, field: str) -> Path:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        raise CharacterPackError("invalid_manifest", f"{field} is empty")
    relative = Path(text)
    if relative.is_absolute() or ".." in relative.parts:
        raise CharacterPackError("invalid_manifest", f"{field} must be relative")
    return root / relative


def validate_character_pack_graph(graph: dict[str, Any]) -> None:
    """Validate the runtime graph contract without applying authoring policy."""
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list) or not nodes:
        raise CharacterPackError("invalid_graph", "graph.nodes must be a non-empty array")
    if not isinstance(edges, list):
        raise CharacterPackError("invalid_graph", "graph.edges must be an array")

    node_ids: set[str] = set()
    root_count = 0
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise CharacterPackError("invalid_graph", f"graph.nodes[{index}] must be an object")
        node_id = str(node.get("id") or "").strip()
        label = str(node.get("label") or "").strip()
        if not node_id or not label:
            raise CharacterPackError(
                "invalid_graph", f"graph.nodes[{index}] requires non-empty id and label"
            )
        if node_id in node_ids:
            raise CharacterPackError("invalid_graph", f"duplicate graph node id: {node_id}")
        if "isRoot" in node and not isinstance(node["isRoot"], bool):
            raise CharacterPackError(
                "invalid_graph", f"graph node {node_id!r} has a non-boolean isRoot"
            )
        if str(node.get("root") or "").strip():
            raise CharacterPackError(
                "invalid_graph", "runtime graph must not contain authoring asset roots"
            )
        node_ids.add(node_id)
        root_count += int(node.get("isRoot") is True)

    if root_count != 1:
        raise CharacterPackError(
            "invalid_graph", f"runtime graph requires exactly one root node; found {root_count}"
        )

    edge_ids: set[str] = set()
    edge_pairs: set[tuple[str, str]] = set()
    for index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            raise CharacterPackError("invalid_graph", f"graph.edges[{index}] must be an object")
        edge_id = str(edge.get("id") or "").strip()
        source = str(edge.get("from") or "").strip()
        target = str(edge.get("to") or "").strip()
        if not edge_id or not source or not target:
            raise CharacterPackError(
                "invalid_graph", f"graph.edges[{index}] requires non-empty id, from, and to"
            )
        if edge_id in edge_ids:
            raise CharacterPackError("invalid_graph", f"duplicate graph edge id: {edge_id}")
        if source not in node_ids or target not in node_ids:
            raise CharacterPackError(
                "invalid_graph", f"graph edge {edge_id!r} references an unknown node"
            )
        pair = (source, target)
        if pair in edge_pairs:
            raise CharacterPackError(
                "invalid_graph", f"duplicate graph edge: {source} -> {target}"
            )
        probability = edge.get("prob")
        if (
            isinstance(probability, bool)
            or not isinstance(probability, (int, float))
            or not math.isfinite(probability)
            or probability < 0
        ):
            raise CharacterPackError(
                "invalid_graph", f"graph edge {edge_id!r} requires a finite non-negative prob"
            )
        edge_ids.add(edge_id)
        edge_pairs.add(pair)


def load_character_pack(root: Path = SPRITEFORGE_RUNTIME_ROOT) -> CharacterPack:
    root = Path(root).resolve()
    manifest_path = root / SPRITEFORGE_RUNTIME_MANIFEST.name
    if not manifest_path.is_file():
        raise CharacterPackError("not_installed", "runtime_manifest.json is not installed")

    manifest = _read_json(manifest_path, code="invalid_manifest")
    if manifest.get("format") != CHARACTER_PACK_FORMAT:
        raise CharacterPackError("invalid_manifest", "unsupported character pack format")
    if str(manifest.get("textureFormat") or "").lower() != "ktx2":
        raise CharacterPackError("invalid_manifest", "character pack must use KTX2 textures")
    for field in ("id", "displayName", "version"):
        if not str(manifest.get(field) or "").strip():
            raise CharacterPackError("invalid_manifest", f"{field} must be a non-empty string")

    graph_path = _asset_path(root, manifest.get("graph"), field="graph")
    mouth_path = _asset_path(root, manifest.get("mouthConfig"), field="mouthConfig")
    graph = _read_json(graph_path, code="invalid_graph")
    mouth_config = _read_json(mouth_path, code="invalid_mouth_config")
    validate_character_pack_graph(graph)

    expressions = mouth_config.get("expressions", {})
    profiles = mouth_config.get("profiles", {})
    if not isinstance(expressions, dict) or not isinstance(profiles, dict):
        raise CharacterPackError(
            "invalid_mouth_config", "mouth config must contain expressions and profiles"
        )
    if any(not isinstance(value, dict) for value in expressions.values()) or any(
        not isinstance(value, dict) for value in profiles.values()
    ):
        raise CharacterPackError(
            "invalid_mouth_config", "mouth expressions and profiles must be objects"
        )
    if any(
        isinstance(expression, dict) and "speaking_frames" in expression
        for expression in expressions.values()
    ):
        raise CharacterPackError(
            "invalid_mouth_config", "runtime mouth expressions must not reference PNG frames"
        )
    authoring_profile_fields = {"root", "phase", "frame_names", "closed_source"}
    if any(
        isinstance(profile, dict) and authoring_profile_fields.intersection(profile)
        for profile in profiles.values()
    ):
        raise CharacterPackError(
            "invalid_mouth_config", "runtime mouth profiles contain authoring paths"
        )

    raw_clips = manifest.get("clips")
    if not isinstance(raw_clips, dict) or not raw_clips:
        raise CharacterPackError("invalid_manifest", "character pack has no clips")

    clip_paths: dict[str, tuple[Path, ...]] = {}
    referenced_textures: set[Path] = set()
    for raw_label, raw_clip in raw_clips.items():
        label = str(raw_label or "").strip()
        if not label or not isinstance(raw_clip, dict):
            raise CharacterPackError("invalid_manifest", "clip entries require a label and object")
        raw_frames = raw_clip.get("frames")
        if not isinstance(raw_frames, list) or not raw_frames:
            raise CharacterPackError("invalid_manifest", f"clip {label!r} has no frames")
        try:
            interval_ms = int(raw_clip.get("frameIntervalMs"))
        except (TypeError, ValueError) as exc:
            raise CharacterPackError(
                "invalid_manifest", f"clip {label!r} has an invalid frame interval"
            ) from exc
        if interval_ms <= 0:
            raise CharacterPackError(
                "invalid_manifest", f"clip {label!r} has an invalid frame interval"
            )
        phase = str(raw_clip.get("phase") or "").strip()
        if not phase:
            raise CharacterPackError("invalid_manifest", f"clip {label!r} has no phase")
        if raw_clip.get("loopMode") not in {"loop", "once_then_hold"}:
            raise CharacterPackError(
                "invalid_manifest", f"clip {label!r} has an unsupported loopMode"
            )
        paths: list[Path] = []
        for index, raw_frame in enumerate(raw_frames):
            frame = _asset_path(root, raw_frame, field=f"clips.{label}.frames[{index}]")
            if frame.suffix.lower() != ".ktx2":
                raise CharacterPackError(
                    "invalid_manifest", f"clip {label!r} contains a non-KTX2 frame"
                )
            if not frame.is_file():
                raise CharacterPackError(
                    "incomplete_pack", f"clip {label!r} is missing {frame.name}"
                )
            paths.append(frame)
            referenced_textures.add(frame)
        clip_paths[label] = tuple(paths)

    graph_labels = {
        str(node.get("label") or "").strip()
        for node in graph["nodes"]
        if isinstance(node, dict)
    }
    missing_clips = sorted(label for label in graph_labels if label and label not in clip_paths)
    if missing_clips:
        raise CharacterPackError(
            "incomplete_pack", f"graph clips missing from manifest: {', '.join(missing_clips)}"
        )

    mouth_overlay_paths: dict[str, tuple[Path, ...]] = {}
    raw_overlays = manifest.get("mouthOverlays", {})
    if not isinstance(raw_overlays, dict):
        raise CharacterPackError("invalid_manifest", "mouthOverlays must be an object")
    for raw_label, raw_frames in raw_overlays.items():
        label = str(raw_label or "").strip()
        if not label or not isinstance(raw_frames, list) or not raw_frames:
            raise CharacterPackError("invalid_manifest", "mouth overlay entries are invalid")
        paths = []
        for index, raw_frame in enumerate(raw_frames):
            frame = _asset_path(root, raw_frame, field=f"mouthOverlays.{label}[{index}]")
            if frame.suffix.lower() != ".ktx2" or not frame.is_file():
                raise CharacterPackError(
                    "incomplete_pack", f"mouth overlay {label!r} is missing a KTX2 frame"
                )
            paths.append(frame)
            referenced_textures.add(frame)
        mouth_overlay_paths[label] = tuple(paths)

    missing_overlays = sorted(
        str(label) for label in profiles if str(label) not in mouth_overlay_paths
    )
    if missing_overlays:
        raise CharacterPackError(
            "incomplete_pack",
            f"mouth overlays missing from manifest: {', '.join(missing_overlays)}",
        )

    frame_count = sum(len(paths) for paths in clip_paths.values())
    declared_clip_count = manifest.get("clipCount")
    if declared_clip_count is not None:
        try:
            matches = int(declared_clip_count) == len(clip_paths)
        except (TypeError, ValueError):
            matches = False
        if not matches:
            raise CharacterPackError("invalid_manifest", "clipCount does not match clips")
    declared_frame_count = manifest.get("frameCount")
    if declared_frame_count is not None:
        try:
            matches = int(declared_frame_count) == frame_count
        except (TypeError, ValueError):
            matches = False
        if not matches:
            raise CharacterPackError("invalid_manifest", "frameCount does not match clip frames")
    declared_texture_count = manifest.get("textureCount")
    if declared_texture_count is not None:
        try:
            matches = int(declared_texture_count) == len(referenced_textures)
        except (TypeError, ValueError):
            matches = False
        if not matches:
            raise CharacterPackError(
                "invalid_manifest", "textureCount does not match referenced textures"
            )
    declared_texture_bytes = manifest.get("textureBytes")
    if declared_texture_bytes is not None:
        try:
            matches = int(declared_texture_bytes) == sum(
                path.stat().st_size for path in referenced_textures
            )
        except (OSError, TypeError, ValueError):
            matches = False
        if not matches:
            raise CharacterPackError(
                "invalid_manifest", "textureBytes does not match referenced textures"
            )

    return CharacterPack(
        root=root,
        manifest=manifest,
        graph=graph,
        mouth_config=mouth_config,
        clip_paths=clip_paths,
        mouth_overlay_paths=mouth_overlay_paths,
    )


def character_pack_status(root: Path = SPRITEFORGE_RUNTIME_ROOT) -> dict[str, Any]:
    try:
        pack = load_character_pack(root)
    except CharacterPackError as exc:
        return {
            "id": "kurisu",
            "display_name": "Kurisu",
            "installed": False,
            "state": "not_installed" if exc.code == "not_installed" else "invalid",
            "reason": exc.code,
            "message": str(exc),
            "relative_path": "assets/spriteforge/runtime/kurisu",
        }

    manifest = pack.manifest
    return {
        "id": str(manifest.get("id") or "kurisu"),
        "display_name": str(manifest.get("displayName") or "Kurisu"),
        "installed": True,
        "state": "installed",
        "version": str(manifest.get("version") or ""),
        "texture_format": "ktx2",
        "clip_count": len(pack.clip_paths),
        "frame_count": sum(len(paths) for paths in pack.clip_paths.values()),
        "texture_bytes": int(manifest.get("textureBytes") or 0),
        "relative_path": "assets/spriteforge/runtime/kurisu",
    }
