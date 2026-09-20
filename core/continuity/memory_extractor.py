"""C2 memory extraction interfaces and a conservative local baseline.

C2 deliberately does not add another mandatory model call.  The default
extractor recognizes only high-confidence user-authored statements and explicit
remember/forget commands.  Passive (non-explicit) facts stay limited to the
recognized stable fact slots that the structured recall path can query; a plain
sentence such as "我的意思是说..." is a clause, not a durable fact, and only an
explicit remember directive may store free-form evidence.  A future
model-backed extractor can implement the same protocol without changing Host
write semantics.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
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


def parse_explicit_memory_directive(text: str) -> MemoryDirective | None:
    """Parse only unambiguous top-level remember/forget commands.

    The parser intentionally does not interpret vague references such as
    "forget that" with an empty target; deleting the wrong durable fact is more
    harmful than declining an ambiguous deletion at C2.
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
    """High-precision user-only baseline used when no model extractor exists."""

    async def extract(self, evidence: TurnEvidence) -> Sequence[MemoryCandidate]:
        user_text = _norm(evidence.user_text)
        if not user_text:
            return ()

        directive = parse_explicit_memory_directive(user_text)
        if directive is not None:
            if directive.action is MemoryDirectiveAction.REMEMBER and directive.candidate:
                return (directive.candidate,)
            return ()

        candidate = _candidate_from_claim(user_text, explicit_keep=False)
        return (candidate,) if candidate is not None else ()


__all__ = [
    "DeterministicMemoryExtractor",
    "MemoryExtractor",
    "parse_explicit_memory_directive",
    "stable_freeform_memory_key",
]
