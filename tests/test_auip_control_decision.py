"""AUIP action existence is source-local and independent from Work routing."""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.chat_runtime import (
    ChatRuntime,
    _TurnState,
    _turn_role_grounding,
    _turn_system_prompt,
    _turn_uses_conversation_history,
)
from core.session_manager import ConversationHistory
from agent_host.provider_catalog import CODEX_APP_SERVER_MANIFEST
from agent_host.provider_runtime import runtime as provider_runtime
from server.auip_control_decision import (
    _ACTIVE_SESSION_SYSTEM_PROMPT,
    _INACTIVE_ENTRY_SYSTEM_PROMPT,
    AuipControlDecision,
    AuipControlDecisionResolver,
    is_live_auip_control_projection,
    parse_auip_control_decision,
    reconcile_active_auip_control,
    render_auip_role_grounding,
)
from server.host_action_dispatcher import _delegate_call
from server.auip_runtime import AuipRuntime
from server.work_destination_service import WorkDestinationService


class _Runtime:
    def __init__(self, projection=None, *, read_answer: str = "") -> None:
        self.projection = projection
        self.read_answer = read_answer
        self.read_calls: list[dict] = []

    def focused_projection(self, _session_id: str):
        return self.projection

    def render_read_only_answer(self, app_session_id: str, **kwargs):
        self.read_calls.append({"app_session_id": app_session_id, **kwargs})
        return self.read_answer


def test_only_live_or_still_open_completed_surfaces_gate_role_grounding() -> None:
    assert is_live_auip_control_projection({"status": "active"}) is True
    assert (
        is_live_auip_control_projection(
            {
                "status": "completed",
                "host_surface_id": "surface-result",
                "surface_close_status": "not_requested",
            }
        )
        is True
    )
    assert (
        is_live_auip_control_projection(
            {
                "status": "completed",
                "host_surface_id": "surface-result",
                "surface_close_status": "closed",
            }
        )
        is False
    )
    assert is_live_auip_control_projection({"status": "closed"}) is False
    assert is_live_auip_control_projection({"status": "disconnected"}) is False
    assert is_live_auip_control_projection(None) is False


def test_collaborate_mode_never_manufactures_application_turns_or_roles() -> None:
    assert "application's accepted mechanics" in _ACTIVE_SESSION_SYSTEM_PROMPT
    assert "does not\ncreate alternating turns, player roles" in _ACTIVE_SESSION_SYSTEM_PROMPT
    assert "application's accepted mechanics" in _INACTIVE_ENTRY_SYSTEM_PROMPT
    assert "without inventing turns or roles" in _INACTIVE_ENTRY_SYSTEM_PROMPT
    assert "`collaborate` means they take turns" not in _ACTIVE_SESSION_SYSTEM_PROMPT
    assert "`collaborate` means they take turns" not in _INACTIVE_ENTRY_SYSTEM_PROMPT
    assert "退出这局，但别关游戏" in _ACTIVE_SESSION_SYSTEM_PROMPT
    assert "再来一局" in _ACTIVE_SESSION_SYSTEM_PROMPT
    assert "你别操作了，我自己来" in _ACTIVE_SESSION_SYSTEM_PROMPT
    assert "关掉游戏" in _ACTIVE_SESSION_SYSTEM_PROMPT
    assert "application-domain lifecycle" in _ACTIVE_SESSION_SYSTEM_PROMPT
    assert "把棋盘改成十九乘十九" in _ACTIVE_SESSION_SYSTEM_PROMPT
    assert "application-authoring Work" in _ACTIVE_SESSION_SYSTEM_PROMPT
    assert "may refer to the\nsame focused application" in _ACTIVE_SESSION_SYSTEM_PROMPT


class _Catalog:
    def __init__(self, *titles: str, preparation_titles: tuple[str, ...] = ()) -> None:
        self.items = [
            SimpleNamespace(
                title=title,
                prompt_dict=lambda title=title: {
                    "app": title,
                    "modes": ["observe", "collaborate", "delegate"],
                },
            )
            for title in titles
        ]
        self.preparation_items = [
            SimpleNamespace(
                title=title,
                work_item_id=f"work-private-{index}",
            )
            for index, title in enumerate(preparation_titles, 1)
        ]

    def candidates(self, _session_id: str, *, limit: int = 8):
        return self.items[:limit]

    def preparation_candidates(self, _session_id: str, *, limit: int = 8):
        return self.preparation_items[:limit]


def test_active_application_launch_is_canonicalized_to_a_mode_transition() -> None:
    active = {
        "status": "active",
        "app": {"title": "井字棋"},
        "engagement_mode": "observe",
    }
    assert reconcile_active_auip_control(
        {"action": "launch", "mode": "collaborate"},
        active,
    ) == {"action": "collaborate"}
    assert reconcile_active_auip_control(
        {"action": "launch", "target": "井字棋", "mode": "delegate"},
        active,
    ) == {"action": "delegate"}
    assert reconcile_active_auip_control(
        {"action": "engage", "target": "井字棋", "mode": "observe"},
        active,
    ) == {"action": "observe"}

    other = {"action": "launch", "target": "2048", "mode": "observe"}
    assert reconcile_active_auip_control(other, active) == other
    deferred = {
        "action": "launch",
        "target": "delivery",
        "mode": "collaborate",
        "after": "work",
    }
    assert reconcile_active_auip_control(deferred, active) == deferred


def test_resolver_does_not_query_ordinary_chat_without_auip_scope() -> None:
    async def query(_messages):
        raise AssertionError("query must not run")

    resolver = AuipControlDecisionResolver(
        query=query,
        app_runtime=_Runtime(),
        launch_catalog=_Catalog(),
    )
    assert resolver.capture(session_id="s", user_text="今天天气不错") is None


def test_active_router_receives_bounded_action_semantics_for_work_boundary() -> None:
    async def scenario() -> None:
        captured: list[dict[str, str]] = []

        async def query(messages):
            captured.extend(messages)
            return '{"action":"none","work_relation":"independent","read":[]}'

        resolver = AuipControlDecisionResolver(
            query=query,
            app_runtime=_Runtime(
                {
                    "status": "active",
                    "app_session_id": "app-private",
                    "app": {"title": "Board"},
                    "role_addressable_action_types": ["game.place"],
                    "available_action_semantics": {
                        "game.place": "Place one stone on an empty intersection."
                    },
                }
            ),
            launch_catalog=_Catalog(),
        )
        pending = resolver.capture(
            session_id="session",
            user_text="把棋盘改成十九乘十九",
        )
        assert pending is not None
        decision = await pending

        assert decision.action == "none"
        assert decision.work_relation == "independent"
        wire = json.dumps(captured, ensure_ascii=False)
        assert "Place one stone on an empty intersection" in wire
        assert "app-private" not in wire

    asyncio.run(scenario())


def test_resolver_sees_titles_and_exact_user_words_but_no_host_ids() -> None:
    async def scenario() -> None:
        captured = []

        async def query(messages):
            captured.extend(messages)
            return json.dumps(
                {
                    "action": "engage",
                    "timing": "now",
                    "mode": "collaborate",
                    "target": "井字棋",
                    "work_relation": "subsumed",
                },
                ensure_ascii=False,
            )

        resolver = AuipControlDecisionResolver(
            query=query,
            app_runtime=_Runtime(),
            launch_catalog=_Catalog("井字棋"),
        )
        pending = resolver.capture(
            session_id="session-secret",
            user_text="打开刚才的井字棋，我们一起玩",
            prior_messages=[{"role": "assistant", "content": "已经做好了。"}],
        )
        assert pending is not None
        decision = await pending
        assert decision.control_attrs() == {
            "action": "launch",
            "mode": "collaborate",
            "target": "井字棋",
        }
        assert decision.work_relation == "subsumed"
        wire = json.dumps(captured, ensure_ascii=False)
        assert "打开刚才的井字棋，我们一起玩" in wire
        assert "井字棋" in wire
        assert "session-secret" not in wire
        assert "artifact_" not in wire
        assert "auip:" not in wire

    asyncio.run(scenario())


def test_resolver_can_request_bounded_preparation_without_exposing_work_identity() -> None:
    async def scenario() -> None:
        captured = []

        async def query(messages):
            captured.extend(messages)
            return json.dumps(
                {
                    "action": "engage",
                    "timing": "now",
                    "mode": "collaborate",
                    "target": "井字棋",
                    "work_relation": "subsumed",
                },
                ensure_ascii=False,
            )

        resolver = AuipControlDecisionResolver(
            query=query,
            app_runtime=_Runtime(),
            launch_catalog=_Catalog(preparation_titles=("井字棋",)),
        )
        pending = resolver.capture(
            session_id="session-secret",
            user_text="不用移到桌面了，直接打开原来的棋，我想和你下一盘。",
            prior_messages=[
                {"role": "assistant", "content": "棋盘已经写好了。"},
            ],
        )
        assert pending is not None
        decision = await pending
        assert decision.control_attrs() == {
            "action": "prepare",
            "mode": "collaborate",
            "target": "井字棋",
            "_host_preparation_work_item_id": "work-private-1",
        }
        wire = json.dumps(captured, ensure_ascii=False)
        assert "preparable_apps" in wire
        assert "井字棋" in wire
        assert "work-private-1" not in wire
        assert "session-secret" not in wire

    asyncio.run(scenario())


