"""Offline native release gate for a prepared Japanese pronunciation pack."""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.japanese_dictionary import PACK_TREE, dictionary_rows, load_dictionary_pack  # noqa: E402
from tools.tts_text_processor import correct_pronunciation_for_tts  # noqa: E402
from tts.japanese_dictionary import configure_japanese_dictionary  # noqa: E402

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


def validate(project_root: Path) -> dict:
    import pyopenjtalk
    sys.path.insert(0, str(ROOT / "GPT_SoVITS"))
    from text import japanese
    project_root = project_root.resolve()
    pack = load_dictionary_pack(project_root / "assets" / PACK_TREE)
    rows = list(dictionary_rows((pack.root / "userdict.csv").read_bytes()))
    jtalk = pyopenjtalk.OpenJTalk(pyopenjtalk.OPEN_JTALK_DICT_DIR)
    surface = jtalk.normalize_for_mecab
    expected = defaultdict(set)
    for row in rows:
        expected[row[0]].add(row[11])
    failures, contexts, frontend_words = [], 0, 0
    try:
        pyopenjtalk.unset_user_dict()
        baseline = {text: japanese.g2p(correct_pronunciation_for_tts(text)) for text in BASE_SAMPLES}
        unit_baseline = {text: japanese.g2p(correct_pronunciation_for_tts(text)) for text in UNIT_SAMPLES}
        status = configure_japanese_dictionary(pyopenjtalk, project_root=project_root, enabled=True)
        if not status["ready"]:
            raise RuntimeError(status["detail"])
        for text, before in baseline.items():
            after = japanese.g2p(correct_pronunciation_for_tts(text))
            if after != before:
                failures.append({"kind": "base_regression", "text": text, "before": before, "after": after})
        unit_prosody_changes = []
        without_prosody = lambda phones: [phone for phone in phones if phone not in {"[", "]", "#"}]
        for text, before in unit_baseline.items():
            after = japanese.g2p(correct_pronunciation_for_tts(text))
            if without_prosody(after) != without_prosody(before):
                failures.append({"kind": "unit_phonemes", "text": text, "before": before, "after": after})
            elif after != before:
                # A different accent boundary alone does not establish a wrong
                # reading. Record it for listening review without inventing an
                # accent correction or a per-unit exclusion.
                unit_prosody_changes.append({"text": text, "before": before, "after": after})
        corrected = list(dictionary_rows((ROOT / "tools/data/japanese_dictionary_corrections.csv").read_bytes()))
        words = {row[0]: row[0] for row in corrected}
        words.update({surface(word): word for word in CONTRACTIONS})
        for key, word in words.items():
            key = surface(key.lower())
            for prefix, suffix in (("", ""), ("", "を確認する。"), ("これは", "です。")):
                whole = [f for f in pyopenjtalk.run_frontend(prefix + word.lower() + suffix) if f["string"] == key]
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
                "additional_unit_phoneme_checks": len(UNIT_SAMPLES),
                "unit_prosody_changes_for_review": unit_prosody_changes,
                "production_frontend_words": frontend_words, "failures": failures,
                "limitations": "Native phoneme/prosody and token-selection checks; no waveform or listening acceptance."}
    finally:
        pyopenjtalk.unset_user_dict()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = validate(args.project_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "failures"}))
    print(f"Failures: {len(report['failures'])}")
    return int(bool(report["failures"]))


if __name__ == "__main__":
    raise SystemExit(main())
