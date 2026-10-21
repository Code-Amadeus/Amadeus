"""A selective alias must behave like its native spelling, without intrusion."""
import hashlib
import json
from pathlib import Path
import sys

import pytest

from config.japanese_dictionary import PACK_FILES, PACK_TREE, JapaneseDictionaryError, load_dictionary_pack
from tts import japanese_dictionary as runtime
from test_japanese_dictionary import _row


def fixture_row(base, word, reading):
    # Native-word tests use real lexical features. Alias-only fixtures need no
    # native accent: the runtime re-enters the Katakana/compound path instead.
    surface = word if not word.isascii() else "\u3000" + base.normalize_for_mecab(word) + "\u3000"
    if word.isascii() and "-" not in word:
        _, morphs = base.run_mecab_detailed(reading)
        tokens = [m for m in morphs if not m["is_ignored"]]
        if len(tokens) == 1 and not tokens[0]["is_unknown"]:
            token = tokens[0]
            fields = token["features"]
            if fields[8:10] == [reading, reading]:
                return [surface, str(token["left_id"]), str(token["right_id"]), str(token["word_cost"]), *fields[1:]]
    return _row(surface, reading, "0/1")


def make_pack(root, pairs, mode="katakana_alias"):
    native = pytest.importorskip("pyopenjtalk")
    base = native.OpenJTalk(native.OPEN_JTALK_DICT_DIR)
    rows = [fixture_row(base, word, reading) for word, reading in pairs]
    assert all(rows)
    data = ("\n".join(",".join(row) for row in rows) + "\n").encode()
    pack = root / "assets" / PACK_TREE
    pack.mkdir(parents=True)
    for name in PACK_FILES:
        (pack / name).write_text("Synthetic test fixture", encoding="utf-8")
    (pack / "userdict.csv").write_bytes(data)
    review = {}
    for word, reading in pairs:
        review.setdefault(word, []).append(reading)
    metadata = {
        "language": "ja", "version": "test", "entry_mode": mode,
        "entry_count": len(rows), "sha256": hashlib.sha256(data).hexdigest(),
        "release_review": {"accepted_readings": review},
        "source": dict(repository="fixture", revision="test", path="fixture", sha256="fixture"),
    }
    (pack / "provenance.json").write_text(json.dumps(metadata), encoding="utf-8")
    return pack


@pytest.fixture
def native(monkeypatch):
    native = pytest.importorskip("pyopenjtalk")
    from config import settings
    monkeypatch.setattr(settings, "ENABLE_JAPANESE_CONTEXT_READING", False)
    monkeypatch.setattr(settings, "ENABLE_JAPANESE_USER_DICTIONARY", False)
    monkeypatch.setattr(runtime, "_official_latin_words", frozenset())
    monkeypatch.setattr(runtime, "_official_katakana_aliases", {})
    monkeypatch.setattr(runtime, "_official_compound_paths", {})
    monkeypatch.setattr(runtime, "_official_base_owner", None)
    monkeypatch.setattr(runtime, "_runtime_status", dict(runtime._runtime_status))
    native.unset_user_dict()
    yield native
    native.unset_user_dict()


@pytest.mark.parametrize("with_prosody", [False, True])
def test_native_alias_resolves_competing_splits_and_contextual_accent(tmp_path, native, with_prosody):
    make_pack(tmp_path, [("non", "ノン"), ("lean", "リーン"), ("model", "モデル")])
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "GPT_SoVITS"))
    from text import japanese
    controls = ["中立派の話です。", "170cmと50kgです。", "nonstopのmodelingです。",
                "model2とxmodelとmodel-xとmodel'sです。",
                "model-2と2-modelとmodel’2です。"]
    before = [japanese.g2p(text, with_prosody) for text in controls]
    assert runtime.configure_japanese_dictionary(native, project_root=tmp_path, enabled=True)["ready"]
    for text, expected in [("nonの話です。", "ノンの話です。"),
                           ("leanを確認します。", "リーンを確認します。"),
                           ("このMODELを選びます。", "このモデルを選びます。")]:
        assert japanese.g2p(text, with_prosody) == japanese.g2p(expected, with_prosody)
    assert [japanese.g2p(text, with_prosody) for text in controls] == before
    assert not (tmp_path / ".cache/japanese_dictionary").exists(), "Aliases need no synthetic MeCab costs"
    runtime.configure_japanese_dictionary(native, project_root=tmp_path, enabled=False)
    assert runtime.prepare_japanese_text("model") == "model"


@pytest.mark.parametrize("with_prosody", [False, True])
def test_native_proper_name_case_survives_without_an_optional_alias(tmp_path, native, with_prosody):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "GPT_SoVITS"))
    from text import japanese
    runtime.configure_japanese_dictionary(native, project_root=tmp_path, enabled=False)
    assert native.run_frontend("Disney")[0]["read"] == "ディズニー"
    assert japanese.g2p("Disneyの映画を見ました。", with_prosody) == japanese.g2p(
        "ディズニーの映画を見ました。", with_prosody)


