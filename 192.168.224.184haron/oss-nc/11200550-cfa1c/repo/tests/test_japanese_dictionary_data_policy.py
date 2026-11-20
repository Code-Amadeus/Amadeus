"""The optional CSV extends unknown vocabulary without owning base words."""
from pathlib import Path
import hashlib
import json

import pytest

from tools.audit_japanese_dictionary import (
    cmu_pronunciations, filter_extension_rows, final_phone_risk,
    native_base_word_lookup, word_key,
)
from test_japanese_dictionary import _row
from config.japanese_dictionary import LATIN_WORD_BOUNDARY
from tts.japanese_dictionary import with_latin_word_boundaries


@pytest.fixture(autouse=True)
def isolate_native_status(monkeypatch):
    from tts import japanese_dictionary as runtime
    monkeypatch.setattr(runtime, "_runtime_status", {
        "state": "not_loaded", "ready": False, "identity": None, "version": "", "detail": "Not loaded",
    })
    monkeypatch.setattr(runtime, "_official_latin_words", frozenset())


@pytest.mark.parametrize("reading,phones,risky", [
    ("ヘルプト", {("HH", "EH1", "L", "P")}, False),
    ("モデルド", {("M", "AA1", "D", "AH0", "L")}, True),
    ("ウェルカムド", {("W", "EH1", "L", "K", "AH0", "M")}, True),
    ("ダイレクトネス", {("D", "ER0", "EH1", "K", "T", "L", "IY0")}, False),
    ("リセッティング", {("R", "IY0", "S", "EH1", "T")}, True),
    ("レットアップス", {("L", "EH1", "T", "S")}, False),
    ("アップス", {("AE1", "P", "S")}, False),
    ("デッドラインズ", {("D", "EH1", "D", "L", "AY2", "N")}, True),
    ("パス", {("P", "AE1", "TH")}, False),
    ("ウィズ", {("W", "IH1", "TH"), ("W", "IH1", "DH")}, True),
    ("コンド", {("K", "AA1", "N", "D", "OW0")}, True),
    ("テストズ", {("T", "EH1", "S", "T"), ("T", "EH1", "S", "T", "Z")}, False),
    ("テスト", set(), False),
])
def test_final_phone_screening_includes_variants_and_documents_false_positives(reading, phones, risky):
    assert final_phone_risk(reading, phones) is risky


def test_filter_yields_base_words_quarantines_variants_and_bounds_whole_words():
    rows = [_row("Ｈｅｌｐ", "ヘルプト"), _row("directly", "ディレクターズ"),
            _row("directly", "ディレクター"), _row("direct", "ダイレクト"), _row("follower", "フォロワー"),
            _row("don't", "ドント"), _row("pricy", "プライシー"), _row("hel", "ヘル")]
    rows[-2][3] = "12000"
    before = [list(row) for row in rows]
    phones = cmu_pronunciations(b"DIRECTLY D ER0 EH1 K T L IY0\nDIRECTLY(1) D IH0 R EH1 K T L IY0\n")
    kept, report = filter_extension_rows(rows, phones, lambda word, _: word == "help")
    assert {row[0] for row in kept} == {LATIN_WORD_BOUNDARY + w + LATIN_WORD_BOUNDARY
                                       for w in ("direct", "follower", "don't", "pricy", "hel")}
    costs = {word_key(row[0]): row[3] for row in kept}
    assert costs["hel"] == rows[-1][3]
    assert costs["direct"] == rows[3][3] and costs["follower"] == rows[4][3]
    assert costs["don't"] == rows[5][3] and costs["pricy"] == "12000"
    assert report["reason_counts"] == {"base_dictionary_word": 1, "cmu_final_phone_risk": 2}
    assert rows == before, "filtering must not rewrite its evidence input"
    assert filter_extension_rows(kept, phones, lambda *_: False)[0] == kept
    assert word_key("Ｌｅｔ’ｓ") == "let's"


def test_native_base_lookup_does_not_treat_letter_spelling_as_a_word():
    pytest.importorskip("pyopenjtalk")
    known = native_base_word_lookup()
    assert all(known(word) for word in ("Help", "learn", "Two", "with", "Production"))
    assert not any(known(word) for word in ("model", "welcome", "directly", "deadline"))
    assert known("rpm", "アールピーエム")
    assert not known("model", "モデル")


def test_official_admission_requires_each_exact_checked_reading():
    rows = [_row("true", "トゥルービュー"), _row("follower", "フォロワー"),
            _row("follower", "フォロワーズ"), _row("unreviewed", "アンレビュード")]
    before = [list(row) for row in rows]
    kept, report = filter_extension_rows(rows, {}, lambda *_: False, {"follower": {"フォロワー"}})
    assert [(word_key(row[0]), row[12]) for row in kept] == [("follower", "フォロワー")]
    assert report["reason_counts"] == {"unreviewed_word_or_reading": 3}
    assert rows == before


@pytest.mark.parametrize("review", [None, {"accepted_readings": {"follower": "フォロワー"}},
                                   {"accepted_readings": {"follower": ["別の読音"]}}])
