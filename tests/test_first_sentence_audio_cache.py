from __future__ import annotations

import json
import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np

from tts import first_sentence_audio_cache as cache_module
from tts.first_sentence_audio_cache import FirstSentenceAudioCache


def test_prebuilt_expanding_pronunciation_hits_runtime_cache(tmp_path, monkeypatch):
    from tts import pipeline
    from tools import prebuild_first_sentence_audio_cache as prebuild

    cache = FirstSentenceAudioCache(tmp_path)
    monkeypatch.setattr(cache_module, "FIRST_SENTENCE_AUDIO_CACHE_ENABLED", True)
    monkeypatch.setattr(prebuild, "get_first_sentence_audio_cache", lambda: cache)
    monkeypatch.setattr(pipeline, "get_first_sentence_audio_cache", lambda: cache)
    monkeypatch.setattr(pipeline, "TTS_OUTPUT_LANGUAGE", "日文")
    monkeypatch.setattr(pipeline, "_tts_interrupt_epoch", 7)
    inferred, played = [], []

    def infer_stream(**kwargs):
        inferred.append(kwargs)
        yield 24000, np.ones(2400, dtype=np.float32), kwargs['text']

    async def play(audio, *_args, **_kwargs):
        played.append(audio)

    monkeypatch.setattr(pipeline, "_tts_runtime", SimpleNamespace(
        backend_id="gpt_sovits", infer_stream=infer_stream,
    ))
    monkeypatch.setattr(pipeline, "_playback_manager", SimpleNamespace(add_to_playlist=play))
    raw = "あ" * 10 + "AI。"
    item = prebuild._prepare_item(raw)
    assert len(raw) == 13 and len(item.processed_text) == 15
    audio = np.full(2400, .25, dtype=np.float32)
    cache.store(item.processed_text, item.params, 24000, audio, raw_text=raw, source="prebuild")
    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(pipeline, "_tts_executor", executor)
        asyncio.run(pipeline.speak_stream_enhanced_asyncio_queue(
            raw, "sentence_1_cache_regression", True, interrupt_epoch=7,
        ))
    assert inferred == [], "a matching prebuilt first sentence must not run inference"
    assert len(played) == 1
    np.testing.assert_array_equal(played[0], audio)


def _current_params() -> dict:
    return {
        "ref_audio_path": "./assets/audio/reference/kurisu_reference.wav",
        "prompt_text": (
            "そういえば,まともに自己紹介していませんでしたね……"
            "牧瀬くりすです.改めまして,よろしく"
        ),
        "text_language": "日文",
        "prompt_language": "日文",
        "how_to_cut": "不切",
        "top_p": 1,
        "top_k": 5,
        "temperature": 0.6,
        "sample_steps": 4,
        "speed": 1.067,
        "if_sr": False,
        "pause_second": 0.4,
        "max_sec_override": 3.5,
    }


def _configure_current_identity(monkeypatch) -> None:
    import tts.pipeline as pipeline

    monkeypatch.setattr(
        cache_module,
        "TTS_GPT_MODEL_PATH",
        "assets/models/gpt-sovits/weights/gpt/v3/xxx-e15.ckpt",
    )
    monkeypatch.setattr(
        cache_module,
        "TTS_SOVITS_MODEL_PATH",
        "assets/models/gpt-sovits/weights/sovits/v3/xxx_e2_s174_l32.pth",
    )
    monkeypatch.setattr(pipeline, "current_tts_language_code", lambda: "ja")


