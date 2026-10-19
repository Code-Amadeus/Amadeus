"""Optional dictionary failures and real pronunciation/cache boundaries."""
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from config import japanese_dictionary as pack_data
from tts import japanese_dictionary as runtime
from tools.tts_text_processor import correct_pronunciation_for_tts
from test_japanese_dictionary import _fake_jtalk, _prepare, _row, LEARNER


@pytest.fixture(autouse=True)
def isolate_native_status(monkeypatch):
    monkeypatch.setattr(runtime, "_official_latin_words", frozenset())
    monkeypatch.setattr(runtime, "_runtime_status", {
        "state": "not_loaded", "ready": False, "identity": None, "version": "", "detail": "Not loaded",
    })


@pytest.mark.parametrize("text,expected", [
    ("APIs GPUs URLs", "エーピーアイs ジーピーユーs ユーアールエルs"),
    ("OpenAI's Claude’s GitHub's", "オープンエーアイ's クロード’s ギットハブ's"),
    ("MLPsを使う APIary CAPITAL cats", "エムエルピーsを使う APIary CAPITAL cats"),
    ("170cm 50kg 60kW 60MW １００ＭＬ", "170cm 50kg 60kW 60MW １００ML"),
    ("3GPUs 3API 11AI", "3ジーピーユーs 3エーピーアイ 11エーアイ"),
])
def test_inflected_known_tokens_and_measurements(text, expected):
    assert correct_pronunciation_for_tts(text) == expected


def test_build_yields_short_latin_fragments_to_the_base_dictionary():
    from tools.audit_japanese_dictionary import normalize_rows
    rows = [_row("ｍ", "メモスリー"), _row("ｗ", "ウォーブリュー"),
            _row("ｃｍ", "シーエム"), _row("ｄｏｎ'ｔ", "ドント")]
    cleaned, stats = normalize_rows(rows, lambda _: None, lambda s: s.replace("'", "’"))
    assert len(cleaned) == 1 and cleaned[0][0] == "ｄｏｎ’ｔ"
    assert cleaned[0][1:] == rows[-1][1:]
    assert stats["short_latin_entries_excluded"] == 3


def test_invalid_pack_hash_is_checked_first_and_failure_cached_by_stat(tmp_path, monkeypatch):
    root = _prepare(tmp_path)
    csv = root / "userdict.csv"
    original = csv.read_bytes()
    csv.write_bytes(original + b"bad line\n")
    calls = []
    validate = pack_data._validate_dictionary_pack
    monkeypatch.setattr(pack_data, "_validate_dictionary_pack", lambda p: (calls.append(p), validate(p))[1])
    def unexpected_parse(_):
        raise AssertionError("hash mismatch must reject before CSV parsing")
    with monkeypatch.context() as patch:
        patch.setattr(pack_data, "dictionary_rows", unexpected_parse)
        for _ in range(3):
            with pytest.raises(pack_data.JapaneseDictionaryError, match="hash"):
                pack_data.load_dictionary_pack(root)
    assert len(calls) == 1
    csv.write_bytes(original)
    assert pack_data.load_dictionary_pack(root).entry_count > 0
    assert len(calls) == 2


@pytest.mark.parametrize("enabled", [True, False])
def test_unsupported_native_api_keeps_the_optional_boundary_nonfatal(tmp_path, enabled):
    status = runtime.configure_japanese_dictionary(SimpleNamespace(), project_root=tmp_path, enabled=enabled)
    assert status["state"] == "unsupported" and status["identity"] is None


def test_configuration_and_reset_failures_do_not_escape(tmp_path, monkeypatch):
    def fail(*_):
        raise AttributeError("native unavailable")
    monkeypatch.setattr(runtime, "_enabled", fail)
    assert runtime.configure_japanese_dictionary(SimpleNamespace(), project_root=tmp_path)["identity"] is None
    jtalk = _fake_jtalk(tmp_path)
    jtalk.unset_user_dict = fail
    assert runtime.configure_japanese_dictionary(jtalk, project_root=tmp_path, enabled=True)["identity"] is None


@pytest.mark.parametrize("personal", [b"", b"\xef\xbb\xbf" + LEARNER.encode(), LEARNER.replace("1/5", "*").encode()])
def test_bad_personal_csv_preserves_the_official_pack_and_names_source(tmp_path, personal):
    _prepare(tmp_path)
    jtalk = _fake_jtalk(tmp_path)
    good = runtime.configure_japanese_dictionary(jtalk, project_root=tmp_path, enabled=True)
    (tmp_path / "assets/text/ja/user.csv").write_bytes(personal)
    bad = runtime.configure_japanese_dictionary(jtalk, project_root=tmp_path, enabled=True)
    assert bad["ready"] and bad["identity"] == good["identity"]
    assert len(jtalk.registered[-1]) == 1
    assert "assets/text/ja/user.csv" in bad["detail"]
    if personal.startswith(b"\xef\xbb\xbf"):
        assert "BOM" in bad["detail"]


