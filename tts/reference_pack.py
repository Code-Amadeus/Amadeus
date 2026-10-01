"""Audio/text pairs for the optional Japanese semantic-reference pack."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath

from llm.emo_presets import EMOTION_DURATION_RANGES

PACK_ID = "voice-kurisu-emotions"
PACK_TREE = "audio/reference/emotions"
MANIFEST_NAME = "references.json"
PACK_FORMAT = "amadeus.tts-reference-pack.v1"


class ReferencePackError(ValueError):
    pass


@dataclass(frozen=True)
class SemanticReference:
    key: str
    audio: Path
    transcript: Path
    text: str


@dataclass(frozen=True)
class ReferencePack:
    root: Path
    references: dict[str, SemanticReference]

    def key_for(self, emotion: str) -> str:
        reference = self.references.get(str(emotion).strip().lower())
        return reference.key if reference else ""

    def distinct_references(self) -> tuple[SemanticReference, ...]:
        return tuple({ref.key: ref for ref in self.references.values()}.values())

    def files(self) -> tuple[Path, ...]:
        files = {self.root / MANIFEST_NAME}
        for ref in self.distinct_references():
            files.update((ref.audio, ref.transcript))
        return tuple(sorted(files))


def _member(root: Path, value: object) -> Path:
    name = str(value or "")
    relative = PurePosixPath(name)
    if (not name or relative.is_absolute() or relative.as_posix() != name
            or "\\" in name or any(part in {".", ".."} or ":" in part for part in relative.parts)):
        raise ReferencePackError("Reference members must be normalized pack-relative paths")
    path = root.joinpath(*relative.parts).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ReferencePackError(f"Missing or escaping reference member: {name}")
    return path


def load_reference_pack(root: Path) -> ReferencePack:
    root = Path(root).resolve()
    try:
        raw = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict) or raw.get("format") != PACK_FORMAT or raw.get("language") != "ja":
            raise ReferencePackError("Unsupported semantic-reference pack format or language")
        entries = raw.get("references")
        if not isinstance(entries, dict) or not entries:
            raise ReferencePackError("Reference pack must contain at least one audio/text pair")
        references = {}
        pairs = {}
        for emotion, entry in entries.items():
            if not isinstance(emotion, str) or not emotion or emotion != emotion.strip().lower() or not isinstance(entry, dict):
                raise ReferencePackError("Invalid reference mapping")
            if emotion == "normal":
                raise ReferencePackError("normal must retain the configured default reference")
            if emotion not in EMOTION_DURATION_RANGES:
                raise ReferencePackError(f"Unknown EMO preset: {emotion}")
            audio = _member(root, entry.get("audio"))
            transcript = _member(root, entry.get("transcript"))
            if audio.suffix.lower() not in {".wav", ".ogg", ".flac"} or transcript.suffix.lower() != ".txt":
                raise ReferencePackError("Reference mapping requires an audio file and UTF-8 TXT")
            text = transcript.read_text(encoding="utf-8-sig").strip()
            if not text:
                raise ReferencePackError(f"Empty reference transcript: {transcript.name}")
            # Aliases share one conditioning key and one warmup, independently
            # of their distinct expression presets.
            pair = (audio, transcript)
            if pair not in pairs:
                pairs[pair] = SemanticReference(emotion, audio, transcript, text)
            references[emotion] = pairs[pair]
        return ReferencePack(root, references)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReferencePackError(f"Cannot load semantic-reference pack: {exc}") from exc
