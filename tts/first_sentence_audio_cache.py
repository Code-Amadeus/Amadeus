from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import Any

import numpy as np

from config.settings import (
    FIRST_SENTENCE_AUDIO_CACHE_DIR,
    FIRST_SENTENCE_AUDIO_CACHE_ENABLED,
    FIRST_SENTENCE_AUDIO_CACHE_MAX_SECONDS,
    TTS_GPT_MODEL_PATH,
    TTS_OUTPUT_LANGUAGE,
    TTS_SOVITS_MODEL_PATH,
)

logger = logging.getLogger(__name__)

_KEY_FIELDS = (
    "ref_audio_path",
    "prompt_text",
    "text_language",
    "prompt_language",
    "how_to_cut",
    "top_p",
    "top_k",
    "temperature",
    "sample_steps",
    "speed",
    "if_sr",
    "pause_second",
    "max_sec_override",
)

_CACHE_SCHEMA = "first_sentence_audio_cache.v1"
_SYNTHESIS_REVISION = "gsv_static_kv_mask_semantic_guard.v1"


def _portable_basename(value: Any) -> str:
    """Return a filename from either Windows or POSIX-style stored paths."""

    normalized = str(value or "").strip().replace("\\", "/")
    return PurePosixPath(normalized).name if normalized else ""


def _decode_meta_json(value: Any) -> dict[str, Any] | None:
    try:
        if isinstance(value, np.ndarray):
            value = value.item()
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if not isinstance(value, str) or not value.strip():
            return None
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