def test_native_registration_failure_preserves_previously_loaded_sources(tmp_path):
    _prepare(tmp_path)
    jtalk = _fake_jtalk(tmp_path)
    (tmp_path / "assets/text/ja/user.csv").write_text(LEARNER, encoding="utf-8")
    register = jtalk.update_global_jtalk_with_user_dict
    def reject_personal(paths):
        if len(paths) == 2:
            raise TypeError("personal registration failed")
        register(paths)
    jtalk.update_global_jtalk_with_user_dict = reject_personal
    status = runtime.configure_japanese_dictionary(jtalk, project_root=tmp_path, enabled=True)
    assert status["ready"] and len(jtalk.registered[-1]) == 1
    assert "user.csv" in status["detail"]


def test_compilation_reuses_a_peer_published_file_and_prunes_obsolete_cache(tmp_path, monkeypatch):
    _prepare(tmp_path)
    jtalk = _fake_jtalk(tmp_path)
    cache = tmp_path / ".cache/japanese_dictionary"
    cache.mkdir(parents=True)
    old = cache / ("a" * 64 + ".dict")
    old.write_bytes(b"obsolete")
    def peer_wins(source, target):
        Path(target).write_bytes(Path(source).read_bytes())
        raise PermissionError("peer already mmap'ed target")
    monkeypatch.setattr(runtime.os, "replace", peer_wins)
    assert runtime.configure_japanese_dictionary(jtalk, project_root=tmp_path, enabled=True)["ready"]
    assert not old.exists()
    assert len(list(cache.glob("*.dict"))) == 1


def test_native_units_and_contractions_against_base_frontend(tmp_path):
    jtalk = pytest.importorskip("pyopenjtalk")
    import hashlib
    import json
    import sys
    from tools.prepare_japanese_dictionary import prepare_dictionary
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "GPT_SoVITS"))
    from text import japanese
    rows = [_row("ｍ", "メモスリー"), _row("ｗ", "ウォーブリュー"), _row("ｈ", "ヘッチ"),
            _row("ｋｇ", "ケイジー"), _row("ｄｏｎ'ｔ", "ドント", "0/3"), _row("ｉ'ｍ", "アイム", "0/3")]
    data = ("\n".join(",".join(row) for row in rows) + "\n").encode()
    source = tmp_path / "upstream.csv"
    source.write_bytes(data)
    recipe = json.loads((root / "tools/data/japanese_dictionary_source.json").read_text(encoding="utf-8"))
    recipe["source"].update(sha256=hashlib.sha256(data).hexdigest(), entry_count=len(rows))
    recipe_path = tmp_path / "recipe.json"
    recipe_path.write_text(json.dumps(recipe), encoding="utf-8")
    prepare_dictionary(source, project_root=tmp_path, recipe_path=recipe_path)
    texts = ("身長は170cmです", "50kgです", "3kmです", "10mmです", "100mlです", "60kWです", "60MWです", "APIsを使う", "OpenAI's API")
    try:
        jtalk.unset_user_dict()
        before = [japanese.g2p(correct_pronunciation_for_tts(text)) for text in texts]
        assert runtime.configure_japanese_dictionary(jtalk, project_root=tmp_path, enabled=True)["ready"]
        assert [japanese.g2p(correct_pronunciation_for_tts(text)) for text in texts] == before
        for word, reading in (("don't", "ドント"), ("I'm", "アイム")):
            assert "".join(f["read"] for f in jtalk.run_frontend(runtime.with_latin_word_boundaries(word.lower()))) == reading
            assert "UNK" not in japanese.g2p(word)
    finally:
        jtalk.unset_user_dict()


def test_audio_cache_uses_worker_selection_and_skips_unknown_and_japanese_legacy(tmp_path, monkeypatch):
    from tts import pipeline, first_sentence_audio_cache as cache_module
    from tts.backend import TTSRuntimeAdapter
    from tts.backends.gpt_sovits import GPTSoVITSBackend
    backend = GPTSoVITSBackend()
    backend.deployment = "subprocess"
    backend._proc = SimpleNamespace(poll=lambda: None)
    backend._ready_info = {"japanese_dictionary": {"identity": "actual-base", "state": "invalid"}}
    monkeypatch.setattr(runtime, "_runtime_status", {"identity": "host-installed"})
    monkeypatch.setattr(pipeline, "_tts_runtime", TTSRuntimeAdapter(backend))
    monkeypatch.setattr(pipeline, "current_tts_language_code", lambda: "ja")
    monkeypatch.setattr(cache_module, "FIRST_SENTENCE_AUDIO_CACHE_ENABLED", True)
    cache = cache_module.FirstSentenceAudioCache(tmp_path)
    assert cache.key_payload("API", {})["japanese_dictionary"] == "actual-base"
    assert cache.store("API", {}, 24000, np.ones(24))
    backend._ready_info["japanese_dictionary"]["identity"] = "actual-pack"
    assert cache.lookup("API", {}) is None
    assert cache._legacy_key_payloads(cache.key_payload("API", {})) == ()
    assert cache.store("API", {}, 24000, np.ones(24))
    assert cache.lookup("API", {}) is not None
    backend._ready_info["japanese_dictionary"]["identity"] = None
    assert not cache.store("API", {}, 24000, np.ones(24))
    assert cache.lookup("API", {}) is None


