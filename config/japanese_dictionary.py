"""Model-free data contract for the optional Japanese dictionary asset pack."""
from __future__ import annotations

import csv
from dataclasses import dataclass
import hashlib
import io
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Iterator
import unicodedata

PACK_ID = "japanese-pronunciation"
PACK_TREE = "text/ja/english"
BUNDLED_TREE = "bundled/japanese-pronunciation"
BUNDLED_ENV = "AMADEUS_BUNDLED_JAPANESE_DICTIONARY"
LATIN_WORD_BOUNDARY = "\u3000"
KANJI_COMPOUND = re.compile(r"[\u3400-\u9fff々]{2,}")
KATAKANA_READING = re.compile(r"[\u30a1-\u30fa\u30fc]+")
PACK_FILES = (
    "userdict.csv", "provenance.json", "NOTICE.md",
    "GPT-SoVITS-MIT.txt", "CMUdict-BSD-2-Clause.txt", "Amadeus-AGPL-3.0.txt",
)
FULL_SOURCE_FILES = ("source.csv", "overlay.csv", "OpenJTalk-COPYING.txt")


def dictionary_pack_root(asset_root: Path) -> Path:
    """Explicit independent installs own selection; app updates replace only the default.

    An incomplete explicit installation remains visible as an error instead of
    silently reverting a user's update or rollback to the bundled version.
    """
    installed = asset_root / PACK_TREE
    if installed.exists():
        return installed
    supplied = os.environ.get(BUNDLED_ENV, "")
    bundled = Path(supplied) if supplied else asset_root / BUNDLED_TREE
    return bundled if bundled.exists() else installed


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
    entry_mode: str = "mecab"
    native_words: tuple[str, ...] = ()
    format_version: int = 1
    identity: str = ""
    excluded_rows: tuple[int, ...] = ()
    overlay_sha256: str = ""
    reviewed_rows: int = 0
    base_dictionary_hashes: tuple[tuple[str, str], ...] = ()

    def files(self) -> tuple[Path, ...]:
        names = PACK_FILES + (FULL_SOURCE_FILES if self.format_version == 2 else ())
        return tuple(self.root / name for name in names)


def _checked_rows(raw: dict, rows: list[list[str]]) -> tuple[str, tuple[str, ...]]:
    """Exact reading admission for newly reviewed entries, never the baseline."""
    entry_mode = raw.get("entry_mode", "mecab")
    if entry_mode not in {"mecab", "katakana_alias"}:
        raise JapaneseDictionaryError("Unsupported Japanese dictionary entry mode")
    native_words = raw.get("native_words", [])
    if (not isinstance(native_words, list)
            or any(not isinstance(w, str) or not w or latin_word_key(w) != w for w in native_words)
            or len(native_words) != len(set(native_words))
            or native_words and entry_mode != "katakana_alias"):
        raise JapaneseDictionaryError("Invalid retained native-word partition")
    native_words = frozenset(native_words)
    release_review = raw.get("release_review")
    review = release_review.get("accepted_readings") if isinstance(release_review, dict) else None
    if not isinstance(review, dict) or not review:
        raise JapaneseDictionaryError("Reviewed entries require their exact checked reading ledger")
    if any(not isinstance(values, list) or not values or
           any(not isinstance(value, str) or not value for value in values) for values in review.values()):
        raise JapaneseDictionaryError("Checked readings must be nonempty lists of exact readings")
    aliases, observed = {}, set()
    for row in rows:
        word = latin_word_key(row[0])
        if row[11] not in review.get(word, ()) or row[12] not in review.get(word, ()):
            raise JapaneseDictionaryError(f"Official dictionary contains an unchecked word or reading: {word}")
        if entry_mode != "katakana_alias":
            continue
        if word in native_words:
            observed.add(word)
            continue
        latin = (re.fullmatch(r"[a-z]{3,}(?:-[a-z]{3,})*", word)
                 and row[0].startswith(LATIN_WORD_BOUNDARY) and row[0].endswith(LATIN_WORD_BOUNDARY))
        if (not (latin or KANJI_COMPOUND.fullmatch(row[0])) or row[11] != row[12]
                or not KATAKANA_READING.fullmatch(row[12])):
            raise JapaneseDictionaryError("Lexical aliases require whole Latin words or complete Kanji compounds and one explicit reading")
        if word in aliases and aliases[word] != row[12]:
            raise JapaneseDictionaryError(f"Ambiguous Katakana alias: {word}")
        aliases[word] = row[12]
    if observed != native_words:
        raise JapaneseDictionaryError("Retained native words must exist in the dictionary data")
    return entry_mode, tuple(sorted(native_words))