def test_strict_parser_separates_auip_timing_from_work_authority() -> None:
    engage = parse_auip_control_decision(
        '{"action":"engage","timing":"now","mode":"observe","target":"井字棋","work_relation":"subsumed"}',
        has_active=False,
        candidate_titles={"井字棋"},
        allow_after_work=True,
    )
    assert engage.action == "engage"
    assert engage.work_relation == "subsumed"
    assert parse_auip_control_decision(
        '{"action":"engage","timing":"now","mode":"observe","target":"井字棋","work_relation":"subsumed"}',
        has_active=True,
        candidate_titles={"井字棋"},
        allow_after_work=True,
    ).status == "invalid"
    active_entry = parse_auip_control_decision(
        '{"action":"engage","timing":"now","mode":"observe","target":"井字棋","work_relation":"subsumed"}',
        has_active=True,
        active_title="井字棋",
        candidate_titles={"井字棋"},
        allow_after_work=True,
    )
    assert active_entry.status == "ok"
    assert active_entry.action == "observe"
    assert active_entry.work_relation == "subsumed"
    deferred = parse_auip_control_decision(
        '{"action":"launch","timing":"after_work","mode":"collaborate","target":""}',
        has_active=False,
        candidate_titles=set(),
        allow_after_work=True,
    )
    assert deferred.control_attrs() == {
        "action": "launch",
        "target": "delivery",
        "mode": "collaborate",
        "after": "work",
    }
    assert parse_auip_control_decision(
        '{"action":"launch","timing":"now","mode":"observe","target":"","work_relation":"subsumed"}',
        has_active=False,
        candidate_titles=set(),
        allow_after_work=True,
    ).status == "invalid"
    assert parse_auip_control_decision(
        '{"action":"launch","timing":"now","mode":"observe","target":"井字棋"}',
        has_active=False,
        candidate_titles={"井字棋"},
        allow_after_work=True,
    ).status == "invalid"
    independent = parse_auip_control_decision(
        '{"action":"launch","timing":"now","mode":"observe","target":"井字棋","work_relation":"independent"}',
        has_active=False,
        candidate_titles={"井字棋"},
        allow_after_work=True,
    )
    assert independent.status == "ok"
    assert independent.work_relation == "independent"
    preparation = parse_auip_control_decision(
        '{"action":"prepare","mode":"collaborate","target":"井字棋"}',
        has_active=False,
        candidate_titles=set(),
        preparation_titles={"井字棋"},
        allow_after_work=True,
    )
    assert preparation.control_attrs() == {
        "action": "prepare",
        "mode": "collaborate",
        "target": "井字棋",
    }
    assert parse_auip_control_decision(
        '{"action":"prepare","mode":"collaborate","target":"井字棋"}',
        has_active=False,
        candidate_titles=set(),
        preparation_titles=set(),
        allow_after_work=True,
    ).status == "invalid"


def test_after_work_engage_compiles_to_preparation_for_existing_unwired_app() -> None:
    async def scenario() -> None:
        async def query(_messages):
            return (
                '{"action":"engage","timing":"after_work",'
                '"mode":"collaborate","target":""}'
            )

        resolver = AuipControlDecisionResolver(
            query=query,
            app_runtime=_Runtime(None),
            launch_catalog=_Catalog(preparation_titles=("信号路由",)),
        )
        pending = resolver.capture(
            session_id="session-prepare-after-work",
            user_text="接好以后直接打开，我们一起试一下。",
        )
        assert pending is not None
        decision = await pending

        assert decision.control_attrs() == {
            "action": "prepare",
            "mode": "collaborate",
            "target": "信号路由",
            "_host_preparation_work_item_id": "work-private-1",
        }

    asyncio.run(scenario())


def test_active_app_amend_and_reopen_binds_the_replaced_appsession() -> None:
    async def scenario() -> None:
        async def query(_messages):
            return (
                '{"action":"launch","timing":"after_work",'
                '"mode":"collaborate","target":"反应堆",'
                '"work_relation":"independent"}'
            )

        resolver = AuipControlDecisionResolver(
            query=query,
            app_runtime=_Runtime(
                {
                    "app_session_id": "app-current-version",
                    "status": "active",
                    "app": {"title": "反应堆"},
                    "available_modes": ["observe", "collaborate", "delegate"],
                    "state": {},
                }
            ),
            launch_catalog=_Catalog(),
        )
        pending = resolver.capture(
            session_id="session-active-replacement",
            user_text="把标题改一下，改好后重新打开，我们继续。",
        )
        assert pending is not None
        decision = await pending

        assert decision.status == "ok"
        assert decision.work_relation == "independent"
        assert decision.app_session_id == "app-current-version"
        assert decision.control_attrs() == {
            "action": "launch",
            "target": "delivery",
            "mode": "collaborate",
            "after": "work",
            "_host_app_session_id": "app-current-version",
        }

    asyncio.run(scenario())


def test_completed_visible_app_cannot_be_bound_as_active_replacement() -> None:
    async def scenario() -> None:
        async def query(_messages):
            return (
                '{"action":"launch","timing":"after_work",'
                '"mode":"collaborate","target":"反应堆",'
                '"work_relation":"independent"}'
            )

        resolver = AuipControlDecisionResolver(
            query=query,
            app_runtime=_Runtime(
                {
                    "app_session_id": "app-completed",
                    "status": "completed",
                    "host_surface_id": "surface-completed",
                    "surface_close_status": "not_requested",
                    "app": {"title": "反应堆"},
                    "available_modes": ["observe", "collaborate", "delegate"],
                    "state": {},
                }
            ),
            launch_catalog=_Catalog(),
        )
        pending = resolver.capture(
            session_id="session-completed-replacement",
            user_text="把标题改一下，改好后重新打开，我们继续。",
        )
        assert pending is not None
        decision = await pending

        assert decision.status == "invalid"
        assert decision.app_session_id == ""

    asyncio.run(scenario())


def test_resolver_compiles_redundant_entry_to_active_mode_without_retry() -> None:
    async def scenario() -> None:
        calls: list[list[dict[str, str]]] = []

        async def query(messages):
            calls.append(messages)
            return (
                '{"action":"engage","timing":"now","mode":"observe",'
                '"target":"井字棋","work_relation":"subsumed"}'
            )

        resolver = AuipControlDecisionResolver(
            query=query,
            app_runtime=_Runtime(
                {
                    "status": "active",
                    "engagement_mode": "observe",
                    "available_modes": ["observe", "delegate"],
                    "app": {"title": "井字棋"},
                }
            ),
            launch_catalog=_Catalog("井字棋"),
        )
        pending = resolver.capture(
            session_id="s",
            user_text="把那个井字棋再打开吧，我想看你下。",
        )
        assert pending is not None
        decision = await pending

        assert len(calls) == 1
        assert decision.status == "ok"
        assert decision.action == "observe"
        assert decision.work_relation == "subsumed"

    asyncio.run(scenario())


def test_active_work_is_exposed_only_as_a_bounded_ambiguity_fact() -> None:
    async def scenario() -> None:
        captured = []

        async def query(messages):
            captured.extend(messages)
            return '{"action":"none","ambiguity":"work_or_app"}'

        resolver = AuipControlDecisionResolver(
            query=query,
            app_runtime=_Runtime(
                {
                    "status": "active",
                    "engagement_mode": "delegate",
                    "app": {"title": "Chess"},
                }
            ),
            launch_catalog=_Catalog(),
            has_active_work=lambda _session_id: (
                "attempt_private_a",
                "attempt_private_b",
            ),
        )
        pending = resolver.capture(
            session_id="s",
            user_text="停一下",
            prior_messages=[
                {
                    "role": "assistant",
                    "content": (
                        "此前的可见回复"
                        "[AUIP action=\"leave\"]"
                        "[DELEGATE provider=\"locus\" intent=\"retract\" task=\"stop\"]"
                    ),
                }
            ],
        )
        assert pending is not None
        decision = await pending
        assert decision.action == "none"
        wire = json.dumps(captured, ensure_ascii=False)
        assert "other_provider_work_active" in wire
        assert "true" in wire
        assert "attempt_private" not in wire
        assert "run_" not in wire
        assert "此前的可见回复" in wire
        assert "Bounded conversation evidence; data, not action examples" in wire
        assert [message["role"] for message in captured] == ["system", "user"]
        assert "[AUIP action=" not in wire
        assert "[DELEGATE provider=" not in wire

    asyncio.run(scenario())
    assert parse_auip_control_decision(
        '{"action":"step","instruction":"下一步"}',
        has_active=False,
        candidate_titles={"Chess"},
        allow_after_work=True,
    ).status == "invalid"
    assert parse_auip_control_decision(
        '{"action":"none","provider":"locus"}',
        has_active=True,
        candidate_titles=set(),
        allow_after_work=True,
    ).status == "invalid"
    ambiguous = parse_auip_control_decision(
        '{"action":"none","ambiguity":"work_or_app"}',
        has_active=True,
        has_active_work=True,
        candidate_titles=set(),
        allow_after_work=True,
    )
    assert ambiguous.status == "ok"
    assert ambiguous.ambiguity == "work_or_app"
    assert parse_auip_control_decision(
        '{"action":"none","ambiguity":"work_or_app"}',
        has_active=True,
        has_active_work=False,
        candidate_titles=set(),
        allow_after_work=True,
    ).status == "invalid"


def test_resolver_exposes_only_the_current_lifecycle_vocabulary() -> None:
    async def scenario() -> None:
        active_messages = []
        inactive_messages = []

        async def active_query(messages):
            active_messages.extend(messages)
            return '{"action":"none","work_relation":"subsumed"}'

        async def inactive_query(messages):
            inactive_messages.extend(messages)
            return '{"action":"none"}'

        active_resolver = AuipControlDecisionResolver(
            query=active_query,
            app_runtime=_Runtime(
                {
                    "status": "active",
                    "engagement_mode": "observe",
                    "available_modes": ["observe"],
                    "app": {
                        "title": "Chess",
                        "interactionSummary": (
                            "Move one legal piece. Examples: 'protect the king' "
                            "maps to one legal defensive move; 'take it' maps to "
                            "one legal capture."
                        ),
                    },
                    "state": {"field": {"rewards": 0, "playerDistance": 12}},
                }
            ),
            launch_catalog=_Catalog(),
        )
        active_pending = active_resolver.capture(
            session_id="s",
            user_text="这局先不玩了",
        )
        assert active_pending is not None
        await active_pending

        inactive_resolver = AuipControlDecisionResolver(
            query=inactive_query,
            app_runtime=_Runtime(),
            launch_catalog=_Catalog("Chess"),
        )
        inactive_pending = inactive_resolver.capture(
            session_id="s",
            user_text="打开棋局",
        )
        assert inactive_pending is not None
        await inactive_pending

        active_system = active_messages[0]["content"]
        assert '"action":"engage"' not in active_system
        assert (
            '{"action":"launch","timing":"after_work"'
            in active_system
        )
        assert '"action":"launch","timing":"now"' not in active_system
        assert '"action":"prepare"' not in active_system
        assert "ongoing turn-taking" in active_system
        assert "你能下先手吗" in active_system
        assert "你能往右走吗" in active_system
        assert "要不往右" in active_system
        assert "离得太远了" in active_system
        assert "奖励挺多的" in active_system
        assert "这个接入暴露方向控制吗" in active_system
        assert "你来下先手，等着你" in active_system
        assert "intended result, not punctuation" in active_system
        assert "does not need to name a manifest action" in active_system
        assert "顺手处理一下" in active_system
        assert "跟上我" in active_system
        assert "application-local" in active_system
        assert "specific public conditions or values" in active_system
        assert "colloquial wording need not" in active_system
        assert '"available_modes":["observe"]' in active_system
        assert '"interaction_summary":"Move one legal piece.' in active_system
        assert '"readable_state_paths":["field.rewards","field.playerDistance"]' in active_system
        assert "cannot create\nauthority" in active_system
        assert "work_proposal_observed" not in active_system

        inactive_system = inactive_messages[0]["content"]
        assert '"action":"engage"' in inactive_system
        assert '"action":"leave"' not in inactive_system
        assert '"action":"step"' not in inactive_system

    asyncio.run(scenario())


