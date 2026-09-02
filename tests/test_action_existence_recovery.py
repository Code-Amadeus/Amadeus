from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

from config import settings
from core.chat_runtime import ChatRuntime, _TurnState
from server.action_existence_recovery import (
    ActionExistenceVerdict,
    build_action_existence_messages,
    classify_action_existence,
    reconstruct_delegate_commitment,
)
from server.auip_control_decision import AuipControlDecision


def _state() -> _TurnState:
    state = _TurnState(
        gui_callback=None,
        turn_id="turn-recovery",
        question="帮我查 Paxos 论文",
        session_id="session-recovery",
        control_prior_messages=[
            {"role": "user", "content": "我们在讨论分布式共识。"},
            {"role": "assistant", "content": "Paxos 是经典方向。"},
        ],
    )
    state.full_response = "好，我去查一下相关论文。"
    return state


def test_neutral_gate_has_no_current_assistant_commitment_or_action_payload() -> None:
    messages = build_action_existence_messages(
        user_text="快去",
        prior_messages=[
            {"role": "user", "content": "帮我查 Paxos 论文。"},
            {"role": "assistant", "content": "你希望我现在开始吗？"},
        ],
    )
    assert messages[-1]["role"] == "user"
    assert "快去" in messages[-1]["content"]
    system = messages[0]["content"]
    assert "Do not choose a provider, task payload, project, or target" in system
    assert "current assistant" not in system.lower()


def test_neutral_gate_parses_only_the_closed_verdict_shape() -> None:
    async def scenario() -> None:
        async def work_query(_messages: list[dict[str, str]]) -> str:
            return json.dumps(
                {
                    "existence": "work",
                    "reason": "The latest turn directly asks for research.",
                }
            )

        verdict = await classify_action_existence(
            work_query,
            user_text="帮我查 Paxos 论文",
        )
        assert verdict.status == "ok" and verdict.existence == "work"

        async def payload_query(_messages: list[dict[str, str]]) -> str:
            return json.dumps(
                {
                    "existence": "work",
                    "reason": "request",
                    "provider": "openclaw",
                }
            )

        invalid = await classify_action_existence(
            payload_query,
            user_text="帮我查 Paxos 论文",
        )
        assert invalid.status == "invalid"

    asyncio.run(scenario())


def test_commitment_recovery_accepts_only_a_closed_model_owned_delegate() -> None:
    async def scenario() -> None:
        async def query(_messages: list[dict[str, str]]) -> str:
            return json.dumps(
                {
                    "commitment": "delegate",
                    "delegate": {
                        "provider": "openclaw",
                        "intent": "execute",
                        "task": "Research the classic Paxos paper.",
                    },
                    "reason": "The visible reply explicitly committed to research.",
                }
            )

        recovered = await reconstruct_delegate_commitment(
            query,
            system_prompt="Use the registered control vocabulary.",
            user_text="帮我查 Paxos 论文",
            assistant_reply="好，我现在去查。",
        )
        assert recovered.status == "ok" and recovered.committed is True
        assert recovered.delegate == {
            "provider": "openclaw",
            "intent": "execute",
            "task": "Research the classic Paxos paper.",
        }

        async def invented(_messages: list[dict[str, str]]) -> str:
            return json.dumps(
                {
                    "commitment": "delegate",
                    "delegate": {"provider": "openclaw", "secret_field": "x"},
                    "reason": "invalid",
                }
            )

        invalid = await reconstruct_delegate_commitment(
            invented,
            system_prompt="control",
            user_text="查一下",
            assistant_reply="好",
        )
        assert invalid.status == "invalid"

    asyncio.run(scenario())


def test_recovered_delegate_keeps_the_originating_chat_turn() -> None:
    async def scenario() -> None:
        state = _state()
        actions = [
            {
                "type": "DELEGATE",
                "attrs": {
                    "provider": "codex",
                    "intent": "execute",
                    "task": "Create the requested page.",
                },
            }
        ]
        with (
            patch("core.chat_runtime.record_actions", return_value=None) as record,
            patch.object(ChatRuntime, "_ground_unique_active_amendment"),
            patch.object(ChatRuntime, "_ground_present_provider_delegate"),
        ):
            dispatched = await ChatRuntime._dispatch_delegate_resend(
                state,
                actions,
                state.question,
                session_id=state.session_id,
            )

        assert dispatched is True
        attrs = record.call_args.args[0][0]["attrs"]
        assert attrs["_host_turn_id"] == state.turn_id
        assert attrs["_host_source_user_text"] == state.question
        context = attrs["_host_source_user_context"]
        assert 'User: "我们在讨论分布式共识。"' in context
        assert 'Main Chat: "Paxos 是经典方向。"' in context

    asyncio.run(scenario())


