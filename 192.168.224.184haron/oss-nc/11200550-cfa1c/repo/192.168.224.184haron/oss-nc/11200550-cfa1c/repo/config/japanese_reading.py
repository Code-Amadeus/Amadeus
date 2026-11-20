"""Pinned, model-free contract for the optional Japanese context-reading model."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
from pathlib import Path

PACK_ID = "japanese-context-reading"
PACK_TREE = "text/ja/context-reading"
VERSION = "2026.10.20.1"
SOURCE_REPOSITORY = "https://huggingface.co/tsukumijima/tsqyomi-models"
SOURCE_REVISION = "81add61ddba9669d328e307c883d05d77d60f5f4"
MODEL_HASHES = {
    "model.onnx": "0ff2950a6fda3befafd7792fd4b8141f3ab97756ec878057d03727c1adca2bd8",
    "tokenizer.json": "6a0734cf21c802169defaffe719bc2ef12bb9d0be37e54b61ed27aa89394723d",
    "metadata.json": "ccf2a795aeb846a281fbe639bc75ad3f74e0439c6eec356287819c74208386bc",
}
PACK_FILES = (*MODEL_HASHES, "provenance.json", "NOTICE.md", "MIT.txt")


class JapaneseReadingError(ValueError):
    pass


@dataclass(frozen=True)
class JapaneseReadingPack:
    root: Path
    identity: str


@lru_cache(maxsize=8)
def _checked_pack(root: Path, signatures: tuple) -> JapaneseReadingPack:
    for name, expected in MODEL_HASHES.items():
        with (root / name).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise JapaneseReadingError(f"Context-reading model hash mismatch: {name}")
    provenance = json.loads((root / "provenance.json").read_text(encoding="utf-8"))
    if (provenance.get("version") != VERSION
            or provenance.get("source") != {"repository": SOURCE_REPOSITORY, "revision": SOURCE_REVISION, "directory": "v5"}
            or provenance.get("sha256") != MODEL_HASHES):
        raise JapaneseReadingError("Context-reading model provenance differs from the pinned source")
    identity = hashlib.sha256(repr(("tsqyomi-v5.v1", MODEL_HASHES)).encode()).hexdigest()
    return JapaneseReadingPack(root, identity)


def load_reading_pack(root: Path) -> JapaneseReadingPack:
    root = Path(root).resolve()
    try:
        signatures = []
        for name in PACK_FILES:
            path = root / name
            if path.is_symlink() or not path.is_file() or not path.stat().st_size:
                raise JapaneseReadingError(f"Missing or empty context-reading pack member: {name}")
            info = path.stat()
            signatures.append((info.st_size, info.st_mtime_ns, info.st_ctime_ns))
        return _checked_pack(root, tuple(signatures))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise JapaneseReadingError(f"Cannot load context-reading pack: {exc}") from exc
