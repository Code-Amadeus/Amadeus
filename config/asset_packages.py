"""Machine-readable boundaries for built-in and separately installed assets."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from config.asset_paths import ASSET_ROOT


ASSET_INDEX_SCHEMA = "amadeus.asset-index.v2"
ASSET_BUNDLE_FORMAT = "amadeus.external-asset-bundle.v1"
ASSET_INDEX_PATH = ASSET_ROOT / "index.json"


class AssetPackageError(ValueError):
    """Raised when the asset catalog or an installed pack violates its contract."""


@dataclass(frozen=True)
class AssetPackSpec:
    id: str
    display_name: str
    spec_version: int
    paths: tuple[PurePosixPath, ...]
    trees: tuple[PurePosixPath, ...]
    required: tuple[PurePosixPath, ...]
    validator: str


def _relative_path(value: Any, *, field: str) -> PurePosixPath:
    text = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(text)
    if (
        not text
        or path.is_absolute()
        or path.as_posix() != text
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(":" in part for part in path.parts)
    ):
        raise AssetPackageError(f"{field} must be a safe asset-relative path")
    return path


def _string_list(value: Any, *, field: str) -> tuple[PurePosixPath, ...]:
    if not isinstance(value, list):
        raise AssetPackageError(f"{field} must be an array")
    return tuple(_relative_path(item, field=f"{field}[]") for item in value)


def load_asset_index(index_path: Path = ASSET_INDEX_PATH) -> dict[str, Any]:
    try:
        raw = json.loads(Path(index_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssetPackageError(f"asset index could not be read: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != ASSET_INDEX_SCHEMA:
        raise AssetPackageError("unsupported asset index schema")
    if raw.get("asset_root") != "assets":
        raise AssetPackageError("asset index root must be 'assets'")
    return raw


def asset_pack_specs(index_path: Path = ASSET_INDEX_PATH) -> dict[str, AssetPackSpec]:
    raw = load_asset_index(index_path)
    entries = raw.get("external_packs")
    if not isinstance(entries, list):
        raise AssetPackageError("external_packs must be an array")

    specs: dict[str, AssetPackSpec] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise AssetPackageError(f"external_packs[{index}] must be an object")
        pack_id = str(entry.get("id") or "").strip()
        if not pack_id or pack_id in specs:
            raise AssetPackageError("external pack ids must be non-empty and unique")
        try:
            spec_version = int(entry.get("spec_version"))
        except (TypeError, ValueError) as exc:
            raise AssetPackageError(f"external pack {pack_id!r} has no spec version") from exc
        if spec_version <= 0:
            raise AssetPackageError(f"external pack {pack_id!r} has an invalid spec version")
        validator = str(entry.get("validator") or "required_files").strip()
        if validator not in {"required_files", "visual_runtime", "character_pack"}:
            raise AssetPackageError(f"external pack {pack_id!r} has an unknown validator")
        spec = AssetPackSpec(
            id=pack_id,
            display_name=str(entry.get("display_name") or pack_id).strip(),
            spec_version=spec_version,
            paths=_string_list(entry.get("paths", []), field=f"{pack_id}.paths"),
            trees=_string_list(entry.get("trees", []), field=f"{pack_id}.trees"),
            required=_string_list(entry.get("required", []), field=f"{pack_id}.required"),
            validator=validator,
        )
        if not spec.required or (not spec.paths and not spec.trees):
            raise AssetPackageError(f"external pack {pack_id!r} has no install contract")
        specs[pack_id] = spec
    return specs


def asset_path(asset_root: Path, relative: PurePosixPath) -> Path:
    root = Path(asset_root).resolve()
    candidate = root.joinpath(*relative.parts).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise AssetPackageError(f"asset path escapes the asset root: {relative}") from exc
    return candidate


def path_belongs_to_pack(relative: PurePosixPath, spec: AssetPackSpec) -> bool:
    if relative in spec.paths:
        return True
    return any(relative == tree or tree in relative.parents for tree in spec.trees)


def external_asset_pack_status(
    pack_id: str,
    *,
    asset_root: Path = ASSET_ROOT,
    index_path: Path = ASSET_INDEX_PATH,
) -> dict[str, Any]:
    specs = asset_pack_specs(index_path)
    try:
        spec = specs[pack_id]
    except KeyError as exc:
        raise AssetPackageError(f"unknown external asset pack: {pack_id}") from exc

    present = [path for path in spec.required if asset_path(asset_root, path).is_file()]
    missing = [path for path in spec.required if path not in present]
    message = ""
    if not present:
        state = "not_installed"
    elif missing:
        state = "incomplete"
    else:
        state = "installed"

    if state == "installed" and spec.validator == "visual_runtime":
        graph_path = asset_path(asset_root, PurePosixPath("scenarios/runtime/scenario_graph.json"))
        try:
            graph = json.loads(graph_path.read_text(encoding="utf-8-sig"))
            if (
                not isinstance(graph, dict)
                or not isinstance(graph.get("nodes"), list)
                or not isinstance(graph.get("edges"), list)
                or any(not isinstance(node, dict) for node in graph["nodes"])
                or any(not isinstance(edge, dict) for edge in graph["edges"])
            ):
                raise ValueError("scenario graph requires object-valued nodes and edges arrays")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            state = "invalid"
            message = str(exc)

    return {
        "id": spec.id,
        "display_name": spec.display_name,
        "installed": state == "installed",
        "state": state,
        "present_required": len(present),
        "required_count": len(spec.required),
        "missing": [path.as_posix() for path in missing],
        "message": message,
        "install_command": "python tools/external_assets.py install <asset-bundle.zip>",
    }
