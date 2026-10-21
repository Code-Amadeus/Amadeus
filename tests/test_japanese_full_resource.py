"""Full coverage, honest review status, and independent resource lifecycle."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from config.japanese_dictionary import (
    FULL_SOURCE_FILES, PACK_FILES, PACK_ID, PACK_TREE, JapaneseDictionaryError,
    load_dictionary_pack,
)
from tools.external_assets import build_bundle, install_bundle, verify_bundle
from tools.prepare_japanese_full_resource import csv_bytes
from tts import japanese_dictionary as runtime
from test_japanese_dictionary import INDEX, _fake_jtalk, _row


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _write_pack(root, version="1", *, corrected=True, alias="グライド"):
    pack = root / "assets" / PACK_TREE
    pack.mkdir(parents=True)
    originals = [_row("follower", "フォロワーズ"), _row("unverified", "アンベリファイド"),
                 _row("read", "リード")]
    rows = [list(r) for r in originals]
    changes = []
    if corrected:
        rows[0][10:13] = ["フォロワー"] * 3
        changes.append({"source_row": 1, "status": "reviewed_reading_correction",
                        "before_columns": originals[0], "after_columns": rows[0],
                        "reason": "Synthetic singular correction", "accent_evidence": "native_lexeme_features"})
    source, data = csv_bytes(originals), csv_bytes(rows)
    overlay = csv_bytes([_row("\u3000glide\u3000", alias)])
    metadata = {"format_version": 2, "version": version, "language": "ja", "sha256": _sha(data),
                "entry_count": len(rows),
                "source": {"repository": "fixture", "revision": "fixture", "path": "fixture.csv",
                           "sha256": _sha(source), "entry_count": len(rows)},
                "source_review": {"unchanged_status": "original_unverified", "changes": changes,
                                  "runtime_exclusions": [{"source_row": 3, "word": "read", "reason": "context_sensitive"}]},
                "overlay": {"entry_mode": "katakana_alias", "sha256": _sha(overlay), "entry_count": 1,
                            "native_words": [], "release_review": {"accepted_readings": {"glide": [alias]}}},
                "base_dictionary_sha256": {name: _sha(b"system dictionary") for name in ("sys.dic", "matrix.bin", "char.bin")}}
    for name in PACK_FILES + FULL_SOURCE_FILES:
        (pack / name).write_text("fixture notice", encoding="utf-8")
    (pack / "source.csv").write_bytes(source)
    (pack / "userdict.csv").write_bytes(data)
    (pack / "overlay.csv").write_bytes(overlay)
    _metadata(pack, metadata)
    return pack


def _metadata(pack, value=None):
    path = pack / "provenance.json"
    if value is None:
        return json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _native(root):
    jtalk = _fake_jtalk(root)
    for name in ("matrix.bin", "char.bin"):
        (root / "system" / name).write_bytes(b"system dictionary")
    jtalk.OpenJTalk = lambda _: SimpleNamespace(normalize_for_mecab=lambda value: value,
                                               run_mecab_detailed=lambda value: (None, []))
    return jtalk


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch):
    monkeypatch.setattr(runtime, "_official_latin_words", frozenset())
    monkeypatch.setattr(runtime, "_official_katakana_aliases", {})
    monkeypatch.setattr(runtime, "_official_compound_paths", {})
    monkeypatch.setattr(runtime, "_official_base_owner", None)
    monkeypatch.setattr(runtime, "_runtime_status", {"state": "not_loaded", "ready": False, "identity": None})
    from tts import japanese_reading
    monkeypatch.setattr(japanese_reading, "configure_japanese_reading", lambda *a, **k: {})
    monkeypatch.setattr(japanese_reading, "reading_runtime_status", lambda: {"ready": False})


def test_full_source_preserves_unknown_rows_without_claiming_review(tmp_path):
    root = _write_pack(tmp_path)
    pack = load_dictionary_pack(root)
    assert pack.entry_count == 3 and pack.reviewed_rows == 1
    assert pack.entry_count - pack.reviewed_rows == 2
    assert set(p.name for p in pack.files()) == set(PACK_FILES + FULL_SOURCE_FILES)
    rows, aliases = runtime.full_source_runtime_rows(pack, (root / "userdict.csv").read_bytes())
    assert [r[0].strip() for r in rows] == ["follower", "unverified"]
    assert aliases == {"glide": "グライド"}
    assert "read" in (root / "userdict.csv").read_text(encoding="utf-8")
    from config.asset_packages import external_asset_pack_status
    status = external_asset_pack_status(PACK_ID, asset_root=tmp_path / "assets", index_path=INDEX)
    assert status["installed"] and "2 original rows remain unverified" in status["message"]


@pytest.mark.parametrize("mutation", ["unreviewed", "drop", "duplicate_change", "stale_before", "cost", "claim_reviewed", "missing_source"])
def test_source_review_rejects_hidden_mutations_even_with_updated_hash(tmp_path, mutation):
    root = _write_pack(tmp_path)
    meta = _metadata(root)
    if mutation in {"unreviewed", "drop"}:
        lines = (root / "userdict.csv").read_text(encoding="utf-8").splitlines(True)
        if mutation == "unreviewed":
            lines[1] = lines[1].replace("アンベリファイド", "テスト")
        else:
            lines.pop()
        data = "".join(lines).encode()
        (root / "userdict.csv").write_bytes(data)
        meta.update(sha256=_sha(data), entry_count=len(lines))
    elif mutation == "duplicate_change":
        meta["source_review"]["changes"] *= 2
    elif mutation == "stale_before":
        meta["source_review"]["changes"][0]["before_columns"][12] = "テスト"
    elif mutation == "cost":
        record = meta["source_review"]["changes"][0]
        record["after_columns"][3] = "-9999"
        from config.japanese_dictionary import dictionary_rows
        rows = list(dictionary_rows((root / "userdict.csv").read_bytes()))
        rows[0][3] = "-9999"
        data = csv_bytes(rows)
        (root / "userdict.csv").write_bytes(data)
        meta["sha256"] = _sha(data)
    elif mutation == "claim_reviewed":
        meta["source_review"]["unchanged_status"] = "all_reviewed"
    else:
        (root / "source.csv").unlink()
    _metadata(root, meta)
    with pytest.raises(JapaneseDictionaryError):
        load_dictionary_pack(root)


def test_unverified_accent_requires_native_fallback_or_reviewed_kana_path(tmp_path):
    root = _write_pack(tmp_path)
    meta = _metadata(root)
    meta["source_review"]["changes"][0]["accent_evidence"] = "source_accent_retained_pending_review"
    _metadata(root, meta)
    with pytest.raises(JapaneseDictionaryError, match="accent"):
        load_dictionary_pack(root)
    meta["source_review"]["runtime_exclusions"].append({"source_row": 1, "word": "follower", "reason": "pending_accent"})
    _metadata(root, meta)
    pack = load_dictionary_pack(root)
    assert pack.entry_count == 3 and pack.excluded_rows == (1, 3)


def test_invalid_overlay_cannot_bypass_review_or_context_deferral(tmp_path):
    root = _write_pack(tmp_path)
    meta = _metadata(root)
    data = csv_bytes([_row("\u3000read\u3000", "リード")])
    (root / "overlay.csv").write_bytes(data)
    meta["overlay"].update(sha256=_sha(data), release_review={"accepted_readings": {"read": ["リード"]}})
    _metadata(root, meta)
    with pytest.raises(JapaneseDictionaryError, match="context-sensitive"):
        load_dictionary_pack(root)


def test_source_alternatives_require_a_reviewed_winner_or_complete_deferral(tmp_path):
    root = _write_pack(tmp_path)
    meta = _metadata(root)
    extra = csv_bytes([_row("follower", "フォローワー")])
    for name in ("source.csv", "userdict.csv"):
        (root / name).write_bytes((root / name).read_bytes() + extra)
    meta["source"].update(sha256=_sha((root / "source.csv").read_bytes()), entry_count=4)
    meta.update(sha256=_sha((root / "userdict.csv").read_bytes()), entry_count=4)
    _metadata(root, meta)
    with pytest.raises(JapaneseDictionaryError, match="alternate runtime readings"):
        load_dictionary_pack(root)
    meta["source_review"]["runtime_exclusions"].append({"source_row": 4, "word": "follower", "reason": "unreviewed_alternative"})
    _metadata(root, meta)
    assert load_dictionary_pack(root).entry_count == 4
    data = csv_bytes([_row("\u3000follower\u3000", "フォロワー")])
    (root / "overlay.csv").write_bytes(data)
    meta["overlay"].update(sha256=_sha(data), release_review={"accepted_readings": {"follower": ["フォロワー"]}})
    _metadata(root, meta)
    # The exact reviewed overlay can own a spelling while all original
    # alternatives remain stored; historical typos are not lexical authority.
    pack = load_dictionary_pack(root)
    rows, aliases = runtime.full_source_runtime_rows(pack, (root / "userdict.csv").read_bytes())
    assert aliases == {"follower": "フォロワー"}
    assert all(row[0].strip() != "follower" for row in rows)


def test_base_dictionary_drift_invalidates_resource_without_loading_legacy(tmp_path):
    _write_pack(tmp_path)
    jtalk = _native(tmp_path)
    assert runtime.configure_japanese_dictionary(jtalk, project_root=tmp_path, enabled=True)["ready"]
    (tmp_path / "system/sys.dic").write_bytes(b"changed base")
    legacy = tmp_path / "GPT_SoVITS/text/ja_userdic/userdict.csv"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(csv_bytes([_row("legacy", "テスト")]))
    status = runtime.configure_japanese_dictionary(jtalk, project_root=tmp_path, enabled=True)
    assert status["state"] == "invalid" and "ownership" in status["detail"]
    assert runtime.with_latin_readings("glide") == "glide" and jtalk.registered[-1] == []


def test_builder_quarantines_suspect_originals_without_losing_source_or_reviewed_winners(tmp_path, monkeypatch):
    import sys
    from tools import prepare_japanese_full_resource as builder
    jtalk = _native(tmp_path)
    (tmp_path / "system/COPYING").write_text("fixture native notice", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "pyopenjtalk", jtalk)
    monkeypatch.setattr(builder, "native_base_word_lookup", lambda: lambda *_: False)
    source = csv_bytes([_row("municipal", "マニシパリティーズ"), _row("during", "ダーリング"),
                        _row("eric", "エクスブローカーズ"), _row("eric", "エリック"),
                        _row("remains", "レメインズ"), _row("remains", "リメインズ"),
                        _row("plain", "プレイン"), _row("invalid", "テスt")])
    overlay = csv_bytes([_row("\u3000remains\u3000", "リメインズ")])
    cmu = b"MUNICIPAL M Y UW1 N IH0 S AH0 P AH0 L\nERIC EH1 R IH0 K\n"
    data = tmp_path / "data"
    data.mkdir()
    review = {"source": {"repository": "fixture", "revision": "fixture", "path": "source.csv",
                         "sha256": _sha(source), "entry_count": 8},
              "changes": [], "hold_words": {}, "rejected_readings": {"during": ["ダーリング"]},
              "screening": {"cmudict_sha256": _sha(cmu)},
              "overlay": {"entry_mode": "katakana_alias", "sha256": _sha(overlay), "entry_count": 1,
                          "native_words": [], "release_review": {"accepted_readings": {"remains": ["リメインズ"]}},
                          "research": {"alkana_reading": "supporting evidence only", "source": "fixture"}}}
    for name in PACK_FILES:
        (data / name).write_text("fixture notice", encoding="utf-8")
    for name, content in [("source.csv", source), ("overlay.csv", overlay), ("cmudict.rep", cmu)]:
        (data / name).write_bytes(content)
    (data / "review.json").write_text(json.dumps(review), encoding="utf-8")
    builder.prepare(data, tmp_path / "output", "fixture")
    root = tmp_path / "output/assets" / PACK_TREE
    pack = load_dictionary_pack(root)
    assert pack.entry_count == 8 and (root / "source.csv").read_bytes() == source
    assert (root / "userdict.csv").read_bytes() == source
    active, aliases = runtime.full_source_runtime_rows(pack, source)
    assert [row[0].strip() for row in active] == ["plain"]
    assert aliases == {"remains": "リメインズ"}
    meta = _metadata(root)
    assert {r["reason"] for r in meta["source_review"]["runtime_exclusions"]} == {
        "cmu_final_phone_risk", "known_bad_reading", "unreviewed_alternative", "native_reading_requires_review"}
    assert meta["overlay"]["research"] == {"source": "fixture"}
    assert review["overlay"]["research"]["alkana_reading"]


def test_reference_alignment_does_not_hide_changed_inputs():
    torch = pytest.importorskip("torch")
    from tools.probes.japanese_pronunciation_evidence import align_reference_conditioning
    frozen = {"phones1": [1, 2], "refer_spec": torch.ones(2), "prompt_fea_ref": torch.tensor([0.5])}
    fresh = {**frozen, "prompt_fea_ref": torch.tensor([0.5001])}
    assert align_reference_conditioning(fresh, frozen)
    assert fresh["prompt_fea_ref"].data_ptr() != frozen["prompt_fea_ref"].data_ptr()
    fresh["phones1"] = [1, 3]
    assert not align_reference_conditioning(fresh, frozen)
    fresh["phones1"] = [1, 2]
    fresh["refer_spec"] = torch.zeros(2)
    assert not align_reference_conditioning(fresh, frozen)


def test_native_case_ownership_precedes_optional_case_folding(monkeypatch):
    native = pytest.importorskip("pyopenjtalk")
    owner = runtime.native_spelling_owner(native.OpenJTalk(native.OPEN_JTALK_DICT_DIR))
    assert owner("API") and not owner("api")
    assert owner("Disney") and not owner("disney")
    monkeypatch.setattr(runtime, "_official_base_owner", owner)
    monkeypatch.setattr(runtime, "_official_latin_words", frozenset({"api", "disney", "model"}))
    monkeypatch.setattr(runtime, "_official_katakana_aliases", {"api": "アピ", "disney": "ディズニー"})
    assert runtime.prepare_japanese_text("API api Disney disney") == "API アピ ディズニー ディズニー"
    assert runtime.prepare_japanese_text("MODEL") == "\u3000model\u3000"


def test_full_runtime_is_versioned_and_preserves_unknown_and_personal_inputs(tmp_path):
    root = _write_pack(tmp_path)
    jtalk = _native(tmp_path)
    status = runtime.configure_japanese_dictionary(jtalk, project_root=tmp_path, enabled=True)
    assert status["ready"] and "2 original rows remain unverified" in status["detail"]
    assert runtime.with_latin_readings("Glide xglide glide2") == "グライド xglide glide2"
    assert runtime.with_latin_word_boundaries("unknown read") == "unknown read"
    assert runtime.with_latin_word_boundaries("Follower") == "\u3000follower\u3000"
    first = status["identity"]
    meta = _metadata(root)
    meta["source_review"]["runtime_exclusions"].append({"source_row": 1, "word": "follower", "reason": "context_sensitive"})
    _metadata(root, meta)
    second = runtime.configure_japanese_dictionary(jtalk, project_root=tmp_path, enabled=True)
    assert second["ready"] and second["identity"] != first
    assert runtime.with_latin_word_boundaries("Follower") == "Follower"
    (tmp_path / "assets/text/ja/user.csv").write_bytes(csv_bytes([_row("glide", "テスト")]))
    assert runtime.configure_japanese_dictionary(jtalk, project_root=tmp_path, enabled=True)["ready"]
    assert runtime.with_latin_readings("glide") == "glide"


def test_compiled_full_resource_reuses_normalized_native_view_but_still_validates_sources(tmp_path):
    root = _write_pack(tmp_path)
    jtalk = _native(tmp_path)
    normalized = []
    def normalize(value):
        normalized.append(value)
        return value
    jtalk.OpenJTalk = lambda _: SimpleNamespace(normalize_for_mecab=normalize,
                                               run_mecab_detailed=lambda value: (None, []))
    first = runtime.configure_japanese_dictionary(jtalk, project_root=tmp_path, enabled=True)
    assert first["ready"] and normalized
    normalized.clear()
    second = runtime.configure_japanese_dictionary(jtalk, project_root=tmp_path, enabled=True)
    assert second["identity"] == first["identity"] and not normalized
    assert runtime.with_latin_word_boundaries("follower") == "\u3000follower\u3000"
    (root / "source.csv").write_bytes((root / "source.csv").read_bytes() + b"changed")
    failed = runtime.configure_japanese_dictionary(jtalk, project_root=tmp_path, enabled=True)
    assert failed["state"] == "invalid" and not failed["ready"]


def test_resource_lifecycle_update_rollback_and_failed_update(tmp_path, monkeypatch):
    from tools import external_assets
    first = _write_pack(tmp_path / "publisher1", "1", corrected=False)
    second = _write_pack(tmp_path / "publisher2", "2", alias="グライダー")
    archives = []
    for root, name in [(first, "v1"), (second, "v2")]:
        archive = tmp_path / f"{name}.zip"
        build_bundle(project_root=root.parents[3], output=archive, pack_ids=[PACK_ID], index_path=INDEX)
        verify_bundle(archive, index_path=INDEX)
        archives.append(archive)
    recipient = tmp_path / "日語 recipient"
    personal = recipient / "assets/text/ja/user.csv"
    personal.parent.mkdir(parents=True)
    personal.write_bytes(csv_bytes([_row("personal", "テスト")]))
    legacy = recipient / "GPT_SoVITS/text/ja_userdic/userdict.csv"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(csv_bytes([_row("legacy", "テスト")]))
    protected = personal.read_bytes(), legacy.read_bytes()
    jtalk = _native(recipient)
    install_bundle(archives[0], project_root=recipient, index_path=INDEX)
    assert install_bundle(archives[0], project_root=recipient, index_path=INDEX)["installed_files"] == 0
    state1 = runtime.configure_japanese_dictionary(jtalk, project_root=recipient, enabled=True)
    assert state1["ready"] and runtime.with_latin_readings("glide") == "グライド"
    install_bundle(archives[1], project_root=recipient, index_path=INDEX, overwrite=True)
    state2 = runtime.configure_japanese_dictionary(jtalk, project_root=recipient, enabled=True)
    assert state2["ready"] and state2["version"] == "2" and state1["identity"] != state2["identity"]
    assert runtime.with_latin_readings("glide") == "グライダー"
    install_bundle(archives[0], project_root=recipient, index_path=INDEX, overwrite=True)
    restored = runtime.configure_japanese_dictionary(jtalk, project_root=recipient, enabled=True)
    assert restored["identity"] == state1["identity"] and runtime.with_latin_readings("glide") == "グライド"
    installed = recipient / "assets" / PACK_TREE
    before = {p.name: p.read_bytes() for p in installed.iterdir()}
    real_replace = external_assets.os.replace
    injected = []
    def fail_mid_commit(source, target):
        if Path(target) == installed / "provenance.json" and not injected:
            injected.append(True)
            raise OSError("injected update failure after overlay replacement")
        return real_replace(source, target)
    monkeypatch.setattr(external_assets.os, "replace", fail_mid_commit)
    with pytest.raises(OSError, match="injected update failure"):
        install_bundle(archives[1], project_root=recipient, index_path=INDEX, overwrite=True)
    assert injected and {p.name: p.read_bytes() for p in installed.iterdir()} == before
    assert load_dictionary_pack(installed).version == "1"
    assert (personal.read_bytes(), legacy.read_bytes()) == protected
