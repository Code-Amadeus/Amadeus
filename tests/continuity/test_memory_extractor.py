from __future__ import annotations

from core.continuity.memory_extractor import (
    DeterministicMemoryExtractor,
    parse_explicit_memory_directive,
)
from core.continuity.models import (
    MemoryDirectiveAction,
    MemoryKind,
    MemoryPriorityClass,
    TurnEvidence,
)


def test_explicit_remember_creates_pinned_candidate() -> None:
    directive = parse_explicit_memory_directive("记住我的生日是1月2日")
    assert directive is not None
    assert directive.action is MemoryDirectiveAction.REMEMBER
    assert directive.target_key == "user.fact.birth_date"
    assert directive.candidate is not None
    assert directive.candidate.kind is MemoryKind.USER_FACT
    assert directive.candidate.object_text == "1月2日"
    assert directive.candidate.pinned is True
    assert directive.candidate.priority_class is MemoryPriorityClass.P0


def test_explicit_forget_slot_does_not_retain_value_in_target_key() -> None:
    directive = parse_explicit_memory_directive("忘记我的生日")
    assert directive is not None
    assert directive.action is MemoryDirectiveAction.FORGET
    assert directive.target_key == "user.fact.birth_date"
    assert directive.target_text == "我的生日"


async def test_default_extractor_reads_user_only_and_ignores_assistant_claims() -> None:
    extractor = DeterministicMemoryExtractor()
    evidence = TurnEvidence(
        session_id="s1",
        turn_id="t1",
        user_text="你好",
        assistant_text="你的生日是1月2日。",
    )
    assert tuple(await extractor.extract(evidence)) == ()


async def test_default_extractor_passively_accepts_high_confidence_user_fact() -> None:
    extractor = DeterministicMemoryExtractor()
    evidence = TurnEvidence(
        session_id="s1",
        turn_id="t2",
        user_text="我的名字叫冈部",
        assistant_text="知道了。",
    )
    candidates = tuple(await extractor.extract(evidence))
    assert len(candidates) == 1
    assert candidates[0].memory_key == "user.fact.name"
    assert candidates[0].object_text == "冈部"
    assert candidates[0].pinned is False


async def test_default_extractor_ignores_prose_clauses_that_are_not_stable_facts() -> None:
    extractor = DeterministicMemoryExtractor()
    evidence = TurnEvidence(
        session_id="s1",
        turn_id="t3",
        user_text=(
            "我的意思是说,我并没有忙着 读书,而是在升级你本身的项目代码。"
            "你就当产生了些小误会吧,不要继续纠结了"
        ),
        assistant_text="收到了。",
    )
    assert tuple(await extractor.extract(evidence)) == ()


async def test_explicit_remember_still_stores_free_form_evidence() -> None:
    extractor = DeterministicMemoryExtractor()
    evidence = TurnEvidence(
        session_id="s1",
        turn_id="t4",
        user_text="记住我们之间的暗号是蓝鲸",
        assistant_text="记住了。",
    )
    candidates = tuple(await extractor.extract(evidence))
    assert len(candidates) == 1
    assert candidates[0].pinned is True
    assert candidates[0].explicit_keep is True
    assert candidates[0].object_text == "我们之间的暗号是蓝鲸"