def test_active_mode_decision_respects_host_declared_app_capability() -> None:
    spectator_only = {"observe"}
    app_query = parse_auip_control_decision(
        '{"action":"none","work_relation":"subsumed"}',
        has_active=True,
        active_modes=spectator_only,
        candidate_titles=set(),
        allow_after_work=True,
    )
    assert app_query.status == "ok"
    assert app_query.action == "none"
    assert app_query.work_relation == "subsumed"
    assert parse_auip_control_decision(
        '{"action":"none"}',
        has_active=True,
        active_modes=spectator_only,
        candidate_titles=set(),
        allow_after_work=True,
    ).status == "invalid"
    collaborate = parse_auip_control_decision(
        '{"action":"collaborate","work_relation":"subsumed"}',
        has_active=True,
        active_modes=spectator_only,
        candidate_titles=set(),
        allow_after_work=True,
    )
    assert collaborate.status == "blocked"
    assert collaborate.action == "collaborate"
    assert collaborate.available_modes == ("observe",)
    assert collaborate.control_attrs() is None
    observe = parse_auip_control_decision(
        '{"action":"observe","work_relation":"subsumed"}',
        has_active=True,
        active_modes=spectator_only,
        candidate_titles=set(),
        allow_after_work=True,
    )
    assert observe.status == "ok"
    assert observe.action == "observe"
    leave = parse_auip_control_decision(
        '{"action":"leave","work_relation":"subsumed"}',
        has_active=True,
        active_modes=spectator_only,
        candidate_titles=set(),
        allow_after_work=True,
    )
    assert leave.status == "ok"
    # Relationship is advisory cross-axis metadata, not action authority. If
    # omitted, keep the AUIP action but conservatively suppress no Work.
    terse_leave = parse_auip_control_decision(
        '{"action":"leave"}',
        has_active=True,
        active_modes=spectator_only,
        candidate_titles=set(),
        allow_after_work=True,
    )
    assert terse_leave.status == "ok"
    assert terse_leave.action == "leave"
    assert terse_leave.work_relation == ""


def test_read_facets_are_semantic_only_and_host_bound() -> None:
    async def scenario() -> None:
        app_runtime = _Runtime(
            {
                "status": "active",
                "app_session_id": "app-read-exact",
                "app": {"title": "順序ゲーム"},
                "available_modes": ["observe", "collaborate"],
            },
            read_answer="現在は次の段階へ進めるわ。",
        )

        async def query(_messages):
            return (
                '{"action":"none","work_relation":"subsumed",'
                '"read":["receipt","state"]}'
            )

        resolver = AuipControlDecisionResolver(
            query=query,
            app_runtime=app_runtime,
            launch_catalog=_Catalog(),
        )
        pending = resolver.capture(
            session_id="read-session",
            user_text="刚才真的执行了吗？现在是什么状态？",
        )
        assert pending is not None
        decision = await pending
        assert decision.status == "ok"
        assert decision.action == "none"
        assert decision.read_facets == ("receipt", "state")
        assert decision.app_session_id == "app-read-exact"
        assert decision.control_attrs() is None
        assert resolver.render_read_only_answer(decision, language="ja") == (
            "現在は次の段階へ進めるわ。"
        )
        assert app_runtime.read_calls == [
            {
                "app_session_id": "app-read-exact",
                "facets": ("receipt", "state"),
                "state_paths": (),
                "language": "ja",
            }
        ]

    asyncio.run(scenario())

    assert parse_auip_control_decision(
        '{"action":"none","work_relation":"independent","read":["state"]}',
        has_active=True,
        candidate_titles=set(),
        allow_after_work=True,
    ).status == "invalid"
    assert parse_auip_control_decision(
        '{"action":"none","work_relation":"subsumed","read":["state","state"]}',
        has_active=True,
        candidate_titles=set(),
        allow_after_work=True,
    ).status == "invalid"
    selected = parse_auip_control_decision(
        (
            '{"action":"none","work_relation":"subsumed",'
            '"read":["state"],"state_paths":["field.rewards"]}'
        ),
        has_active=True,
        active_state_paths={"field.rewards", "field.playerDistance"},
        candidate_titles=set(),
        allow_after_work=True,
    )
    assert selected.status == "ok"
    assert selected.read_paths == ("field.rewards",)
    invented = parse_auip_control_decision(
        (
            '{"action":"none","work_relation":"subsumed",'
            '"read":["state"],"state_paths":["field.score"]}'
        ),
        has_active=True,
        active_state_paths={"field.rewards"},
        candidate_titles=set(),
        allow_after_work=True,
    )
    assert invented.status == "invalid"


def test_standard_situation_root_is_a_readable_resource_without_dense_children() -> None:
    async def scenario() -> None:
        captured: list[list[dict[str, str]]] = []

        async def query(messages):
            captured.append(messages)
            return (
                '{"action":"none","work_relation":"subsumed",'
                '"read":["state"],'
                '"state_paths":["board","winner","lifecycle"]}'
            )

        resolver = AuipControlDecisionResolver(
            query=query,
            app_runtime=_Runtime(
                {
                    "status": "completed",
                    "app_session_id": "app-grid-read",
                    "host_surface_id": "surface-grid-read",
                    "surface_close_status": "not_requested",
                    "app": {"title": "Grid Read"},
                    "available_modes": ["observe", "collaborate"],
                    "state": {
                        "board": {
                            "kind": "grid/v1",
                            "width": 9,
                            "height": 9,
                            "rows": ["........."] * 9,
                        },
                        "winner": "white",
                        "lifecycle": "concluded",
                    },
                }
            ),
            launch_catalog=_Catalog(),
        )

        pending = resolver.capture(
            session_id="grid-read-session",
            user_text="棋盘现在是空的吗，谁赢了？",
        )
        assert pending is not None
        decision = await pending

        assert decision.status == "ok"
        assert decision.read_paths == ("board", "winner", "lifecycle")
        system = captured[0][0]["content"]
        assert '"readable_state_paths":["board","winner","lifecycle"]' in system
        assert "board.rows" not in system

    asyncio.run(scenario())


def test_completed_owned_surface_keeps_lifecycle_close_in_scope() -> None:
    async def scenario() -> None:
        captured: list[list[dict[str, str]]] = []

        async def query(messages):
            captured.append(messages)
            return '{"action":"leave"}'

        resolver = AuipControlDecisionResolver(
            query=query,
            app_runtime=_Runtime(
                {
                    "status": "completed",
                    "app_session_id": "app-completed",
                    "host_surface_id": "surface-completed",
                    "surface_close_status": "not_requested",
                    "app": {"title": "温控游戏"},
                    "available_modes": ["observe", "collaborate", "delegate"],
                }
            ),
            launch_catalog=_Catalog(),
        )
        pending = resolver.capture(
            session_id="completed-session",
            user_text="现在把它关掉。",
        )
        assert pending is not None
        decision = await pending
        assert decision.status == "ok"
        assert decision.action == "leave"
        assert decision.app_session_id == "app-completed"
        system = captured[0][0]["content"]
        encoded = system.split("[Host AUIP capability facts]\n", 1)[1].split(
            "\n[/Host AUIP capability facts]",
            1,
        )[0]
        payload = json.loads(encoded)
        assert payload["active_app"]["status"] == "completed"
        assert payload["active_app"]["available_modes"] == []

    asyncio.run(scenario())


class _Decider:
    def __init__(self, decision: AuipControlDecision, *, read_answer: str = "") -> None:
        self.decision = decision
        self.read_answer = read_answer

    def capture(self, **_context):
        async def resolve():
            return self.decision

        return resolve()

    def render_read_only_answer(self, decision, *, language: str = "ja"):
        assert decision is self.decision
        assert language in {"ja", "en"}
        return self.read_answer


def _state(
    turn_id: str = "turn-auip-decision",
    *,
    question: str = "打开它，我们一起玩",
) -> _TurnState:
    return _TurnState(
        gui_callback=None,
        turn_id=turn_id,
        question=question,
        session_id="session-auip",
    )


def test_host_read_facts_are_roleified_without_yielding_factual_authority() -> None:
    async def scenario() -> None:
        answer = "直近の操作は受理済みで、次は燃料加圧よ。"
        role_reply = "ええ、操作はちゃんと通ったわ。次は燃料加圧ね。"
        decision = AuipControlDecision(
            status="ok",
            action="none",
            work_relation="subsumed",
            read_facets=("receipt", "state"),
            app_session_id="app-read",
        )
        runtime = ChatRuntime()
        runtime.configure(
            pending_sentence_items=asyncio.Queue(),
            provider="deepseek",
            auip_control_decider=_Decider(decision, read_answer=answer),
        )
        visible: list[str] = []
        spoken: list[str] = []
        grounding: list[str] = []

        async def roleify_model(state, *_args, **_kwargs):
            grounding.append(_turn_role_grounding(state))
            state.full_response = role_reply
            state.history_response = role_reply
            if state.gui_callback:
                state.gui_callback(role_reply)
            await runtime._process_sentence(state, role_reply)

        async def capture_sentence(_state, text, **_kwargs):
            spoken.append(str(text))

        with (
            patch.object(runtime, "_ensure_clients", return_value=None),
            patch.object(runtime, "_run_deepseek_openai", side_effect=roleify_model),
            patch.object(runtime, "_process_sentence", side_effect=capture_sentence),
            patch(
                "server.auip_runtime.runtime.focused_projection",
                return_value={"status": "active", "app_session_id": "app-read"},
            ),
            patch(
                "core.chat_runtime._turn_system_prompt",
                return_value="ROLE\n必ず日本語で回答すること",
            ),
        ):
            result = await runtime.stream_llm_query(
                "刚才真的执行了吗？现在是什么状态？",
                gui_callback=visible.append,
                provider="deepseek",
                enable_conversation=False,
                turn_id="turn-direct-read",
            )

        assert result == role_reply
        assert visible == [role_reply]
        assert spoken == [role_reply]
        assert len(grounding) == 1
        assert "[Authoritative AUIP read facts]" in grounding[0]
        assert answer in grounding[0]
        assert "Do not expose schema keys" in grounding[0]
        assert "Do not say you will check, look, wait, or answer later" in grounding[0]
        assert "later idle, revoked, or observe state describes only the present" in grounding[0]
        assert "exact action/policy payload values" in grounding[0]

    asyncio.run(scenario())


