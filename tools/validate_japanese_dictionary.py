"""Offline native release gate for a prepared Japanese pronunciation pack."""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.japanese_dictionary import LATIN_WORD_BOUNDARY, PACK_TREE, dictionary_rows, load_dictionary_pack  # noqa: E402
from tools.audit_japanese_dictionary import (  # noqa: E402
    cmu_pronunciations, filter_extension_rows, native_base_hashes, native_base_word_lookup, word_key,
)
from tools.tts_text_processor import correct_pronunciation_for_tts  # noqa: E402
from tts.japanese_dictionary import configure_japanese_dictionary, with_latin_word_boundaries  # noqa: E402

# Numeric Latin suffixes, inflected initialisms/names, native Japanese and
# punctuation exercise both sides of the optional pack's authority boundary.
BASE_SAMPLES = (
    "身長は170cmです", "体重は50kgです", "距離は3kmです", "長さは10mmです",
    "量は100mlです", "出力は60kWです", "出力は60MWです", "5mを歩く",
    "7hです", "100msです", "周波数は50Hzです", "室温は20℃です", "１００ＭＬです",
    "APIsを使う", "GPUsを使う", "URLsを使う", "OpenAI's API", "Claude’s API", "GitHub's API",
    "今日は生ビールを飲みます。", "生きることは大切です。", "生徒が学校へ行く。",
    "昨日は雨が降った。", "明日は晴れるでしょう。", "東京駅で電車に乗る。",
    "日本橋を渡ります。", "日本語を勉強しています。", "彼は上手に歌います。",
    "部屋の上手に立つ。", "人気のある店です。", "人の気配がない。",
    "今日の市場はにぎやかです。", "市場価格を確認する。", "一日中働きました。",
    "四月一日に出発します。", "大人になった。", "小豆を煮る。", "明後日にまた会いましょう。",
    "紅茶を飲みたい。", "行ったことがあります。", "銀行へ行く。", "この行を読みます。",
    "重い荷物を運ぶ。", "荷物を重ねます。", "橋の端で箸を使う。", "雨が降り、飴を買った。",
    "花が咲き、鼻がむずむずする。", "新しい本を読む。", "研究結果を発表します。",
    "音声合成の品質を確認する。", "こんにちは、元気ですか。", "猫-犬。",
)
CONTRACTIONS = ("don't", "won't", "I'm", "I've", "we're", "they're", "isn't", "can't", "wouldn't")
UNIT_SAMPLES = tuple("100" + unit + "です" for unit in (
    "g", "mg", "kg", "t", "m", "cm", "mm", "km", "ml", "mL", "L", "kW", "MW", "W", "V", "mV",
    "A", "mA", "Hz", "kHz", "MHz", "GHz", "s", "ms", "ns", "kB", "MB", "GB", "TB", "dB", "rpm", "℃",
))
REPORTED_CONTEXTS = {
    "Helpを押して。": "help", "このbugからlearnしましょう。": "learn", "Twoつ目の方法。": "two",
    "Python with Docker。": "with", "Productionに出す。": "production", "モデルのresetをする。": "reset",
    **{f"この{word}を確認して。": word for word in ("model", "welcome", "directly", "reset", "deadline")},
}


