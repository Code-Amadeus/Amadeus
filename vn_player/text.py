"""Text normalization helpers for VN line matching and dedupe."""

from __future__ import annotations

import hashlib
import re
import unicodedata

_TAG_RE = re.compile(r"<[^>]*>|\[[^\]]*\]")
_CONTROL_REPLACEMENTS = {
    "[r]": "\n",
    "[l]": "",
    "[p]": "",
}
_SPACE_RE = re.compile(r"\s+")
_SENTENCE_PUNCT_RE = re.compile(r"[。.!！？?…]")
_TITLE_PARTICLES = {"啊", "呀", "吧", "呢", "吗", "嘛", "哦", "唔"}
_DIALOGUE_VERB_HINTS = {
    "是",
    "在",
    "有",
    "能",
    "能够",
    "看到",
    "看见",
    "觉得",
    "说",
    "听",
    "知道",
    "想",
    "做",
    "存在",
    "相信",
    "获得",
    "失去",
}


def strip_vn_tags(text: str) -> str:
    value = str(text or "")
    for old, new in _CONTROL_REPLACEMENTS.items():
        value = value.replace(old, new)
    value = _TAG_RE.sub("", value)
    value = value.replace("\\n", "\n")
    value = _SPACE_RE.sub(" ", value)
    return value.strip()


def normalize_for_match(text: str) -> str:
    value = unicodedata.normalize("NFKC", strip_vn_tags(text)).casefold()
    kept: list[str] = []
    for ch in value:
        category = unicodedata.category(ch)
        if category[0] in {"C", "P", "S", "Z"}:
            continue
        kept.append(ch)
    return "".join(kept).strip()


def text_hash(text: str) -> str:
    return hashlib.sha1(normalize_for_match(text).encode("utf-8")).hexdigest()[:16]


def looks_like_topic_label(text: str) -> bool:
    """Detect menu/topic labels that should not become evidence nodes.

    VN scripts often interleave displayed UI labels with spoken lines. These
    labels can be useful as attention hints, but treating short topic labels as
    hard story evidence pollutes retrieval.
    """
    value = strip_vn_tags(text)
    compact = normalize_for_match(value)
    if not compact:
        return False
    if "■" in value or value.startswith(("#", "※")):
        return True
    if _SENTENCE_PUNCT_RE.search(value):
        return False
    if any(compact.endswith(particle) for particle in _TITLE_PARTICLES):
        return False
    if compact.endswith("的事") and len(compact) <= 14:
        return True
    if compact.startswith(("从最初", "回忆", "调查", "选择")) and len(compact) <= 14:
        return True
    if len(compact) <= 5 and not any(hint in compact for hint in _DIALOGUE_VERB_HINTS):
        return True
    return len(compact) <= 12 and not any(hint in compact for hint in _DIALOGUE_VERB_HINTS)


def rough_similarity(a: str, b: str) -> float:
    """Cheap similarity for short VN lines.

    This is intentionally dependency-free. It is good enough as a fallback after
    exact normalized hash matching.
    """
    left = normalize_for_match(a)
    right = normalize_for_match(b)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        return min(len(left), len(right)) / max(len(left), len(right))
    left_chars = set(left)
    right_chars = set(right)
    if not left_chars or not right_chars:
        return 0.0
    overlap = len(left_chars & right_chars)
    return overlap / max(len(left_chars | right_chars), 1)