def test_closed_session_candidate_decision_does_not_gate_next_role_stream() -> None:
    async def scenario() -> None:
        release_decision = asyncio.Event()
        role_started = asyncio.Event()
        first_sentence_enqueued = asyncio.Event()
        capture_started = threading.Event()
        ordering: list[str] = []

        class _SlowCandidateDecider:
            def capture(self, **context):
                assert context["include_work_followup"] is False
                ordering.append("candidate_capture")
                capture_started.set()

                async def resolve():
                    await release_decision.wait()
                    return AuipControlDecision(
                        status="ok",
                        action="none",
                        work_relation="independent",
                    )

                return resolve()

        sentence_queue: asyncio.Queue = asyncio.Queue()
        runtime = ChatRuntime()
        runtime.configure(
            pending_sentence_items=sentence_queue,
            provider="deepseek",
            auip_control_decider=_SlowCandidateDecider(),
        )

        async def role_model(state, *_args, **_kwargs):
            ordering.append("main_role")
            role_started.set()
            await asyncio.sleep(0)
            assert capture_started.is_set() is False
            state.full_response = "自然に答えるわ。"
            state.history_response = state.full_response
            ordering.append("first_sentence")
            await runtime._append_and_dispatch(state, state.full_response)
            assert sentence_queue.get_nowait().text == state.full_response
            sentence_queue.task_done()
            first_sentence_enqueued.set()
            assert await asyncio.to_thread(capture_started.wait, 2.0) is True

        with (
            patch.object(runtime, "_ensure_clients", return_value=None),
            patch.object(runtime, "_run_deepseek_openai", side_effect=role_model),
            patch.object(
                runtime,
                "_repair_missing_delegate",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "server.auip_runtime.runtime.focused_projection",
                return_value={
                    "status": "closed",
                    "app_session_id": "app-closed",
                    "experience_capsule": {"status": "closed"},
                },
            ),
            patch(
                "server.task_lookup.pre_turn_resolve",
                new_callable=AsyncMock,
            ),
        ):
            turn = asyncio.create_task(
                runtime.stream_llm_query(
                    "顺便聊聊别的。",
                    provider="deepseek",
                    enable_conversation=False,
                    turn_id="turn-after-leave",
                )
            )
            await asyncio.wait_for(role_started.wait(), timeout=2.0)
            await asyncio.wait_for(first_sentence_enqueued.wait(), timeout=2.0)
            assert ordering[0] == "main_role"
            assert release_decision.is_set() is False
            assert ordering[:2] == ["main_role", "first_sentence"]
            assert turn.done() is False

            release_decision.set()
            assert await asyncio.wait_for(turn, timeout=2.0) == "自然に答えるわ。"

        assert ordering == ["main_role", "first_sentence", "candidate_capture"]

    asyncio.run(scenario())


def test_background_candidate_capture_releases_at_turn_settlement_without_speech() -> None:
    async def scenario() -> None:
        capture_started = threading.Event()

        class _CandidateDecider:
            def capture(self, **_context):
                capture_started.set()

                async def resolve():
                    return AuipControlDecision(
                        status="ok",
                        action="none",
                        work_relation="independent",
                    )

                return resolve()

        runtime = ChatRuntime()
        runtime.configure(
            pending_sentence_items=asyncio.Queue(),
            provider="deepseek",
            auip_control_decider=_CandidateDecider(),
        )

        async def silent_role(state, *_args, **_kwargs):
            assert capture_started.is_set() is False
            state.full_response = ""
            state.history_response = ""

        with (
            patch.object(runtime, "_ensure_clients", return_value=None),
            patch.object(runtime, "_run_deepseek_openai", side_effect=silent_role),
            patch.object(
                runtime,
                "_repair_missing_delegate",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "server.auip_runtime.runtime.focused_projection",
                return_value={"status": "closed", "app_session_id": "app-closed"},
            ),
            patch(
                "server.task_lookup.pre_turn_resolve",
                new_callable=AsyncMock,
            ),
        ):
            result = await asyncio.wait_for(
                runtime.stream_llm_query(
                    "……",
                    provider="deepseek",
                    enable_conversation=False,
                    turn_id="turn-silent-background-release",
                ),
                timeout=2.0,
            )

        assert result == ""
        assert capture_started.is_set() is True

    asyncio.run(scenario())


def test_live_appsession_decision_still_precedes_role_generation() -> None:
    async def scenario() -> None:
        release_decision = asyncio.Event()
        capture_started = threading.Event()
        role_started = asyncio.Event()

        class _SlowLiveDecider:
            def capture(self, **_context):
                capture_started.set()

                async def resolve():
                    await release_decision.wait()
                    return AuipControlDecision(
                        status="ok",
                        action="none",
                        work_relation="subsumed",
                        app_session_id="app-live",
                    )

                return resolve()

        runtime = ChatRuntime()
        runtime.configure(
            pending_sentence_items=asyncio.Queue(),
            provider="deepseek",
            auip_control_decider=_SlowLiveDecider(),
        )

        async def role_model(state, *_args, **_kwargs):
            role_started.set()
            state.full_response = "局面を見て答えるわ。"
            state.history_response = state.full_response

        with (
            patch.object(runtime, "_ensure_clients", return_value=None),
            patch.object(runtime, "_run_deepseek_openai", side_effect=role_model),
            patch.object(
                runtime,
                "_repair_missing_delegate",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "server.auip_runtime.runtime.focused_projection",
                return_value={"status": "active", "app_session_id": "app-live"},
            ),
            patch(
                "server.task_lookup.pre_turn_resolve",
                new_callable=AsyncMock,
            ),
        ):
            turn = asyncio.create_task(
                runtime.stream_llm_query(
                    "现在该怎么走？",
                    provider="deepseek",
                    enable_conversation=False,
                    turn_id="turn-live-scope",
                )
            )
            assert await asyncio.to_thread(capture_started.wait, 2.0) is True
            await asyncio.sleep(0)
            assert role_started.is_set() is False

            release_decision.set()
            assert await asyncio.wait_for(turn, timeout=2.0) == "局面を見て答えるわ。"
            assert role_started.is_set() is True

    asyncio.run(scenario())


def test_background_scope_scan_retries_once_when_work_followup_appears() -> None:
    async def scenario() -> None:
        capture_modes: list[bool] = []

        class _FollowupDecider:
            def capture(self, **context):
                followup = bool(context["include_work_followup"])
                capture_modes.append(followup)
                if not followup:
                    return None

                async def resolve():
                    return AuipControlDecision(
                        status="ok",
                        action="none",
                        work_relation="independent",
                    )

                return resolve()

        runtime = ChatRuntime()
        runtime.configure(auip_control_decider=_FollowupDecider())
        state = _state("turn-background-work-followup")

        assert runtime._start_auip_decision(
            state,
            background_capture=True,
        ) is True
        with patch.object(
            provider_runtime,
            "provider_manifests",
            return_value=(CODEX_APP_SERVER_MANIFEST,),
        ):
            runtime._start_auip_decision_for_work(
                state,
                [
                    {
                        "type": "DELEGATE",
                        "attrs": {
                            "provider": CODEX_APP_SERVER_MANIFEST.provider_id,
                            "intent": "execute",
                            "task": "实现一个文件。",
                        },
                    }
                ],
            )
        assert state.auip_decision_task is not None
        await state.auip_decision_task

        assert capture_modes == [False, True]
        assert state.auip_decision_result is not None
        assert state.auip_decision_result.work_relation == "independent"

    asyncio.run(scenario())


class _AuthorityObserver:
    def __init__(self, *, outcome: str, actions=()) -> None:
        self.outcome = outcome
        self.actions = tuple(actions)

    def capture(self, _batch):
        async def resolve():
            return SimpleNamespace(
                decision_status="ok",
                outcome=self.outcome,
                canonical_actions=self.actions,
                notes=(),
                reason="",
            )

        return resolve()


class _CompoundAuthorityObserver(_AuthorityObserver):
    def capture_compound_shadow(self, batch):
        return self.capture(batch)

def test_role_grounding_describes_requested_state_not_completion() -> None:
    decision = AuipControlDecision(
        status="ok",
        action="launch",
        mode="collaborate",
        target="private-app-title",
    )
    grounding = render_auip_role_grounding(decision)
    assert "pending" in grounding
    assert "application_active_at_decision=false" in grounding
    assert "not already open" in grounding
    assert "overrides conflicting assistant claims" in grounding
    assert 'resolved_application_title="private-app-title"' in grounding
    assert "work_item_id" not in grounding
    assert "attach_ticket" not in grounding

    step = render_auip_role_grounding(
        AuipControlDecision(status="ok", action="step")
    )
    assert "step_candidate=current_application_outcome_proposed" in step
    assert "different exposed action" in step
    assert "give one concise situational" in step
    assert "bind that exact alternative" in step
    assert "not a manifest action type" in step
    assert "board location in ordinary language" in step

    preparation = render_auip_role_grounding(
        AuipControlDecision(
            status="ok",
            action="prepare",
            mode="collaborate",
            target="Gomoku",
        )
    )
    assert "requested_transition=prepare" in preparation
    assert "preparation_operation=amend_existing_application" in preparation
    assert "already exists" in preparation
    assert "not a request to create or rebuild" in preparation
    assert "新しいアプリを作る" in preparation

    independent = render_auip_role_grounding(
        AuipControlDecision(
            status="ok",
            action="leave",
            work_relation="independent",
        )
    )
    assert "provider_work_relation=independent" in independent
    assert "Preserve that clause" in independent
    assert "explicit_leave_authorization=true" in independent
    assert "Do not ask for the same confirmation again" in independent

    subsumed = render_auip_role_grounding(
        AuipControlDecision(
            status="ok",
            action="launch",
            work_relation="subsumed",
        )
    )
    assert "provider_work_relation=subsumed" in subsumed
    assert "duplicate Provider Work" in subsumed

    none = render_auip_role_grounding(AuipControlDecision(status="ok"))
    assert "requested_transition=none" in none
    app_query = render_auip_role_grounding(
        AuipControlDecision(status="ok", work_relation="subsumed")
    )
    assert "Do not create a Work Ledger report" in app_query
    invalid = render_auip_role_grounding(
        AuipControlDecision(status="invalid", reason="malformed model result")
    )
    assert "application_resolution=unavailable" in invalid
    assert "Do not translate opening" in invalid
    unavailable = render_auip_role_grounding(
        AuipControlDecision(status="unavailable")
    )
    assert "application_resolution=unavailable" in unavailable
    assert "Do not translate opening" in unavailable

    blocked = render_auip_role_grounding(
        AuipControlDecision(
            status="blocked",
            action="collaborate",
            available_modes=("observe",),
        )
    )
    assert "transition_receipt=blocked" in blocked
    assert "available_modes=observe" in blocked