def _full_source_review(root: Path, raw: dict, rows: list[list[str]]) -> dict:
    """Every baseline row is retained verbatim or has an exact reviewed delta.

    Hash integrity is not a pronunciation verdict. Unchanged source rows are
    explicitly unverified; only the listed changes claim reading review.
    """
    source_data = (root / "source.csv").read_bytes()
    source = raw["source"]
    if hashlib.sha256(source_data).hexdigest() != source["sha256"]:
        raise JapaneseDictionaryError("Preserved source hash mismatch")
    originals = list(dictionary_rows(source_data))
    if len(originals) != source.get("entry_count") or len(rows) != len(originals):
        raise JapaneseDictionaryError("Full dictionary must preserve every source row")
    review = raw.get("source_review")
    if not isinstance(review, dict) or review.get("unchanged_status") != "original_unverified":
        raise JapaneseDictionaryError("Full dictionary must label unchanged rows original_unverified")
    changes = review.get("changes")
    exclusions = review.get("runtime_exclusions")
    if not isinstance(changes, list) or not isinstance(exclusions, list):
        raise JapaneseDictionaryError("Full dictionary requires change and runtime disposition records")
    indexed, excluded = {}, {}
    for record in changes:
        number = record.get("source_row")
        if type(number) is not int or not 1 <= number <= len(rows) or number in indexed:
            raise JapaneseDictionaryError("Invalid or duplicate reviewed source row")
        if (record.get("status") != "reviewed_reading_correction" or not record.get("reason")
                or record.get("accent_evidence") not in {"native_lexeme_features", "unidic_lexical_accent", "source_accent_retained_pending_review"}):
            raise JapaneseDictionaryError("Correction requires reading and separate accent evidence")
        indexed[number] = record
    allowed_reasons = {"base_owned", "short_token_native_owner", "context_sensitive", "pending_accent",
                       "cmu_final_phone_risk", "unreviewed_alternative", "known_bad_reading",
                       "native_reading_requires_review"}
    for record in exclusions:
        number = record.get("source_row")
        if (type(number) is not int or not 1 <= number <= len(rows) or number in excluded
                or record.get("reason") not in allowed_reasons
                or record.get("word") != latin_word_key(originals[number - 1][0])):
            raise JapaneseDictionaryError("Invalid runtime source disposition")
        excluded[number] = record
    for number, (before, after) in enumerate(zip(originals, rows), 1):
        change = indexed.get(number)
        if change is None:
            if before != after:
                raise JapaneseDictionaryError(f"Unreviewed source row changed: {number}")
        elif (before == after or change.get("before_columns") != before or change.get("after_columns") != after
              or before[0] != after[0] or before[3] != after[3]):
            raise JapaneseDictionaryError(f"Stale correction or changed source identity/cost: {number}")
    overlay_data = (root / "overlay.csv").read_bytes()
    overlay = raw.get("overlay")
    if not isinstance(overlay, dict) or hashlib.sha256(overlay_data).hexdigest() != overlay.get("sha256"):
        raise JapaneseDictionaryError("Reviewed overlay hash mismatch")
    overlay_rows = list(dictionary_rows(overlay_data))
    if len(overlay_rows) != overlay.get("entry_count"):
        raise JapaneseDictionaryError("Reviewed overlay count mismatch")
    mode, native_words = _checked_rows(overlay, overlay_rows)
    if mode != "katakana_alias":
        raise JapaneseDictionaryError("Full-source overlay requires explicit native/alias partition")
    aliases = {latin_word_key(r[0]): r[12] for r in overlay_rows if latin_word_key(r[0]) not in native_words}
    reviewed_readings = {}
    for row in overlay_rows:
        reviewed_readings.setdefault(latin_word_key(row[0]), set()).add(row[12])
    for change in changes:
        row = change["after_columns"]
        reviewed_readings.setdefault(latin_word_key(row[0]), set()).add(row[12])
    active_readings = {}
    replaced_words = {latin_word_key(row[0]) for row in overlay_rows}
    for number, row in enumerate(rows, 1):
        word = latin_word_key(row[0])
        if number not in excluded and word not in replaced_words:
            if not KATAKANA_READING.fullmatch(row[12]):
                raise JapaneseDictionaryError(f"Native pronunciation requires normalization/review: {word}")
            active_readings.setdefault(word, set()).add(row[12])
    for word, active in active_readings.items():
        approved = reviewed_readings.get(word)
        if approved is None and len(active) != 1 or approved is not None and not active <= approved:
            raise JapaneseDictionaryError(f"Unresolved alternate runtime readings: {word}")
    context_words = {r["word"] for r in exclusions if r["reason"] == "context_sensitive"}
    if context_words & {latin_word_key(r[0]) for r in overlay_rows}:
        raise JapaneseDictionaryError("Overlay would bypass a context-sensitive deferral")
    if any(latin_word_key(row[0]) in context_words and number not in excluded
           for number, row in enumerate(rows, 1)):
        raise JapaneseDictionaryError("Context deferral must preserve all alternate source rows from forced runtime selection")
    for number, change in indexed.items():
        if change["accent_evidence"] == "source_accent_retained_pending_review":
            word = latin_word_key(rows[number - 1][0])
            if number not in excluded and aliases.get(word) != rows[number - 1][12]:
                raise JapaneseDictionaryError("Unverified copied accent must not enter native runtime")
    base = raw.get("base_dictionary_sha256")
    if (not isinstance(base, dict) or set(base) != {"sys.dic", "matrix.bin", "char.bin"}
            or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in base.values())):
        raise JapaneseDictionaryError("Full-source runtime ownership requires pinned base dictionaries")
    return dict(entry_mode="full_source", native_words=native_words, excluded_rows=tuple(sorted(excluded)),
                overlay_sha256=overlay["sha256"], reviewed_rows=len(indexed),
                base_dictionary_hashes=tuple(sorted(base.items())))


