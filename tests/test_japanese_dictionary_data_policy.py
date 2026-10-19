"""The optional CSV extends unknown vocabulary without owning base words."""
from pathlib import Path
import hashlib
import json

import pytest

from tools.audit_japanese_dictionary import (
    LATIN_WORD_COST_FLOOR, cmu_pronunciations, filter_extension_rows, final_phone_risk,
    native_base_word_lookup, word_key,
)
from test_japanese_dictionary import _row


@pytest.fixture(autouse=True)
def isolate_native_status(monkeypatch):
    from tts import japanese_dictionary as runtime
    monkeypatch.setattr(runtime, "_runtime_status", {
        "state": "not_loaded", "ready": False, "identity": None, "version": "", "detail": "Not loaded",
    })


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


def test_filter_yields_base_words_quarantines_variants_and_bounds_fragment_costs():
    rows = [_row("Ｈｅｌｐ", "ヘルプト"), _row("directly", "ディレクターズ"),
            _row("directly", "ディレクター"), _row("follower", "フォロワー"),
            _row("don't", "ドント"), _row("pricy", "プライシー")]
    rows[-1][3] = "12000"
    before = [list(row) for row in rows]
    phones = cmu_pronunciations(b"DIRECTLY D ER0 EH1 K T L IY0\nDIRECTLY(1) D IH0 R EH1 K T L IY0\n")
    kept, report = filter_extension_rows(rows, phones, lambda word: word == "help")
    assert {row[0] for row in kept} == {"follower", "don't", "pricy"}
    assert [row[3] for row in kept] == [str(LATIN_WORD_COST_FLOOR), rows[-2][3], "12000"]
    assert report["reason_counts"] == {"base_dictionary_word": 1, "cmu_final_phone_risk": 2}
    assert rows == before, "filtering must not rewrite its evidence input"
    assert filter_extension_rows(kept, phones, lambda _: False)[0] == kept
    assert word_key("Ｌｅｔ’ｓ") == "let's"


def test_native_base_lookup_does_not_treat_letter_spelling_as_a_word():
    pytest.importorskip("pyopenjtalk")
    known = native_base_word_lookup()
    assert all(known(word) for word in ("Help", "learn", "Two", "with", "Production"))
    assert not any(known(word) for word in ("model", "welcome", "directly", "deadline"))


def test_native_cost_floor_preserves_known_words_and_retains_unknown_word_support(tmp_path: Path):
    native = pytest.importorskip("pyopenjtalk")
    base = native.OpenJTalk(native.OPEN_JTALK_DICT_DIR)
    rows = [_row("ｈｉｌｔ", "ヒルト", "1/3"), _row("ｐｒｏ", "プロ", "1/2"),
            _row("ｄｕｃｔ", "ダクト", "1/3"), _row("ｉｏｎ", "イオン", "1/3"),
            _row("ｆｏｌｌｏｗｅｒ", "フォロワー", "0/4")]
    kept, _ = filter_extension_rows(rows, {}, native_base_word_lookup())
    source, binary = tmp_path / "source.csv", tmp_path / "user.dict"
    source.write_text("".join(",".join(row) + "\n" for row in kept), encoding="utf-8")
    native.mecab_dict_index(str(source), str(binary))
    extension = native.OpenJTalk(native.OPEN_JTALK_DICT_DIR, userdic=str(binary).encode())
    for text in ("production", "hiltonを確認する。", "これはhiltonです。"):
        assert extension.run_frontend(text) == base.run_frontend(text)
    for text in ("follower", "followerを確認する。", "これはfollowerです。"):
        assert any(f["read"] == "フォロワー" for f in extension.run_frontend(text))


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
        assert [f["read"] for f in native.run_frontend("let’s")] == ["レッツ"]
        assert phones("let’s") == phones("レッツ")
        assert source.read_bytes() == data
    finally:
        native.unset_user_dict()