def test_registered_native_entries_keep_case_insensitive_lookup_without_folding_other_names(tmp_path, native):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "GPT_SoVITS"))
    from text import japanese
    make_pack(tmp_path, [("model", "モデル")], mode="mecab")
    assert runtime.configure_japanese_dictionary(native, project_root=tmp_path, enabled=True)["ready"]
    for spelling in ["model", "Model", "MODEL", "Ｍｏｄｅｌ"]:
        assert japanese.g2p(f"この{spelling}を選ぶ。") == japanese.g2p("このmodelを選ぶ。")
    assert japanese.g2p("Disneyの映画です。", False) == japanese.g2p("ディズニーの映画です。", False)
    assert runtime.with_latin_word_boundaries("Disney MODEL model-x") == "Disney \u3000model\u3000 model-x"


@pytest.mark.parametrize("with_prosody", [False, True])
def test_punctuation_split_does_not_expose_native_word_fragments(tmp_path, native, with_prosody):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "GPT_SoVITS"))
    from text import japanese
    runtime.configure_japanese_dictionary(native, project_root=tmp_path, enabled=False)
    controls = ["game-2", "2-game", "game2-3", "game-x", "game’2", "1-2", "170cmと50kg"]
    before = [japanese.g2p(text, with_prosody) for text in controls]
    make_pack(tmp_path, [("game", "ゲーム")], mode="mecab")
    assert runtime.configure_japanese_dictionary(native, project_root=tmp_path, enabled=True)["ready"]
    assert [japanese.g2p(text, with_prosody) for text in controls] == before
    assert "ゲ" in "".join(node["read"] for node in native.run_frontend(runtime.prepare_japanese_text("game")))


def test_proper_name_case_fix_keeps_existing_numeric_unit_and_initialism_behavior(tmp_path, native):
    from tools.tts_text_processor import correct_pronunciation_for_tts
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "GPT_SoVITS"))
    from text import japanese
    runtime.configure_japanese_dictionary(native, project_root=tmp_path, enabled=False)
    # Compatibility checks, not new claims about every SI unit interpretation.
    for text in ["170CMです。", "50KGです。", "100Lです。", "100MLです。", "60kWです。", "60MWです。"]:
        assert japanese.g2p(text) == japanese.g2p(text.lower())
    assert japanese.g2p(correct_pronunciation_for_tts("USBを使う。")) == japanese.g2p("ユーエスビーを使う。")
    assert correct_pronunciation_for_tts("Disneyの映画です。") == "Disneyの映画です。"


def test_personal_dictionary_keeps_ownership_of_explicit_spellings(tmp_path, native):
    make_pack(tmp_path, [("model", "モデル"), ("lean", "リーン")])
    row = fixture_row(native.OpenJTalk(native.OPEN_JTALK_DICT_DIR), "model", "テスト")
    row[0] = row[0].strip()
    (tmp_path / "assets/text/ja/user.csv").write_text(",".join(row) + "\n", encoding="utf-8")
    assert runtime.configure_japanese_dictionary(native, project_root=tmp_path, enabled=True)["ready"]
    assert runtime.prepare_japanese_text("modelとlean") == "modelとリーン"


def test_alias_conflicts_fail_closed(tmp_path):
    pack = make_pack(tmp_path, [("model", "モデル"), ("model", "テスト")])
    with pytest.raises(JapaneseDictionaryError, match="Ambiguous"):
        load_dictionary_pack(pack)


def test_reading_mode_changes_inference_cache_identity(tmp_path, native):
    pack = make_pack(tmp_path, [("model", "モデル")])
    first = runtime.configure_japanese_dictionary(native, project_root=tmp_path, enabled=True)
    metadata_path = pack / "provenance.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["entry_mode"] = "mecab"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    second = runtime.configure_japanese_dictionary(native, project_root=tmp_path, enabled=True)
    assert first["ready"] and second["ready"] and first["identity"] != second["identity"]
    assert runtime.prepare_japanese_text("model") == "\u3000model\u3000"


def test_extension_preserves_retained_native_words_and_their_reading_mode(tmp_path, native):
    original = tmp_path / "original"
    extension = tmp_path / "extension"
    make_pack(original, [("model", "モデル")], mode="mecab")
    pack = make_pack(extension, [("model", "モデル"), ("lean", "リーン")])
    metadata_path = pack / "provenance.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["native_words"] = ["model"]
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    runtime.configure_japanese_dictionary(native, project_root=original, enabled=True)
    before = native.run_frontend(runtime.prepare_japanese_text("このmodelを確認する"))
    assert runtime.configure_japanese_dictionary(native, project_root=extension, enabled=True)["ready"]
    assert native.run_frontend(runtime.prepare_japanese_text("このmodelを確認する")) == before
    assert runtime.prepare_japanese_text("modelとlean") == "\u3000model\u3000とリーン"
    identity = runtime.dictionary_runtime_status()["identity"]
    metadata["native_words"] = []
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    assert runtime.configure_japanese_dictionary(native, project_root=extension, enabled=True)["identity"] != identity