def test_cache_store_snapshots_native_identity_once(tmp_path, monkeypatch):
    from tts import pipeline, first_sentence_audio_cache as cache_module
    calls = []
    def status():
        calls.append(1)
        return {"identity": "native"}
    monkeypatch.setattr(pipeline, "japanese_dictionary_status", status)
    monkeypatch.setattr(pipeline, "current_tts_language_code", lambda: "ja")
    monkeypatch.setattr(cache_module, "FIRST_SENTENCE_AUDIO_CACHE_ENABLED", True)
    cache = cache_module.FirstSentenceAudioCache(tmp_path)
    assert cache.store("API", {}, 24000, np.ones(24))
    assert len(calls) == 1


def test_offline_prebuild_refreshes_the_identity_after_native_startup(tmp_path, monkeypatch):
    from tts import pipeline, first_sentence_audio_cache as cache_module
    from tools import prebuild_first_sentence_audio_cache as prebuild
    status = {"identity": None}
    monkeypatch.setattr(pipeline, "japanese_dictionary_status", lambda: status)
    monkeypatch.setattr(pipeline, "current_tts_language_code", lambda: "ja")
    monkeypatch.setattr(cache_module, "FIRST_SENTENCE_AUDIO_CACHE_ENABLED", True)
    cache = cache_module.FirstSentenceAudioCache(tmp_path)
    monkeypatch.setattr(prebuild, "get_first_sentence_audio_cache", lambda: cache)
    planned = prebuild._prepare_item("こんにちは。")
    status["identity"] = "native-loaded"
    calls = []
    class Inferencer:
        def infer_stream(self, **kwargs):
            calls.append(kwargs)
            yield 24000, np.ones(24), kwargs["text"]
    result, _, path = prebuild._synthesize_to_cache(Inferencer(), planned, False)
    assert result.startswith("stored") and path != planned.cache_path and path.is_file()
    assert prebuild._synthesize_to_cache(Inferencer(), planned, False)[0] == "hit"
    assert len(calls) == 1


@pytest.mark.parametrize("language", ["日文", "英文"])
def test_absent_pack_is_optional_and_inactive(tmp_path, monkeypatch, language):
    from config import asset_packages, settings
    from server.handlers.system_handler import _voice_configuration
    from tts import pipeline
    original = asset_packages.external_asset_pack_status
    monkeypatch.setattr(asset_packages, "external_asset_pack_status", lambda pack_id, **kwargs: {
        "installed": False, "state": "not_installed", "version": "", "message": "",
    } if pack_id == pack_data.PACK_ID else original(pack_id, **kwargs))
    monkeypatch.setattr(settings, "TTS_BACKEND", "gpt_sovits")
    monkeypatch.setattr(settings, "TTS_OUTPUT_LANGUAGE", language)
    monkeypatch.setattr(settings, "ENABLE_JAPANESE_USER_DICTIONARY", True)
    monkeypatch.setattr(pipeline, "japanese_dictionary_status", lambda: {"state": "not_loaded", "ready": False, "detail": "Not loaded"})
    group = next(g for g in _voice_configuration(settings) if g["id"] == "tts_japanese_dictionary")
    assert not group["active"] and not group["configured"] and group["status_ok"]
    assert "base Japanese" in group["status_detail"]


def test_live_english_output_does_not_activate_an_installed_pack(monkeypatch):
    from config import asset_packages, settings
    from server.handlers.system_handler import _voice_configuration
    from tts import pipeline
    original = asset_packages.external_asset_pack_status
    monkeypatch.setattr(asset_packages, "external_asset_pack_status", lambda pack_id, **kwargs: {
        "installed": True, "state": "installed", "version": "test", "message": "",
    } if pack_id == pack_data.PACK_ID else original(pack_id, **kwargs))
    monkeypatch.setattr(settings, "TTS_BACKEND", "gpt_sovits")
    monkeypatch.setattr(settings, "TTS_OUTPUT_LANGUAGE", "日文")
    monkeypatch.setattr(settings, "ENABLE_JAPANESE_USER_DICTIONARY", True)
    monkeypatch.setattr(pipeline, "current_tts_language_code", lambda: "en")
    monkeypatch.setattr(pipeline, "japanese_dictionary_status", lambda: {
        "state": "ready", "ready": True, "detail": "Loaded",
    })
    group = next(g for g in _voice_configuration(settings) if g["id"] == "tts_japanese_dictionary")
    assert not group["active"] and group["configured"] and group["status_ok"]
