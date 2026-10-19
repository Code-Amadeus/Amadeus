"""Model-free data contract for the optional Japanese dictionary asset pack."""
from __future__ import annotations

import csv
from dataclasses import dataclass
import hashlib
import io
import json
from functools import lru_cache
from pathlib import Path
from typing import Iterator
import unicodedata

PACK_ID = "japanese-pronunciation"
PACK_TREE = "text/ja/english"
LATIN_WORD_BOUNDARY = "\u3000"
PACK_FILES = (
    "userdict.csv", "provenance.json", "NOTICE.md",
    "GPT-SoVITS-MIT.txt", "CMUdict-BSD-2-Clause.txt", "Amadeus-AGPL-3.0.txt",
)


def latin_word_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).translate(str.maketrans("’−", "'-")).casefold().strip(" ")


class JapaneseDictionaryError(ValueError):
    pass


def dictionary_rows(data: bytes) -> Iterator[list[str]]:
    """Check the native 15-column OpenJTalk CSV without importing model extras."""
    if data.startswith(b"\xef\xbb\xbf"):
        raise JapaneseDictionaryError("Dictionary CSV has a UTF-8 BOM; save as UTF-8 without BOM so the first entry can match")
    try:
        count = 0
        for number, row in enumerate(csv.reader(io.StringIO(data.decode("utf-8")), strict=True), 1):
            if len(row) != 15 or not row[0] or not row[11] or not row[12]:
                raise JapaneseDictionaryError(f"Invalid dictionary entry at line {number}: expected 15 columns and a reading")
            try:
                for value in row[1:4]:
                    if value not in {"", "*"}:
                        int(value)
                accent, mora = map(int, row[13].split("/"))
                if not 0 <= accent <= mora or mora <= 0:
                    raise ValueError
            except ValueError as exc:
                raise JapaneseDictionaryError(f"Invalid dictionary cost/accent at line {number}") from exc
            count += 1
            yield row
        if not count:
            raise JapaneseDictionaryError("Dictionary CSV is empty")
    except (UnicodeError, csv.Error) as exc:
        raise JapaneseDictionaryError(f"Cannot read dictionary CSV: {exc}") from exc


@dataclass(frozen=True)
class JapaneseDictionaryPack:
    root: Path
    version: str
    sha256: str
    entry_count: int

    def files(self) -> tuple[Path, ...]:
        return tuple(self.root / name for name in PACK_FILES)


def _validate_dictionary_pack(root: Path) -> JapaneseDictionaryPack:
    try:
        for name in PACK_FILES:
            path = root / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
                raise JapaneseDictionaryError(f"Missing or empty dictionary pack member: {name}")
        raw = json.loads((root / "provenance.json").read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("language") != "ja" or not isinstance(raw.get("version"), str) or not raw["version"].strip():
            raise JapaneseDictionaryError("Dictionary provenance requires a version and Japanese language")
        data = (root / "userdict.csv").read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if raw.get("sha256") != digest:
            raise JapaneseDictionaryError("Dictionary content does not match its provenance hash")
        count = sum(1 for _ in dictionary_rows(data))
        if raw.get("entry_count") != count:
            raise JapaneseDictionaryError("Dictionary content does not match its provenance entry count")
        source = raw.get("source")
        if not isinstance(source, dict) or any(not source.get(key) for key in ("repository", "revision", "path", "sha256")):
            raise JapaneseDictionaryError("Dictionary provenance requires a pinned source and source hash")
        return JapaneseDictionaryPack(root, raw["version"], digest, count)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise JapaneseDictionaryError(f"Cannot load dictionary pack: {exc}") from exc


@lru_cache(maxsize=8)
def _cached_pack(root: Path, signatures: tuple) -> tuple[JapaneseDictionaryPack | None, str]:
    # Cache failures as values too: lru_cache does not retain raised exceptions.
    try:
        return _validate_dictionary_pack(root), ""
    except JapaneseDictionaryError as exc:
        return None, str(exc)


def load_dictionary_pack(root: Path) -> JapaneseDictionaryPack:
    root = Path(root).resolve()
    try:
        signatures = []
        for name in PACK_FILES:
            stat = (root / name).lstat()
            signatures.append((stat.st_mode, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    except OSError as exc:
        raise JapaneseDictionaryError(f"Cannot load dictionary pack: {exc}") from exc
    pack, error = _cached_pack(root, tuple(signatures))
    if error:
        raise JapaneseDictionaryError(error)
    return pack
