"""Application delivery must include its default without owning personal selections."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import zipfile

import pytest

from config.japanese_dictionary import BUNDLED_ENV, PACK_TREE, PACK_ID, load_dictionary_pack, dictionary_pack_root
from config.asset_packages import external_asset_pack_status
from tools.external_assets import build_bundle, install_bundle
from tools.stage_japanese_dictionary import load_pin, stage_bundle, validate_directory
from tools.build_source_release import create_archive, include_bundled_dictionary
from test_japanese_full_resource import _write_pack
from test_japanese_aliases import make_pack, fixture_row, native as native

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "assets/index.json"


def archive_and_pin(root, output, *, version):
    metadata = root / "assets" / PACK_TREE / "provenance.json"
    raw = json.loads(metadata.read_bytes())
    raw["version"] = version
    metadata.write_text(json.dumps(raw), encoding="utf-8")
    archive = output / f"{version}.zip"
    manifest = build_bundle(project_root=root, output=archive, pack_ids=[PACK_ID], index_path=INDEX)
    pin = {"format": "amadeus.bundled-japanese-dictionary.v1", "pack_id": PACK_ID,
           "version": version, "archive": archive.name,
           "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
           "resource_identity": load_dictionary_pack(metadata.parent).identity,
           "files": manifest["files"]}
    path = output / f"{version}.json"
    path.write_text(json.dumps(pin), encoding="utf-8")
    return archive, path


def test_stage_and_application_archive_are_pinned_complete_and_deterministic(tmp_path):
    publisher = tmp_path / "publisher"
    _write_pack(publisher)
    archive, pin_path = archive_and_pin(publisher, tmp_path, version="dictionary-1")
    staged = stage_bundle(archive, tmp_path / "staged", pin_path=pin_path, index_path=INDEX)
    assert stage_bundle(archive, tmp_path / "staged", pin_path=pin_path, index_path=INDEX) == staged
    report = {"release_ready": True, "version": "app-2", "source_date_epoch": 1700000000,
              "selected_files": [], "selected_file_count": 0}
    report = include_bundled_dictionary(report, staged, pin_path=pin_path)
    assert report["bundled_dictionary"]["version"] == "dictionary-1"
    assert report["distribution_kind"] == "application_with_dictionary"
    assert report["selected_file_count"] == 9
    first, second = tmp_path / "app1.zip", tmp_path / "app2.zip"
    for target in (first, second):
        create_archive(root=publisher, output=target, report=report, policy={}, modes={}, bundled_directory=staged)
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as z:
        assert len(z.namelist()) == 10
        assert not any(name.endswith("/user.csv") or name.endswith(".dict") for name in z.namelist())
        z.extractall(tmp_path / "installed")
    status = external_asset_pack_status(PACK_ID, asset_root=tmp_path / "installed/amadeus-app-2/assets", index_path=INDEX)
    assert status["installed"] and status["source"] == "bundled" and status["version"] == "dictionary-1"
    (staged / "source.csv").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="differs"):
        validate_directory(staged, load_pin(pin_path))
    with pytest.raises(ValueError, match="changed"):
        create_archive(root=publisher, output=tmp_path / "broken.zip", report=report, policy={}, modes={}, bundled_directory=staged)
    assert not (tmp_path / "broken.zip").exists()


def test_release_gate_rejects_missing_wrong_or_personal_resource(tmp_path):
    publisher = tmp_path / "publisher"
    _write_pack(publisher)
    archive, pin_path = archive_and_pin(publisher, tmp_path, version="dictionary-1")
    with pytest.raises(ValueError, match="missing"):
        validate_directory(tmp_path / "absent", load_pin(pin_path))
    damaged = tmp_path / "damaged.zip"
    damaged.write_bytes(archive.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="SHA-256"):
        stage_bundle(damaged, tmp_path / "output", pin_path=pin_path, index_path=INDEX)
    pin = json.loads(pin_path.read_bytes())
    pin["files"][0]["path"] = "assets/text/ja/user.csv"
    pin_path.write_text(json.dumps(pin), encoding="utf-8")
    with pytest.raises(ValueError, match="nine pinned"):
        load_pin(pin_path)
    assert not (tmp_path / "output").exists()


def test_application_cli_cannot_silently_omit_the_dictionary(tmp_path, monkeypatch):
    from tools import build_source_release as builder
    monkeypatch.setattr(builder, "build_report", lambda **kwargs: {"release_ready": True})
    output = tmp_path / "amadeus.zip"
    monkeypatch.setattr(sys, "argv", ["build", "--output", str(output)])
    with pytest.raises(SystemExit, match="require --dictionary-bundle"):
        builder.main()
    monkeypatch.setattr(sys, "argv", ["build", "--output", str(output), "--code-only"])
    with pytest.raises(SystemExit, match="filename must contain code-only"):
        builder.main()
    assert not output.exists()


def test_bundled_default_upgrade_independent_rollback_and_personal_native_loading(tmp_path, native, monkeypatch):
    from config import settings
    from tts import japanese_dictionary as runtime
    monkeypatch.setattr(settings, "ENABLE_JAPANESE_USER_DICTIONARY", True)
    monkeypatch.delenv(BUNDLED_ENV, raising=False)
    sys.path.insert(0, str(ROOT / "GPT_SoVITS"))
    from text import japanese
    recipient = tmp_path / "installation"
    for version in ("dictionary-1", "dictionary-2"):
        publisher = tmp_path / version
        make_pack(publisher, [("menu", "メニュー"), ("game", "ゲーム")])
        archive_and_pin(publisher, tmp_path, version=version)
    v1, v2 = tmp_path / "dictionary-1.zip", tmp_path / "dictionary-2.zip"
    p1, p2 = tmp_path / "dictionary-1.json", tmp_path / "dictionary-2.json"
    bundled = stage_bundle(v1, recipient, pin_path=p1, index_path=INDEX)
    assert not (recipient / "assets" / PACK_TREE).exists(), "fresh install needs no independent installation step"
    first = runtime.configure_japanese_dictionary(native, project_root=recipient)
    assert first["ready"] and first["source"] == "bundled" and first["version"] == "dictionary-1"
    assert japanese.g2p("menuです。", False) == japanese.g2p("メニューです。", False)
    user = recipient / "assets/text/ja/user.csv"
    user.parent.mkdir(parents=True, exist_ok=True)
    row = fixture_row(native.OpenJTalk(native.OPEN_JTALK_DICT_DIR), "Menu", "メヌー")
    row[0], row[3] = "Menu", "-10000"
    personal = (",".join(row) + "\n").encode()
    user.write_bytes(personal)
    stage_bundle(v2, recipient, pin_path=p2, index_path=INDEX, overwrite=True)
    second = runtime.configure_japanese_dictionary(native, project_root=recipient)
    assert second["source"] == "bundled" and second["version"] == "dictionary-2"
    assert first["identity"] != second["identity"]
    assert japanese.g2p("menuです。", False) == japanese.g2p("メヌーです。", False)
    default_bytes = {p.name: p.read_bytes() for p in bundled.iterdir()}
    for archive, version in ((v2, "dictionary-2"), (v1, "dictionary-1")):
        install_bundle(archive, project_root=recipient, index_path=INDEX, overwrite=True)
        for _ in range(2):
            selected = runtime.configure_japanese_dictionary(native, project_root=recipient)
            assert selected["ready"] and selected["source"] == "installed" and selected["version"] == version
            assert japanese.g2p("menuです。", False) == japanese.g2p("メヌーです。", False)
            assert user.read_bytes() == personal
            assert {p.name: p.read_bytes() for p in bundled.iterdir()} == default_bytes
    independent = recipient / "assets" / PACK_TREE
    (independent / "provenance.json").write_text("invalid", encoding="utf-8")
    selected = runtime.configure_japanese_dictionary(native, project_root=recipient)
    assert selected["version"] != "dictionary-2", "an invalid explicit selection must not silently activate the default"
    assert external_asset_pack_status(PACK_ID, asset_root=recipient / "assets", index_path=INDEX)["state"] == "invalid"


def test_packaged_resource_path_is_used_without_writing_to_it(tmp_path, native, monkeypatch):
    from tts import japanese_dictionary as runtime
    publisher = tmp_path / "publisher"
    make_pack(publisher, [("menu", "メニュー")])
    archive, pin = archive_and_pin(publisher, tmp_path, version="bundled")
    supplied = stage_bundle(archive, tmp_path / "program-files", pin_path=pin, index_path=INDEX)
    before = {p.name: p.read_bytes() for p in supplied.iterdir()}
    project = tmp_path / "backend"
    monkeypatch.setenv(BUNDLED_ENV, str(supplied))
    assert dictionary_pack_root(project / "assets") == supplied
    status = runtime.configure_japanese_dictionary(native, project_root=project, enabled=True)
    assert status["ready"] and status["source"] == "bundled"
    assert external_asset_pack_status(PACK_ID, asset_root=project / "assets", index_path=INDEX)["installed"]
    assert {p.name: p.read_bytes() for p in supplied.iterdir()} == before
    assert not (supplied / "user.dict").exists()
