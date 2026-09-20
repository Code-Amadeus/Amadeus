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


async def test_prose_clause_is_not_read_as_a_stable_fact() -> None:
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
    candidates = tuple(await extractor.extract(evidence))
    assert len(candidates) == 1
    # It is kept as a conversation record, never as a durable user fact slot.
    assert candidates[0].kind is MemoryKind.EPISODIC
    assert candidates[0].memory_key.startswith("user.episode.")


async def test_substantive_utterance_is_kept_as_conversation_memory() -> None:
    extractor = DeterministicMemoryExtractor()
    candidates = tuple(
        await extractor.extract(
            TurnEvidence(
                session_id="s1",
                turn_id="t5",
                user_text="其实是说魔族不懂人性,只是说谎",
                assistant_text="そうね。",
            )
        )
    )
    assert len(candidates) == 1
    assert candidates[0].kind is MemoryKind.EPISODIC
    assert candidates[0].summary == "其实是说魔族不懂人性,只是说谎"
    assert candidates[0].object_text == candidates[0].summary
    assert candidates[0].priority_class is MemoryPriorityClass.P3


async def test_short_acknowledgements_are_not_remembered() -> None:
    extractor = DeterministicMemoryExtractor()
    for turn_id, text in (("t6", "好了知道了"), ("t7", "还是。"), ("t8", "你好")):
        candidates = tuple(
            await extractor.extract(
                TurnEvidence(session_id="s1", turn_id=turn_id, user_text=text)
            )
        )
        assert candidates == (), text


async def test_time_anchored_short_statement_is_remembered() -> None:
    extractor = DeterministicMemoryExtractor()
    candidates = tuple(
        await extractor.extract(
            TurnEvidence(session_id="s1", turn_id="t9", user_text="昨天去海边了")
        )
    )
    assert len(candidates) == 1
    assert candidates[0].kind is MemoryKind.EPISODIC
    assert candidates[0].summary == "昨天去海边了"


async def test_future_plan_becomes_an_open_loop() -> None:
    extractor = DeterministicMemoryExtractor()
    candidates = tuple(
        await extractor.extract(
            TurnEvidence(
                session_id="s1",
                turn_id="t10",
                user_text="下个月打算去京都看那家甜点店",
            )
        )
    )
    assert len(candidates) == 1
    assert candidates[0].kind is MemoryKind.OPEN_LOOP
    assert candidates[0].future_value == 0.6


def test_mute_directive_parses_concrete_topic_only() -> None:
    directive = parse_explicit_memory_directive("不要再提京都那家甜点店了")
    assert directive is not None
    assert directive.action is MemoryDirectiveAction.MUTE
    assert directive.target_text == "京都那家甜点店"

    english = parse_explicit_memory_directive("don't mention the release plan anymore")
    assert english is not None
    assert english.action is MemoryDirectiveAction.MUTE
    assert english.target_text == "the release plan"

    for ambiguous in ("别再提这个了", "不要聊天了", "don't mention it"):
        declined = parse_explicit_memory_directive(ambiguous)
        assert declined is not None and declined.action is MemoryDirectiveAction.MUTE, ambiguous
        assert declined.target_text == "", ambiguous


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
