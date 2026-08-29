from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path, PurePosixPath

import pytest

from config.asset_packages import (
    ASSET_BUNDLE_FORMAT,
    ASSET_INDEX_SCHEMA,
    AssetPackageError,
    asset_pack_specs,
    external_asset_pack_status,
    path_belongs_to_pack,
)
from tools.external_assets import build_bundle, install_bundle, verify_bundle


def _write_catalog(root: Path) -> Path:
    index_path = root / "assets" / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(
            {
                "schema": ASSET_INDEX_SCHEMA,
                "asset_root": "assets",
                "built_in": [],
                "external_packs": [
                    {
                        "id": "visual-test",
                        "display_name": "Visual Test Pack",
                        "spec_version": 1,
                        "paths": ["images/ambient.png"],
                        "trees": ["scenarios/runtime"],
                        "required": [
                            "images/ambient.png",
                            "scenarios/runtime/scenario_graph.json",
                        ],
                        "validator": "visual_runtime",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return index_path


def _write_visual_pack(root: Path) -> None:
    image = root / "assets" / "images" / "ambient.png"
    graph = root / "assets" / "scenarios" / "runtime" / "scenario_graph.json"
    frame = root / "assets" / "scenarios" / "runtime" / "scene" / "frame.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    graph.parent.mkdir(parents=True, exist_ok=True)
    frame.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"ambient")
    graph.write_text('{"nodes": [], "edges": []}', encoding="utf-8")
    frame.write_bytes(b"frame")


def _bundle_manifest(files: list[dict[str, object]]) -> dict[str, object]:
    return {
        "format": ASSET_BUNDLE_FORMAT,
        "packs": [{"id": "visual-test", "spec_version": 1}],
        "file_count": len(files),
        "total_bytes": sum(int(record["size"]) for record in files),
        "files": files,
    }


def test_external_pack_status_distinguishes_absent_partial_and_installed(tmp_path: Path) -> None:
    index_path = _write_catalog(tmp_path)

    absent = external_asset_pack_status(
        "visual-test", asset_root=tmp_path / "assets", index_path=index_path
    )
    assert absent["state"] == "not_installed"

    image = tmp_path / "assets" / "images" / "ambient.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"ambient")
    partial = external_asset_pack_status(
        "visual-test", asset_root=tmp_path / "assets", index_path=index_path
    )
    assert partial["state"] == "incomplete"
    assert partial["missing"] == ["scenarios/runtime/scenario_graph.json"]

    graph = tmp_path / "assets" / "scenarios" / "runtime" / "scenario_graph.json"
    graph.parent.mkdir(parents=True)
    graph.write_text('{"nodes": [], "edges": []}', encoding="utf-8")
    installed = external_asset_pack_status(
        "visual-test", asset_root=tmp_path / "assets", index_path=index_path
    )
    assert installed["state"] == "installed"
    assert installed["installed"] is True

    graph.write_text("[]", encoding="utf-8")
    invalid = external_asset_pack_status(
        "visual-test", asset_root=tmp_path / "assets", index_path=index_path
    )
    assert invalid["state"] == "invalid"
    assert invalid["installed"] is False


def test_bundle_build_is_deterministic_and_install_is_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source_index = _write_catalog(source)
    destination_index = _write_catalog(destination)
    _write_visual_pack(source)
    wallpaper = source / "assets" / "images" / "amadeus_desktop_wallpaper.png"
    wallpaper.write_bytes(b"built-in")

    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    first_manifest = build_bundle(
        project_root=source,
        output=first,
        pack_ids=["visual-test"],
        index_path=source_index,
    )
    second_manifest = build_bundle(
        project_root=source,
        output=second,
        pack_ids=["visual-test"],
        index_path=source_index,
    )

    assert first_manifest == second_manifest
    assert first.read_bytes() == second.read_bytes()
    assert "assets/images/amadeus_desktop_wallpaper.png" not in {
        record["path"] for record in first_manifest["files"]
    }
    assert verify_bundle(first, index_path=source_index) == first_manifest

    installed = install_bundle(
        first,
        project_root=destination,
        index_path=destination_index,
    )
    assert installed == {
        "packs": ["visual-test"],
        "installed_files": 3,
        "unchanged_files": 0,
        "total_files": 3,
    }
    assert (destination / "assets" / "images" / "ambient.png").read_bytes() == b"ambient"
    assert (destination / "assets" / "scenarios" / "runtime" / "scene" / "frame.png").read_bytes() == b"frame"

    repeated = install_bundle(
        first,
        project_root=destination,
        index_path=destination_index,
    )
    assert repeated["installed_files"] == 0
    assert repeated["unchanged_files"] == 3


def test_install_conflict_fails_before_writing_and_overwrite_is_explicit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source_index = _write_catalog(source)
    destination_index = _write_catalog(destination)
    _write_visual_pack(source)
    bundle = tmp_path / "visual.zip"
    build_bundle(
        project_root=source,
        output=bundle,
        pack_ids=["visual-test"],
        index_path=source_index,
    )

    conflict = destination / "assets" / "images" / "ambient.png"
    conflict.parent.mkdir(parents=True)
    conflict.write_bytes(b"local-edit")
    with pytest.raises(AssetPackageError, match="would replace a different local asset"):
        install_bundle(bundle, project_root=destination, index_path=destination_index)

    assert conflict.read_bytes() == b"local-edit"
    assert not (destination / "assets" / "scenarios" / "runtime" / "scene" / "frame.png").exists()

    result = install_bundle(
        bundle,
        project_root=destination,
        index_path=destination_index,
        overwrite=True,
    )
    assert result["installed_files"] == 3
    assert conflict.read_bytes() == b"ambient"


def test_bundle_output_cannot_be_written_into_its_asset_source_tree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    index_path = _write_catalog(source)
    _write_visual_pack(source)

    with pytest.raises(AssetPackageError, match="must not be written inside"):
        build_bundle(
            project_root=source,
            output=source / "assets" / "visual.zip",
            pack_ids=["visual-test"],
            index_path=index_path,
        )


def test_bundle_rejects_traversal_before_extracting(tmp_path: Path) -> None:
    index_path = _write_catalog(tmp_path)
    archive_path = tmp_path / "malicious.zip"
    payload = b"escape"
    record = {
        "path": "assets/../escape.txt",
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("assets/../escape.txt", payload)
        archive.writestr(
            "ASSET_BUNDLE_MANIFEST.json",
            json.dumps(_bundle_manifest([record])),
        )

    with pytest.raises(AssetPackageError, match="unsafe bundle path segment"):
        verify_bundle(archive_path, index_path=index_path)
    assert not (tmp_path / "escape.txt").exists()


def test_repository_catalog_keeps_built_in_art_outside_external_packs() -> None:
    specs = asset_pack_specs()
    visual = specs["visual-runtime"]
    character = specs["character-kurisu"]

    assert not path_belongs_to_pack(
        PurePosixPath("images/amadeus_desktop_wallpaper.png"), visual
    )
    assert not path_belongs_to_pack(PurePosixPath("icons/app/app_icon.ico"), visual)
    assert not path_belongs_to_pack(PurePosixPath("images/kurisu_normal1.png"), visual)
    assert path_belongs_to_pack(
        PurePosixPath("spriteforge/runtime/kurisu/textures/idle/0001.ktx2"), character
    )


def test_wallpaper_disables_a_malformed_optional_scenario_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wallpaper import scene_assets

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "scenario_graph.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(scene_assets, "_RUNTIME_SCENARIOS", runtime)
    monkeypatch.setattr(scene_assets, "_SCENARIO_SOURCE_ROOT", tmp_path / "missing-source")

    result = scene_assets._prepare_scenario_payload(12345)

    assert result == {"enabled": False, "reason": "scenario graph has an invalid shape"}


def test_wallpaper_disables_missing_optional_scenarios_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wallpaper import scene_assets

    runtime = tmp_path / "missing-runtime"
    source = tmp_path / "missing-source"
    monkeypatch.setattr(scene_assets, "_RUNTIME_SCENARIOS", runtime)
    monkeypatch.setattr(scene_assets, "_SCENARIO_SOURCE_ROOT", source)

    result = scene_assets._prepare_scenario_payload(12345)

    assert result["enabled"] is False
    assert "scenario graph not found" in result["reason"]
