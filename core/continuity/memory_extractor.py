"""C2 memory extraction interfaces and a conservative local baseline.

C2 deliberately does not add another mandatory model call.  The default
extractor recognizes high-confidence user-authored statements and explicit
remember/forget/mute commands:

- passive stable facts stay limited to the recognized fact slots that the
  structured recall path can query (a clause such as "我的意思是说..." is never a
  fact);
- preferences ("我喜欢...") are stored as preferences;
- any other substantive user utterance is kept verbatim (bounded) as
  conversation memory - EPISODIC for statements/回忆, OPEN_LOOP for plans - so a
  later "we discussed ..." question has an anchor to recall from;
- explicit remember/forget/mute directives are control-plane text and are
  never stored as memory themselves.

Assistant prose is never a user fact.  A future model-backed extractor can
implement the same protocol without changing Host write semantics.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import unicodedata
from dataclasses import replace
from typing import Protocol, Sequence

from core.continuity.models import (
    ContinuityFactSource,
    MemoryCandidate,
    MemoryDirective,
    MemoryDirectiveAction,
    MemoryKind,
    MemoryPriorityClass,
    TurnEvidence,
)
from core.continuity.text_excerpt import (
    DeterministicTextCompressor,
    TextCompressor,
    compress_excerpt,
)
from core.continuity.topic_mute import normalize_topic


class MemoryExtractor(Protocol):
    async def extract(self, evidence: TurnEvidence) -> Sequence[MemoryCandidate]:
        """Return structured candidates from one accepted turn.

        Implementations must treat assistant prose as non-authoritative unless
        an independent Host authority explicitly supplies a different source.
        """


def _norm(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _comparison(value: str) -> str:
    return _norm(value).casefold().rstrip("。.!！?？").strip()


def _hash_key(prefix: str, value: str) -> str:
    digest = hashlib.sha256(_comparison(value).encode("utf-8")).hexdigest()[:20]
    return f"{prefix}.{digest}"


_SLOT_ALIASES = {
    "名字": "name",
    "姓名": "name",
    "name": "name",
    "生日": "birth_date",
    "出生日期": "birth_date",
    "birthday": "birth_date",
    "birth date": "birth_date",
    "职业": "occupation",
    "工作": "occupation",
    "occupation": "occupation",
    "job": "occupation",
    "住址": "location",
    "地址": "location",
    "居住地": "location",
    "location": "location",
    "city": "location",
    "城市": "location",
    "最喜欢的颜色": "favorite_color",
    "喜欢的颜色": "favorite_color",
    "favorite color": "favorite_color",
    "最喜欢的食物": "favorite_food",
    "喜欢的食物": "favorite_food",
    "favorite food": "favorite_food",
}


def _slot_key(field: str) -> tuple[str, str]:
    clean = _comparison(field)
    canonical = _SLOT_ALIASES.get(clean)
    if canonical:
        return f"user.fact.{canonical}", canonical
    return _hash_key("user.fact.slot", clean), clean


def _preference_key(target: str) -> str:
    return _hash_key("user.preference", target)


def stable_freeform_memory_key(text: str) -> str:
    """Return the stable opaque key used for free-form user evidence.

    C5 relationship provenance reuses this key so an explicit forget of the
    same user-authored evidence can invalidate derived relationship effects
    without retaining the plaintext inside the relationship ledger.
    """

    return _hash_key("user.note", text)


def _freeform_key(text: str) -> str:
    return stable_freeform_memory_key(text)


def _strip_sentence_tail(value: str) -> str:
    return _norm(value).rstrip("。.!！").strip()


def _looks_like_question(value: str) -> bool:
    text = _norm(value)
    return text.endswith(("?", "？")) or text.endswith(("吗", "么", "呢"))


def _candidate_from_claim(
    claim: str,
    *,
    explicit_keep: bool = False,
) -> MemoryCandidate | None:
    text = _strip_sentence_tail(claim)
    if not text or _looks_like_question(text):
        return None

    # Chinese stable fact: 我的生日是... / 我的名字叫...
    match = re.fullmatch(
        r"我的(?P<field>[^，,。.!！?？：:]{1,32}?)(?:是|为|叫)(?P<value>.+)",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        field = _norm(match.group("field"))
        value = _norm(match.group("value"))
        if not value:
            return None
        if not explicit_keep and _SLOT_ALIASES.get(_comparison(field)) is None:
            # Unknown "我的X是..." slots are ordinary prose (the observed
            # failure was "我的意思是说..."), not stable facts: the structured
            # recall path can only query the registered slots, and C2 stays
            # high-precision for passive writes.
            return None
        key, predicate = _slot_key(field)
        return MemoryCandidate(
            memory_key=key,
            kind=MemoryKind.USER_FACT,
            summary=text,
            subject="user",
            predicate=predicate,
            object_text=value,
            importance=0.72 if explicit_keep else 0.62,
            confidence=0.98,
            priority_class=(MemoryPriorityClass.P0 if explicit_keep else MemoryPriorityClass.P2),
            pinned=explicit_keep,
            explicit_keep=explicit_keep,
            source_type=ContinuityFactSource.USER_ASSERTED,
        )

    # English stable fact: my birthday is ...
    match = re.fullmatch(
        r"my\s+(?P<field>.{1,48}?)\s+(?:is|are)\s+(?P<value>.+)",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        field = _norm(match.group("field"))
        value = _norm(match.group("value"))
        if not value:
            return None
        if not explicit_keep and _SLOT_ALIASES.get(_comparison(field)) is None:
            return None
        key, predicate = _slot_key(field)
        return MemoryCandidate(
            memory_key=key,
            kind=MemoryKind.USER_FACT,
            summary=text,
            subject="user",
            predicate=predicate,
            object_text=value,
            importance=0.72 if explicit_keep else 0.62,
            confidence=0.98,
            priority_class=(MemoryPriorityClass.P0 if explicit_keep else MemoryPriorityClass.P2),
            pinned=explicit_keep,
            explicit_keep=explicit_keep,
            source_type=ContinuityFactSource.USER_ASSERTED,
        )

    # Preferences: 我喜欢咖啡 / 我不喜欢咖啡
    match = re.fullmatch(r"我(?P<neg>不)?喜欢(?P<target>.+)", text, flags=re.IGNORECASE)
    if match:
        target = _norm(match.group("target"))
        if not target:
            return None
        sentiment = "dislike" if match.group("neg") else "like"
        return MemoryCandidate(
            memory_key=_preference_key(target),
            kind=MemoryKind.PREFERENCE,
            summary=text,
            subject="user",
            predicate="preference",
            object_text=f"{sentiment}:{target}",
            importance=0.68 if explicit_keep else 0.58,
            confidence=0.97,
            priority_class=(MemoryPriorityClass.P0 if explicit_keep else MemoryPriorityClass.P2),
            pinned=explicit_keep,
            explicit_keep=explicit_keep,
            source_type=ContinuityFactSource.USER_ASSERTED,
        )

    # Preferences: I like coffee / I don't like coffee
    match = re.fullmatch(
        r"i\s+(?:(?P<neg>do\s+not|don't)\s+)?like\s+(?P<target>.+)",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        target = _norm(match.group("target"))
        if not target:
            return None
        sentiment = "dislike" if match.group("neg") else "like"
        return MemoryCandidate(
            memory_key=_preference_key(target),
            kind=MemoryKind.PREFERENCE,
            summary=text,
            subject="user",
            predicate="preference",
            object_text=f"{sentiment}:{target}",
            importance=0.68 if explicit_keep else 0.58,
            confidence=0.97,
            priority_class=(MemoryPriorityClass.P0 if explicit_keep else MemoryPriorityClass.P2),
            pinned=explicit_keep,
            explicit_keep=explicit_keep,
            source_type=ContinuityFactSource.USER_ASSERTED,
        )

    if explicit_keep:
        # Explicitly requested free-form memories are allowed even when the
        # conservative parser cannot assign a semantic slot.  The key contains
        # only a one-way digest, so a later tombstone does not retain plaintext.
        kind = MemoryKind.OPEN_LOOP if _looks_like_open_loop(text) else MemoryKind.USER_FACT
        return MemoryCandidate(
            memory_key=_freeform_key(text),
            kind=kind,
            summary=text,
            subject="user",
            predicate="explicit_note",
            object_text=text,
            importance=0.75,
            confidence=1.0,
            future_value=0.8 if kind is MemoryKind.OPEN_LOOP else 0.2,
            priority_class=MemoryPriorityClass.P0,
            pinned=True,
            explicit_keep=True,
            source_type=ContinuityFactSource.USER_ASSERTED,
        )
    return None


def _looks_like_open_loop(text: str) -> bool:
    lowered = _comparison(text)
    return bool(
        re.match(r"^(?:下次|之后|以后|回头|稍后)", lowered)
        or re.match(r"^(?:to\s+|next\s+time\b|later\b)", lowered)
    )


# A substantive user utterance is remembered even when it matches no stable
# slot: the Host keeps what the user chose to talk about so a later "we
# discussed ..." question has an anchor to recall from.  The thresholds are
# deliberately simple, testable, and lower than the stable-slot rules.
_SUBSTANTIVE_MIN_CONTENT_CHARS = 12
_ANCHORED_MIN_CONTENT_CHARS = 4
# Public because the renderer bounds memory lines by the same verbatim budget:
# one source of truth for "how much of an utterance stays recallable".
EPISODIC_SUMMARY_MAX_CHARS = 800

_PAST_ANCHOR_MARKERS = (
    "昨天", "前天", "前几天", "几天前", "上周", "上周末", "上个月", "去年", "前年",
    "当时", "那天", "那次", "小时候", "刚刚", "刚才", "今天", "最近", "这几天",
    "之前", "以前", "曾经",
    "yesterday", "last night", "last week", "last month", "last year",
    "a few days ago", "the other day", "earlier today", "today", "recently",
    "back then", "when i was",
)
_FUTURE_PLAN_MARKERS = (
    "下次", "之后", "以后", "回头", "稍后", "晚点", "待会", "等会",
    "明天", "后天", "下周", "下个月", "明年", "打算", "计划", "准备",
    "tomorrow", "next week", "next month", "next year", "later", "soon",
    "plan to", "planning to", "going to",
)

_CONTENT_CHAR_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7afA-Za-z0-9]")


def _content_length(value: str) -> int:
    """Count content characters, ignoring punctuation, spacing, and emoji."""

    return len(_CONTENT_CHAR_RE.findall(_norm(value)))


def _contains_marker(text: str, markers: tuple[str, ...]) -> bool:
    lowered = _norm(text).casefold()
    return any(marker in lowered for marker in markers)


def _candidate_from_substantive_utterance(claim: str) -> MemoryCandidate | None:
    """Fallback passive write: remember the user's own substantive sentence.

    The summary keeps the user's wording verbatim up to the bounded budget; an
    overlong utterance is semantically compressed later in ``extract()`` (see
    ``EPISODIC_SUMMARY_MAX_CHARS``).  The key hashes the *full* normalized text,
    so re-extraction with any compressor stays idempotent.  Stable slots and
    preferences are resolved by the earlier rules and never reach here.
    """

    text = _norm(claim)
    if not text:
        return None
    planned = _contains_marker(text, _FUTURE_PLAN_MARKERS)
    anchored = planned or _contains_marker(text, _PAST_ANCHOR_MARKERS)
    content_length = _content_length(text)
    minimum = _ANCHORED_MIN_CONTENT_CHARS if anchored else _SUBSTANTIVE_MIN_CONTENT_CHARS
    if content_length < minimum:
        return None
    summary = text.strip()
    if not summary:
        return None
    return MemoryCandidate(
        memory_key=_hash_key("user.episode", summary),
        kind=MemoryKind.OPEN_LOOP if planned else MemoryKind.EPISODIC,
        summary=summary,
        subject="user",
        predicate="stated",
        object_text=summary,
        importance=0.58 if anchored else 0.5,
        confidence=0.95,
        future_value=0.6 if planned else 0.15,
        priority_class=MemoryPriorityClass.P3,
        source_type=ContinuityFactSource.USER_ASSERTED,
    )


def _key_from_forget_target(target: str) -> tuple[str, MemoryKind | None]:
    text = _strip_sentence_tail(target)
    candidate = _candidate_from_claim(text, explicit_keep=False)
    if candidate is not None:
        return candidate.memory_key, candidate.kind

    cn_slot = re.fullmatch(r"我的(?P<field>[^，,。.!！?？：:]{1,32})", text)
    if cn_slot:
        key, _predicate = _slot_key(cn_slot.group("field"))
        return key, MemoryKind.USER_FACT

    en_slot = re.fullmatch(r"my\s+(?P<field>.{1,48})", text, flags=re.IGNORECASE)
    if en_slot:
        key, _predicate = _slot_key(en_slot.group("field"))
        return key, MemoryKind.USER_FACT

    return _freeform_key(text), None


# Mute targets must be concrete topics.  Reference words ("这个"/"it") and
# one-character fragments ("不要聊天了" -> "天") are refused rather than
# silently muting an over-broad pattern.
_MUTE_DEICTIC_TARGETS = {
    "这个", "那个", "这些", "那些", "这件事", "那件事", "这个话题", "那个话题",
    "这个问题", "那个问题", "它", "他", "她", "他们", "她们", "什么", "啥",
    "东西", "事情", "话题", "问题", "事",
    "it", "that", "this", "him", "her", "them", "something",
}


def _clean_mute_target(raw: str) -> str:
    text = _strip_sentence_tail(raw)
    text = re.sub(r"(?:了|吧|啊|嘛|呗|呀)[。.!！?？]*\s*$", "", text).strip()
    text = re.sub(r"\s*(?:anymore|again)\s*$", "", text, flags=re.IGNORECASE).strip()
    for prefix in (
        "这件事", "这个事情", "这个话题", "这个问题", "这个",
        "那件事", "那个话题", "那个问题", "那个", "这些", "那些",
    ):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    if text.casefold() in _MUTE_DEICTIC_TARGETS:
        return ""
    if _content_length(text) < 2:
        return ""
    return normalize_topic(text)


def parse_explicit_memory_directive(text: str) -> MemoryDirective | None:
    """Parse only unambiguous top-level remember/forget/mute commands.

    The parser intentionally does not interpret vague references such as
    "forget that" with an empty target; deleting the wrong durable fact is more
    harmful than declining an ambiguous deletion at C2.  The same rule applies
    to "don't bring that up again": an unresolvable topic is declined, not
    guessed.
    """

    raw = _norm(text)
    if not raw:
        return None

    # Forget is checked before remember so Chinese "不要再记" never gets read
    # as a keep instruction.
    forget_patterns = (
        r"^(?:请)?(?:忘掉|忘记|清除|不要再记得|不要再记|别再记得|别再记)(?:这件事|这个信息)?[：:，,\s]*(?P<target>.*)$",
        r"^(?:please\s+)?(?:forget|do\s+not\s+remember|don't\s+remember|clear)(?:\s+(?:that|about))?[\s,:-]*(?P<target>.*)$",
    )
    for pattern in forget_patterns:
        match = re.fullmatch(pattern, raw, flags=re.IGNORECASE)
        if match:
            target = _strip_sentence_tail(match.group("target"))
            if not target:
                return MemoryDirective(
                    action=MemoryDirectiveAction.FORGET,
                    target_text="",
                )
            key, kind = _key_from_forget_target(target)
            return MemoryDirective(
                action=MemoryDirectiveAction.FORGET,
                target_key=key,
                target_kind=kind,
                target_text=target,
            )

    mute_patterns = (
        r"^(?:请)?(?:以后|之后|从现在开始)?(?:都)?(?:不要再|别再|不用再|不要|别)(?:跟我)?(?:提起|提|聊|说|讲|谈)(?:起|到)?(?P<target>.*)$",
        r"^(?:please\s+)?(?:don'?t|do\s+not|stop)\s+(?:bring\s+up|mention|talk\s+about|discuss)(?P<target>.*)$",
    )
    for pattern in mute_patterns:
        match = re.fullmatch(pattern, raw, flags=re.IGNORECASE)
        if not match:
            continue
        return MemoryDirective(
            action=MemoryDirectiveAction.MUTE,
            target_text=_clean_mute_target(match.group("target")),
        )

    remember_patterns = (
        r"^(?:请)?(?:记住|记得)(?:这件事|这个信息)?[：:，,\s]*(?P<target>.+)$",
        r"^(?:please\s+)?(?:remember|don't\s+forget)(?:\s+that)?[\s,:-]+(?P<target>.+)$",
    )
    for pattern in remember_patterns:
        match = re.fullmatch(pattern, raw, flags=re.IGNORECASE)
        if not match:
            continue
        target = _strip_sentence_tail(match.group("target"))
        candidate = _candidate_from_claim(target, explicit_keep=True)
        if candidate is None:
            return None
        return MemoryDirective(
            action=MemoryDirectiveAction.REMEMBER,
            target_key=candidate.memory_key,
            target_kind=candidate.kind,
            target_text=target,
            candidate=candidate,
        )
    return None


class DeterministicMemoryExtractor:
    """High-precision user-only baseline used when no model extractor exists.

    The optional ``text_compressor`` only shapes over-budget utterances; it is
    the shared semantic-compression port (model-backed in production, the
    deterministic sampler in the model-less baseline).
    """

    def __init__(self, *, text_compressor: TextCompressor | None = None) -> None:
        self._text_compressor = text_compressor or DeterministicTextCompressor()

    async def extract(self, evidence: TurnEvidence) -> Sequence[MemoryCandidate]:
        user_text = _norm(evidence.user_text)
        if not user_text:
            return ()

        directive = parse_explicit_memory_directive(user_text)
        if directive is not None:
            if directive.action is MemoryDirectiveAction.REMEMBER and directive.candidate:
                candidate = directive.candidate
                original = str(candidate.summary or "")
                if len(original) > EPISODIC_SUMMARY_MAX_CHARS:
                    # An explicit "记住…" payload over budget is still one
                    # over-limit turn: compress it as a whole (semantic when a
                    # model is wired, deterministic otherwise) instead of
                    # storing an unbounded note or tail-cutting it.
                    compressed = await asyncio.to_thread(
                        self._text_compressor.compress,
                        original,
                        EPISODIC_SUMMARY_MAX_CHARS,
                        speaker="用户发言",
                    )
                    summary = str(compressed or "").strip() or compress_excerpt(
                        original, EPISODIC_SUMMARY_MAX_CHARS
                    )
                    candidate = replace(
                        candidate, summary=summary, object_text=summary
                    )
                return (candidate,)
            return ()

        candidate = _candidate_from_claim(user_text, explicit_keep=False)
        if candidate is not None:
            return (candidate,)
        remembered = _candidate_from_substantive_utterance(user_text)
        if remembered is None:
            return ()
        if len(user_text) > EPISODIC_SUMMARY_MAX_CHARS:
            compressed = await asyncio.to_thread(
                self._text_compressor.compress,
                user_text,
                EPISODIC_SUMMARY_MAX_CHARS,
                speaker="用户发言",
            )
            summary = str(compressed or "").strip() or compress_excerpt(
                user_text, EPISODIC_SUMMARY_MAX_CHARS
            )
            remembered = replace(remembered, summary=summary, object_text=summary)
        return (remembered,)


__all__ = [
    "DeterministicMemoryExtractor",
    "MemoryExtractor",
    "parse_explicit_memory_directive",
    "stable_freeform_memory_key",
]