def validate(project_root: Path) -> dict:
    import pyopenjtalk
    sys.path.insert(0, str(ROOT / "GPT_SoVITS"))
    from text import japanese
    from text.cleaner import clean_text

    def production_phones(text: str) -> list[str]:
        return clean_text(correct_pronunciation_for_tts(text), "ja", "v2")[0]
    project_root = project_root.resolve()
    pack = load_dictionary_pack(project_root / "assets" / PACK_TREE)
    rows = list(dictionary_rows((pack.root / "userdict.csv").read_bytes()))
    metadata = json.loads((pack.root / "provenance.json").read_text(encoding="utf-8"))
    policy = metadata["extension_filter"]
    if native_base_hashes() != policy["base_dictionary_sha256"]:
        raise RuntimeError("Release gate requires the recipe's base dictionary")
    base_word = native_base_word_lookup()
    cmu_data = (ROOT / policy["cmudict_path"]).read_bytes().replace(b"\r\n", b"\n")
    if hashlib.sha256(cmu_data).hexdigest() != policy["cmudict_sha256"]:
        raise RuntimeError("Release gate requires the recipe's CMU screening data")
    cmu = cmu_pronunciations(cmu_data)
    correction_data = (ROOT / "tools/data/japanese_dictionary_corrections.csv").read_bytes().replace(b"\r\n", b"\n")
    if hashlib.sha256(correction_data).hexdigest() != metadata["corrections_sha256"]:
        raise RuntimeError("Release gate requires the pack's correction recipe")
    corrected = list(dictionary_rows(correction_data))
    excluded = policy["results"]["excluded"]
    _, screening = filter_extension_rows(rows, cmu, base_word)
    vocabulary_data = json.loads((ROOT / "tools/data/japanese_dictionary_common_words.json").read_text(encoding="utf-8"))
    common = vocabulary_data["words"]
    base_owned = sorted({item["surface"] for item in excluded if "base_dictionary_word" in item["reasons"]})
    jtalk = pyopenjtalk.OpenJTalk(pyopenjtalk.OPEN_JTALK_DICT_DIR)
    surface = jtalk.normalize_for_mecab

    def native_text(text: str) -> str:
        return with_latin_word_boundaries(correct_pronunciation_for_tts(text).lower())

    def lexical_surface(value: str) -> str:
        return value.strip(LATIN_WORD_BOUNDARY)

    def target_readings(text: str, word: str) -> list[str]:
        return [f["read"] for f in pyopenjtalk.run_frontend(native_text(text))
                if lexical_surface(f["string"]) == surface(word)]
    expected = defaultdict(set)
    for row in rows:
        expected[lexical_surface(row[0])].add(row[11])
    failures, contexts, frontend_words = [], 0, 0
    if screening["excluded_entries"]:
        failures.append({"kind": "unscreened_entries", "entries": screening["excluded"]})
    if screening["latin_word_boundaries_added"]:
        failures.append({"kind": "missing_word_boundaries", "count": screening["latin_word_boundaries_added"]})
    try:
        configure_japanese_dictionary(pyopenjtalk, project_root=project_root, enabled=False)
        baseline = {text: production_phones(text) for text in BASE_SAMPLES}
        unit_baseline = {text: production_phones(text) for text in UNIT_SAMPLES}
        # Compare every base-owned imported surface, not just a hand-picked
        # sample. Include Japanese contexts so cheap prefix splits cannot pass.
        base_controls = {prefix + word + suffix for word in base_owned
                         for prefix, suffix in (("", ""), ("", "を確認する。"), ("これは", "です。"))}
        english_controls = base_controls | set(common) | set(REPORTED_CONTEXTS)
        space_controls = base_controls | set(BASE_SAMPLES) | set(UNIT_SAMPLES) | set(vocabulary_data["base_fallback_words"])
        english_baseline = {text: production_phones(text) for text in sorted(english_controls)}
        context_baseline = {text: target_readings(text, word) for text, word in REPORTED_CONTEXTS.items()}
        status = configure_japanese_dictionary(pyopenjtalk, project_root=project_root, enabled=True)
        if not status["ready"]:
            raise RuntimeError(status["detail"])
        boundary_checks = 0
        for text in sorted(space_controls):
            prepared = correct_pronunciation_for_tts(text).lower()
            bounded = with_latin_word_boundaries(prepared)
            boundary_checks += 1
            if bounded != prepared:
                failures.append({"kind": "unexpected_base_token_boundary", "text": text})
        for text, before in baseline.items():
            after = production_phones(text)
            if after != before:
                failures.append({"kind": "base_regression", "text": text, "before": before, "after": after})
        unit_prosody_changes = []
        without_prosody = lambda phones: [phone for phone in phones if phone not in {"[", "]", "#"}]
        for text, before in unit_baseline.items():
            after = production_phones(text)
            if without_prosody(after) != without_prosody(before):
                failures.append({"kind": "unit_phonemes", "text": text, "before": before, "after": after})
            elif after != before:
                # A different accent boundary alone does not establish a wrong
                # reading. Record it for listening review without inventing an
                # accent correction or a per-unit exclusion.
                unit_prosody_changes.append({"text": text, "before": before, "after": after})
        vocabulary = []
        quarantined = {item["surface"] for item in excluded} - {word_key(row[0]) for row in rows}
        for text, before in english_baseline.items():
            after = production_phones(text)
            must_preserve = text in base_controls or text in vocabulary_data["base_fallback_words"]
            if must_preserve and after != before:
                failures.append({"kind": "english_base_regression", "text": text, "before": before, "after": after})
            if text in common:
                observed = pyopenjtalk.run_frontend(native_text(text))
                reading = vocabulary_data["reviewed_readings"].get(text)
                if reading and [f["read"] for f in observed] != [reading]:
                    failures.append({"kind": "reviewed_english_reading", "text": text, "expected": reading,
                                     "observed": observed})
                vocabulary.append({"word": text, "base_owned": word_key(text) in base_owned,
                                   "quarantined": word_key(text) in quarantined,
                                   "changed": after != before, "readings": [f["read"] for f in observed]})
        sentence_results = []
        for text, word in REPORTED_CONTEXTS.items():
            before, after = context_baseline[text], target_readings(text, word)
            if len(before) != 1 or after != before:
                failures.append({"kind": "reported_sentence_reading", "text": text, "word": word,
                                 "before": before, "after": after})
            # Other English words may legitimately improve (Python in the
            # with sentence). Keep the full production-path observation visible.
            sentence_results.append({"text": text, "word": word, "before": before, "after": after,
                                     "sentence_phones_changed": production_phones(text) != english_baseline[text]})
        words = {row[0]: row[0] for row in corrected}
        words.update({surface(word): word for word in CONTRACTIONS})
        for key, word in words.items():
            key = surface(key.lower())
            if key not in expected:
                # Corrections are historical evidence, not mandatory overrides
                # of the base or of conservative release-data exclusions.
                continue
            for prefix, suffix in (("", ""), ("", "を確認する。"), ("これは", "です。")):
                whole = [f for f in pyopenjtalk.run_frontend(with_latin_word_boundaries(prefix + word.lower() + suffix))
                         if lexical_surface(f["string"]) == key]
                contexts += 1
                if len(whole) != 1 or whole[0]["read"] not in expected[key]:
                    failures.append({"kind": "whole_word", "word": word, "prefix": prefix, "suffix": suffix, "observed": whole})
            # Use the actual production prosody path, not only native row lookup.
            phones = japanese.g2p(word)
            native = [japanese.post_replace_ph(p) for p in japanese.pyopenjtalk_g2p_prosody(word.lower())[1:-1]]
            frontend_words += 1
            if not phones or "UNK" in phones or phones != native:
                failures.append({"kind": "frontend", "word": word, "phones": phones})
        return {"version": pack.version, "entry_count": pack.entry_count, "sha256": pack.sha256,
                "base_samples": len(BASE_SAMPLES), "native_contexts": contexts,
                "unmodified_base_inputs": boundary_checks,
                "additional_unit_phoneme_checks": len(UNIT_SAMPLES),
                "base_owned_words": len(base_owned), "english_base_contexts": len(base_controls),
                "common_english_words": len(common), "common_english_results": vocabulary,
                "external_sentence_contexts": len(REPORTED_CONTEXTS),
                "external_sentence_results": sentence_results,
                "unit_prosody_changes_for_review": unit_prosody_changes,
                "production_frontend_words": frontend_words, "failures": failures,
                "limitations": "Native phoneme/prosody and token-selection checks; no waveform or listening acceptance."}
    finally:
        configure_japanese_dictionary(pyopenjtalk, project_root=project_root, enabled=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = validate(args.project_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items()
                      if key not in {"failures", "common_english_results", "external_sentence_results"}}))
    print(f"Failures: {len(report['failures'])}")
    return int(bool(report["failures"]))


if __name__ == "__main__":
    raise SystemExit(main())
