"""Host-owned visual asset profiles, independent of conversational identities.

The store edits presentation choices only. Original model assets remain read-only.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import threading
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from config.durable_io import write_text
from render.spriteforge_intent import spriteforge_intent_payload

_LAYOUT = {"scale": 1.0, "x": 0.0, "y": 0.0}
_DEFAULT_EXPRESSIONS = {
    "smile": "Smile", "happy": "Smile", "thinking": "Thinking",
    "angry": "Angry", "sad": "Disappointed", "disappointed": "Disappointed",
    "work": "Thinking", "working": "Thinking", "serious_speaking": "Thinking",
    "normal": None, "neutral": None, "shy": None, "blush": None, "surprised": None,
}


def default_config() -> dict[str, Any]:
    return {"backend": "sprite", "selected_profile_id": "", "core_path": "", "profiles": []}


def _number(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return result


def _keys(value: Any, allowed: set[str], name: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"unsupported {name} fields: {', '.join(sorted(unknown))}")
    return value


def validate_profile(value: Any) -> dict[str, Any]:
    data = _keys(value, {"profile_id", "kind", "name", "model_path", "emotion_map",
                         "mouth", "layouts"}, "profile")
    identity = str(data.get("profile_id") or "").strip()
    if not identity or len(identity) > 80 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in identity):
        raise ValueError("profile_id must be a stable visual asset identifier")
    if data.get("kind") != "live2d":
        raise ValueError("profile.kind must be live2d")
    model_path = str(data.get("model_path") or "").strip()
    if not model_path or not Path(model_path).is_absolute() or not model_path.lower().endswith(".model3.json"):
        raise ValueError("model_path must be an absolute .model3.json path")
    mapping = data.get("emotion_map", {})
    if not isinstance(mapping, dict):
        raise ValueError("emotion_map must be an object")
    emotion_map = {}
    for label, expression in mapping.items():
        semantic = str(label).strip().lower()
        if not semantic or len(semantic) > 100:
            raise ValueError("invalid emotion_map semantic label")
        if expression is not None and (not isinstance(expression, str) or not expression.strip()):
            raise ValueError("an expression mapping must be its registered name or null")
        emotion_map[semantic] = expression.strip() if expression is not None else None
    mouth = _keys(data.get("mouth", {}), {"gain", "smoothing_ms", "parameter_ids"}, "mouth")
    ids = mouth.get("parameter_ids", [])
    if not isinstance(ids, list) or any(not isinstance(item, str) or not item.strip() for item in ids):
        raise ValueError("mouth.parameter_ids must contain model parameter IDs")
    layouts = _keys(data.get("layouts", {}), {"render", "wallpaper"}, "layouts")
    normalized_layouts = {}
    for surface in ("render", "wallpaper"):
        layout = _keys(layouts.get(surface, {}), {"scale", "x", "y"}, f"{surface} layout")
        normalized_layouts[surface] = {
            "scale": _number(layout.get("scale", 1), "layout scale", .1, 5),
            "x": _number(layout.get("x", 0), "layout x", -1, 1),
            "y": _number(layout.get("y", 0), "layout y", -1, 1),
        }
    return {
        "profile_id": identity, "kind": "live2d",
        "name": str(data.get("name") or Path(model_path).stem).strip()[:120],
        "model_path": str(Path(model_path)),
        "emotion_map": emotion_map,
        "mouth": {
            "gain": _number(mouth.get("gain", 1), "mouth gain", 0, 5),
            "smoothing_ms": _number(mouth.get("smoothing_ms", 60), "mouth smoothing", 0, 500),
            "parameter_ids": list(dict.fromkeys(item.strip() for item in ids)),
        },
        "layouts": normalized_layouts,
    }


def validate_config(value: Any) -> dict[str, Any]:
    data = _keys(value, {"backend", "selected_profile_id", "core_path", "profiles"}, "config")
    backend = data.get("backend", "sprite")
    if backend not in {"sprite", "live2d"}:
        raise ValueError("backend must be sprite or live2d")
    profiles = data.get("profiles", [])
    if not isinstance(profiles, list):
        raise ValueError("profiles must be an array")
    profiles = [validate_profile(item) for item in profiles]
    identities = [item["profile_id"] for item in profiles]
    if len(identities) != len(set(identities)):
        raise ValueError("profile_id must be unique")
    selected = str(data.get("selected_profile_id") or "")
    if selected and selected not in identities:
        raise ValueError("selected_profile_id does not exist")
    if backend == "live2d" and not selected:
        raise ValueError("select a Live2D visual profile")
    core = str(data.get("core_path") or "").strip()
    if core and (not Path(core).is_absolute() or Path(core).name.lower() != "live2dcubismcore.min.js"):
        raise ValueError("select the local live2dcubismcore.min.js SDK file")
    return {"backend": backend, "selected_profile_id": selected,
            "core_path": core, "profiles": profiles}


def inspect_model(model_path: Path | str) -> tuple[dict, dict[str, Path]]:
    entry = Path(model_path).resolve()
    if not entry.name.lower().endswith(".model3.json") or not entry.is_file():
        raise ValueError("the selected .model3.json model does not exist")
    try:
        settings = json.loads(entry.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read model settings: {exc}") from exc
    if not isinstance(settings, dict):
        raise ValueError("model settings must be a JSON object")
    refs = settings.get("FileReferences")
    if settings.get("Version") != 3 or not isinstance(refs, dict) or not refs.get("Moc") or not refs.get("Textures"):
        raise ValueError("model settings require Cubism 3 Moc and Textures references")
    assets = {entry.name: entry}
    warnings = []

    def add_file(relative: Any, *, required: bool = True) -> Path | None:
        if not isinstance(relative, str) or not relative or "\\" in relative:
            raise ValueError("model asset references must be relative package paths")
        target = (entry.parent / relative).resolve()
        if not target.is_relative_to(entry.parent) or Path(relative).is_absolute():
            raise ValueError("model asset reference leaves the selected model package")
        if not target.is_file():
            if required:
                raise ValueError(f"model asset is missing: {relative}")
            warnings.append(f"Optional model asset is missing: {relative}")
            return None
        assets[relative] = target
        return target

    add_file(refs["Moc"])
    if not isinstance(refs["Textures"], list):
        raise ValueError("model Textures must be an array")
    for texture in refs["Textures"]:
        add_file(texture)
    expressions = []
    expression_refs = refs.get("Expressions", [])
    if not isinstance(expression_refs, list):
        raise ValueError("model Expressions must be an array")
    for expression in expression_refs:
        if not isinstance(expression, dict) or not expression.get("Name"):
            raise ValueError("expression requires its registered Name and File")
        name = str(expression["Name"])
        if name in expressions:
            raise ValueError(f"duplicate expression name: {name}")
        add_file(expression.get("File"))
        expressions.append(name)
    for key in ("Physics", "Pose", "DisplayInfo", "UserData"):
        if refs.get(key):
            asset = add_file(refs[key], required=False)
            if key == "Physics" and asset:
                try:
                    physics = json.loads(asset.read_text(encoding="utf-8-sig"))
                    if not isinstance(physics, dict) or physics.get("Version") != 3 or not isinstance(physics.get("Meta"), dict):
                        warnings.append("Physics asset is not Cubism physics3 format; the SDK may omit physics.")
                except (OSError, ValueError):
                    warnings.append("Physics asset could not be parsed; the SDK may omit physics.")
    motion_groups = refs.get("Motions", {})
    if not isinstance(motion_groups, dict):
        raise ValueError("model Motions must be an object")
    for motions in motion_groups.values():
        if not isinstance(motions, list):
            raise ValueError("model motion groups must be arrays")
        for motion in motions:
            if not isinstance(motion, dict):
                raise ValueError("model motion entries must be objects")
            asset = add_file(motion.get("File"), required=False)
            if not asset:
                continue
            try:
                content = json.loads(asset.read_text(encoding="utf-8-sig"))
                meta = content.get("Meta", {})
                curves = content.get("Curves", [])
                segment_count = 0
                point_count = 0
                for curve in curves:
                    segments = curve["Segments"]
                    cursor = 2
                    point_count += 1
                    while cursor < len(segments):
                        segment_type = segments[cursor]
                        points = 3 if segment_type == 1 else 1
                        cursor += 1 + 2 * points
                        segment_count += 1
                        point_count += points
                    if cursor != len(segments):
                        raise ValueError("invalid motion segments")
                if (meta.get("CurveCount") != len(curves)
                        or meta.get("TotalSegmentCount") != segment_count
                        or meta.get("TotalPointCount") != point_count):
                    warnings.append(f"Motion metadata is inconsistent: {motion['File']}")
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                warnings.append(f"Motion could not be validated: {motion['File']}")
    lip_ids = []
    groups = settings.get("Groups", [])
    if not isinstance(groups, list):
        raise ValueError("model Groups must be an array")
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("model group entries must be objects")
        if group.get("Target") == "Parameter" and group.get("Name") == "LipSync":
            if not isinstance(group.get("Ids", []), list):
                raise ValueError("model LipSync Ids must be an array")
            lip_ids.extend(group.get("Ids", []))
    if any(not isinstance(item, str) or not item for item in lip_ids):
        raise ValueError("invalid model LipSync IDs")
    return {"expressions": expressions, "lip_sync_ids": list(dict.fromkeys(lip_ids)),
            "warnings": warnings}, assets


def draft_profile(model_path: str) -> tuple[dict, dict]:
    capabilities, _ = inspect_model(model_path)
    return _draft_profile(model_path, capabilities), capabilities


def _draft_profile(model_path: str, capabilities: dict) -> dict:
    available = {name.lower(): name for name in capabilities["expressions"]}
    profile = {
        "profile_id": str(uuid4()), "kind": "live2d",
        "name": Path(model_path).name.removesuffix(".model3.json"),
        "model_path": str(Path(model_path).resolve()),
        "emotion_map": {label: available.get(label) or
                        (available.get(expression.lower()) if expression else None)
                        for label, expression in _DEFAULT_EXPRESSIONS.items()},
        "mouth": {"gain": 1.0, "smoothing_ms": 60.0, "parameter_ids": []},
        "layouts": {"render": dict(_LAYOUT), "wallpaper": dict(_LAYOUT)},
    }
    return profile


def _inspect_profile(profile: dict | None) -> dict:
    result = {"capabilities": {"expressions": [], "lip_sync_ids": [], "warnings": []}, "files": {}}
    if profile is not None:
        try:
            result["capabilities"], result["files"] = inspect_model(profile["model_path"])
        except (OSError, ValueError) as exc:
            result["diagnostic"] = str(exc)
    return result


class VisualProfileStore:
    def __init__(self, path: Path | str, *, project_root: Path | str) -> None:
        self.path = Path(path)
        self.project_root = Path(project_root)
        self._lock = threading.RLock()
        self._config = default_config()
        self.runtime_id = str(uuid4())
        self.revision = 0
        self.reload_revision = 0
        self.load_error = ""
        if self.path.is_file():
            try:
                self._config = validate_config(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                self.load_error = f"Cannot load visual profiles: {exc}"
        self._inspection = _inspect_profile(self.selected_profile())

    @property
    def config(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._config)

    @property
    def backend(self) -> str:
        with self._lock:
            return self._config["backend"]

    def selected_profile(self) -> dict | None:
        config = self.config
        return next((item for item in config["profiles"]
                     if item["profile_id"] == config["selected_profile_id"]), None)

    def snapshot(self) -> dict:
        """Observed state reads the last inspection; it never parses model files."""
        with self._lock:
            result = {"config": copy.deepcopy(self._config),
                      "capabilities": copy.deepcopy(self._inspection["capabilities"])}
            diagnostic = self.load_error or self._inspection.get("diagnostic", "")
            if diagnostic:
                result["diagnostic"] = diagnostic
            return result

    def inspect(self, model_path: str) -> tuple[dict, dict]:
        """An explicit inspection also refreshes the selected asset's cached facts."""
        with self._lock:
            inspection = _inspect_profile({"model_path": model_path})
            selected = self.selected_profile()
            if selected and Path(selected["model_path"]).resolve() == Path(model_path).resolve():
                self._inspection = inspection
            if inspection.get("diagnostic"):
                raise ValueError(inspection["diagnostic"])
            capabilities = inspection["capabilities"]
            return _draft_profile(model_path, capabilities), copy.deepcopy(capabilities)

    def reload(self) -> None:
        with self._lock:
            self.revision += 1
            self.reload_revision += 1
            self._inspection = _inspect_profile(self.selected_profile())

    def save(self, value: Any) -> dict:
        if self.load_error:
            raise ValueError(self.load_error + ". Repair the saved file before saving; the original file is preserved.")
        config = validate_config(value)
        with self._lock:
            # Saving is an explicit refresh boundary. Broken dormant assets
            # never constrain another selection or the Sprite fallback.
            selected = next((item for item in config["profiles"]
                             if item["profile_id"] == config["selected_profile_id"]), None)
            inspection = _inspect_profile(selected)
            if config["backend"] == "live2d" and selected and not inspection.get("diagnostic"):
                available = inspection["capabilities"]["expressions"]
                unknown = set(selected["emotion_map"].values()) - {None} - set(available)
                if unknown:
                    raise ValueError(f"Unregistered model expressions: {', '.join(sorted(unknown))}")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_text(self.path, json.dumps(config, indent=2, ensure_ascii=False) + "\n")
            self._config = config
            self._inspection = inspection
            self.revision += 1
            self.load_error = ""
            return self.config

    def intent_payload(self, label: str, **metadata: Any) -> dict:
        if self.backend == "sprite":
            return {**spriteforge_intent_payload(label, **metadata), "backend": "sprite"}
        return {**metadata, "backend": "live2d", "label": str(label or "").strip(),
                "semantic_label": str(label or "").strip()}

    def runtime_config(self, surface: str, *, asset_server=None,
                       profile: dict | None = None, core_path: str | None = None,
                       preview: bool = False) -> dict:
        with self._lock:
            if surface not in {"render", "wallpaper"}:
                raise ValueError("unknown render surface")
            saved = self.config
            backend = "live2d" if preview else saved["backend"]
            profile = validate_profile(profile) if profile is not None else self.selected_profile()
            result = {"backend": backend, "profile_id": profile["profile_id"] if profile else "",
                      "revision": self.revision, "runtime_id": self.runtime_id,
                      "reload_revision": self.reload_revision, "surface": surface}
            if backend == "sprite":
                if asset_server is not None:
                    asset_server.mount_files("/visual-model", {})
                    asset_server.mount_files("/visual-core", {})
                return result
            if not profile:
                if asset_server is not None:
                    asset_server.mount_files("/visual-model", {})
                    asset_server.mount_files("/visual-core", {})
                return {**result, "error": "Select a Live2D visual profile."}
            result.update(emotion_map=profile["emotion_map"], mouth=profile["mouth"],
                          layout=profile["layouts"][surface])
            try:
                inspection = _inspect_profile(profile)
                selected = self.selected_profile()
                if selected and Path(selected["model_path"]).resolve() == Path(profile["model_path"]).resolve():
                    self._inspection = inspection
                if inspection.get("diagnostic"):
                    raise ValueError(inspection["diagnostic"])
                files = inspection["files"]
                result["warnings"] = inspection["capabilities"]["warnings"]
                entry = Path(profile["model_path"]).resolve()
                core = Path(core_path or saved["core_path"] or
                            self.project_root / "render/web/vendor/live2dcubismcore.min.js").resolve()
                if not core.is_file():
                    raise ValueError("Cubism Core is missing. Select your local live2dcubismcore.min.js SDK.")
                if core.name.lower() != "live2dcubismcore.min.js":
                    raise ValueError("Select the local live2dcubismcore.min.js SDK.")
                core_stat = core.stat()
                core_identity = hashlib.sha256(
                    f"{core}:{core_stat.st_mtime_ns}:{core_stat.st_size}".encode("utf-8")
                ).hexdigest()[:16]
                model_version = f"{entry.stat().st_mtime_ns}-{self.reload_revision}"
                if asset_server is None:
                    result.update(model_url=entry.as_uri() + "?v=" + model_version,
                                  core_url=core.as_uri() + "?v=" + core_identity)
                else:
                    prefix = f"/visual-model/{profile['profile_id']}"
                    asset_server.mount_files("/visual-model", {
                        f"{profile['profile_id']}/{relative}": target for relative, target in files.items()
                    })
                    asset_server.mount_files("/visual-core", {f"{core_identity}/live2dcubismcore.min.js": core})
                    result.update(model_url=f"{prefix}/{quote(entry.name)}?v={model_version}",
                                  core_url=f"/visual-core/{core_identity}/live2dcubismcore.min.js")
            except (ValueError, OSError) as exc:
                if asset_server is not None:
                    asset_server.mount_files("/visual-model", {})
                    asset_server.mount_files("/visual-core", {})
                result["error"] = str(exc)
            return result
