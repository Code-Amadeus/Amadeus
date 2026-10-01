"""Runtime character profiles: art, voice reference and persona.

The pack is intentionally a thin manifest over existing repository assets. It
never copies or rewrites voice/model files; switching a profile only changes
which existing references the runtime resolves.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import threading
from typing import Any

from config.asset_paths import PROJECT_ROOT


CHARACTERS_ROOT = PROJECT_ROOT / "assets" / "characters"
STATE_PATH = PROJECT_ROOT / "runtime" / "companion" / "character.json"


@dataclass(frozen=True, slots=True)
class CharacterProfile:
    id: str
    name: str
    art_dir: str = ""
    voice_audio: str = ""
    voice_text: str = ""
    voice_gpt_model: str = ""
    voice_sovits_model: str = ""
    persona_file: str = ""
    persona_prompt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "art_dir": self.art_dir,
            "voice_audio": self.voice_audio,
            "voice_text": self.voice_text,
            "voice_gpt_model": self.voice_gpt_model,
            "voice_sovits_model": self.voice_sovits_model,
            "persona_file": self.persona_file,
            "persona_prompt": self.persona_prompt,
        }


def _resolve(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    path = Path(text)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return str(path.resolve())


def _read_profile(path: Path) -> CharacterProfile | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    character_id = str(payload.get("id") or path.parent.name).strip()
    if not character_id:
        return None
    voice = payload.get("voice") if isinstance(payload.get("voice"), dict) else {}
    persona_file = _resolve(str(payload.get("persona_file") or ""))
    persona_prompt = ""
    if persona_file:
        try:
            persona_prompt = Path(persona_file).read_text(encoding="utf-8").strip()
        except OSError:
            persona_prompt = ""
    return CharacterProfile(
        id=character_id,
        name=str(payload.get("name") or character_id).strip() or character_id,
        art_dir=_resolve(str(payload.get("art_dir") or "")),
        voice_audio=_resolve(str(voice.get("audio") or "")),
        voice_text=str(voice.get("text") or "").strip(),
        voice_gpt_model=_resolve(str(voice.get("gpt_model") or payload.get("gpt_model") or "")),
        voice_sovits_model=_resolve(str(voice.get("sovits_model") or payload.get("sovits_model") or "")),
        persona_file=persona_file,
        persona_prompt=persona_prompt,
    )


class CharacterStore:
    def __init__(self, root: Path = CHARACTERS_ROOT):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._profiles = self._scan()
        self._current_id = self._load_current_id()

    def _scan(self) -> dict[str, CharacterProfile]:
        profiles: dict[str, CharacterProfile] = {}
        for path in sorted(self.root.glob("*/character.json")):
            profile = _read_profile(path)
            if profile is not None:
                profiles[profile.id] = profile
        return profiles

    def _load_current_id(self) -> str:
        try:
            payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            candidate = str(payload.get("id") or "").strip()
        except (OSError, ValueError):
            candidate = ""
        if candidate in self._profiles:
            return candidate
        return next(iter(self._profiles), "")

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [profile.to_dict() for profile in self._profiles.values()]

    def current(self) -> CharacterProfile | None:
        with self._lock:
            return self._profiles.get(self._current_id)

    def switch(self, character_id: str) -> CharacterProfile:
        normalized = str(character_id or "").strip()
        with self._lock:
            if normalized not in self._profiles:
                raise ValueError(f"unknown character profile: {normalized}")
            self._current_id = normalized
            try:
                STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
                STATE_PATH.write_text(
                    json.dumps({"id": normalized}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except OSError:
                # The in-process switch remains authoritative when the state
                # directory is read-only (tests or a locked runtime install).
                pass
            return self._profiles[normalized]

    def active_voice(self) -> dict[str, str]:
        profile = self.current()
        if profile is None:
            return {}
        return {
            "audio": profile.voice_audio,
            "text": profile.voice_text,
            "gpt_model": profile.voice_gpt_model,
            "sovits_model": profile.voice_sovits_model,
            "name": profile.name,
        }

    def active_persona(self) -> str:
        profile = self.current()
        return profile.persona_prompt if profile is not None else ""

    def active_art_dir(self) -> str:
        profile = self.current()
        return profile.art_dir if profile is not None else ""


_store: CharacterStore | None = None
_store_lock = threading.Lock()


def store() -> CharacterStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = CharacterStore()
        return _store


def list_characters() -> list[dict[str, Any]]:
    return store().list()


def current_character() -> dict[str, Any]:
    profile = store().current()
    return profile.to_dict() if profile is not None else {}


def switch_character(character_id: str) -> dict[str, Any]:
    return store().switch(character_id).to_dict()


def active_voice() -> dict[str, str]:
    return store().active_voice()


def active_persona() -> str:
    return store().active_persona()


def active_art_dir() -> str:
    return store().active_art_dir()
