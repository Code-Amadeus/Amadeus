"""Streaming LLM output tag parsing.

Extracted from main.py. The parser used to live as module-level globals
(_st_in_tag / _st_tag_buf / _st_delegate_seen / _think_strip_buf /
_think_strip_active); it is now a per-turn instance so concurrent or
interleaved turns can never corrupt each other's parse state.
"""

from __future__ import annotations

import re

from tools.text_utils import (
    _parse_attr_kv,
    _parse_delegate_attrs,
    parse_tags_and_clean,
)

_TAG_RE = re.compile(
    r"^\[(PARAM|EXPR|HOTKEY|EMO|ANIM|DELEGATE|CONTROL|AUIP)([^\]]*)\]$",
    flags=re.IGNORECASE,
)


class StreamTagParser:
    """字符级流式解析器（每轮对话一个实例）。

    - 文本模式下逐字符输出到 cleaned
    - 遇到 '[' 切换到标签模式并开始缓存，直到 ']' 完整闭合再解析；
      整个标签不会输出到 cleaned
    - 跨 chunk 保持状态，避免半截标签泄漏
    - 同时过滤 Qwen3 思维链残留 token：<think>...</think>（含跨 chunk 情况）
    """

    def __init__(self, *, control_envelope_enabled: bool = False) -> None:
        self._in_tag = False
        self._tag_buf = ""
        self._delegate_seen = False
        self._think_buf = ""
        self._think_active = False
        self._control_envelope_enabled = bool(control_envelope_enabled)

    def reset(self) -> None:
        self._in_tag = False
        self._tag_buf = ""
        self._delegate_seen = False
        self._think_buf = ""
        self._think_active = False

    def _strip_think_tokens(self, text: str) -> str:
        result = []
        i = 0
        while i < len(text):
            if self._think_active:
                end = text.find("</think>", i)
                if end == -1:
                    self._think_buf += text[i:]
                    return "".join(result)
                self._think_active = False
                self._think_buf = ""
                i = end + len("</think>")
            else:
                start = text.find("<think>", i)
                if start == -1:
                    result.append(text[i:])
                    break
                result.append(text[i:start])
                self._think_active = True
                self._think_buf = "<think>"
                i = start + len("<think>")
        return "".join(result)

    def process_chunk(self, raw_text: str) -> tuple[str, list[dict]]:
        """返回 (cleaned_text, actions)。"""

        cleaned, actions, _parts = self.process_chunk_parts(raw_text)
        return cleaned, actions

    def process_chunk_parts(
        self,
        raw_text: str,
    ) -> tuple[str, list[dict], list[tuple[str, object]]]:
        """Return clean text, actions, and their exact stream order.

        ``parts`` contains ``("text", str)`` and ``("action", dict)`` items.
        It lets the history boundary retain selected EMO actions in place while
        the visible/TTS path continues consuming only ``cleaned_text``.
        """

        if not raw_text:
            return "", [], []
        raw_text = self._strip_think_tokens(raw_text)
        if self._delegate_seen:
            return "", [], []
        actions: list[dict] = []
        out_chars: list[str] = []
        text_part: list[str] = []
        parts: list[tuple[str, object]] = []

        def flush_text_part() -> None:
            if text_part:
                parts.append(("text", "".join(text_part)))
                text_part.clear()

        for ch in raw_text:
            if self._in_tag:
                self._tag_buf += ch
                if ch == "]":
                    full = self._tag_buf
                    self._in_tag = False
                    self._tag_buf = ""
                    m = _TAG_RE.match(full)
                    if m:
                        tag_type = m.group(1).upper()
                        attr_text = m.group(2) or ""
                        # With the canary off, CONTROL has exactly the legacy
                        # unknown-tag behaviour: strip it from visible speech,
                        # do not expose an action, and continue the stream.
                        if tag_type == "CONTROL" and not self._control_envelope_enabled:
                            continue
                        if tag_type == "DELEGATE":
                            attrs = _parse_delegate_attrs(attr_text)
                        else:
                            attrs = _parse_attr_kv(attr_text)
                        action = {"type": tag_type, "attrs": attrs, "raw": full}
                        actions.append(action)
                        parts.append(("action", action))
                        if tag_type in {"DELEGATE", "CONTROL"}:
                            self._delegate_seen = True
                            break
                    # else: 非法标签直接丢弃
            else:
                if ch == "[":
                    flush_text_part()
                    self._in_tag = True
                    self._tag_buf = "["
                else:
                    out_chars.append(ch)
                    text_part.append(ch)

        flush_text_part()
        return "".join(out_chars), actions, parts


def clean_sentence_for_tts(sentence: str, record_actions_fn=None):
    """强力清理：移除完整标签，残留的半截标签/孤立右括号。

    返回 (clean_text, expr_actions)。
    DELEGATE 动作通过 record_actions_fn 立即触发（不依赖播放时序）；
    EXPR/PARAM/EMO/HOTKEY 动作以列表返回，由调用方交给 ExpressionController
    按播放时序延迟触发。
    """
    if not sentence:
        return sentence, []
    cleaned, actions = parse_tags_and_clean(sentence)
    delegate_acts = [a for a in actions if a.get("type") == "DELEGATE"]
    expr_acts = [
        a for a in actions if a.get("type") not in {"DELEGATE", "AUIP", "CONTROL"}
    ]
    if delegate_acts and record_actions_fn is not None:
        record_actions_fn(delegate_acts)
    s = cleaned
    # 移除字符串开头的孤立右括号及其前缀噪声，例如 "8 dur=2s] ..."
    while True:
        new_s = re.sub(r"^\s*[^\[]*\]", "", s)
        if new_s == s:
            break
        s = new_s
    # 移除未闭合的左括号到结尾，例如 "...[EXPR name=..."
    s = re.sub(r"\[[^\]]*$", "", s)
    return s.strip(), expr_acts
