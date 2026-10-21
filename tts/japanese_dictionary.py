"""Optional Japanese data loading; source CSVs are never modified."""
from __future__ import annotations

import hashlib
import csv
import io
import logging
import os
from pathlib import Path
import re
import tempfile
from functools import lru_cache

from config.asset_paths import PROJECT_ROOT
from config.japanese_dictionary import (
    KANJI_COMPOUND, LATIN_WORD_BOUNDARY, PACK_TREE, dictionary_rows, latin_word_key, load_dictionary_pack,
)

logger = logging.getLogger(__name__)
_runtime_status = {"state": "not_loaded", "ready": False, "version": "", "identity": None,
                   "detail": "Japanese frontend has not loaded."}
_official_latin_words: frozenset[str] = frozenset()
_official_katakana_aliases: dict[str, str] = {}
_official_compound_paths: dict[str, tuple[str, tuple[str, ...]]] = {}
_official_base_owner = None

_LATIN_TOKEN = re.compile(
    r"(?:[0-9０-９]+['’＇−－-])*[0-9０-９]*[A-Za-zＡ-Ｚａ-ｚ][A-Za-zＡ-Ｚａ-ｚ0-9０-９]*"
    r"(?:['’＇−－-][A-Za-zＡ-Ｚａ-ｚ0-9０-９]+)*['’＇]?"
)


def latin_token_spans(text: str) -> list[tuple[int, int]]:
    """Share token identity with punctuation segmentation, including digits."""
    return [match.span() for match in _LATIN_TOKEN.finditer(text)]


def with_latin_word_boundaries(text: str, words: frozenset[str] | None = None) -> str:
    """Native-ignored spaces make official entries match complete Latin tokens.

    Apply after sentence/punctuation splitting. Both edges are necessary: an
    imported word must not consume either the prefix or suffix of another word.
    Only registered official words are folded and marked for their lowercase
    native rows. Other text keeps the original
    base, legacy and personal dictionary behavior, including measurement units.
    """
    words = _official_latin_words if words is None else words
    return _LATIN_TOKEN.sub(lambda match: (LATIN_WORD_BOUNDARY + match.group().lower() + LATIN_WORD_BOUNDARY
                                          if latin_word_key(match.group()) in words
                                          and not (_official_base_owner and _official_base_owner(match.group()))
                                          else match.group()), text)


def with_latin_readings(text: str, aliases: dict[str, str] | None = None) -> str:
    """Resolve only explicitly installed whole-word aliases before native G2P."""
    aliases = _official_katakana_aliases if aliases is None else aliases
    if not aliases:
        return text
    return _LATIN_TOKEN.sub(lambda m: (aliases[latin_word_key(m.group())]
                                      if latin_word_key(m.group()) in aliases
                                      and not (_official_base_owner and _official_base_owner(m.group()))
                                      else m.group()), text)


def prepare_japanese_text(text: str) -> str:
    return with_latin_word_boundaries(with_latin_readings(text))


def native_compound_path(jtalk, word: str, reading: str) -> tuple[str, ...] | None:
    """Select an existing native path, never synthesize cost/accent features."""
    for path in jtalk.run_mecab_nbest_features(word, 128):
        morphs = path["morphs"]
        if (all(not m["is_unknown"] and not m["is_ignored"] and m["dictionary_index"] == 0 for m in morphs)
                and "".join(m["surface"] for m in morphs) == word
                and "".join(m["features"][8] for m in morphs) == reading):
            return tuple(path["features"])
    return None