def test_current_semantic_resolution_is_serialized_after_history() -> None:
    state = _state("turn-role-grounding")
    state.auip_decision_result = AuipControlDecision(
        status="ok",
        action="launch",
        target="private-app-title",
    )
    with (
        patch("core.chat_runtime._get_system_prompt", return_value="ROLE"),
        patch(
            "core.chat_runtime._with_active_provider_context",
            side_effect=lambda value: value,
        ),
        patch(
            "core.chat_runtime._finalize_system_prompt_language",
            side_effect=lambda value: value,
        ),
    ):
        prompt = _turn_system_prompt(state, "with_delegate")
    assert prompt == "ROLE"

    history = ConversationHistory()
    history.add_user("open it")
    history.add_assistant("it is already open")
    messages = history.build_deepseek_messages(
        prompt,
        "open it again",
        current_turn_system=_turn_role_grounding(state),
    )
    assert messages[-2]["role"] == "system"
    assert "Authoritative Current-Turn Application State" in messages[-2]["content"]
    assert 'resolved_application_title="private-app-title"' in messages[-2]["content"]
    assert messages[-1] == {"role": "user", "content": "open it again"}


def test_only_an_auip_control_acknowledgement_uses_bounded_context() -> None:
    state = _state("turn-role-context")
    assert _turn_uses_conversation_history(state, True) is True

    state.auip_decision_result = AuipControlDecision(status="ok")
    assert _turn_uses_conversation_history(state, True) is True

    state.auip_decision_result = AuipControlDecision(
        status="ok",
        action="none",
        read_facets=("state", "receipt"),
    )
    assert _turn_uses_conversation_history(state, True) is False

    state.auip_decision_result = AuipControlDecision(
        status="ok",
        action="launch",
    )
    assert _turn_uses_conversation_history(state, True) is False
    assert _turn_uses_conversation_history(state, False) is False


def test_a1_scopes_operational_turns_but_preserves_independent_parent_chat() -> None:
    app_runtime = AuipRuntime(role_branch_mode="a1")
    registered = app_runtime.register(
        manifest={
            "schema": "amadeus.auip/v0",
            "app": {
                "id": "branch-test",
                "title": "Branch Test",
                "interactionSummary": "Move one step in a declared direction.",
            },
            "events": {"game.moved": {"beat": True}},
            "actions": {
                "game.move": {
                    "description": "Move one step.",
                    "risk": "local_execution",
                }
            },
            "stances": ["spectator", "participant"],
        },
        conversation_id="session-auip",
    )
    app_session_id = registered["app_session_id"]
    app_runtime.publish_state(
        app_session_id=app_session_id,
        bridge_token=registered["bridge_token"],
        revision=1,
        state={"direction": "idle"},
    )
    app_runtime.record_role_branch_turn(
        conversation_id="session-auip",
        app_session_id=app_session_id,
        user_text="刚才先别乱跑。",
        assistant_text="知道了。",
    )

    operational = _state(question="你能往右走吗？")
    operational.full_response = "いいわ、右へ行く。"
    operational.auip_decision_result = AuipControlDecision(
        status="ok",
        action="step",
        instruction="往右走",
        work_relation="subsumed",
        app_session_id=app_session_id,
    )
    with patch("server.auip_runtime.runtime", app_runtime):
        grounding = _turn_role_grounding(operational)
        ChatRuntime._record_auip_role_branch_turn(operational)

    assert "Active AUIP AppSession dialogue branch" in grounding
    assert "刚才先别乱跑" in grounding
    assert operational.auip_role_branch_isolated is True
    assert app_runtime.recent_role_branch_messages("session-auip")[-1] == {
        "role": "assistant",
        "content": "いいわ、右へ行く。",
    }

    app_banter = _state(question="你这步下得真冒险。")
    app_banter.full_response = "勝負には少しくらい大胆さも必要よ。"
    app_banter.auip_decision_result = AuipControlDecision(
        status="ok",
        action="none",
        work_relation="subsumed",
        app_session_id=app_session_id,
    )
    with patch("server.auip_runtime.runtime", app_runtime):
        assert _turn_uses_conversation_history(app_banter, True) is False
        ChatRuntime._record_auip_role_branch_turn(app_banter)
    assert app_banter.auip_role_branch_isolated is True

    independent = _state(question="帮我查一下 Paxos 论文")
    independent.full_response = "調べてみるわ。"
    independent.auip_decision_result = AuipControlDecision(
        status="ok",
        action="none",
        work_relation="independent",
    )
    before = app_runtime.recent_role_branch_messages("session-auip")
    with patch("server.auip_runtime.runtime", app_runtime):
        ChatRuntime._record_auip_role_branch_turn(independent)
    assert independent.auip_role_branch_isolated is False
    assert app_runtime.recent_role_branch_messages("session-auip") == before

    compound = _state(question="往右走，再帮我查一下 Paxos。")
    compound.full_response = "右へ行くわ。論文の方も調べておく。"
    compound.auip_decision_result = AuipControlDecision(
        status="ok",
        action="step",
        instruction="往右走",
        work_relation="independent",
        app_session_id=app_session_id,
    )
    with patch("server.auip_runtime.runtime", app_runtime):
        assert _turn_uses_conversation_history(compound, True) is True
        ChatRuntime._record_auip_role_branch_turn(compound)
    assert compound.auip_role_branch_isolated is False
    assert app_runtime.recent_role_branch_messages("session-auip")[-1] == {
        "role": "assistant",
        "content": "右へ行くわ。論文の方も調べておく。",
    }


def test_active_app_dialogue_and_blocked_mode_bind_identity_without_action() -> None:
    from server.auip_control_decision import _bind_active_decision

    active = {"app_session_id": "app-bound"}
    dialogue = _bind_active_decision(
        AuipControlDecision(
            status="ok",
            action="none",
            work_relation="subsumed",
        ),
        active,
    )
    blocked = _bind_active_decision(
        AuipControlDecision(status="blocked", action="delegate"),
        active,
    )
    independent = _bind_active_decision(
        AuipControlDecision(
            status="ok",
            action="none",
            work_relation="independent",
        ),
        active,
    )

    assert dialogue.app_session_id == "app-bound"
    assert dialogue.control_attrs() is None
    assert blocked.app_session_id == "app-bound"
    assert blocked.control_attrs() is None
    assert independent.app_session_id == ""


def test_a1_control_resolver_reads_branch_antecedent_instead_of_parent_history() -> None:
    async def scenario() -> None:
        app_runtime = AuipRuntime(role_branch_mode="a1")
        registered = app_runtime.register(
            manifest={
                "schema": "amadeus.auip/v0",
                "app": {"id": "branch-control", "title": "Branch Control"},
                "events": {"game.moved": {"beat": True}},
                "actions": {
                    "game.move": {
                        "description": "Move one step.",
                        "risk": "local_execution",
                    }
                },
                "stances": ["spectator", "participant"],
            },
            conversation_id="session-auip",
        )
        app_runtime.record_role_branch_turn(
            conversation_id="session-auip",
            app_session_id=registered["app_session_id"],
            user_text="先往右走。",
            assistant_text="好。",
        )
        captured: dict = {}

        class _Capture:
            def capture(self, **context):
                captured.update(context)

                async def resolve():
                    return AuipControlDecision(status="ok", action="none")

                return resolve()

        chat = ChatRuntime()
        chat.configure(auip_control_decider=_Capture())
        state = _TurnState(
            gui_callback=None,
            turn_id="a1-followup",
            question="那继续。",
            session_id="session-auip",
            control_prior_messages=[
                {"role": "user", "content": "父会话里的无关工作。"},
                {"role": "assistant", "content": "正在处理。"},
            ],
        )
        with patch("server.auip_runtime.runtime", app_runtime):
            assert chat._start_auip_decision(state) is True
            await state.auip_decision_task

        assert captured["prior_messages"] == (
            {"role": "user", "content": "先往右走。"},
            {"role": "assistant", "content": "好。"},
        )

    asyncio.run(scenario())


def test_role_grounding_is_ready_before_host_dispatch_completes() -> None:
    async def scenario() -> None:
        dispatch_started = asyncio.Event()
        release_dispatch = asyncio.Event()

        async def route(_attrs, **_context):
            dispatch_started.set()
            await release_dispatch.wait()

        runtime = ChatRuntime()
        runtime.configure(
            auip_control_callback=route,
            auip_control_decider=_Decider(
                AuipControlDecision(status="ok", action="launch")
            ),
        )
        state = _state("turn-grounding-before-dispatch")
        assert runtime._start_auip_decision(state) is True
        assert await runtime._wait_for_auip_role_grounding(
            state, timeout_s=0.5
        ) is True
        await asyncio.wait_for(dispatch_started.wait(), timeout=0.5)
        assert state.auip_decision_task is not None
        assert state.auip_decision_task.done() is False

        release_dispatch.set()
        await runtime._wait_for_auip_controls(state)

    asyncio.run(scenario())


def test_role_grounding_timeout_does_not_cancel_the_control_decision() -> None:
    async def scenario() -> None:
        release_decision = asyncio.Event()

        class _SlowDecider:
            def capture(self, **_context):
                async def resolve():
                    await release_decision.wait()
                    return AuipControlDecision(status="ok")

                return resolve()

        runtime = ChatRuntime()
        runtime.configure(auip_control_decider=_SlowDecider())
        state = _state("turn-grounding-timeout")
        assert runtime._start_auip_decision(state) is True
        assert await runtime._wait_for_auip_role_grounding(
            state, timeout_s=0.001
        ) is False
        assert state.auip_decision_task is not None
        assert state.auip_decision_task.cancelled() is False

        release_decision.set()
        await runtime._wait_for_auip_controls(state)
        assert state.auip_decision_result is not None

    asyncio.run(scenario())


