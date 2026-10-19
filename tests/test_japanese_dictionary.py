from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from config.asset_packages import AssetPackageError, external_asset_pack_status
from config import japanese_dictionary as dictionary_pack
from tools.audit_japanese_dictionary import audit_rows, native_mora_counter, native_surface_normalizer, normalize_rows
from tools.external_assets import build_bundle, install_bundle, verify_bundle
from tools.prepare_japanese_dictionary import prepare_dictionary
from tools.tts_text_processor import correct_pronunciation_for_tts
from tts import japanese_dictionary as dictionary

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "assets/index.json"
FOLLOWER = "ｆｏｌｌｏｗｅｒ,1348,1348,556,名詞,固有名詞,一般,*,*,*,フォロワーズ,フォロワーズ,フォロワーズ,0/5,*\n"
LEARNER = "ｌｅａｒｎｅｒ,1348,1348,504,名詞,固有名詞,一般,*,*,*,ラーナーズ,ラーナーズ,ラーナーズ,1/5,*\n"


@pytest.fixture(autouse=True)
def clear_runtime_sources(monkeypatch):
    monkeypatch.setattr(dictionary, "_runtime_status", {
        "state": "not_loaded", "ready": False, "version": "", "identity": None, "detail": "Not loaded in this test.",
    })
    yield