def test_delegate_source_context_keeps_the_multi_turn_goal_and_commitment() -> None:
    action = {"type": "DELEGATE", "attrs": {}}
    ChatRuntime._annotate_delegate_source(
        action,
        "你怎么没去？",
        turn_id="turn-missed-handoff",
        prior_messages=[
            {"role": "user", "content": "更新桌面的宝可梦战斗小游戏。"},
            {
                "role": "assistant",
                "content": "官方素材有版权风险，我会找可用的免费或公版像素素材。",
            },
            {
                "role": "user",
                "content": "这是学习使用，不是商业创作，你去找找看，然后更新。",
            },
            {
                "role": "assistant",
                "content": (
                    "我现在开始找素材并更新桌面文件。 "
                    '[DELEGATE provider="codex" task="update the game"]'
                ),
            },
        ],
    )

    attrs = action["attrs"]
    context = attrs["_host_source_user_context"]
    assert attrs["_host_source_user_text"] == "你怎么没去？"
    assert 'User: "更新桌面的宝可梦战斗小游戏。"' in context
    assert "Main Chat:" in context
    assert "免费或公版像素素材" in context
    assert "你去找找看，然后更新" in context
    assert "我现在开始找素材并更新桌面文件" in context
    assert "[DELEGATE" not in context
    assert "你怎么没去？" not in context
    assert len(context) <= 2000


def test_recovered_active_app_amendment_rejoins_deferred_launch_composition() -> None:
    async def scenario() -> None:
        state = _state()
        state.question = "把标题改成实验台，改好后重新打开。"
        state.auip_decision_result = AuipControlDecision(
            status="ok",
            action="launch",
            timing="after_work",
            mode="collaborate",
            work_relation="independent",
            app_session_id="app-old",
        )
        state.auip_decision_task = asyncio.create_task(asyncio.sleep(0))
        routed: list[dict] = []

        async def route(attrs, **_context):
            routed.append(dict(attrs))

        runtime = ChatRuntime()
        runtime.configure(auip_control_callback=route)
        actions = [
            {
                "type": "DELEGATE",
                "attrs": {
                    "provider": "codex",
                    "intent": "amend",
                    "subject": "work_item",
                    "workspace_ref": "work-reactor",
                    "task": "change the title and reopen",
                },
            }
        ]
        with (
            patch("core.chat_runtime.record_actions", return_value=None) as record,
            patch.object(ChatRuntime, "_ground_unique_active_amendment"),
            patch.object(ChatRuntime, "_ground_present_provider_delegate"),
        ):
            dispatched = await ChatRuntime._dispatch_delegate_resend(
                state,
                actions,
                state.question,
                session_id=state.session_id,
                work_guard=runtime._guard_work_actions_against_auip,
                schedule_auip_after_work=runtime._schedule_auip_after_effective_work,
            )

        assert dispatched is True
        attrs = record.call_args.args[0][0]["attrs"]
        assert attrs["_host_dispatch_source"] == "auip_create"
        assert state.work_delegate_seen is True
        assert state.auip_decision_dispatched is True
        assert routed == [
            {
                "action": "launch",
                "target": "delivery",
                "mode": "collaborate",
                "after": "work",
                "_host_app_session_id": "app-old",
                "_host_work_binding": "turn",
                "_host_work_item_id": "work-reactor",
            }
        ]

    asyncio.run(scenario())


def test_candidate_recovery_requires_neutral_work_and_role_commitment() -> None:
    async def scenario() -> None:
        state = _state()
        recovered = [{"type": "DELEGATE", "attrs": {"task": "Research Paxos"}}]
        with (
            patch.object(
                settings,
                "ACTION_EXISTENCE_COMMITMENT_RECOVERY_MODE",
                "candidate",
            ),
            patch.object(
                ChatRuntime,
                "_request_neutral_action_existence",
                new=AsyncMock(
                    return_value=ActionExistenceVerdict(
                        status="ok",
                        existence="work",
                        reason="direct request",
                    )
                ),
            ) as gate,
            patch.object(
                ChatRuntime,
                "_request_structured_commitment_recovery",
                new=AsyncMock(return_value=recovered),
            ) as resend,
            patch.object(
                ChatRuntime,
                "_dispatch_delegate_resend",
                new=AsyncMock(return_value=True),
            ) as dispatch,
        ):
            result = await ChatRuntime._repair_missing_delegate(
                state,
                "帮我查 Paxos 论文",
                session_id="session-recovery",
                control_resolver=object(),
            )
        assert result is True
        gate.assert_awaited_once()
        resend.assert_awaited_once()
        dispatch.assert_awaited_once()

    asyncio.run(scenario())