def _write_entry(
    cache: FirstSentenceAudioCache,
    payload: dict,
    audio: np.ndarray,
    *,
    metadata: bool = True,
    metadata_payload: dict | None = None,
) -> None:
    path = cache._path_for_payload(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    values = {
        "sr": np.array(24000, dtype=np.int32),
        "audio": np.asarray(audio, dtype=np.float32),
    }
    if metadata:
        recorded_payload = metadata_payload or payload
        values["meta_json"] = np.array(
            json.dumps(
                {
                    "schema": "first_sentence_audio_cache.v1",
                    "key": cache._fingerprint_payload(recorded_payload),
                    "source": "runtime_stream",
                    "raw_text": payload["text"],
                    "processed_text": payload["text"],
                    "duration": len(audio) / 24000,
                    "sample_rate": 24000,
                    "samples": len(audio),
                    "key_payload": recorded_payload,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    with path.open("wb") as stream:
        np.savez(stream, **values)


def test_legacy_asset_layout_cache_is_reused_and_migrated(
    tmp_path,
    monkeypatch,
) -> None:
    _configure_current_identity(monkeypatch)
    cache = FirstSentenceAudioCache(tmp_path)
    params = _current_params()
    text = "ん、"
    legacy_payload = cache._legacy_key_payloads(text, params)[0]
    audio = np.linspace(-0.2, 0.2, 2400, dtype=np.float32)
    _write_entry(cache, legacy_payload, audio)

    current_path = cache.path_for(text, params)
    legacy_path = cache._path_for_payload(legacy_payload)
    assert current_path.exists() is False

    result = cache.lookup(text, params)

    assert result is not None
    assert result[0] == 24000
    np.testing.assert_array_equal(result[1], audio)
    assert legacy_path.exists() is True
    assert current_path.exists() is True
    with np.load(current_path, allow_pickle=False) as data:
        metadata = json.loads(str(data["meta_json"].item()))
    assert metadata["source"] == "legacy_identity_migration"
    assert metadata["key_payload"] == cache.key_payload(text, params)


def test_legacy_cache_without_metadata_remains_usable(
    tmp_path,
    monkeypatch,
) -> None:
    _configure_current_identity(monkeypatch)
    cache = FirstSentenceAudioCache(tmp_path)
    params = _current_params()
    text = "え、"
    legacy_payload = cache._legacy_key_payloads(text, params)[0]
    audio = np.ones(1200, dtype=np.float32) * 0.05
    _write_entry(cache, legacy_payload, audio, metadata=False)

    result = cache.lookup(text, params)

    assert result is not None
    assert result[0] == 24000
    np.testing.assert_array_equal(result[1], audio)
    assert cache.path_for(text, params).exists() is True


def test_legacy_cache_with_mismatched_identity_is_rejected(
    tmp_path,
    monkeypatch,
) -> None:
    _configure_current_identity(monkeypatch)
    cache = FirstSentenceAudioCache(tmp_path)
    params = _current_params()
    text = "ああ、"
    legacy_payload = cache._legacy_key_payloads(text, params)[0]
    mismatched = dict(legacy_payload)
    mismatched["gpt_model"] = "GPT_weights_v3/different-model.ckpt"
    _write_entry(
        cache,
        legacy_payload,
        np.ones(800, dtype=np.float32),
        metadata_payload=mismatched,
    )

    assert cache.lookup(text, params) is None
    assert cache.path_for(text, params).exists() is False


def test_previous_synthesis_revisions_are_not_reused(
    tmp_path,
    monkeypatch,
) -> None:
    _configure_current_identity(monkeypatch)
    cache = FirstSentenceAudioCache(tmp_path)
    params = _current_params()
    text = "うーん……"
    audio = np.ones(2400, dtype=np.float32) * 0.05
    for previous_revision in (
        None,
        "gsv_static_kv_mask_semantic_guard.v1",
        "gsv_static_kv_mask_semantic_guard.local_only.v2",
        "gsv_static_kv_mask_semantic_guard.local_only.speech_onset.v3",
    ):
        old_payload = cache.key_payload(text, params)
        if previous_revision is None:
            old_payload.pop("synthesis_revision")
        else:
            old_payload["synthesis_revision"] = previous_revision
        _write_entry(cache, old_payload, audio)

        assert cache._path_for_payload(old_payload).exists() is True
        assert cache.path_for(text, params).exists() is False
        assert cache.lookup(text, params) is None


def test_legacy_cache_with_malformed_metadata_is_rejected(
    tmp_path,
    monkeypatch,
) -> None:
    _configure_current_identity(monkeypatch)
    cache = FirstSentenceAudioCache(tmp_path)
    params = _current_params()
    text = "そうね、"
    legacy_payload = cache._legacy_key_payloads(text, params)[0]
    path = cache._path_for_payload(legacy_payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        np.savez(
            stream,
            sr=np.array(24000, dtype=np.int32),
            audio=np.ones(800, dtype=np.float32),
            meta_json=np.array("{not-json"),
        )

    assert cache.lookup(text, params) is None
    assert cache.path_for(text, params).exists() is False


def test_current_cache_identity_takes_precedence_over_legacy(
    tmp_path,
    monkeypatch,
) -> None:
    _configure_current_identity(monkeypatch)
    cache = FirstSentenceAudioCache(tmp_path)
    params = _current_params()
    text = "わかったわ。"
    legacy_payload = cache._legacy_key_payloads(text, params)[0]
    legacy_audio = np.ones(900, dtype=np.float32) * 0.1
    current_audio = np.ones(600, dtype=np.float32) * 0.2
    _write_entry(cache, legacy_payload, legacy_audio)
    assert cache.store(
        text,
        params,
        24000,
        current_audio,
        source="current",
    )

    result = cache.lookup(text, params)

    assert result is not None
    np.testing.assert_array_equal(result[1], current_audio)
