"""Audit dictionary data without guessing or rewriting ambiguous word readings."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from functools import lru_cache
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import re
import sys
from typing import Callable
import unicodedata

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.tts_text_processor import convert_english_abbreviations_to_katakana  # noqa: E402
from config.japanese_dictionary import dictionary_rows  # noqa: E402

_KANA = re.compile(r"[\u30a1-\u30fa\u30fc]+")
_MORA_PHONES = frozenset({"a", "i", "u", "e", "o", "A", "I", "U", "E", "O", "N", "cl"})
# A screening rule, not an English-to-Japanese translator. Loanword adaptation
# can violate these correspondences. Exclude a flagged imported row rather than
# invent its replacement; keep every CMU pronunciation alternative eligible.
_FINAL_CONSONANTS = {
    "ズ": {"Z", "S"}, "ド": {"D", "T"}, "ング": {"NG"},
}
LATIN_WORD_COST_FLOOR = 10300


def word_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).translate(str.maketrans("’−", "'-")).casefold()


def cmu_pronunciations(data: bytes) -> dict[str, set[tuple[str, ...]]]:
    phones = defaultdict(set)
    for line in data.decode("utf-8").splitlines():
        fields = line.split()
        if fields and not fields[0].startswith(";;;"):
            phones[fields[0].split("(", 1)[0].casefold()].add(tuple(fields[1:]))
    return dict(phones)


def final_phone_risk(reading: str, pronunciations: set[tuple[str, ...]]) -> bool:
    phones = {tuple(re.sub(r"[012]$", "", p) for p in variant) for variant in pronunciations if variant}
    if not phones:
        return False
    for ending, expected in _FINAL_CONSONANTS.items():
        if reading.endswith(ending):
            return not any(p[-1] in expected for p in phones)
    return False


def native_base_word_lookup() -> Callable[[str], bool]:
    import pyopenjtalk

    # Independent base instance: installed official/personal dictionaries must
    # never decide which upstream entries the release builder accepts.
    jtalk = pyopenjtalk.OpenJTalk(pyopenjtalk.OPEN_JTALK_DICT_DIR)

    @lru_cache(maxsize=None)
    def known(word: str) -> bool:
        word = word_key(word)
        _, morphs = jtalk.run_mecab_detailed(word)
        tokens = [m for m in morphs if not m["is_ignored"]]
        return (len(tokens) == 1 and not tokens[0]["is_unknown"]
                and tokens[0]["surface"] == jtalk.normalize_for_mecab(word))

    return known


def native_base_hashes() -> dict[str, str]:
    import os
    import pyopenjtalk

    root = Path(os.fsdecode(pyopenjtalk.OPEN_JTALK_DICT_DIR))
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in ("sys.dic", "matrix.bin", "char.bin")}


def filter_extension_rows(rows: list[list[str]], phones: dict[str, set[tuple[str, ...]]],
                          base_word: Callable[[str], bool], *,
                          cost_floor: int = LATIN_WORD_COST_FLOOR) -> tuple[list[list[str]], dict]:
    """Yield known words to the base and quarantine suspect readings offline."""
    kept, excluded = [], []
    counts = Counter()
    direct_flags = [row for row in rows if final_phone_risk(row[11], phones.get(word_key(row[0]), set()))]
    suspect = {word_key(row[0]) for row in direct_flags}
    repriced = 0
    for number, row in enumerate(rows, 1):
        word = word_key(row[0])
        reasons = []
        if base_word(word):
            reasons.append("base_dictionary_word")
        # A contaminated surface can have several shifted alternatives. Do not
        # merely promote the next unverified variant (e.g. directly -> director).
        if word in suspect:
            reasons.append("cmu_final_phone_risk")
        if reasons:
            counts.update(reasons)
            excluded.append({"normalized_line": number, "surface": word, "reading": row[12],
                             "reasons": reasons})
        else:
            # Imported short words must not cheaply split a known base word.
            # Use one floor for plain Latin words; punctuation-bearing compounds
            # need their existing cost to compete with native boundary splits.
            if re.fullmatch(r"[a-z]+", word) and int(row[3]) < cost_floor:
                row = list(row)
                row[3] = str(cost_floor)
                repriced += 1
            kept.append(row)
    return kept, {"input_entries": len(rows), "output_entries": len(kept), "excluded_entries": len(excluded),
                  "reason_counts": dict(counts), "latin_word_cost_floor": cost_floor,
                  "direct_final_phone_flags": len(direct_flags),
                  "direct_final_phone_suffix_counts": dict(Counter(
                      next(ending for ending in _FINAL_CONSONANTS if row[11].endswith(ending))
                      for row in direct_flags)),
                  "latin_word_costs_raised": repriced, "excluded": excluded}


def _without_reading_separators(value: str) -> str:
    # These delimit written name components; they are not OpenJTalk morae.
    # Apply only when every remaining character is already Katakana. Never
    # discard letters, kanji, annotations or untranslated inflection endings.
    joined = "".join(char for char in value
                     if not char.isspace() and unicodedata.category(char) != "Cf" and char not in "・=-'")
    return joined if _KANA.fullmatch(joined) else value


def native_mora_counter() -> tuple[Callable[[str], int], str]:
    import pyopenjtalk

    template = pyopenjtalk.run_frontend("テスト", use_sudachi_kanji_yomi=False, predict_nani=False)[0]

    @lru_cache(maxsize=None)
    def count(pronunciation: str) -> int:
        # Feed one explicit pronunciation to the native label builder. Running
        # g2p(kana) would tokenize it again and can split シュ into シ + ュ,
        # falsely counting two morae. Lowercase n is a consonant; only N is a mora.
        feature = {**template, "read": pronunciation, "pron": pronunciation, "acc": 0, "mora_size": 1}
        labels = pyopenjtalk.make_label([feature])
        phones = (re.search(r"-(.*?)\+", label).group(1) for label in labels)
        return sum(phone in _MORA_PHONES for phone in phones)

    return count, version("pyopenjtalk-plus")


def native_surface_normalizer() -> Callable[[str], str]:
    import pyopenjtalk

    jtalk = pyopenjtalk.OpenJTalk(pyopenjtalk.OPEN_JTALK_DICT_DIR)
    return jtalk.normalize_for_mecab


def normalize_rows(rows: list[list[str]], count_mora: Callable[[str], int | None],
                   normalize_surface: Callable[[str], str] | None = None) -> tuple[list[list[str]], dict]:
    """Apply representation repairs; preserve lexical alternatives and accent nuclei."""
    cleaned, seen = [], set()
    stats = {"unicode_normalized_rows": 0, "literal_initialism_readings_repaired": 0,
             "reading_separators_normalized": 0, "surfaces_normalized": 0,
             "short_latin_entries_excluded": 0,
             "mora_counts_repaired": 0, "exact_duplicates_removed": 0}
    unresolved = []
    for number, original in enumerate(rows, 1):
        row = list(original)
        # The Japanese base dictionary owns letters and short Latin tokens,
        # including measurement suffixes. Imported English fragments must not
        # displace cm/kg/km or supply unrelated readings for isolated m/w/h.
        if re.fullmatch(r"[A-Za-z]{1,2}", unicodedata.normalize("NFKC", row[0])):
            stats["short_latin_entries_excluded"] += 1
            continue
        row[10:13] = [unicodedata.normalize("NFC", value) for value in row[10:13]]
        stats["unicode_normalized_rows"] += row != original
        joined = [_without_reading_separators(value) for value in row[10:13]]
        if joined != row[10:13] and _KANA.fullmatch(joined[2]):
            actual = count_mora(joined[2])
            accent = int(row[13].split("/")[0])
            if actual is None or 0 < actual and accent <= actual:
                row[10:13] = joined
                stats["reading_separators_normalized"] += 1
        literal = unicodedata.normalize("NFKC", row[12])
        if (re.fullmatch(r"[A-Z]{2,}", literal)
                and literal.casefold() == unicodedata.normalize("NFKC", row[0]).casefold()):
            # An untranslated initialism is data, not a new word-specific rule.
            translated = convert_english_abbreviations_to_katakana(literal)
            if _KANA.fullmatch(translated):
                row[10:13] = [translated] * 3
                stats["literal_initialism_readings_repaired"] += 1
        if _KANA.fullmatch(row[12]):
            actual = count_mora(row[12])
            accent, declared = map(int, row[13].split("/"))
            if actual is not None and actual != declared:
                if 0 < actual and accent <= actual:
                    row[13] = f"{accent}/{actual}"
                    stats["mora_counts_repaired"] += 1
                else:
                    unresolved.append({"line": number, "surface": row[0], "pronunciation": row[12],
                                       "reason": "accent_needs_review", "declared": row[13], "native_mora": actual})
        else:
            unresolved.append({"line": number, "surface": row[0], "pronunciation": row[12],
                               "reason": "non_kana_reading_needs_review"})
        # Surface spelling is a representation rule for every entry, including
        # unchanged contractions/compounds and readings awaiting lexical review.
        if normalize_surface is not None:
            surface = normalize_surface(row[0])
            stats["surfaces_normalized"] += surface != row[0]
            row[0] = surface
        entry = tuple(row)
        if entry in seen:
            stats["exact_duplicates_removed"] += 1
        else:
            seen.add(entry)
            cleaned.append(row)
    return cleaned, {**stats, "input_entries": len(rows), "output_entries": len(cleaned),
                     "unresolved_entries": len(unresolved), "unresolved": unresolved}


def audit_rows(rows: list[list[str]], cmudict: Path, count_mora: Callable[[str], int | None],
               normalize_surface: Callable[[str], str] | None = None,
               base_word: Callable[[str], bool] | None = None) -> dict:
    words = defaultdict(list)
    for number, row in enumerate(rows, 1):
        key = word_key(row[0])
        words[key].append((number, row))
    cmu_data = Path(cmudict).read_bytes().replace(b"\r\n", b"\n")
    phones = cmu_pronunciations(cmu_data)
    multiple = [{"surface": word, "readings": sorted({row[12] for _, row in entries}),
                 "lines": [line for line, _ in entries]}
                for word, entries in words.items() if len({row[12] for _, row in entries}) > 1]
    candidates = []
    for word, entries in words.items():
        for suffix in ("s", "'s"):
            plural = word + suffix
            pairs = [(base, form) for base in phones.get(word, ()) for form in phones.get(plural, ())
                     if form in {base + ("S",), base + ("Z",)}]
            if not pairs:
                continue
            for line, row in entries:
                matches = [n for n, other in words.get(plural, ()) if row[12] == other[12]]
                if matches:
                    candidates.append({"surface": word, "line": line, "reading": row[12],
                                       "inflected_surface": plural, "inflected_lines": matches,
                                       "cmu_phones": [list(pair) for pair in pairs[0]],
                                       "disposition": "review_only; Japanese loanword forms may legitimately coincide"})
    _, normalization = normalize_rows(rows, count_mora, normalize_surface)
    duplicates = Counter(tuple(row) for row in rows)
    _, extension = filter_extension_rows(rows, phones, base_word or (lambda _: False))
    return {"entries": len(rows), "distinct_surfaces": len(words),
            "cmudict_sha256": hashlib.sha256(cmu_data).hexdigest(),
            "exact_duplicate_rows": sum(count - 1 for count in duplicates.values()),
            "multiple_reading_surfaces": len(multiple), "multiple_readings": multiple,
            "inflection_candidate_count": len(candidates), "inflection_candidates": candidates,
            "extension_filter": extension,
            "normalization": normalization,
            "limitations": "Candidates do not prove lexical errors. Legitimate heteronyms and loanword inflections are retained. No LLM, network, waveform synthesis or semantic classifier is used."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--cmudict", type=Path, default=PROJECT_ROOT / "GPT_SoVITS/text/cmudict.rep")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    count, version = native_mora_counter()
    source_data = args.source.read_bytes()
    report = audit_rows(list(dictionary_rows(source_data)), args.cmudict, count, native_surface_normalizer(),
                        native_base_word_lookup())
    report["base_dictionary_sha256"] = native_base_hashes()
    report["source_sha256"] = hashlib.sha256(source_data).hexdigest()
    report["native_runtime"] = version
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("entries", "exact_duplicate_rows", "multiple_reading_surfaces", "inflection_candidate_count", "native_runtime")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
