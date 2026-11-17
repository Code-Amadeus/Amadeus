"""Offline probe of real Chinese frontend code with a fixed external G2PW boundary.

The converter fixture supplies pypinyin readings instead of loading external
ONNX weights. Normalization, pronunciation corrections, segmentation/POS,
search-cut, sandhi, erhua, phones, alignment and symbol IDs use the real code.
This is not neural G2PW or synthesized-audio qualification.
"""
from __future__ import annotations

import importlib.abc
import json
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ("v1", "v2", "v2Pro", "v2ProPlus", "v3")
TEXTS = (
    "你好，世界！",
    "我很好，你也很好。",
    "不对，不好，不是，听不懂。",
    "看一看，听一听，一个，一天，第一，一零一。",
    "妈妈和爸爸慢慢地走，孩子们看看桌子。",
    "蒙古包里有纸老虎，展览馆里很美好。",
    "小院儿里的花儿很好看，女儿喜欢玩儿。",
    "银行行长说，重庆的音乐很好听。",
    "今天是2026年11月18日，气温是零下3.5摄氏度。",
    "价格12.50元，电话12345，完成率80%。",
    "中文ABC测试，嗯呣～就是……这样？！",
    "南京市长江大桥欢迎你，研究生命起源。",
    "啊——但是《原神》是由,米哈\\游自主，研发的一款全.新开放世界.冒险游戏",
    "呣呣呣～就是…大人的鼹鼠党吧？",
)
TOKEN_TEXTS = (
    "南京市长江大桥欢迎你",
    "研究生命起源",
    "中华人民共和国万岁",
    "不对不好听不懂，看一看听一听",
    "小院儿里的花儿和女儿",
    "蒙古包纸老虎展览馆",
)
TONE_WORDS = (
    ("不对", "a"), ("不好", "a"), ("听不懂", "v"),
    ("看一看", "v"), ("一个", "m"), ("一天", "m"), ("第一", "m"),
    ("妈妈", "n"), ("看看", "v"), ("桌子", "n"),
    ("你好", "l"), ("蒙古包", "n"), ("纸老虎", "n"), ("展览馆", "n"),
)


class _NoLegacyTokenizer(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] == "jieba_fast":
            raise ModuleNotFoundError("jieba-fast is absent from T4", name=fullname)
        return None


def probe(*, block_legacy=True):
    if block_legacy:
        sys.meta_path.insert(0, _NoLegacyTokenizer())
    sys.path.insert(0, str(ROOT / "GPT_SoVITS"))
    from pypinyin import Style, lazy_pinyin

    class FixedConverter:
        def __init__(self, **kwargs):
            pass

        def __call__(self, sentence):
            return [lazy_pinyin(sentence, style=Style.TONE3, neutral_tone_with_five=True)]

    # Replace only the external ONNX boundary, retaining the G2PW wrapper and
    # its real tracked pronunciation dictionaries/corrections.
    boundary = types.ModuleType("text.g2pw.onnx_api")
    boundary.G2PWOnnxConverter = FixedConverter
    sys.modules[boundary.__name__] = boundary

    from text import chinese, cleaned_text_to_sequence
    from text.cleaner import clean_text
    from text.tone_sandhi import ToneSandhi
    import text.tone_sandhi as sandhi

    tokenizer = sandhi.jieba
    tokenization = [
        {"text": text, "cut": tokenizer.lcut(text),
         "pos": [list(pair) for pair in chinese.psg.lcut(text)],
         "search": tokenizer.lcut_for_search(text)}
        for text in TOKEN_TEXTS
    ]
    modifier = ToneSandhi()
    tones = []
    for word, pos in TONE_WORDS:
        finals = lazy_pinyin(word, style=Style.FINALS_TONE3, neutral_tone_with_five=True)
        tones.append({"word": word, "pos": pos,
                      "finals": modifier.modified_tone(word, pos, finals)})
    frontends = {}
    for version in VERSIONS:
        rows = []
        for text in TEXTS:
            phones, word2ph, normalized = clean_text(text, "zh", version)
            assert len(phones) == sum(word2ph)
            assert len(normalized) == len(word2ph)
            rows.append({"text": text, "normalized": normalized,
                         "phones": " ".join(phones), "word2ph": " ".join(map(str, word2ph)),
                         "ids": " ".join(map(str, cleaned_text_to_sequence(phones, version)))})
        frontends[version] = rows
    # An unknown three-character sequence used to reach sandhi as [word, ""].
    # jieba's 1+2 grouping now exercises the existing middle-third-tone rule.
    unknown_word = {}
    for version in VERSIONS:
        phones, word2ph, normalized = clean_text("憭怃憯。", "zh", version)
        unknown_word[version] = {
            "phones": phones, "word2ph": word2ph, "normalized": normalized,
        }
    return {"tokenization": tokenization, "tones": tones, "frontends": frontends,
            "unknown_word": unknown_word}


if __name__ == "__main__":
    print(json.dumps(probe(), ensure_ascii=False))
