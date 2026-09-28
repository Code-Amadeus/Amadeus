from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from config.asset_packages import ASSET_INDEX_PATH, AssetPackageError, asset_pack_specs, external_asset_pack_status
from render.companion_pack import CompanionPackError, load_companion_pack, validate_clip
from tools.external_assets import build_bundle, install_bundle, verify_bundle


def _write_pack(root: Path) -> Path:
    pack = root / "assets/companion/kurisu"
    pack.mkdir(parents=True)
    # Transport validation checks metadata/hash, not image decoding (authoring/browser own that).
    payload = b"synthetic portrait for transport contract tests"
    (pack / "portrait.webp").write_bytes(payload)
    spec = {"url": "portrait.webp", "size": 2, "columns": 2, "sequence": [0, 1, 0],
            "durationMs": 1020, "decodedBytes": 32, "fileBytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest()}
    manifest = {"format": "amadeus.companion-atlas.v1", "emotions": {"normal": {
        mode: copy.deepcopy(spec) for mode in ("idle", "speaking", "idleStatic", "speakingAlternate")}}}
    (pack / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return pack


def _status(root: Path) -> dict:
    return external_asset_pack_status("companion-kurisu", asset_root=root / "assets")


def _write_visual(root: Path):
    for relative in asset_pack_specs()["visual-runtime"].required:
        path = root / "assets" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"nodes":[],"edges":[]}' if path.name == "scenario_graph.json" else b"test")


def test_companion_is_independent_optional_and_manifest_indexed(tmp_path: Path):
    source, target = tmp_path / "source", tmp_path / "target"
    assert _status(source)["state"] == "not_installed"
    pack = _write_pack(source)
    (pack / "unused-authoring.png").write_bytes(b"not for distribution")
    assert _status(source)["state"] == "installed"
    bundle = tmp_path / "companion.zip"
    manifest = build_bundle(project_root=source, output=bundle, pack_ids=["companion-kurisu"],
                            index_path=ASSET_INDEX_PATH)
    assert manifest["file_count"] == 2
    assert verify_bundle(bundle) == manifest
    result = install_bundle(bundle, project_root=target, index_path=ASSET_INDEX_PATH)
    assert result["installed_files"] == 2
    assert _status(target)["state"] == "installed"
    assert not (target / "assets/spriteforge").exists()
    assert not (target / "assets/companion/kurisu/unused-authoring.png").exists()
    assert install_bundle(bundle, project_root=target, index_path=ASSET_INDEX_PATH)["unchanged_files"] == 2
    (target / "assets/companion/kurisu/portrait.webp").unlink()
    assert _status(target)["state"] == "invalid"


@pytest.mark.parametrize("field,value,message", [
    ("url", "../portrait.webp", "Unsafe"),
    ("url", "C:/private/portrait.webp", "Unsafe"),
    ("sequence", [], "timeline"),
    ("sequence", [2048], "timeline"),
    ("sequence", [True], "timeline"),
    ("durationMs", 0, "timeline"),
    ("durationMs", float("nan"), "timeline"),
    ("durationMs", True, "timeline"),
    ("decodedBytes", 999, "budget"),
    ("sha256", "0" * 64, "checksum"),
    ("fileBytes", 1, "size mismatch"),
])
def test_bad_selected_pack_fails_before_build(tmp_path: Path, field, value, message):
    pack = _write_pack(tmp_path)
    path = pack / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["emotions"]["normal"]["speakingAlternate"][field] = value
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert _status(tmp_path)["state"] == "invalid"
    output = tmp_path / "bad.zip"
    with pytest.raises(AssetPackageError, match=message):
        build_bundle(project_root=tmp_path, output=output, pack_ids=["companion-kurisu"],
                     index_path=ASSET_INDEX_PATH)
    assert not output.exists()


def test_budget_limit_and_required_states(tmp_path: Path):
    with pytest.raises(CompanionPackError, match="budget"):
        validate_clip({"url": "x.webp", "size": 512, "columns": 16, "sequence": [16],
                       "durationMs": 1020, "decodedBytes": 32 * 512 * 512 * 4})
    pack = _write_pack(tmp_path)
    path = pack / "manifest.json"
    manifest = json.loads(path.read_text())
    del manifest["emotions"]["normal"]["idle"]
    path.write_text(json.dumps(manifest))
    with pytest.raises(CompanionPackError, match="Missing portrait state"):
        load_companion_pack(pack)


def test_inner_manifest_rejected_even_with_valid_archive_hashes(tmp_path: Path):
    source = tmp_path / "source"
    _write_pack(source)
    good, bad = tmp_path / "good.zip", tmp_path / "bad.zip"
    build_bundle(project_root=source, output=good, pack_ids=["companion-kurisu"],
                 index_path=ASSET_INDEX_PATH)
    with zipfile.ZipFile(good) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
        infos = {info.filename: info for info in archive.infolist()}
    key = "assets/companion/kurisu/manifest.json"
    inner = json.loads(contents[key])
    inner["emotions"]["normal"]["speaking"]["durationMs"] = -1
    contents[key] = json.dumps(inner).encode()
    outer = json.loads(contents["ASSET_BUNDLE_MANIFEST.json"])
    for record in outer["files"]:
        payload = contents[record["path"]]
        record.update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    outer["total_bytes"] = sum(record["size"] for record in outer["files"])
    contents["ASSET_BUNDLE_MANIFEST.json"] = json.dumps(outer).encode()
    with zipfile.ZipFile(bad, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, payload in contents.items():
            archive.writestr(infos[name], payload)
    with pytest.raises(AssetPackageError, match="timeline"):
        verify_bundle(bad)
    target = tmp_path / "target"
    with pytest.raises(AssetPackageError, match="timeline"):
        install_bundle(bad, project_root=target, index_path=ASSET_INDEX_PATH)
    assert not (target / "assets").exists()


@pytest.mark.parametrize("include_companion", [False, True])
def test_existing_visual_bundles_and_explicit_combined_selection(tmp_path: Path, include_companion):
    source, target = tmp_path / "source", tmp_path / "target"
    _write_visual(source)
    ids = ["visual-runtime"]
    if include_companion:
        _write_pack(source)
        ids.append("companion-kurisu")
    bundle = tmp_path / "art.zip"
    build_bundle(project_root=source, output=bundle, pack_ids=ids, index_path=ASSET_INDEX_PATH)
    verify_bundle(bundle)
    assert install_bundle(bundle, project_root=target, index_path=ASSET_INDEX_PATH)["packs"] == ids
    assert _status(target)["state"] == ("installed" if include_companion else "not_installed")


@pytest.mark.parametrize("corrupt_original", [False, True])
def test_download_collection_keeps_originals_and_optional_separate_zip(tmp_path: Path, corrupt_original):
    from tools.build_art_collection import COLLECTION_SCHEMA, NOTICE, build_collection

    source = tmp_path / "source"
    _write_visual(source)
    _write_pack(source)
    visual, companion = tmp_path / "visual.zip", tmp_path / "companion.zip"
    build_bundle(project_root=source, output=visual, pack_ids=["visual-runtime"], index_path=ASSET_INDEX_PATH)
    build_bundle(project_root=source, output=companion, pack_ids=["companion-kurisu"], index_path=ASSET_INDEX_PATH)
    base = tmp_path / "base.zip"
    original = visual.read_bytes()
    manifest = {"schema": COLLECTION_SCHEMA, "packages": [
        {"file": "01-visual.zip", "bytes": len(original), "kind": "installable_visual_runtime",
         "sha256": "0" * 64 if corrupt_original else hashlib.sha256(original).hexdigest()}]}
    with zipfile.ZipFile(base, "w") as archive:
        archive.writestr("COLLECTION_MANIFEST.json", json.dumps(manifest))
        archive.writestr("01-visual.zip", original)
        archive.writestr(NOTICE, b"original sharing notice")
    readme = tmp_path / "README.md"
    readme.write_text("Companion is optional.")
    output = tmp_path / "new.zip"
    kwargs = dict(base=base, companion=companion, readme=readme, output=output, date="2026-09-19")
    if corrupt_original:
        with pytest.raises(AssetPackageError, match="checksum mismatch"):
            build_collection(**kwargs)
        assert not output.exists()
        return
    report = build_collection(**kwargs)
    assert report["nested_hash_verification"] == "passed"
    assert report["companion_clean_install"]["installed_files"] == 2
    with zipfile.ZipFile(output) as archive:
        assert archive.read("01-visual.zip") == original
        assert archive.read(NOTICE) == b"original sharing notice"
        assert archive.read("04-amadeus-companion-kurisu-2026.09.19.zip") == companion.read_bytes()
        assert json.loads(archive.read("COLLECTION_MANIFEST.json"))["packages"][-1]["optional"] is True
    with pytest.raises(AssetPackageError, match="already exists"):
        build_collection(**kwargs)