class FirstSentenceAudioCache:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._lock = Lock()

    def key_payload(self, text: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            from tts.pipeline import current_tts_language_code

            output_language = current_tts_language_code()
        except Exception:
            output_language = "en" if TTS_OUTPUT_LANGUAGE == "英文" else "ja"
        return {
            "text": text,
            # Audio produced before this decoder invariant/guard revision may
            # already contain a collapsed long vowel.  Keep it out of the new
            # path without deleting user files or scanning the cache tree.
            "synthesis_revision": _SYNTHESIS_REVISION,
            "tts_output_language": output_language,
            "gpt_model": TTS_GPT_MODEL_PATH,
            "sovits_model": TTS_SOVITS_MODEL_PATH,
            "params": {field: params.get(field) for field in _KEY_FIELDS},
        }

    def _fingerprint(self, text: str, params: dict[str, Any]) -> str:
        return self._fingerprint_payload(self.key_payload(text, params))

    @staticmethod
    def _fingerprint_payload(payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _path_for_payload(self, payload: dict[str, Any]) -> Path:
        key = self._fingerprint_payload(payload)
        return self.root / key[:2] / f"{key}.npz"

    def path_for(self, text: str, params: dict[str, Any]) -> Path:
        return self._path_for_payload(self.key_payload(text, params))

    def _legacy_key_payloads(
        self,
        text: str,
        params: dict[str, Any],
    ) -> tuple[dict[str, Any], ...]:
        """Recreate the bounded pre-asset-layout cache identity.

        The 2026-08 asset relocation changed three identity strings without
        changing the selected voice: model roots, reference-audio root, and
        the public language value (``日文``/``英文`` -> ``ja``/``en``).  Older
        cache files are content-addressed from those exact strings, so derive
        that one known predecessor instead of scanning arbitrary local cache
        metadata on the latency-critical lookup path.
        """

        current = self.key_payload(text, params)
        language = str(current.get("tts_output_language") or "").strip().lower()
        legacy_language = {"ja": "日文", "en": "英文"}.get(language)
        gpt_name = _portable_basename(current.get("gpt_model"))
        sovits_name = _portable_basename(current.get("sovits_model"))
        current_params = current.get("params")
        if not isinstance(current_params, dict):
            return ()
        ref_name = _portable_basename(current_params.get("ref_audio_path"))
        if not legacy_language or not gpt_name or not sovits_name or not ref_name:
            return ()

        legacy_params = dict(current_params)
        legacy_params["ref_audio_path"] = f"./reference audio/{ref_name}"
        legacy = dict(current)
        legacy.update(
            {
                "tts_output_language": legacy_language,
                "gpt_model": f"GPT_weights_v3/{gpt_name}",
                "sovits_model": f"SoVITS_weights_v3/{sovits_name}",
                "params": legacy_params,
            }
        )
        return (legacy,)

    def _read_entry(
        self,
        path: Path,
        *,
        expected_payload: dict[str, Any],
    ) -> tuple[int, np.ndarray, dict[str, Any] | None] | None:
        try:
            with np.load(path, allow_pickle=False) as data:
                sr = int(data["sr"])
                audio = np.asarray(data["audio"], dtype=np.float32)
                has_metadata = "meta_json" in data.files
                metadata = (
                    _decode_meta_json(data["meta_json"])
                    if has_metadata
                    else None
                )
            if sr <= 0 or audio.size <= 0:
                return None
            if has_metadata and metadata is None:
                return None

            expected_key = self._fingerprint_payload(expected_payload)
            if path.stem != expected_key:
                return None
            if metadata is not None:
                if metadata.get("schema") != _CACHE_SCHEMA:
                    return None
                if metadata.get("key") != expected_key:
                    return None
                if metadata.get("key_payload") != expected_payload:
                    return None
                if str(metadata.get("processed_text") or "") != str(
                    expected_payload.get("text") or ""
                ):
                    return None
                if int(metadata.get("sample_rate")) != sr:
                    return None
                if int(metadata.get("samples")) != int(audio.size):
                    return None
            return sr, audio, metadata
        except Exception as exc:
            logger.debug("[FirstSentenceAudioCache] read failed %s: %s", path, exc)
            return None

    def lookup(self, text: str, params: dict[str, Any]) -> tuple[int, np.ndarray] | None:
        if not FIRST_SENTENCE_AUDIO_CACHE_ENABLED:
            return None
        current_payload = self.key_payload(text, params)
        current_path = self._path_for_payload(current_payload)
        if current_path.exists():
            current = self._read_entry(
                current_path,
                expected_payload=current_payload,
            )
            if current is not None:
                return current[0], current[1]

        for legacy_payload in self._legacy_key_payloads(text, params):
            legacy_path = self._path_for_payload(legacy_payload)
            if legacy_path == current_path or not legacy_path.exists():
                continue
            legacy = self._read_entry(
                legacy_path,
                expected_payload=legacy_payload,
            )
            if legacy is None:
                logger.warning(
                    "[FirstSentenceAudioCache] rejected incompatible legacy entry %s",
                    legacy_path.name,
                )
                continue

            sr, audio, metadata = legacy
            raw_text = (
                str(metadata.get("raw_text") or text)
                if metadata is not None
                else text
            )
            migrated = self.store(
                text,
                params,
                sr,
                audio,
                raw_text=raw_text,
                source="legacy_identity_migration",
            )
            logger.info(
                "[FirstSentenceAudioCache] legacy hit %s migrated=%s",
                legacy_path.name,
                bool(migrated or current_path.exists()),
            )
            return sr, audio
        return None

    def store(
        self,
        text: str,
        params: dict[str, Any],
        sr: int,
        audio: np.ndarray,
        *,
        raw_text: str | None = None,
        source: str = "runtime",
    ) -> bool:
        if not FIRST_SENTENCE_AUDIO_CACHE_ENABLED:
            return False
        if sr <= 0 or audio is None:
            return False
        audio = np.asarray(audio, dtype=np.float32)
        if audio.size <= 0:
            return False
        duration = float(audio.size) / float(sr)
        if duration > FIRST_SENTENCE_AUDIO_CACHE_MAX_SECONDS:
            logger.debug(
                "[FirstSentenceAudioCache] skip store: %.2fs > %.2fs",
                duration,
                FIRST_SENTENCE_AUDIO_CACHE_MAX_SECONDS,
            )
            return False

        path = self.path_for(text, params)
        key_payload = self.key_payload(text, params)
        key = self._fingerprint(text, params)
        metadata = {
            "schema": _CACHE_SCHEMA,
            "key": key,
            "source": source,
            "created_at": time.time(),
            "raw_text": raw_text if raw_text is not None else text,
            "processed_text": text,
            "duration": duration,
            "sample_rate": int(sr),
            "samples": int(audio.size),
            "max_cache_seconds": FIRST_SENTENCE_AUDIO_CACHE_MAX_SECONDS,
            "key_payload": key_payload,
        }
        meta_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str)
        with self._lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                np.savez(
                    path,
                    sr=np.array(sr, dtype=np.int32),
                    audio=audio,
                    meta_json=np.array(meta_json),
                )
                logger.info("[FirstSentenceAudioCache] stored %.2fs -> %s", duration, path.name)
                return True
            except Exception as exc:
                logger.debug("[FirstSentenceAudioCache] store failed %s: %s", path, exc)
                return False


_CACHE: FirstSentenceAudioCache | None = None


def get_first_sentence_audio_cache() -> FirstSentenceAudioCache:
    global _CACHE
    if _CACHE is None:
        _CACHE = FirstSentenceAudioCache(FIRST_SENTENCE_AUDIO_CACHE_DIR)
    return _CACHE
