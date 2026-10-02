"""Deterministic emotion hints for companion portrait selection.

This is deliberately a small lexical fallback, not a second language model. It
turns clear visible language into an atlas key when the main model omitted its
optional [EMO preset=...] tag.
"""

from __future__ import annotations


_AFFECTION_TERMS: tuple[str, ...] = (
    "喜欢你", "爱你", "最爱", "心动", "告白", "喜欢你", "喜欢他", "喜欢她", "好喜欢", "很喜欢",
    "恥ずかしい", "好き", "大好き",
)

_NEGATIONS: tuple[str, ...] = ("不要", "别", "不用", "不必", "没有", "不是", "并不", "才不")

_INTENSE_SAD_TERMS: tuple[str, ...] = (
    "绝望", "心碎", "崩溃", "大哭", "哭死", "悲痛", "痛不欲生", "伤心欲绝",
    "心灰意冷", "撑不住了", "活不下去", "空洞", "空虚",
)
_SAD_BASE_TERMS: tuple[str, ...] = ("难过", "伤心", "悲伤", "痛苦", "难受")
_SAD_INTENSIFIERS: tuple[str, ...] = ("真的", "非常", "特别", "极度", "无比", "太", "很", "好")

_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("sided_surprised", ("诶", "欸", "咦", "哇", "真的吗", "不会吧", "没想到", "吓", "吃惊", "まさか", "びっくり", "うそ")),
    ("angry", ("生气", "讨厌", "不许", "笨蛋", "坏蛋", "气死", "别这样", "ムカ", "腹立", "ふざけ", "ばか")),
    ("blush", ("害羞", "不好意思", "脸红", "喜欢你", "告白", "可爱", "恥ずか", "照れ")),
    ("sad", ("难过", "伤心", "想哭", "呜", "寂寞", "撑不住", "失落", "难受", "抱歉", "对不起", "悲しい", "つらい", "寂しい", "疲れ")),
    ("disappointed", ("失望", "算了", "没办法", "无所谓", "随便", "落寞", "残念", "仕方ない")),
    ("happy", ("开心", "高兴", "太好了", "哈哈", "嘻嘻", "嘿嘿", "好耶", "真棒", "谢谢", "谢谢", "欢迎", "夸", "沾光", "愉快", "嬉しい", "楽しい", "よかった")),
    ("sided_thinking", ("为什么", "怎么", "思考", "想想", "或许", "应该", "说明", "解释", "等一下", "どうして", "なぜ", "考え", "たぶん")),
)


def has_affection_signal(text: str) -> bool:
    value = str(text or "")
    return any(term in value for term in _AFFECTION_TERMS)


def _has_intense_sadness(value: str) -> bool:
    if any(term in value for term in _INTENSE_SAD_TERMS):
        return True
    for term in _SAD_BASE_TERMS:
        index = value.find(term)
        if index < 0:
            continue
        prefix = value[max(0, index - 5):index]
        if any(prefix.endswith(marker) for marker in _SAD_INTENSIFIERS):
            return True
    return False


def _negated(value: str, word: str) -> bool:
    index = value.find(word)
    if index < 0:
        return False
    prefix = value[max(0, index - 6):index]
    return any(negation in prefix for negation in _NEGATIONS)


def infer_emotion_from_text(text: str) -> str:
    """Return an atlas emotion key for clear lexical emotion signals."""

    value = str(text or "")
    if not value.strip():
        return ""
    if has_affection_signal(value):
        return "blush"
    if _has_intense_sadness(value):
        return "disappointed"
    best_key = ""
    best_score = 0
    for key, words in _HINTS:
        score = sum(
            value.count(word)
            for word in words
            if word and not (key == "sad" and _negated(value, word))
        )
        if score > best_score:
            best_key, best_score = key, score
    return best_key