def run_lexical_frontend(jtalk, text: str, *, use_tsqyomi: bool = False) -> list[dict]:
    """Constrain only complete native morph spans with source-backed readings.

    Preserve the normal path when already correct, inside a larger lexeme, or
    owned by a user dictionary. Re-run native NJD on the complete feature list
    after a repair so accent chaining, numbers and particles remain contextual.
    """
    paths = {word: path for word, path in _official_compound_paths.items() if word in text}
    if not paths:
        return jtalk.run_frontend(text, use_tsqyomi=True) if use_tsqyomi else jtalk.run_frontend(text)
    nodes, morphs = jtalk.run_frontend_detailed(text, use_tsqyomi=use_tsqyomi)
    features, changed, i = [], False, 0
    max_length = max(map(len, paths))
    while i < len(morphs):
        surface, selected = "", None
        for end in range(i, len(morphs)):
            morph = morphs[end]
            if morph["is_ignored"] or morph["is_unknown"] or morph["dictionary_index"] != 0:
                break
            surface += morph["surface"]
            if len(surface) > max_length:
                break
            if surface in paths:
                reading, replacement = paths[surface]
                same_reading = "".join(m["features"][8] for m in morphs[i:end + 1]) == reading
                selected = (end + 1, None if same_reading else replacement)
        if selected and selected[1] is not None:
            i, replacement = selected
            features.extend(replacement)
            changed = True
        else:
            # A correct longer word owns its entire span. Do not reconsider
            # internal words with a different standalone reading (中立派/立派).
            end = selected[0] if selected else i + 1
            while i < end:
                if not morphs[i]["is_ignored"]:
                    buffer = io.StringIO()
                    csv.writer(buffer, lineterminator="").writerow(morphs[i]["features"])
                    features.append(buffer.getvalue())
                i += 1
    if not changed:
        return nodes
    return jtalk.apply_postprocessing(
        text, jtalk.run_njd_from_mecab(features),
        use_sudachi_kanji_yomi=not use_tsqyomi, predict_nani=not use_tsqyomi)


def _enabled() -> bool:
    from config.settings import ENABLE_JAPANESE_USER_DICTIONARY
    return ENABLE_JAPANESE_USER_DICTIONARY


def dictionary_runtime_status() -> dict:
    from tts.japanese_reading import reading_runtime_status
    status = dict(_runtime_status)
    reading = reading_runtime_status()
    status["reading_model"] = reading
    if status.get("identity") and reading["ready"]:
        status["identity"] = hashlib.sha256(repr((status["identity"], reading["identity"])).encode()).hexdigest()
    return status


def full_source_runtime_rows(pack, data: bytes, normalize_surface=lambda value: value) -> tuple[list[list[str]], dict[str, str]]:
    """Build the runtime view without deleting or rewriting the stored master."""
    overlay = (pack.root / "overlay.csv").read_bytes()
    if hashlib.sha256(overlay).hexdigest() != pack.overlay_sha256:
        raise ValueError("Dictionary overlay changed during loading; restart after installation finishes")
    supplemental = list(dictionary_rows(overlay))
    native = frozenset(pack.native_words)
    aliases = {latin_word_key(r[0]): r[12] for r in supplemental if latin_word_key(r[0]) not in native}
    excluded = frozenset(pack.excluded_rows)
    replaced = native | aliases.keys()
    rows = []
    for number, original in enumerate(dictionary_rows(data), 1):
        if number in excluded or latin_word_key(original[0]) in replaced:
            continue
        row = list(original)
        row[0] = LATIN_WORD_BOUNDARY + normalize_surface(row[0].strip(LATIN_WORD_BOUNDARY)) + LATIN_WORD_BOUNDARY
        rows.append(row)
    rows.extend(r for r in supplemental if latin_word_key(r[0]) in native)
    return rows, aliases


def native_spelling_owner(base):
    """The base lexicon owns its exact case before optional case folding.

    API is a known initialism even when api is an unrelated imported word.
    Query only matching optional tokens and cache native evidence, not guesses.
    """
    @lru_cache(maxsize=4096)
    def owns(word):
        _, morphs = base.run_mecab_detailed(word)
        tokens = [m for m in morphs if not m["is_ignored"]]
        return (len(tokens) == 1 and not tokens[0]["is_unknown"]
                and tokens[0]["surface"] == base.normalize_for_mecab(word))
    return owns


