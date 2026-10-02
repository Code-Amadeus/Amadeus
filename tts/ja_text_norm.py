# -*- coding: utf-8 -*-
"""日语 TTS 文本归一化：拉丁字母/数字会让 GPT-SoVITS 日文前端抛 LookupError。

实测：`No,no、あなたにしか…` 与 `…歌って踊れるAIライバー…` 两句直接失败
（LookupError: 空消息），其余 10 句正常。Amadeus 的中文台词译成日语后同样会
出现 AI / No / OK 这类词，所以这一步必须做在合成之前。

策略：常见词用词典读法（No→ノー、AI→エーアイ），其余按字母逐个读成片假名；
半角标点转全角；危险的裸符号去掉。

用法:
  from ja_text_norm import norm_ja
  norm_ja("No,no、あなたにしかできないこともあるんだよ。")
"""
import re

# 常见词（小写匹配）→ 日语读法
LEXICON = {
    "no,no": "ノーノー", "no": "ノー", "yes": "イエス", "ok": "オーケー",
    "ng": "エヌジー", "ai": "エーアイ", "it": "アイティー", "pc": "ピーシー",
    "vtuber": "ブイチューバー", "youtube": "ユーチューブ", "x": "エックス",
    "live": "ライブ", "wifi": "ワイファイ", "id": "アイディー",
}

LETTER = {
    "a": "エー", "b": "ビー", "c": "シー", "d": "ディー", "e": "イー",
    "f": "エフ", "g": "ジー", "h": "エイチ", "i": "アイ", "j": "ジェー",
    "k": "ケー", "l": "エル", "m": "エム", "n": "エヌ", "o": "オー",
    "p": "ピー", "q": "キュー", "r": "アール", "s": "エス", "t": "ティー",
    "u": "ユー", "v": "ブイ", "w": "ダブリュー", "x": "エックス",
    "y": "ワイ", "z": "ゼット",
}
DIGIT = {"0": "ゼロ", "1": "いち", "2": "に", "3": "さん", "4": "よん",
         "5": "ご", "6": "ろく", "7": "なな", "8": "はち", "9": "きゅう"}

PUNCT = {",": "、", ".": "。", "!": "！", "?": "？", ":": "、",
         ";": "、", "~": "ー", "ｰ": "ー"}


def _letters_to_katakana(s: str) -> str:
    return "".join(LETTER.get(c.lower(), c) for c in s)


def norm_ja(text: str) -> str:
    """把会让日文前端崩溃的拉丁字母换成假名读法。

    只做「必要且安全」的替换：
      · 拉丁字母（崩溃根因）-> 假名读法
      · ASCII 标点 -> 全角标点
    刻意**不**处理阿拉伯数字：数字不会让前端崩溃，而逐字转假名会把
    「2026」读成「にゼロにろく」，属于引入回归。数字交给 TTS 前端自己处理。
    """
    s = text

    # 1) 词典优先（按长度倒序，避免 no 抢走 no,no）
    for k in sorted(LEXICON, key=len, reverse=True):
        s = re.sub(re.escape(k), LEXICON[k], s, flags=re.IGNORECASE)

    # 2) 剩余 ASCII 字母 → 逐个片假名
    s = re.sub(r"[A-Za-z]+", lambda m: _letters_to_katakana(m.group(0)), s)

    # 3) 半角标点 → 全角
    for a, b in PUNCT.items():
        s = s.replace(a, b)

    return s


if __name__ == "__main__":
    tests = [
        "No,no、あなたにしかできないこともあるんだよ。",
        "ヤチヨだよ。月読の管理人、月見ヤチヨ。なんと、八千歳！歌って踊れるAIライバー、分身もできるよ。",
        "月読の仮想世界へようこそ、管理人・月見ヤチヨだよ。",
        "エネルギー満タンだよ。さあ、今のヤチヨは無敵なんだから。",
    ]
    for t in tests:
        out = norm_ja(t)
        bad = [c for c in out if c.isascii() and (c.isalnum())]
        print(f"{'OK ' if not bad else 'BAD'} {t}\n  -> {out}")
