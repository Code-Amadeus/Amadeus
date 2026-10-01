from __future__ import annotations

from core.emotion_hints import has_affection_signal, infer_emotion_from_text


def test_emotion_hints_cover_clear_visible_signals() -> None:
    assert infer_emotion_from_text("我今天特别开心，快夸夸我") == "happy"
    assert infer_emotion_from_text("呜，今天有点难过") == "sad"
    assert infer_emotion_from_text("喂，不许说我老！") == "angry"
    assert infer_emotion_from_text("欸？真的吗，没想到会这样") == "sided_surprised"
    assert infer_emotion_from_text("其实我有一点害羞") == "blush"
    assert infer_emotion_from_text("算了，反正也没办法") == "disappointed"
    assert infer_emotion_from_text("为什么这个会这样，我想想") == "sided_thinking"
    assert infer_emotion_from_text("今天天气不错") == ""


def test_affection_does_not_become_sad_from_reply_words() -> None:
    assert has_affection_signal("我喜欢你，想一直和你说话")
    assert infer_emotion_from_text("我喜欢你，喜欢到想哭") == "blush"
    assert infer_emotion_from_text("不要哭，我在这里") == ""
    assert infer_emotion_from_text("别难过，会好起来的") == ""
    assert infer_emotion_from_text("我真的好难过，想哭") == "disappointed"


def test_intense_sadness_uses_disappointed() -> None:
    assert infer_emotion_from_text("我真的好难过，已经快撑不住了") == "disappointed"
    assert infer_emotion_from_text("今天有点难过") == "sad"
    assert infer_emotion_from_text("我很失望，也觉得落寞") == "disappointed"