def test_official_pack_rejects_retired_or_inconsistent_checked_ledger(tmp_path, review):
    from test_japanese_dictionary import _prepare
    from config.japanese_dictionary import JapaneseDictionaryError, load_dictionary_pack
    pack = _prepare(tmp_path)
    provenance = pack / "provenance.json"
    data = json.loads(provenance.read_text(encoding="utf-8"))
    data["release_review"] = review
    provenance.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(JapaneseDictionaryError, match="ledger|readings|unchecked"):
        load_dictionary_pack(pack)


def test_boundaries_require_a_complete_registered_token_and_preserve_numeric_units():
    words = frozenset({"mode", "con", "ion", "don't"})
    text = "model icon production 100rpm 100L 100人"
    assert with_latin_word_boundaries(text, words) == text
    boundary = LATIN_WORD_BOUNDARY
    assert with_latin_word_boundaries("MODEとdon't", words) == boundary + "mode" + boundary + "と" + boundary + "don't" + boundary


def test_native_boundaries_prevent_prefix_and_suffix_intrusion_without_repricing(tmp_path: Path):
    native = pytest.importorskip("pyopenjtalk")
    base = native.OpenJTalk(native.OPEN_JTALK_DICT_DIR)
    rows = [_row("ｈｉｌｔｏｎ", "ヒルトン"), _row("ｐｒｏｄｕｃｔｉｏｎ", "プロダクション"),
            _row("ｈｉｌｔ", "ヒルト", "1/3"), _row("ｐｒｏ", "プロ", "1/2"),
            _row("ｄｕｃｔ", "ダクト", "1/3"), _row("ｉｏｎ", "イオン", "1/3"),
            _row("ｆｏｌｌｏｗｅｒ", "フォロワー", "0/4"), _row("ｃｏｎ", "コン", "1/2"),
            _row("ｍｏｄｅ", "モード", "1/3")]
    kept, _ = filter_extension_rows(rows, {}, native_base_word_lookup())
    words = frozenset(word_key(row[0]) for row in kept)
    source, binary = tmp_path / "source.csv", tmp_path / "user.dict"
    source.write_text("".join(",".join(row) + "\n" for row in kept), encoding="utf-8")
    native.mecab_dict_index(str(source), str(binary))
    extension = native.OpenJTalk(native.OPEN_JTALK_DICT_DIR, userdic=str(binary).encode())
    for word in ("production", "hilton", "icon", "model", "100人", "100rpm", "１００ＭＬ"):
        for prefix, suffix in (("", ""), ("", "を確認する。"), ("これは", "です。")):
            text = prefix + word + suffix
            assert extension.run_frontend(with_latin_word_boundaries(text, words)) == base.run_frontend(text)
    for word, reading in (("follower", "フォロワー"), ("hilt", "ヒルト"), ("con", "コン"), ("mode", "モード")):
        for prefix, suffix in (("", ""), ("", "を確認する。"), ("これは", "です。")):
            assert any(f["read"] == reading for f in extension.run_frontend(with_latin_word_boundaries(prefix + word + suffix, words)))


def test_reported_regressions_through_preparation_and_production_frontend(tmp_path, monkeypatch):
    native = pytest.importorskip("pyopenjtalk")
    from tools.prepare_japanese_dictionary import prepare_dictionary, RECIPE_PATH
    from tools.tts_text_processor import correct_pronunciation_for_tts
    from tts.japanese_dictionary import configure_japanese_dictionary

    root = RECIPE_PATH.parents[2]
    monkeypatch.syspath_prepend(str(root / "GPT_SoVITS"))
    from text.cleaner import clean_text

    def phones(text):
        return clean_text(correct_pronunciation_for_tts(text), "ja", "v2")[0]

    bad = {"Help": "ヘルプト", "learn": "レアン", "Two": "ツーズ", "with": "ウィズドロールズ",
           "Production": "プロダクションズ", "model": "モデルド", "let's": "レットアップス",
           "welcome": "ウェルカムド", "directly": "ダイレクトネス", "reset": "リセッティング",
           "deadline": "デッドラインズ"}
    rows = [_row(word, reading, "0/10") for word, reading in bad.items()]
    # The real data has a flagged third variant; its non-flagged shifted
    # alternatives must not become the next selected reading after filtering.
    rows += [_row("directly", "ディレクター"), _row("directly", "ディレクターズ"),
             _row("reset", "リセット")]
    data = "".join(",".join(row) + "\n" for row in rows).encode()
    source, recipe_path = tmp_path / "upstream.csv", tmp_path / "recipe.json"
    source.write_bytes(data)
    recipe = json.loads(RECIPE_PATH.read_text(encoding="utf-8"))
    recipe["source"].update(sha256=hashlib.sha256(data).hexdigest(), entry_count=len(rows))
    recipe_path.write_text(json.dumps(recipe), encoding="utf-8")
    prepare_dictionary(source, recipe_path=recipe_path, project_root=tmp_path)
    try:
        native.unset_user_dict()
        baseline = {word: phones(word) for word in bad}
        assert configure_japanese_dictionary(native, project_root=tmp_path, enabled=True)["ready"]
        for word in bad.keys() - {"let's"}:
            assert phones(word) == baseline[word]
        assert [f["read"] for f in native.run_frontend(with_latin_word_boundaries("let’s"))] == ["レッツ"]
        assert phones("let’s") == phones("レッツ")
        assert source.read_bytes() == data
    finally:
        native.unset_user_dict()