def configure_japanese_dictionary(jtalk, *, project_root: Path = PROJECT_ROOT, enabled: bool | None = None) -> dict:
    """Load independent sources without making optional failures fatal."""
    global _runtime_status, _official_latin_words, _official_katakana_aliases, _official_compound_paths, _official_base_owner
    _official_latin_words = frozenset()
    _official_katakana_aliases = {}
    _official_compound_paths = {}
    _official_base_owner = None
    state, version, identity = "invalid", "", None
    detail = "Optional Japanese dictionary unavailable."
    reset = None
    try:
        enabled = _enabled() if enabled is None else enabled
        reset = getattr(jtalk, "unset_user_dict", None)
        if not callable(reset):
            state = "unsupported"
            raise RuntimeError("Japanese user dictionaries require the pinned pyopenjtalk-plus API (unset_user_dict)")
        reset()
        project_root = Path(project_root).resolve()
        base = Path(os.fsdecode(jtalk.OPEN_JTALK_DICT_DIR))
        system = (base / "sys.dic").stat()
        native_key = (jtalk.__version__, str(base), system.st_size, system.st_mtime_ns,
                      "lexical-morph-boundaries.v6-full-source")
        compiled, digests, issues = [], [], []
        data_review = None
        cache = project_root / ".cache/japanese_dictionary"
        official = project_root / "assets" / PACK_TREE / "userdict.csv"
        legacy = project_root / "GPT_SoVITS/text/ja_userdic/userdict.csv"
        personal = project_root / "assets/text/ja/user.csv"
        sources = []
        if enabled:
            if official.parent.exists():
                sources.append(official)
            elif legacy.is_file():
                sources.append(legacy)
            if personal.is_file():
                sources.append(personal)
        for source in sources:
            label = source.relative_to(project_root).as_posix()
            pending_aliases = {}
            pending_review = None
            pending_owner = None
            try:
                pack = load_dictionary_pack(source.parent) if source == official else None
                data = source.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                if pack is not None:
                    if digest != pack.sha256:
                        raise ValueError("Dictionary changed during loading; restart after installation finishes")
                    if pack.format_version == 2:
                        for name, expected in pack.base_dictionary_hashes:
                            if hashlib.sha256((base / name).read_bytes()).hexdigest() != expected:
                                raise ValueError("Japanese base dictionary differs from this resource's ownership checks")
                        normalizer = jtalk.OpenJTalk(jtalk.OPEN_JTALK_DICT_DIR)
                        pending_owner = native_spelling_owner(normalizer)
                        native_rows, pending_aliases = full_source_runtime_rows(pack, data, normalizer.normalize_for_mecab)
                        buffer = io.StringIO(newline="")
                        csv.writer(buffer, lineterminator="\n").writerows(native_rows)
                        data = buffer.getvalue().encode("utf-8")
                        pending_review = {"source_rows": pack.entry_count, "reviewed_corrections": pack.reviewed_rows,
                                       "original_unverified": pack.entry_count - pack.reviewed_rows,
                                       "native_runtime_rows": len(native_rows), "aliases": len(pending_aliases)}
                    official_words = frozenset(latin_word_key(row[0]) for row in dictionary_rows(data)
                                               if row[0].startswith(LATIN_WORD_BOUNDARY)
                                               and row[0].endswith(LATIN_WORD_BOUNDARY))
                else:
                    for _ in dictionary_rows(data):
                        pass
                if pack is not None and pack.entry_mode == "katakana_alias":
                    # Re-enter the native Katakana lexicon in context. Copying
                    # one isolated entry's cost/accent can lose to a Latin split
                    # or select the wrong contextual accent (e.g. non, lean).
                    rows = list(dictionary_rows(data))
                    retained = frozenset(pack.native_words)
                    pending_aliases = {latin_word_key(row[0]): row[12] for row in rows
                                       if latin_word_key(row[0]) not in retained}
                    native_rows = [row for row in rows if latin_word_key(row[0]) in retained]
                    if not native_rows:
                        _official_katakana_aliases = pending_aliases
                        digests.append(pack.identity)
                        version = pack.version
                        continue
                    official_words &= retained
                    buffer = io.StringIO(newline="")
                    csv.writer(buffer, lineterminator="\n").writerows(native_rows)
                    data = buffer.getvalue().encode("utf-8")
                # Partition changes also change the compiled native content.
                native_digest = hashlib.sha256(data).hexdigest()
                key = hashlib.sha256(repr((native_key, native_digest)).encode()).hexdigest()
                target = cache / f"{key}.dict"
                if not target.is_file():
                    cache.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory(prefix="compile-", dir=cache) as temporary:
                        snapshot = Path(temporary) / "source.csv"
                        snapshot.write_bytes(data)
                        output = Path(temporary) / "user.dict"
                        jtalk.mecab_dict_index(str(snapshot), str(output))
                        try:
                            os.replace(output, target)
                        except OSError:
                            # A peer may already have published and mmap'ed the
                            # same content key on Windows. Reuse its file.
                            if not target.is_file():
                                raise
            except Exception as exc:
                issues.append(f"{label}: {exc}")
                logger.warning("[JapaneseDictionary] Skipped %s: %s", label, exc)
                continue
            try:
                jtalk.update_global_jtalk_with_user_dict([*compiled, str(target)])
            except Exception as exc:
                # Recovery failure escapes to the outer optional boundary:
                # never claim an identity for an unknown native selection.
                if compiled:
                    jtalk.update_global_jtalk_with_user_dict(compiled)
                else:
                    reset()
                issues.append(f"{label}: {exc}")
                logger.warning("[JapaneseDictionary] Skipped %s: %s", label, exc)
                continue
            compiled.append(str(target))
            digests.append(pack.identity if pack is not None else digest)
            if pack is not None:
                version = pack.version
                data_review = pending_review
                _official_latin_words = official_words
                _official_katakana_aliases = pending_aliases
                _official_base_owner = pending_owner
            elif source == legacy:
                version = "legacy-local"
            if source == personal:
                # Explicit personal spellings retain ownership of their input.
                for row in dictionary_rows(data):
                    _official_katakana_aliases.pop(latin_word_key(row[0]), None)
        for word, reading in _official_katakana_aliases.items():
            if KANJI_COMPOUND.fullmatch(word):
                path = native_compound_path(jtalk, word, reading)
                if path is None:
                    issues.append(f"No native path for compound: {word}")
                else:
                    _official_compound_paths[word] = (reading, path)
        identity = hashlib.sha256(repr((native_key, tuple(digests))).encode()).hexdigest()
        if compiled or _official_katakana_aliases:
            state = "ready"
            detail = f"Japanese dictionary {version or 'personal'} loaded ({len(digests)} source(s))."
            if data_review:
                detail += (f" {data_review['source_rows']} original rows preserved; "
                           f"{data_review['reviewed_corrections']} reading corrections; "
                           f"{data_review['original_unverified']} original rows remain unverified.")
            # Windows can retain files mmap'ed by another live process. Their
            # cleanup is retried on a later successful startup.
            for old in cache.glob("*.dict"):
                if str(old) not in compiled and len(old.stem) == 64 and all(c in "0123456789abcdef" for c in old.stem):
                    try:
                        old.unlink()
                    except OSError as exc:
                        logger.debug("[JapaneseDictionary] Cache still in use: %s (%s)", old.name, exc)
        elif not enabled:
            state, detail = "disabled", "Japanese user dictionaries are disabled; using the base dictionary."
        elif issues:
            state, detail = "invalid", "Using the base Japanese dictionary."
        else:
            state, detail = "not_installed", "Optional Japanese dictionary is not installed; using the base dictionary."
        if issues:
            detail += " Skipped " + "; ".join(issues)
    except Exception as exc:
        # Configuration, native API and reset errors belong to this optional
        # boundary. An unsupported runtime must still import the base frontend.
        identity = None
        _official_latin_words = frozenset()
        _official_katakana_aliases = {}
        _official_compound_paths = {}
        _official_base_owner = None
        state = "unsupported" if state == "unsupported" else "invalid"
        detail = str(exc)
        if callable(reset):
            try:
                reset()
            except Exception as cleanup:
                logger.warning("[JapaneseDictionary] Native reset failed: %s", cleanup)
        logger.warning("[JapaneseDictionary] Optional dictionary unavailable: %s", exc)
    _runtime_status = {"state": state, "ready": state == "ready", "version": version,
                       "identity": identity, "detail": detail}
    from tts.japanese_reading import configure_japanese_reading
    configure_japanese_reading(jtalk, project_root=project_root)
    return dictionary_runtime_status()