def test_candidate_recovery_is_shadowed_when_auip_subsumes_the_turn() -> None:
    async def scenario() -> None:
        state = _state()
        state.auip_decision_result = AuipControlDecision(
            status="ok",
            action="step",
            instruction="start the round",
            work_relation="subsumed",
        )
        recovered = [{"type": "DELEGATE", "attrs": {"task": "start it"}}]
        with (
            patch.object(
                settings,
                "ACTION_EXISTENCE_COMMITMENT_RECOVERY_MODE",
                "candidate",
            ),
            patch.object(
                ChatRuntime,
                "_request_neutral_action_existence",
                new=AsyncMock(
                    return_value=ActionExistenceVerdict(
                        status="ok",
                        existence="work",
                        reason="fixture deliberately misclassifies the app action",
                    )
                ),
            ) as gate,
            patch.object(
                ChatRuntime,
                "_request_structured_commitment_recovery",
                new=AsyncMock(return_value=recovered),
            ) as resend,
            patch.object(
                ChatRuntime,
                "_dispatch_delegate_resend",
                new=AsyncMock(return_value=True),
            ) as dispatch,
        ):
            result = await ChatRuntime._repair_missing_delegate(
                state,
                "那你开始吧",
                session_id="session-recovery",
                control_resolver=object(),
            )
        assert result is False
        gate.assert_awaited_once()
        resend.assert_awaited_once()
        dispatch.assert_not_awaited()

    asyncio.run(scenario())


def test_candidate_recovery_does_not_duplicate_dispatched_auip_prepare() -> None:
    async def scenario() -> None:
        state = _state()
        state.auip_decision_result = AuipControlDecision(
            status="ok",
            action="prepare",
            preparation_work_item_id="work-existing-app",
        )
        state.auip_decision_dispatched = True
        with (
            patch.object(
                settings,
                "ACTION_EXISTENCE_COMMITMENT_RECOVERY_MODE",
                "candidate",
            ),
            patch.object(
                ChatRuntime,
                "_request_neutral_action_existence",
                new=AsyncMock(),
            ) as gate,
            patch.object(
                ChatRuntime,
                "_request_structured_commitment_recovery",
                new=AsyncMock(),
            ) as resend,
            patch.object(
                ChatRuntime,
                "_dispatch_delegate_resend",
                new=AsyncMock(return_value=True),
            ) as dispatch,
        ):
            result = await ChatRuntime._repair_missing_delegate(
                state,
                "请你接入它。",
                session_id="session-recovery",
                control_resolver=object(),
            )
        assert result is False
        gate.assert_not_awaited()
        resend.assert_not_awaited()
        dispatch.assert_not_awaited()

    asyncio.run(scenario())


def test_candidate_recovery_stays_live_for_auip_independent_work() -> None:
    async def scenario() -> None:
        state = _state()
        state.auip_decision_result = AuipControlDecision(
            status="ok",
            action="none",
            work_relation="independent",
        )
        recovered = [
            {
                "type": "DELEGATE",
                "attrs": {"task": "Change the active game's board to 19x19"},
            }
        ]
        with (
            patch.object(
                settings,
                "ACTION_EXISTENCE_COMMITMENT_RECOVERY_MODE",
                "candidate",
            ),
            patch.object(
                ChatRuntime,
                "_request_neutral_action_existence",
                new=AsyncMock(
                    return_value=ActionExistenceVerdict(
                        status="ok",
                        existence="work",
                        reason="separate application authoring request",
                    )
                ),
            ),
            patch.object(
                ChatRuntime,
                "_request_structured_commitment_recovery",
                new=AsyncMock(return_value=recovered),
            ),
            patch.object(
                ChatRuntime,
                "_dispatch_delegate_resend",
                new=AsyncMock(return_value=True),
            ) as dispatch,
        ):
            result = await ChatRuntime._repair_missing_delegate(
                state,
                "这个棋盘太小了，把游戏改成十九乘十九",
                session_id="session-recovery",
                control_resolver=object(),
            )
        assert result is True
        dispatch.assert_awaited_once()

    asyncio.run(scenario())


def test_no_work_or_shadow_never_reaches_dispatch() -> None:
    async def scenario() -> None:
        for mode, existence, expected_resend in (
            ("candidate", "no_work", 0),
            ("candidate", "unsure", 0),
            ("shadow", "work", 1),
        ):
            state = _state()
            with (
                patch.object(
                    settings,
                    "ACTION_EXISTENCE_COMMITMENT_RECOVERY_MODE",
                    mode,
                ),
                patch.object(
                    ChatRuntime,
                    "_request_neutral_action_existence",
                    new=AsyncMock(
                        return_value=ActionExistenceVerdict(
                            status="ok",
                            existence=existence,
                            reason="fixture",
                        )
                    ),
                ),
                patch.object(
                    ChatRuntime,
                    "_request_structured_commitment_recovery",
                    new=AsyncMock(
                        return_value=[
                            {
                                "type": "DELEGATE",
                                "attrs": {"task": "should not dispatch"},
                            }
                        ]
                    ),
                ) as resend,
                patch.object(
                    ChatRuntime,
                    "_dispatch_delegate_resend",
                    new=AsyncMock(return_value=True),
                ) as dispatch,
            ):
                result = await ChatRuntime._repair_missing_delegate(
                    state,
                    "这只是一次评论",
                    session_id="session-recovery",
                    control_resolver=object(),
                )
            assert result is False
            assert resend.await_count == expected_resend
            dispatch.assert_not_awaited()

    asyncio.run(scenario())