def _validate_dictionary_pack(root: Path) -> JapaneseDictionaryPack:
    try:
        metadata = (root / "provenance.json").read_bytes()
        raw = json.loads(metadata)
        if not isinstance(raw, dict) or raw.get("language") != "ja" or not isinstance(raw.get("version"), str) or not raw["version"].strip():
            raise JapaneseDictionaryError("Dictionary provenance requires a version and Japanese language")
        format_version = raw.get("format_version", 1)
        if type(format_version) is not int or format_version not in {1, 2}:
            raise JapaneseDictionaryError("Unsupported Japanese dictionary format; update the application")
        names = PACK_FILES + (FULL_SOURCE_FILES if format_version == 2 else ())
        for name in names:
            path = root / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
                raise JapaneseDictionaryError(f"Missing or empty dictionary pack member: {name}")
        data = (root / "userdict.csv").read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if raw.get("sha256") != digest:
            raise JapaneseDictionaryError("Dictionary content does not match its provenance hash")
        rows = list(dictionary_rows(data))
        if raw.get("entry_count") != len(rows):
            raise JapaneseDictionaryError("Dictionary content does not match its provenance entry count")
        source = raw.get("source")
        if not isinstance(source, dict) or any(not source.get(key) for key in ("repository", "revision", "path", "sha256")):
            raise JapaneseDictionaryError("Dictionary provenance requires a pinned source and source hash")
        if format_version == 2:
            fields = _full_source_review(root, raw, rows)
        else:
            if raw.get("entry_mode", "mecab") != "mecab":
                raise JapaneseDictionaryError("Katakana aliases require format 2 and its reviewed overlay")
            mode, words = _checked_rows(raw, rows)
            fields = dict(entry_mode=mode, native_words=words)
        return JapaneseDictionaryPack(root, raw["version"], digest, len(rows),
                                      format_version=format_version,
                                      identity=hashlib.sha256(metadata).hexdigest(), **fields)
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
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
        # All members affecting data/ownership participate in memoization.
        raw = json.loads((root / "provenance.json").read_bytes())
        names = PACK_FILES + (FULL_SOURCE_FILES if isinstance(raw, dict) and raw.get("format_version") == 2 else ())
        signatures = []
        for name in names:
            stat = (root / name).lstat()
            signatures.append((stat.st_mode, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    except (OSError, ValueError) as exc:
        raise JapaneseDictionaryError(f"Cannot load dictionary pack: {exc}") from exc
    pack, error = _cached_pack(root, tuple(signatures))
    if error:
        raise JapaneseDictionaryError(error)
    return pack