def test_runtime_decision_owns_action_and_inline_is_only_unavailable_fallback() -> None:
    async def scenario() -> None:
        routed: list[dict] = []

        async def route(attrs, **_context):
            routed.append(dict(attrs))

        runtime = ChatRuntime()
        runtime.configure(
            auip_control_callback=route,
            auip_control_decider=_Decider(AuipControlDecision(status="ok")),
        )
        state = _state("turn-none")
        assert runtime._start_auip_decision(state) is True
        runtime._consume_stream_chunk(state, '[AUIP action="launch" mode="collaborate"]')
        await runtime._wait_for_auip_controls(state)
        assert routed == []
        assert "[AUIP" not in state.history_response

        runtime.configure(
            auip_control_decider=_Decider(AuipControlDecision(status="unavailable"))
        )
        fallback = _state("turn-fallback")
        assert runtime._start_auip_decision(fallback) is True
        runtime._consume_stream_chunk(
            fallback,
            '[AUIP action="launch" mode="collaborate"]',
        )
        await runtime._wait_for_auip_controls(fallback)
        assert routed == [{"action": "launch", "mode": "collaborate"}]
        assert '[AUIP action="launch" mode="collaborate"]' in fallback.history_response

    asyncio.run(scenario())


def test_deferred_launch_requires_an_effective_work_action() -> None:
    async def scenario() -> None:
        routed: list[dict] = []

        async def route(attrs, **_context):
            routed.append(dict(attrs))

        decision = AuipControlDecision(
            status="ok",
            action="launch",
            timing="after_work",
            mode="collaborate",
        )
        runtime = ChatRuntime()
        runtime.configure(
            auip_control_callback=route,
            auip_control_decider=_Decider(decision),
        )

        blocked = _state("turn-no-work")
        runtime._start_auip_decision(blocked, include_work_followup=True)
        await runtime._wait_for_auip_controls(blocked)
        assert routed == []

        accepted = _state("turn-with-work")
        accepted.work_delegate_seen = True
        accepted.control_effective_actions = [
            {
                "type": "DELEGATE",
                "attrs": {
                    "provider": "codex",
                    "intent": "execute",
                    "subject": "project",
                    "task": "build the requested app",
                },
                "raw": "",
            }
        ]
        runtime._start_auip_decision(accepted, include_work_followup=True)
        await runtime._wait_for_auip_controls(accepted)
        assert routed == [
            {
                "action": "launch",
                "target": "delivery",
                "mode": "collaborate",
                "after": "work",
                "_host_work_binding": "turn",
            }
        ]

    asyncio.run(scenario())


def test_authorized_step_accepts_one_inline_consensus_payload_without_second_authority() -> None:
    async def scenario() -> None:
        routed: list[dict] = []

        async def route(attrs, **_context):
            routed.append(dict(attrs))

        runtime = ChatRuntime()
        runtime.configure(
            auip_control_callback=route,
            auip_control_decider=_Decider(
                AuipControlDecision(
                    status="ok",
                    action="step",
                    instruction="Take one appropriate turn.",
                    app_session_id="app-consensus",
                )
            ),
        )
        state = _state("turn-step-consensus")
        assert runtime._start_auip_decision(state) is True
        visible = runtime._consume_stream_chunk(
            state,
            'では H7 に置くわ。[AUIP action="step" instruction="Place the stone at H7 as agreed"]',
        )
        await runtime._wait_for_auip_controls(state)

        assert visible == "では H7 に置くわ。"
        assert routed == [
            {
                "action": "step",
                "instruction": "Place the stone at H7 as agreed",
                "_host_app_session_id": "app-consensus",
            }
        ]
        assert "[AUIP action=" in state.history_response

    asyncio.run(scenario())


def test_authorized_step_without_concrete_chat_consensus_uses_participant_instruction() -> None:
    async def scenario() -> None:
        routed: list[dict] = []

        async def route(attrs, **_context):
            routed.append(dict(attrs))

        runtime = ChatRuntime()
        runtime.configure(
            auip_control_callback=route,
            auip_control_decider=_Decider(
                AuipControlDecision(
                    status="ok",
                    action="step",
                    instruction="Take the next turn now.",
                )
            ),
        )
        state = _state("turn-step-participant")
        assert runtime._start_auip_decision(state) is True
        await runtime._wait_for_auip_controls(state)

        assert routed == [
            {"action": "step", "instruction": "Take the next turn now."}
        ]

    asyncio.run(scenario())


def test_turn_final_step_carries_the_current_visible_role_decision() -> None:
    async def scenario() -> None:
        routed: list[dict] = []

        async def route(attrs, **_context):
            routed.append(dict(attrs))

        runtime = ChatRuntime()
        runtime.configure(
            auip_control_callback=route,
            auip_control_decider=_Decider(
                AuipControlDecision(
                    status="ok",
                    action="step",
                    instruction="Take the first move now.",
                )
            ),
        )
        state = _state("turn-role-declines-step")
        state.full_response = "私は後手でいいわ。あなたからどうぞ。"
        assert runtime._start_auip_decision(state) is True
        await runtime._wait_for_auip_controls(state)

        assert routed == [
            {
                "action": "step",
                "instruction": "Take the first move now.",
                "_host_current_role_response": "私は後手でいいわ。あなたからどうぞ。",
            }
        ]

    asyncio.run(scenario())


def test_preparation_fills_only_a_missing_bounded_work_proposal() -> None:
    async def scenario() -> None:
        routed: list[dict] = []

        async def route(attrs, **_context):
            routed.append(dict(attrs))

        runtime = ChatRuntime()
        runtime.configure(
            auip_control_callback=route,
            auip_control_decider=_Decider(
                AuipControlDecision(
                    status="ok",
                    action="prepare",
                    mode="collaborate",
                    target="井字棋",
                    preparation_work_item_id="work-existing-game",
                )
            ),
        )
        state = _state("turn-prepare-without-role-work")
        assert runtime._start_auip_decision(state) is True
        await runtime._wait_for_auip_controls(state)
        assert state.work_delegate_seen is False
        assert routed == [
            {
                "action": "prepare",
                "mode": "collaborate",
                "target": "井字棋",
                "_host_preparation_work_item_id": "work-existing-game",
            }
        ]

    asyncio.run(scenario())


def test_preparation_reuses_role_work_authority_without_role_invented_mechanics() -> None:
    async def scenario() -> None:
        routed: list[dict] = []

        async def route(attrs, **_context):
            routed.append(dict(attrs))

        role_work = {
            "provider": "browser",
            "intent": "execute",
            "subject": "none",
            "work_placement": "session_draft",
            "action": "open",
            "mode": "observe",
            "branch": "new",
            "task": "open the existing game",
        }
        runtime = ChatRuntime()
        runtime.configure(
            control_proposal_observer=_AuthorityObserver(
                outcome="agree",
                actions=(role_work,),
            ),
            control_proposal_authority=True,
            auip_control_callback=route,
            auip_control_decider=_Decider(
                AuipControlDecision(
                    status="ok",
                    action="prepare",
                    mode="collaborate",
                    target="井字棋",
                    preparation_work_item_id="work-existing-game",
                )
            ),
        )
        state = _state("turn-prepare-with-role-work")
        assert runtime._start_auip_decision(state) is True
        with (
            patch("core.chat_runtime.record_actions") as record,
            patch.object(
                provider_runtime,
                "provider_manifests",
                return_value=(CODEX_APP_SERVER_MANIFEST,),
            ),
        ):
            runtime._consume_stream_chunk(
                state,
                '[DELEGATE provider="browser" intent="execute" action="open" '
                'mode="observe" branch="new" task="open the existing game"]',
            )
            await runtime._wait_for_control_authority(state)
            await runtime._wait_for_auip_controls(state)

        record.assert_called_once()
        dispatched = record.call_args.args[0]
        assert len(dispatched) == 1
        attrs = dispatched[0]["attrs"]
        assert attrs["intent"] == "amend"
        assert attrs["subject"] == "work_item"
        assert attrs["workspace_ref"] == "work-existing-game"
        assert attrs["_host_reference_resolved"] is True
        assert attrs["_host_dispatch_source"] == "auip_prepare"
        assert "provider" not in attrs
        assert "action" not in attrs
        assert "mode" not in attrs
        assert "branch" not in attrs
        task, final_attrs = _delegate_call(dispatched[0]) or ("", {})
        assert task == "打开它，我们一起玩"
        assert attrs["task"] == "打开它，我们一起玩"
        assert attrs["_host_source_user_text"] == "打开它，我们一起玩"
        assert final_attrs == attrs
        for deleted in (
            "provider",
            "project_id",
            "projectId",
            "mode",
            "branch",
            "focus",
        ):
            assert deleted not in final_attrs
        effective_raw = str(dispatched[0].get("raw") or "")
        assert "project_id=" not in effective_raw
        assert "provider=" not in effective_raw
        with tempfile.TemporaryDirectory(prefix="auip_guard_dispatch_route_") as temp:
            workspace = Path(temp)

            class _RouteStore:
                @staticmethod
                def get_focus(_surface):
                    return None

                @staticmethod
                def get_work_item(work_item_id):
                    if work_item_id != "work-existing-game":
                        return None
                    return SimpleNamespace(
                        work_item_id=work_item_id,
                        project_id="project-correct",
                        workspace_path=str(workspace),
                        workspace_mode="local",
                    )

            route = WorkDestinationService(
                _RouteStore(),  # type: ignore[arg-type]
                registry_check=lambda _path: True,
            ).resolve_workspace_route(final_attrs)
            assert route["status"] == "resolved"
            assert route["workItemId"] == "work-existing-game"
            assert route["projectId"] == "project-correct"
        assert state.work_delegate_seen is True
        assert routed == [
            {
                "action": "launch",
                "target": "delivery",
                "mode": "collaborate",
                "after": "work",
                "_host_work_binding": "turn",
                "_host_work_item_id": "work-existing-game",
            }
        ]

    asyncio.run(scenario())