def test_retained_native_partition_cannot_name_absent_rows(tmp_path):
    pack = make_pack(tmp_path, [("model", "モデル")])
    path = pack / "provenance.json"
    metadata = json.loads(path.read_text())
    metadata["native_words"] = ["missing"]
    path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(JapaneseDictionaryError, match="must exist"):
        load_dictionary_pack(pack)


def test_compound_constraint_fixes_segmentation_without_intruding_on_other_lexemes(tmp_path, native):
    from tts.japanese_reading import run_frontend
    make_pack(tmp_path, [("中立派", "チュウリツハ"), ("因数分解", "インスウブンカイ"),
                         ("最適解", "サイテキカイ"), ("高次元", "コウジゲン")])
    controls = ["大中立派連合", "立派な人だ。", "中は暗い。", "解脱を目指す。",
                "三十分待った。", "高次元の図形", "大中さんは中立だ。"]
    before = [native.run_frontend(text) for text in controls]
    assert runtime.configure_japanese_dictionary(native, project_root=tmp_path, enabled=True)["ready"]
    for word, expected in [("中立派", "チュウリツハ"), ("因数分解", "インスウブンカイ"),
                           ("最適解", "サイテキカイ")]:
        from tools.probes.japanese_pronunciation_evidence import target_readings
        result = run_frontend(native, f"これは{word}の話です。")
        assert target_readings(result, word) == [expected]
        assert "".join(n["string"] for n in result) == f"これは{word}の話です。"
    assert [run_frontend(native, text) for text in controls] == before
    assert runtime.with_latin_readings("因数分解") == "因数分解", "Keep kanji morphology, not global replacement"
    spaced = run_frontend(native, " 中立派、因数分解 です")
    assert "".join(n["read"] for n in spaced) == "チュウリツハ、インスウブンカイデス"
    runtime.configure_japanese_dictionary(native, project_root=tmp_path, enabled=False)
    assert run_frontend(native, "因数分解") == native.run_frontend("因数分解")


def test_compound_constraint_preserves_personal_dictionary_subspan(tmp_path, native):
    from tts.japanese_reading import run_frontend
    make_pack(tmp_path, [("因数分解", "インスウブンカイ")])
    row = fixture_row(native.OpenJTalk(native.OPEN_JTALK_DICT_DIR), "test", "テスト")
    row[0] = row[10] = "因数"
    row[3] = "-10000"  # Explicit synthetic personal override wins its own span.
    (tmp_path / "assets/text/ja/user.csv").write_text(",".join(row) + "\n", encoding="utf-8")
    assert runtime.configure_japanese_dictionary(native, project_root=tmp_path, enabled=True)["ready"]
    assert run_frontend(native, "因数分解です") == native.run_frontend("因数分解です")
    assert native.run_frontend("因数分解です")[0]["read"] == "テスト"


def test_attested_hyphenated_alias_requires_the_complete_token(tmp_path, native):
    make_pack(tmp_path, [("navier-stokes", "ナビエストークス"), ("deepmind", "ディープマインド")])
    assert runtime.configure_japanese_dictionary(native, project_root=tmp_path, enabled=True)["ready"]
    assert runtime.with_latin_readings("Navier-StokesとDeepMind") == "ナビエストークスとディープマインド"
    for text in ["xNavier-Stokes", "Navier-Stokes2", "Navier-Stokes-2", "2-DeepMind", "DeepMind's"]:
        assert runtime.with_latin_readings(text) == text


def test_correct_longer_compound_protects_its_internal_shorter_word(native, monkeypatch):
    from types import SimpleNamespace
    # This is a real native alternate path, not invented morphology. A context
    # selector may supply it; its complete reading is already correct.
    path = next(p for p in native.run_mecab_nbest_features("中立派", 128)
                if [m["surface"] for m in p["morphs"]] == ["中", "立", "派"]
                and "".join(m["features"][8] for m in p["morphs"]) == "チュウリツハ")
    expected = native.apply_postprocessing("中立派", native.run_njd_from_mecab(path["features"]))
    monkeypatch.setattr(runtime, "_official_compound_paths", {
        word: (reading, runtime.native_compound_path(native, word, reading))
        for word, reading in [("中立派", "チュウリツハ"), ("立派", "リッパ")]
    })
    frontend = SimpleNamespace(
        run_frontend_detailed=lambda *args, **kwargs: (expected, path["morphs"]),
        run_njd_from_mecab=native.run_njd_from_mecab, apply_postprocessing=native.apply_postprocessing)
    actual = runtime.run_lexical_frontend(frontend, "中立派")
    assert "".join(n["read"] for n in actual) == "チュウリツハ"
    assert actual == expected