def _prepare(root: Path, *, native: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    source = root / "upstream.csv"
    data = (FOLLOWER + LEARNER).encode()
    if native:
        # Preserve the real competing short words: a corrected full-word row
        # must actually win native segmentation, not merely exist in the CSV.
        for surface, reading, cost in [("ｃｏｍｐ", "コンプ", "344"),
                                        ("ｏｎ", "オン", "227"), ("ｅｎｔ", "エント", "287")]:
            row = _row(surface, reading, "1/3" if surface != "ｏｎ" else "1/2")
            row[3] = cost
            data += (",".join(row) + "\n").encode()
    source.write_bytes(data)
    recipe = json.loads((ROOT / "tools/data/japanese_dictionary_source.json").read_text(encoding="utf-8"))
    recipe["source"].update(sha256=hashlib.sha256(data).hexdigest(), entry_count=len(list(dictionary_pack.dictionary_rows(data))))
    recipe_path = root / "recipe.json"
    recipe_path.write_text(json.dumps(recipe), encoding="utf-8")
    return prepare_dictionary(source, project_root=root, recipe_path=recipe_path,
                              count_mora=None if native else lambda _: None,
                              base_word=None if native else lambda _: False)


@pytest.mark.parametrize("text,expected", [
    ("LeaderとMLPを確認する。", "Leaderとエムエルピーを確認する。"),
    ("MLP2とGPT4o", "エムエルピー2とジーピーティー4オー"),
    ("OpenAIのAPI", "オープンエーアイのエーピーアイ"),
    ("CAPITAL", "CAPITAL"),
    ("BilibiliとGitHub", "ビリビリとギットハブ"),
    ("APIaryとHTTPServer", "APIaryとHTTPServer"),
    ("ｍｌｐとＭＬＰとＡＰＩ", "mlpとエムエルピーとエーピーアイ"),
    ("openaiとOPENAIとＢｉｌｉｂｉｌｉ", "オープンエーアイとオープンエーアイとビリビリ"),
    ("ＡＰＩaryとＣＡＰＩＴＡＬ", "APIaryとCAPITAL"),
    ("API's and APIs", "エーピーアイ's and エーピーアイs"),
    ("今日は生ビールを飲む。", "今日は生ビールを飲む。"),
])
def test_latin_words_and_initialisms_are_complete_tokens(text, expected):
    assert correct_pronunciation_for_tts(text) == expected


def test_pipeline_applies_japanese_readings_only_to_japanese_speech(monkeypatch):
    from tts import pipeline
    original = "OpenAI API and MLP."
    monkeypatch.setattr(pipeline, "TTS_OUTPUT_LANGUAGE", "英文")
    assert pipeline._prepare_synthesis_text(original) == original
    monkeypatch.setattr(pipeline, "TTS_OUTPUT_LANGUAGE", "日文")
    assert pipeline._prepare_synthesis_text("MLPを確認する。") == "エムエルピーを確認する。"


def test_preparation_records_derivative_and_does_not_edit_source(tmp_path):
    pack_root = _prepare(tmp_path)
    assert (tmp_path / "upstream.csv").read_text(encoding="utf-8") == FOLLOWER + LEARNER
    pack = dictionary_pack.load_dictionary_pack(pack_root)
    corrections = list(dictionary_pack.dictionary_rows((ROOT / "tools/data/japanese_dictionary_corrections.csv").read_bytes()))
    # Conservative final-phone screening also quarantines legitimate loanword
    # adaptations; condo -> コンド is a documented false positive (CMU OW).
    accepted = [row for row in corrections if row[0] != "ｃｏｎｄｏ"]
    assert pack.entry_count == len(accepted)
    readings = {row[0]: row[11] for row in dictionary_pack.dictionary_rows((pack_root / "userdict.csv").read_bytes())}
    assert readings == {row[0]: row[11] for row in accepted}
    assert readings["ｆｏｌｌｏｗｅｒ"] == "フォロワー"
    assert readings["ｌｅａｒｎｅｒ"] == "ラーナー"
    provenance = json.loads((pack_root / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["normalization"]["rules"]
    assert provenance["normalization"]["results"]["output_entries"] == provenance["extension_filter"]["results"]["input_entries"]
    assert provenance["extension_filter"]["results"]["output_entries"] == pack.entry_count
    assert all(path.is_file() for path in pack.files())
    # A maintainer cannot accidentally publish a customized installation as the
    # pinned upstream base, even if it has the expected CSV shape.
    with pytest.raises(dictionary_pack.JapaneseDictionaryError, match="differs from the pinned"):
        prepare_dictionary(tmp_path / "upstream.csv", project_root=tmp_path / "rejected")
    assert not (tmp_path / "rejected/assets").exists()


def test_optional_bundle_round_trip_preserves_personal_and_legacy_sources(tmp_path):
    publisher = tmp_path / "publisher"
    pack_root = _prepare(publisher)
    (pack_root / "user.dict").write_bytes(b"derived cache must not ship")
    recipient = tmp_path / "recipient"
    personal = recipient / "assets/text/ja/user.csv"
    personal.parent.mkdir(parents=True)
    personal.write_text(FOLLOWER, encoding="utf-8")
    legacy = recipient / "GPT_SoVITS/text/ja_userdic/userdict.csv"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(LEARNER, encoding="utf-8")
    assert external_asset_pack_status(dictionary_pack.PACK_ID, asset_root=recipient / "assets", index_path=INDEX)["state"] == "not_installed"
    archive = tmp_path / "dictionary.zip"
    manifest = build_bundle(project_root=publisher, output=archive, pack_ids=[dictionary_pack.PACK_ID], index_path=INDEX)
    assert manifest["file_count"] == len(dictionary_pack.PACK_FILES)
    with zipfile.ZipFile(archive) as bundle:
        assert not any(name.endswith("user.dict") or name.endswith("user.csv") for name in bundle.namelist())
    assert verify_bundle(archive, index_path=INDEX) == manifest
    result = install_bundle(archive, project_root=recipient, index_path=INDEX)
    assert result["installed_files"] == len(dictionary_pack.PACK_FILES)
    assert install_bundle(archive, project_root=recipient, index_path=INDEX)["installed_files"] == 0
    status = external_asset_pack_status(dictionary_pack.PACK_ID, asset_root=recipient / "assets", index_path=INDEX)
    assert status["installed"] and status["version"] == dictionary_pack.load_dictionary_pack(pack_root).version
    assert personal.read_text(encoding="utf-8") == FOLLOWER
    assert legacy.read_text(encoding="utf-8") == LEARNER
    # Existing different data is refused before any recipient file changes.
    installed_csv = recipient / "assets" / dictionary_pack.PACK_TREE / "userdict.csv"
    installed_csv.write_bytes(b"private edit")
    with pytest.raises(AssetPackageError, match="different|exists|conflict"):
        install_bundle(archive, project_root=recipient, index_path=INDEX)
    assert installed_csv.read_bytes() == b"private edit"
    assert external_asset_pack_status(dictionary_pack.PACK_ID, asset_root=recipient / "assets", index_path=INDEX)["state"] == "invalid"


@pytest.mark.parametrize("bad", [b"", b"not,an,entry\n", b"\xff"])
def test_dictionary_contract_rejects_invalid_csv(bad):
    with pytest.raises(dictionary_pack.JapaneseDictionaryError):
        list(dictionary_pack.dictionary_rows(bad))


def _row(surface, pronunciation, accent="0/4"):
    row = next(dictionary_pack.dictionary_rows(FOLLOWER.encode()))
    row[0], row[13] = surface, accent
    row[10:13] = [pronunciation] * 3
    return row


def test_normalization_repairs_representation_and_preserves_distinct_readings():
    decomposed = _row("abzug", "アブツク\u3099", "2/5")
    rows = [decomposed, list(decomposed), _row("read", "リード", "1/3"),
            _row("read", "レッド", "1/3"), _row("path", "パス", "1/2"),
            _row("paths", "パス", "1/2"), _row("guangjo", "グァンジョ", "4/4"),
            _row("bad", "アサルトed", "0/5")]
    morae = {"アブツグ": 4, "リード": 3, "レッド": 3, "パス": 2, "グァンジョ": 3}
    cleaned, report = normalize_rows(rows, morae.__getitem__)
    assert cleaned[0][10:14] == ["アブツグ"] * 3 + ["2/4"]
    assert {row[12] for row in cleaned if row[0] == "read"} == {"リード", "レッド"}
    assert [row[0] for row in cleaned if row[12] == "パス"] == ["path", "paths"]
    assert next(row for row in cleaned if row[0] == "guangjo")[13] == "4/4"
    assert next(row for row in cleaned if row[0] == "bad")[12] == "アサルトed"
    assert report["exact_duplicates_removed"] == 1
    assert report["unicode_normalized_rows"] == 2
    assert {entry["reason"] for entry in report["unresolved"]} == {
        "accent_needs_review", "non_kana_reading_needs_review",
    }
    # Normalizing an already repaired source is stable.
    assert normalize_rows(cleaned, morae.__getitem__)[0] == cleaned


def test_normalization_repairs_only_literal_whole_token_initialism_readings():
    rows = [_row("ｃｅｏ", "CEO", "0/3"), _row("else", "CEO", "0/3"),
            _row("apiary", "アピアリー", "0/5"), _row("UNRESOLVED", "UNRESOLVED", "0/10")]
    cleaned, report = normalize_rows(rows, lambda _: 6)
    assert cleaned[0][10:14] == ["シーイーオー"] * 3 + ["0/6"]
    assert cleaned[1][12] == "CEO" and cleaned[2][12] == "アピアリー"
    assert cleaned[3][12] == "UNRESOLVED"
    assert report["literal_initialism_readings_repaired"] == 1


def test_reading_separators_are_removed_without_discarding_lexical_residue_or_guessing_accents():
    rows = [_row("coca-cola", "コカ・コーラ", "4/6"),
            _row("speaking", "スピー\u200b\u200bキング", "0/8"),
            _row("jean-luc", "ジャン=リュック", "4/6"),
            _row("unknown", "アイ・ウ", "4/4"),
            _row("named", "スイス（スィス）", "5/7"),
            _row("untranslated", "ショックした", "0/5"),
            _row("possessive", "モリス'sズ", "4/6")]
    counts = {"コカコーラ": 5, "スピーキング": 6, "ジャンリュック": 5, "アイウ": 3}
    cleaned, report = normalize_rows(rows, counts.__getitem__)
    assert [(row[12], row[13]) for row in cleaned[:3]] == [
        ("コカコーラ", "4/5"), ("スピーキング", "0/6"), ("ジャンリュック", "4/5"),
    ]
    assert cleaned[3:] == rows[3:], "keep uncertain accents, translations and spelling residues unchanged"
    assert report["reading_separators_normalized"] == 3
    assert normalize_rows(cleaned, counts.__getitem__)[0] == cleaned


def test_reviewed_bad_reading_repair_preserves_other_readings_of_the_same_surface(tmp_path):
    recipe = json.loads((ROOT / "tools/data/japanese_dictionary_source.json").read_text(encoding="utf-8"))
    repair = next(entry for entry in recipe["corrections"] if entry["word"] == "corps")
    bad = _row("ｃｏｒｐｓ", repair["before"], "18/75")
    legitimate_plural = _row("ｃｏｒｐｓ", "コアズ", "1/3")
    source = tmp_path / "upstream.csv"
    # csv.writer handles the commas and quotes inside the original LLM annotation.
    import csv
    import io
    buffer = io.StringIO(newline="")
    csv.writer(buffer, lineterminator="\n").writerows([bad, legitimate_plural])
    data = buffer.getvalue().encode()
    source.write_bytes(data)
    recipe["source"].update(sha256=hashlib.sha256(data).hexdigest(), entry_count=2)
    recipe_path = tmp_path / "recipe.json"
    recipe_path.write_text(json.dumps(recipe), encoding="utf-8")
    pack_root = prepare_dictionary(source, project_root=tmp_path, recipe_path=recipe_path,
                                   count_mora=lambda _: None, base_word=lambda _: False)
    readings = [row[12] for row in dictionary_pack.dictionary_rows((pack_root / "userdict.csv").read_bytes()) if row[0] == "ｃｏｒｐｓ"]
    assert set(readings) == {"コア", "コアズ"}
    assert source.read_bytes() == data


def test_inflection_audit_is_evidence_for_review_and_does_not_guess_lexical_repairs(tmp_path):
    cmu = tmp_path / "cmu.rep"
    cmu.write_text("PATH P AE1 TH\nPATHS P AE1 TH S\nBANK B AE1 NG K\nBANKS B AE1 NG K S\n", encoding="utf-8")
    rows = [_row("path", "パス", "1/2"), _row("paths", "パス", "1/2"),
            _row("bank", "バンクス", "1/4"), _row("banks", "バンクス", "1/4")]
    report = audit_rows(rows, cmu, lambda _: None)
    assert {entry["surface"] for entry in report["inflection_candidates"]} == {"path", "bank"}
    assert normalize_rows(rows, lambda _: None)[0] == rows


def test_native_mora_counter_uses_pronunciation_without_retokenizing_kana():
    pytest.importorskip("pyopenjtalk")
    count, _ = native_mora_counter()
    for pronunciation, expected in {"アボリッシュ": 5, "アファブル": 4, "アドゥレイト": 5,
                                    "アッバティエロ": 6, "アグリフュエル": 6, "エィ": 2,
                                    "フォロワー": 4, "グァンジョ": 3, "カンナ": 3}.items():
        assert count(pronunciation) == expected


def test_native_surface_spelling_is_applied_to_all_entries():
    pytest.importorskip("pyopenjtalk")
    count, _ = native_mora_counter()
    surface = native_surface_normalizer()
    rows = [_row("ｃｏｃａ-ｃｏｌａ", "コカ・コーラ", "4/6"),
            _row("ｕｎｋｎｏｗｎ'ｓ", "モリス'sズ", "4/6"),
            _row("ａｌｒｅａｄｙ-ｖａｌｉｄ", "テスト", "0/3")]
    cleaned, report = normalize_rows(rows, count, surface)
    assert cleaned[0][0] == "ｃｏｃａ−ｃｏｌａ" and cleaned[0][12] == "コカコーラ"
    assert cleaned[1][0] == surface(rows[1][0])
    assert cleaned[1][1:] == rows[1][1:]
    assert cleaned[2][0] == surface(rows[2][0])
    assert cleaned[2][1:] == rows[2][1:]
    assert report["surfaces_normalized"] == 3


def _fake_jtalk(root: Path):
    base = root / "system"
    base.mkdir(parents=True)
    (base / "sys.dic").write_bytes(b"system dictionary")
    compiled, registered = [], []
    def compile_csv(source, target):
        compiled.append(source)
        Path(target).write_bytes(b"compiled " + Path(source).read_bytes())
    return SimpleNamespace(
        OPEN_JTALK_DICT_DIR=str(base).encode(), __version__="test-1",
        mecab_dict_index=compile_csv, unset_user_dict=lambda: registered.append([]),
        update_global_jtalk_with_user_dict=lambda paths: registered.append(paths),
        compiled=compiled, registered=registered,
    )


def test_runtime_uses_pack_before_legacy_and_compiles_only_disposable_cache(tmp_path):
    root = tmp_path / "日語"
    pack_root = _prepare(root)
    legacy = root / "GPT_SoVITS/text/ja_userdic/userdict.csv"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(FOLLOWER, encoding="utf-8")
    personal = root / "assets/text/ja/user.csv"
    personal.write_text(LEARNER, encoding="utf-8")
    jtalk = _fake_jtalk(root)
    status = dictionary.configure_japanese_dictionary(jtalk, project_root=root, enabled=True)
    assert status["ready"]
    assert len(jtalk.compiled) == 2
    assert all(Path(path).is_relative_to(root / ".cache") for path in jtalk.compiled)
    assert [Path(path).read_bytes() for path in jtalk.registered[-1]] == [
        b"compiled " + (pack_root / "userdict.csv").read_bytes(), b"compiled " + personal.read_bytes(),
    ]
    assert all(Path(path).is_relative_to(root / ".cache") for path in jtalk.registered[-1])
    assert legacy.read_text(encoding="utf-8") == FOLLOWER
    assert not (pack_root / "user.dict").exists()
    dictionary.configure_japanese_dictionary(jtalk, project_root=root, enabled=True)
    assert len(jtalk.compiled) == 2, "unchanged sources and runtime must reuse compiled files"
    jtalk.__version__ = "test-2"
    dictionary.configure_japanese_dictionary(jtalk, project_root=root, enabled=True)
    assert len(jtalk.compiled) == 4, "a changed native runtime must rebuild its cache"
    for path in jtalk.registered[-1]:
        Path(path).unlink()
    assert dictionary.configure_japanese_dictionary(jtalk, project_root=root, enabled=True)["ready"]
    assert len(jtalk.compiled) == 6, "deleting disposable compiled caches must rebuild from the selected CSVs"
    assert legacy.read_text(encoding="utf-8") == FOLLOWER
    assert personal.read_text(encoding="utf-8") == LEARNER
    dictionary.configure_japanese_dictionary(jtalk, project_root=root, enabled=False)
    assert dictionary.dictionary_runtime_status()["state"] == "disabled" and jtalk.registered[-1] == []


def test_runtime_missing_invalid_and_compile_failure_keep_base_frontend(tmp_path):
    jtalk = _fake_jtalk(tmp_path)
    assert dictionary.configure_japanese_dictionary(jtalk, project_root=tmp_path, enabled=True)["state"] == "not_installed"
    pack_root = _prepare(tmp_path)
    def fail_compile(*args):
        raise RuntimeError("compiler failed")
    jtalk.mecab_dict_index = fail_compile
    assert dictionary.configure_japanese_dictionary(jtalk, project_root=tmp_path, enabled=True)["state"] == "invalid"
    assert jtalk.registered[-1] == []
    assert not list((tmp_path / ".cache/japanese_dictionary").glob("*.dict"))
    (pack_root / "userdict.csv").write_bytes(b"corrupt")
    assert dictionary.configure_japanese_dictionary(jtalk, project_root=tmp_path, enabled=True)["state"] == "invalid"


def test_audio_cache_distinguishes_dictionary_selection_and_english_is_independent(tmp_path, monkeypatch):
    from tts import first_sentence_audio_cache as audio_cache, pipeline
    status = {"identity": "actual-base"}
    monkeypatch.setattr(pipeline, "japanese_dictionary_status", lambda: status)
    monkeypatch.setattr(pipeline, "current_tts_language_code", lambda: "ja")
    cache = audio_cache.FirstSentenceAudioCache(tmp_path / "audio")
    without = cache.path_for("Follower。", {})
    status["identity"] = "actual-installed"
    installed = cache.path_for("Follower。", {})
    assert installed != without
    status["identity"] = "actual-disabled"
    assert cache.path_for("Follower。", {}) not in {without, installed}
    monkeypatch.setattr(pipeline, "current_tts_language_code", lambda: "en")
    assert "japanese_dictionary" not in cache.key_payload("Follower.", {})


@pytest.mark.parametrize("runtime_state,installed,enabled,expected", [
    ("invalid", True, True, "invalid"),
    ("not_loaded", False, True, "invalid"),
    ("invalid", True, False, "disabled"),
])
def test_settings_expose_dictionary_failures_and_the_disable_switch(monkeypatch, runtime_state, installed, enabled, expected):
    from config import asset_packages, settings
    from server.handlers.system_handler import _voice_configuration
    original = asset_packages.external_asset_pack_status
    monkeypatch.setattr(asset_packages, "external_asset_pack_status", lambda pack_id, **kwargs: {
        "installed": installed, "state": "installed" if installed else "invalid",
        "version": "test", "message": "" if installed else "CSV checksum failed",
    } if pack_id == dictionary_pack.PACK_ID else original(pack_id, **kwargs))
    monkeypatch.setattr(settings, "ENABLE_JAPANESE_USER_DICTIONARY", enabled)
    monkeypatch.setattr(settings, "TTS_OUTPUT_LANGUAGE", "日文")
    monkeypatch.setattr(dictionary, "_runtime_status", {
        "state": runtime_state, "ready": False, "version": "test", "detail": "Dictionary compilation failed",
    })
    from tts import pipeline
    monkeypatch.setattr(pipeline, "japanese_dictionary_status", dictionary.dictionary_runtime_status)
    group = next(item for item in _voice_configuration(settings) if item["id"] == "tts_japanese_dictionary")
    assert group["status"] == expected
    assert group["status_ok"] is (not enabled)
    if enabled:
        assert "failed" in group["status_detail"]


def test_native_runtime_reads_corrected_words_and_keeps_japanese_intact(tmp_path):
    jtalk = pytest.importorskip("pyopenjtalk")
    root = tmp_path / "日本語"
    _prepare(root, native=True)
    original_source = (root / "assets" / dictionary_pack.PACK_TREE / "userdict.csv").read_bytes()
    try:
        jtalk.unset_user_dict()
        japanese = jtalk.g2p("今日は生ビールを飲みます。")
        assert dictionary.configure_japanese_dictionary(jtalk, project_root=root, enabled=True)["ready"]
        for word, expected in {"Follower": "フォロワー", "Learner": "ラーナー", "Proposer": "プロポーザー",
                               "Bank": "バンク", "Component": "コンポーネント", "Components": "コンポーネンツ",
                               "Handler": "ハンドラー", "Dentist": "デンティスト", "Balloonist": "バルーニスト",
                               "Ninth": "ナインス", "Intruder": "イントルーダー", "Lawrence": "ローレンス"}.items():
            features = jtalk.run_frontend(word.lower())
            assert len(features) == 1 and features[0]["read"] == expected
        prepared = correct_pronunciation_for_tts("LeaderとMLPを確認する。")
        assert "エムエルピー" in "".join(feature["read"] for feature in jtalk.run_frontend(prepared.lower()))
        assert jtalk.g2p("今日は生ビールを飲みます。") == japanese
        assert (root / "assets" / dictionary_pack.PACK_TREE / "userdict.csv").read_bytes() == original_source
    finally:
        jtalk.unset_user_dict()


def test_japanese_frontend_preserves_latin_compounds_and_ordinary_punctuation(tmp_path, monkeypatch):
    jtalk = pytest.importorskip("pyopenjtalk")
    monkeypatch.syspath_prepend(str(ROOT / "GPT_SoVITS"))
    from text import japanese
    root = tmp_path / "日本語"
    _prepare(root, native=True)
    try:
        assert dictionary.configure_japanese_dictionary(jtalk, project_root=root, enabled=True)["ready"]
        count, _ = native_mora_counter()
        template = jtalk.run_frontend("テスト")[0]
        for word, kana in {"ballet's": "バレエズ", "coca-cola": "コカコーラ",
                           "t-shirt": "ティーシャツ", "economists'": "エコノミスツ"}.items():
            labels = jtalk.make_label([{**template, "read": kana, "pron": kana, "acc": 0, "mora_size": count(kana)}])
            expected = [re.search(r"-(.*?)\+", label).group(1) for label in labels[1:-1]]
            actual = japanese.preprocess_jap(word)
            assert [phone.lower() if phone in "AIUEO" else phone for phone in actual] == expected
        assert japanese._japanese_marks.findall("cat - dog") == [" ", "-", " "]
        assert japanese._japanese_marks.findall("猫-犬。") == ["-", "。"]
        assert japanese._japanese_marks.findall("'hello'")[0] == "'"
    finally:
        jtalk.unset_user_dict()