def test_preparation_collapses_compound_expansion_to_the_grounded_role_proposal() -> None:
    """AUIP prepare owns one prerequisite even if Work decomposition over-splits it."""

    async def scenario() -> None:
        routed: list[dict] = []

        async def route(attrs, **_context):
            routed.append(dict(attrs))

        runtime = ChatRuntime()
        runtime.configure(
            control_proposal_observer=_CompoundAuthorityObserver(
                outcome="diverge",
                actions=(
                    {
                        "provider": "codex",
                        "intent": "execute",
                        "subject": "project",
                        "task": "connect and open this game",
                    },
                    {
                        "provider": "codex",
                        "intent": "amend",
                        "subject": "work_item",
                        "workspace_ref": "work-existing-game",
                        "task": "keep collaborative operation active",
                    },
                    {
                        "provider": "codex",
                        "intent": "execute",
                        "subject": "project",
                        "task": "stop sustained control when observing",
                    },
                ),
            ),
            control_proposal_authority=True,
            compound_control_authority=True,
            auip_control_callback=route,
            auip_control_decider=_Decider(
                AuipControlDecision(
                    status="ok",
                    action="prepare",
                    mode="collaborate",
                    target="the current game",
                    preparation_work_item_id="work-existing-game",
                )
            ),
        )
        state = _state(
            "turn-compound-prepare",
            question=(
                "把这个小游戏接好以后直接打开。打开后保持共同操作；"
                "切到只观察时停止持续响应。"
            ),
        )
        assert runtime._start_auip_decision(state) is True
        with (
            patch("core.chat_runtime.record_actions") as record,
            patch.object(
                provider_runtime,
                "provider_manifests",
                return_value=(CODEX_APP_SERVER_MANIFEST,),
            ),
        ):
            runtime._consume_stream_chunk(
                state,
                '[DELEGATE provider="codex" intent="amend" '
                'subject="work_item" workspace_ref="work-existing-game" '
                'task="adapt the existing game for AUIP and open it"]',
            )
            await runtime._wait_for_control_authority(state)
            await runtime._wait_for_auip_controls(state)

        record.assert_called_once()
        dispatched = record.call_args.args[0]
        assert len(dispatched) == 1
        attrs = dispatched[0]["attrs"]
        assert attrs["task"] == (
            "把这个小游戏接好以后直接打开。打开后保持共同操作；"
            "切到只观察时停止持续响应。"
        )
        assert attrs["_host_source_user_text"] == attrs["task"]
        assert attrs["intent"] == "amend"
        assert attrs["subject"] == "work_item"
        assert attrs["workspace_ref"] == "work-existing-game"
        assert attrs["_host_dispatch_source"] == "auip_prepare"
        assert "provider" not in attrs
        assert state.control_effective_actions == dispatched
        assert state.work_delegate_seen is True
        assert routed == [
            {
                "action": "launch",
                "target": "delivery",
                "mode": "collaborate",
                "after": "work",
                "_host_work_binding": "turn",
                "_host_work_item_id": "work-existing-game",
            }
        ]

    asyncio.run(scenario())


def test_preparation_does_not_resurrect_a_rejected_work_start() -> None:
    """A source-local prepare decision cannot add a missing Work action."""

    async def scenario() -> None:
        runtime = ChatRuntime()
        runtime.configure(
            auip_control_decider=_Decider(
                AuipControlDecision(
                    status="ok",
                    action="prepare",
                    mode="collaborate",
                    target="the current game",
                    preparation_work_item_id="work-existing-game",
                )
            ),
        )
        state = _state("turn-prepare-no-authorized-start")
        assert runtime._start_auip_decision(state) is True
        report = {
            "type": "DELEGATE",
            "attrs": {
                "task": "summarise the current work",
                "intent": "report",
            },
            "raw": "<report>",
        }
        rejected_role_start = {
            "type": "DELEGATE",
            "attrs": {
                "task": "adapt the current game",
                "provider": "codex",
                "intent": "execute",
            },
            "raw": "<start>",
        }

        guarded = await runtime._guard_work_actions_against_auip(
            state,
            [report],
            fallback_actions=[rejected_role_start],
        )

        assert guarded == [report]
        assert not any(runtime._delegate_action_starts_work(row) for row in guarded)

    asyncio.run(scenario())


def test_launch_only_auip_suppresses_a_duplicate_accepted_work_proposal() -> None:
    """A duplicate Work proposal cannot steal a verified app launch turn."""

    async def scenario() -> None:
        routed: list[dict] = []

        async def route(attrs, **_context):
            routed.append(dict(attrs))

        runtime = ChatRuntime()
        runtime.configure(
            control_proposal_observer=_AuthorityObserver(
                outcome="agree",
                actions=(
                    {
                        "provider": "openclaw",
                        "intent": "execute",
                        "action": "open",
                        "task": "open the existing game and watch it",
                    },
                ),
            ),
            control_proposal_authority=True,
            auip_control_callback=route,
            auip_control_decider=_Decider(
                AuipControlDecision(
                    status="ok",
                    action="launch",
                    mode="collaborate",
                    target="井字棋",
                    work_relation="subsumed",
                )
            ),
        )
        state = _state(
            "turn-launch-only",
            question="打开刚才那个井字棋，我们一起玩。",
        )
        assert runtime._start_auip_decision(state) is True
        with patch("core.chat_runtime.record_actions") as record:
            runtime._consume_stream_chunk(
                state,
                '[DELEGATE provider="openclaw" intent="execute" action="open" '
                'task="open the existing game and watch it"]',
            )
            await runtime._wait_for_control_authority(state)
            await runtime._wait_for_auip_controls(state)

        record.assert_not_called()
        assert state.work_delegate_seen is False
        assert state.control_effective_actions == []
        assert "[DELEGATE" not in state.history_response
        assert routed == [
            {
                "action": "launch",
                "mode": "collaborate",
                "target": "井字棋",
            }
        ]

    asyncio.run(scenario())


def test_active_app_query_suppresses_a_misclassified_work_report() -> None:
    """App state is not a second request to report the authoring WorkItem."""

    async def scenario() -> None:
        runtime = ChatRuntime()
        runtime.configure(
            control_proposal_observer=_AuthorityObserver(
                outcome="agree",
                actions=(
                    {
                        "provider": "codex",
                        "intent": "report",
                        "subject": "work_item",
                        "workspace_ref": "work-game-authoring",
                        "task": "report the game's current state",
                    },
                ),
            ),
            control_proposal_authority=True,
            auip_control_decider=_Decider(
                AuipControlDecision(
                    status="ok",
                    action="none",
                    work_relation="subsumed",
                )
            ),
        )
        state = _state(
            "turn-app-state-query",
            question="刚才真的操作了吗？现在是什么状态？",
        )
        assert runtime._start_auip_decision(state) is True
        with patch("core.chat_runtime.record_actions") as record:
            runtime._consume_stream_chunk(
                state,
                '[DELEGATE provider="codex" intent="report" '
                'subject="work_item" workspace_ref="work-game-authoring" '
                'task="report the game current state"]',
            )
            await runtime._wait_for_control_authority(state)
            await runtime._wait_for_auip_controls(state)

        record.assert_not_called()
        assert state.control_effective_actions == []
        assert state.work_delegate_seen is False
        assert "[DELEGATE" not in state.history_response

    asyncio.run(scenario())


def test_canonical_work_repair_starts_auip_composition_before_dispatch() -> None:
    """A repaired Work shape cannot bypass an AUIP decision that starts late."""

    async def scenario() -> None:
        captures: list[dict] = []

        class _CapturingDecider(_Decider):
            def capture(self, **context):
                captures.append(dict(context))
                return super().capture(**context)

        runtime = ChatRuntime()
        runtime.configure(
            control_proposal_observer=_AuthorityObserver(
                outcome="diverge",
                actions=(
                    {
                        "provider": "codex",
                        "intent": "amend",
                        "subject": "work_item",
                        "task": "open the existing game and watch it",
                    },
                ),
            ),
            control_proposal_authority=True,
            auip_control_decider=_CapturingDecider(
                AuipControlDecision(
                    status="ok",
                    action="delegate",
                    work_relation="subsumed",
                )
            ),
        )
        state = _state(
            "turn-canonical-work-repair",
            question="把那个井字棋再打开吧，我想看你下。",
        )
        with (
            patch("core.chat_runtime.record_actions") as record,
            patch.object(
                provider_runtime,
                "provider_manifests",
                return_value=(CODEX_APP_SERVER_MANIFEST,),
            ),
        ):
            # The role proposal is read-only, so it cannot start the AUIP
            # decision. Canonical authority repairs it to Work.
            runtime._consume_stream_chunk(
                state,
                '[DELEGATE provider="codex" intent="report" '
                'subject="work_item" task="inspect the existing game"]',
            )
            await runtime._wait_for_control_authority(state)

        assert len(captures) == 1
        assert captures[0]["include_work_followup"] is True
        record.assert_not_called()
        assert state.control_effective_actions == []
        assert state.work_delegate_seen is False
        assert "[DELEGATE" not in state.history_response

    asyncio.run(scenario())


def test_immediate_auip_preserves_a_genuinely_independent_work_proposal() -> None:
    async def scenario() -> None:
        routed: list[dict] = []

        async def route(attrs, **_context):
            routed.append(dict(attrs))

        independent_work = {
            "provider": "openclaw",
            "intent": "execute",
            "task": "check tomorrow's weather",
        }
        runtime = ChatRuntime()
        runtime.configure(
            control_proposal_observer=_AuthorityObserver(
                outcome="agree",
                actions=(independent_work,),
            ),
            control_proposal_authority=True,
            auip_control_callback=route,
            auip_control_decider=_Decider(
                AuipControlDecision(
                    status="ok",
                    action="launch",
                    mode="observe",
                    target="井字棋",
                    work_relation="independent",
                )
            ),
        )
        state = _state(
            "turn-launch-and-weather",
            question="打开井字棋，你看我玩；另外查一下明天的天气。",
        )
        assert runtime._start_auip_decision(state) is True
        with patch("core.chat_runtime.record_actions") as record:
            runtime._consume_stream_chunk(
                state,
                '[DELEGATE provider="openclaw" intent="execute" '
                'task="check tomorrow\'s weather"]',
            )
            await runtime._wait_for_control_authority(state)
            await runtime._wait_for_auip_controls(state)

        record.assert_called_once()
        assert state.work_delegate_seen is True
        assert routed == [
            {
                "action": "launch",
                "mode": "observe",
                "target": "井字棋",
            }
        ]

    asyncio.run(scenario())


def test_build_then_open_waits_for_authoritative_work_without_merging_axes() -> None:
    """Work and a deferred AUIP launch remain two coordinated actions."""

    async def scenario() -> None:
        routed: list[dict] = []

        async def route(attrs, **_context):
            routed.append(dict(attrs))

        work = {
            "provider": "locus",
            "intent": "execute",
            "task": "build a small number merge game",
        }
        runtime = ChatRuntime()
        runtime.configure(
            control_proposal_observer=_AuthorityObserver(
                outcome="agree",
                actions=(work,),
            ),
            control_proposal_authority=True,
            auip_control_callback=route,
            auip_control_decider=_Decider(
                AuipControlDecision(
                    status="ok",
                    action="launch",
                    timing="after_work",
                    mode="collaborate",
                )
            ),
        )
        state = _state(
            "turn-build-then-open",
            question="再做个数字合并游戏，做好以后打开，我们一起玩。",
        )
        assert runtime._start_auip_decision(state) is True
        await asyncio.sleep(0)
        assert routed == []

        with patch("core.chat_runtime.record_actions") as record:
            runtime._consume_stream_chunk(
                state,
                '[DELEGATE provider="locus" intent="execute" '
                'task="build a small number merge game"]',
            )
            await runtime._wait_for_control_authority(state)
            await runtime._wait_for_auip_controls(state)

        record.assert_called_once()
        dispatched = record.call_args.args[0]
        assert len(dispatched) == 1
        assert dispatched[0]["attrs"]["provider"] == "locus"
        assert dispatched[0]["attrs"]["_host_dispatch_source"] == "auip_create"
        assert state.work_delegate_seen is True
        assert routed == [
            {
                "action": "launch",
                "target": "delivery",
                "mode": "collaborate",
                "after": "work",
                "_host_work_binding": "turn",
            }
        ]

    asyncio.run(scenario())


def test_deferred_relaunch_collapses_only_same_app_amendment_clauses() -> None:
    async def scenario() -> None:
        routed: list[dict] = []

        async def route(attrs, **_context):
            routed.append(dict(attrs))

        runtime = ChatRuntime()
        runtime.configure(
            auip_control_callback=route,
            auip_control_decider=_Decider(
                AuipControlDecision(
                    status="ok",
                    action="launch",
                    timing="after_work",
                    mode="collaborate",
                    work_relation="independent",
                    app_session_id="app-old",
                )
            )
        )
        question = "把标题改成实验台，改好后重新打开，我们继续。"
        state = _state("turn-amend-relaunch", question=question)
        assert runtime._start_auip_decision(state) is True
        canonical = [
            {
                "type": "DELEGATE",
                "attrs": {
                    "provider": "codex",
                    "intent": "amend",
                    "subject": "work_item",
                    "workspace_ref": "work-reactor",
                    "task": "把标题改成实验台",
                },
                "raw": "<first>",
            },
            {
                "type": "DELEGATE",
                "attrs": {
                    "provider": "codex",
                    "intent": "amend",
                    "subject": "work_item",
                    "workspace_ref": "work-reactor",
                    "task": "改好后重新打开",
                },
                "raw": "<second>",
            },
        ]
        role_fallback = {
            "type": "DELEGATE",
            "attrs": {
                "provider": "codex",
                "intent": "amend",
                "subject": "work_item",
                "workspace_ref": "work-reactor",
                "task": "change the current app",
            },
            "raw": "<role>",
        }

        guarded = await runtime._guard_work_actions_against_auip(
            state,
            canonical,
            fallback_actions=[role_fallback],
        )

        assert len(guarded) == 1
        attrs = guarded[0]["attrs"]
        assert attrs["intent"] == "amend"
        assert attrs["subject"] == "work_item"
        assert attrs["workspace_ref"] == "work-reactor"
        assert attrs["task"] == question
        assert attrs["_host_source_user_text"] == question
        assert attrs["_host_dispatch_source"] == "auip_create"

        separate_state = _state(
            "turn-amend-and-notes",
            question="改完应用再打开，另外写一份会议记录。",
        )
        assert runtime._start_auip_decision(separate_state) is True
        unrelated = {
            "type": "DELEGATE",
            "attrs": {
                "provider": "codex",
                "intent": "execute",
                "subject": "project",
                "task": "write meeting notes",
            },
            "raw": "<notes>",
        }
        kept = await runtime._guard_work_actions_against_auip(
            separate_state,
            [canonical[0], unrelated],
            fallback_actions=[],
        )
        assert kept == [canonical[0], unrelated]
        assert canonical[0]["attrs"]["_host_dispatch_source"] == "auip_create"
        assert "_host_dispatch_source" not in unrelated["attrs"]
        separate_state.control_effective_actions = kept
        separate_state.work_delegate_seen = True
        await runtime._wait_for_auip_controls(separate_state)
        assert routed == [
            {
                "action": "launch",
                "target": "delivery",
                "mode": "collaborate",
                "after": "work",
                "_host_work_binding": "turn",
                "_host_work_item_id": "work-reactor",
                "_host_app_session_id": "app-old",
            }
        ]

        ambiguous_state = _state(
            "turn-two-app-amendments",
            question="把两个应用都改好，然后重新打开。",
        )
        assert runtime._start_auip_decision(ambiguous_state) is True
        ambiguous = [
            {
                "type": "DELEGATE",
                "attrs": {
                    "provider": "codex",
                    "intent": "amend",
                    "subject": "work_item",
                    "workspace_ref": target,
                    "task": f"amend {target}",
                },
                "raw": "",
            }
            for target in ("work-one", "work-two")
        ]
        ambiguous = await runtime._guard_work_actions_against_auip(
            ambiguous_state,
            ambiguous,
            fallback_actions=[],
        )
        assert all(
            "_host_dispatch_source" not in action["attrs"] for action in ambiguous
        )
        ambiguous_state.control_effective_actions = ambiguous
        ambiguous_state.work_delegate_seen = True
        await runtime._wait_for_auip_controls(ambiguous_state)
        assert len(routed) == 1

    asyncio.run(scenario())


def test_unrelated_work_during_an_app_session_does_not_create_auip_control() -> None:
    async def scenario() -> None:
        routed: list[dict] = []

        async def route(attrs, **_context):
            routed.append(dict(attrs))

        work = {
            "provider": "locus",
            "intent": "execute",
            "task": "write meeting notes",
        }
        runtime = ChatRuntime()
        runtime.configure(
            control_proposal_observer=_AuthorityObserver(
                outcome="agree",
                actions=(work,),
            ),
            control_proposal_authority=True,
            auip_control_callback=route,
            auip_control_decider=_Decider(AuipControlDecision(status="ok")),
        )
        state = _state(
            "turn-unrelated-work",
            question="游戏先放着，另外帮我写一份会议记录。",
        )
        assert runtime._start_auip_decision(state) is True
        with patch("core.chat_runtime.record_actions") as record:
            runtime._consume_stream_chunk(
                state,
                '[DELEGATE provider="locus" intent="execute" '
                'task="write meeting notes"]',
            )
            await runtime._wait_for_control_authority(state)
            await runtime._wait_for_auip_controls(state)

        record.assert_called_once()
        assert state.work_delegate_seen is True
        assert routed == []

    asyncio.run(scenario())


def test_followup_launch_binds_the_frozen_active_work_without_a_new_delegate() -> None:
    async def scenario() -> None:
        routed: list[dict] = []

        async def route(attrs, **_context):
            routed.append(dict(attrs))

        runtime = ChatRuntime()
        runtime.configure(
            auip_control_callback=route,
            auip_control_decider=_Decider(
                AuipControlDecision(
                    status="ok",
                    action="launch",
                    timing="after_work",
                    mode="observe",
                    active_work_attempt_ids=("attempt-active",),
                )
            ),
        )
        state = _state(
            "turn-after-existing-work",
            question="加好以后打开，我自己试玩，你在旁边看。",
        )
        assert runtime._start_auip_decision(state) is True
        await runtime._wait_for_auip_controls(state)
        assert state.work_delegate_seen is False
        assert routed == [
            {
                "action": "launch",
                "target": "delivery",
                "mode": "observe",
                "after": "work",
                "_host_active_work_attempt_ids": ("attempt-active",),
                "_host_work_binding": "active",
            }
        ]

    asyncio.run(scenario())


def test_cross_axis_ambiguity_suppresses_only_the_retract_action() -> None:
    async def scenario() -> None:
        runtime = ChatRuntime()
        runtime.configure(
            control_proposal_observer=_AuthorityObserver(
                outcome="agree",
                actions=(
                    {
                        "provider": "locus",
                        "intent": "retract",
                        "task": "stop it",
                    },
                ),
            ),
            control_proposal_authority=True,
            auip_control_decider=_Decider(
                AuipControlDecision(
                    status="ok",
                    ambiguity="work_or_app",
                )
            ),
        )
        state = _state("turn-ambiguous-stop", question="停一下。")
        assert runtime._start_auip_decision(state) is True
        with patch("core.chat_runtime.record_actions") as record:
            runtime._consume_stream_chunk(
                state,
                '[DELEGATE provider="locus" intent="retract" task="stop it"]',
            )
            await runtime._wait_for_control_authority(state)
            await runtime._wait_for_auip_controls(state)
        record.assert_not_called()
        assert state.control_effective_actions == []
        assert state.auip_cross_axis_ambiguous is True
        assert "[DELEGATE" not in state.history_response

        explicit_runtime = ChatRuntime()
        explicit_runtime.configure(
            control_proposal_observer=_AuthorityObserver(
                outcome="agree",
                actions=(
                    {
                        "provider": "locus",
                        "intent": "retract",
                        "task": "stop the background work",
                    },
                ),
            ),
            control_proposal_authority=True,
            auip_control_decider=_Decider(AuipControlDecision(status="ok")),
        )
        explicit = _state(
            "turn-explicit-work-stop",
            question="后台任务先停一下，游戏继续。",
        )
        assert explicit_runtime._start_auip_decision(explicit) is True
        with patch("core.chat_runtime.record_actions") as record:
            explicit_runtime._consume_stream_chunk(
                explicit,
                '[DELEGATE provider="locus" intent="retract" '
                'task="stop the background work"]',
            )
            await explicit_runtime._wait_for_control_authority(explicit)
            await explicit_runtime._wait_for_auip_controls(explicit)
        record.assert_called_once()
        assert explicit.auip_cross_axis_ambiguous is False

    asyncio.run(scenario())


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("ok: AUIP action existence has one source-local authority")


if __name__ == "__main__":
    _main()
