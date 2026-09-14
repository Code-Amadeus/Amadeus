"""Browser delegation keeps current-turn authority and executable targets."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.provider_catalog import (
    BROWSER_MANIFEST,
    CODEX_APP_SERVER_MANIFEST,
    OPENCLAW_MANIFEST,
)
from agent_host.provider_contract import ProviderRequirements, ProviderSelection
from agent_host.provider_identity import (
    MAIN_ROLE_NAME_METADATA_KEY,
    with_main_role_reference,
)
from server.inherited_role_prompt import MAIN_CONVERSATION_ROLE_NAME
import server.interaction_branch as interaction_branch_module
from server.interaction_branch import (
    InteractionBranchCoordinator,
    InteractionBranchRunStopUnconfirmed,
    InteractionBranchState,
)
from server.app import (
    _announce_interaction_branch_lease_block,
    _consume_captured_interaction_branch_intent,
    _delegate_workspace_route,
    _handle_delegate,
    _handle_declared_focus,
    _rebase_web_goal_for_selected_provider,
    _remove_ungrounded_persona_parameters,
    _sanitize_delegate_task_for_provider,
)
from server.event_bus import bus
from server.protocol import Method
from server.control_decision import (
    CONTROL_PAYLOAD_GROUNDING_ATTR,
    parse_control_decision_reply,
    reconcile_control_decision,
)
from tools.text_utils import parse_tags_and_clean
from vts import action as action_dispatcher


class _ConversationHistory:
    def __init__(self, *messages: str | dict) -> None:
        self.dialog = [
            dict(message)
            if isinstance(message, dict)
            else {"role": "user", "content": message}
            for message in messages
        ]


class _SessionManager:
    def __init__(self, *messages: str) -> None:
        self.conversation_history = _ConversationHistory(*messages)


def test_sanitizer_prefers_current_turn_over_stale_history() -> None:
    task, audit = _sanitize_delegate_task_for_provider(
        "Create a Kurisu-themed version of the game.",
        {"_host_source_user_text": "把背景改成蓝色"},
        provider="codex",
        session_manager=_SessionManager("修改棋子的标识"),
    )
    assert task == "把背景改成蓝色"
    assert audit["reason"] == "persona_leak_removed"


def test_sanitizer_uses_frozen_origin_history_after_ui_switch() -> None:
    ambient_b = _SessionManager("请做一个红莉栖主题页面")
    task, audit = _sanitize_delegate_task_for_provider(
        "Create a Kurisu-themed version of the game.",
        {
            "_host_source_user_text": "把背景改成蓝色",
            "_host_delegate_history_snapshot": {
                "latest_user": "把背景改成蓝色",
                "antecedent_user": "做一个普通小游戏",
                "interrupted_antecedent": False,
            },
        },
        provider="codex",
        session_manager=ambient_b,
    )

    assert task == "把背景改成蓝色"
    assert audit["reason"] == "persona_leak_removed"
    assert audit["antecedent_user"] == "做一个普通小游戏"
    assert audit["replacement_source"] == "current_turn"


def test_explicit_self_reference_authorizes_the_resolved_identity() -> None:
    original = (
        "Open the Wikipedia page for Kurisu Makise at "
        "https://en.wikipedia.org/wiki/Kurisu_Makise."
    )
    task, audit = _sanitize_delegate_task_for_provider(
        original,
        {"_host_source_user_text": "帮我打开维基百科找到你自己的页面"},
        provider="browser",
        session_manager=_SessionManager("修改棋子的标识"),
    )
    assert task == original
    assert audit == {}


def test_retry_can_use_the_immediately_preceding_explicit_self_reference() -> None:
    original = (
        "Open the Wikipedia page for Kurisu Makise at "
        "https://en.wikipedia.org/wiki/Kurisu_Makise."
    )
    task, audit = _sanitize_delegate_task_for_provider(
        original,
        {"_host_source_user_text": "你再试试呢"},
        provider="browser",
        session_manager=_SessionManager("帮我打开维基百科找到你自己的页面"),
    )
    assert task == original
    assert audit == {}


def test_retry_uses_the_preceding_user_when_current_turn_is_persisted() -> None:
    original = (
        "Open the Wikipedia page for Kurisu Makise at "
        "https://en.wikipedia.org/wiki/Kurisu_Makise."
    )
    current = "你再试试呢"
    task, audit = _sanitize_delegate_task_for_provider(
        original,
        {"_host_source_user_text": current},
        provider="browser",
        session_manager=_SessionManager(
            "帮我打开维基百科找到你自己的页面",
            current,
        ),
    )
    assert task == original
    assert audit == {}


def test_interrupted_correction_preserves_the_adjacent_identity_reference() -> None:
    original = "Open the Wikipedia page for Makise Kurisu."
    current = "哦，我说的是打开维基百科。"
    session = _SessionManager(
        "帮我打开你自己的页面",
        {
            "role": "assistant",
            "content": "私のページ？ [interrupted by user]",
        },
        current,
    )
    task, audit = _sanitize_delegate_task_for_provider(
        original,
        {"_host_source_user_text": current},
        provider="openclaw",
        session_manager=session,
    )
    assert task == original
    assert audit == {}


def test_browser_to_agent_handoff_rebases_on_exact_conversation_source() -> None:
    current = "哦，我说的是打开维基百科。"
    attrs = {
        "provider": "browser",
        "action": "open",
        "url": "https://ja.wikipedia.org/wiki/Paxos",
        "query": "Paxos",
        "_host_source_user_text": current,
    }
    task, audit = _rebase_web_goal_for_selected_provider(
        "Open the Paxos Wikipedia page.",
        attrs,
        selected_provider="openclaw",
        requirements=ProviderRequirements(task_kind="research"),
        session_manager=_SessionManager(
            "帮我打开你自己的页面",
            {
                "role": "assistant",
                "content": "私のページ？ [interrupted by user]",
            },
            current,
        ),
    )
    assert "Immediate prior user request (context only): 帮我打开你自己的页面" in task
    assert f"Latest user instruction (authoritative): {current}" in task
    assert "Makise Kurisu (牧瀬紅莉栖)" not in task
    assert "Paxos" not in task
    assert audit["identity_grounded"] is True
    assert audit["interrupted_antecedent_included"] is True
    assert "action" not in attrs
    assert "url" not in attrs
    assert "query" not in attrs


def test_explicit_provider_retarget_keeps_the_preceding_authorized_target() -> None:
    original = (
        "Open the Wikipedia page for Kurisu Makise at "
        "https://en.wikipedia.org/wiki/Kurisu_Makise."
    )
    current = "你不是要用openclaw去打开吗"
    task, audit = _sanitize_delegate_task_for_provider(
        original,
        {"_host_source_user_text": current},
        provider="openclaw",
        session_manager=_SessionManager(
            "帮我打开维基百科找到你自己的页面",
            current,
        ),
    )
    assert task == original
    assert audit == {}


def test_taskless_operation_does_not_bypass_persona_parameter_grounding() -> None:
    current = "把游戏背景改成蓝色"
    task, audit = _sanitize_delegate_task_for_provider(
        current,
        {
            "action": "open",
            "url": "https://en.wikipedia.org/wiki/Kurisu_Makise",
            "_host_source_user_text": current,
        },
        provider="browser",
        session_manager=_SessionManager(current),
    )
    assert task == current
    assert audit["reason"] == "persona_leak_removed"


def test_previous_identity_request_does_not_authorize_a_new_unrelated_turn() -> None:
    task, audit = _sanitize_delegate_task_for_provider(
        "Create a Kurisu-themed version of the game.",
        {"_host_source_user_text": "把游戏背景改成蓝色"},
        provider="codex",
        session_manager=_SessionManager("帮我找到你自己的页面"),
    )
    assert task == "把游戏背景改成蓝色"
    assert audit["reason"] == "persona_leak_removed"


def test_confirmed_prior_request_preserves_the_canonical_persona_payload() -> None:
    prior = "不是三维模型，是你自己的个人静态网页，你自己设计一下。"
    current = "啊，那你现在开始做。"
    original = (
        "Create a personal static HTML page for the character Kurisu Makise. "
        "This must be her personal page, not a Codex product page."
    )
    decision = parse_control_decision_reply(
        '{"decisions":[{"proposal_index":0,"provider":"codex",'
        '"intent":"execute","work_placement":"draft",'
        '"session_context":"unchanged","workspace_effect":"write",'
        '"payload_continuity":"confirmed_prior_request",'
        '"reference_mode":"none"}]}',
        proposal_count=1,
    )
    actions, notes = reconcile_control_decision(
        ({"task": original},),
        decision,
        provider_ids=("codex",),
    )
    assert notes == []
    attrs = actions[0]
    attrs["_host_source_user_text"] = current

    task, audit = _sanitize_delegate_task_for_provider(
        original,
        attrs,
        provider="codex",
        session_manager=_SessionManager(
            prior,
            {"role": "assistant", "content": "個人ページね。分かったわ。"},
            current,
        ),
    )

    assert task == original
    assert audit == {}
    assert attrs["_host_payload_source"] == "confirmed_prior_request"
    assert CONTROL_PAYLOAD_GROUNDING_ATTR not in attrs


def test_role_authored_payload_grounding_string_cannot_bypass_sanitization() -> None:
    current = "那就开始吧。"
    attrs = {
        "_host_source_user_text": current,
        CONTROL_PAYLOAD_GROUNDING_ATTR: "confirmed_prior_request",
        "_host_payload_source": "confirmed_prior_request",
    }
    task, audit = _sanitize_delegate_task_for_provider(
        "Create a Kurisu-themed version of the unrelated game.",
        attrs,
        provider="codex",
        session_manager=_SessionManager(
            "帮我找到你自己的页面",
            {"role": "assistant", "content": "見つけたわ。"},
            current,
        ),
    )

    assert task == current
    assert audit["reason"] == "persona_leak_removed"
    assert audit["confirmed_prior_request"] is False
    assert CONTROL_PAYLOAD_GROUNDING_ATTR not in attrs


def test_persona_rewrite_removes_matching_structured_action_arguments() -> None:
    attrs = {
        "url": "https://en.wikipedia.org/wiki/Kurisu_Makise",
        "query": "Kurisu Makise",
        "action": "open",
    }
    removed = _remove_ungrounded_persona_parameters(attrs)
    assert removed == ["url", "query"]
    assert attrs == {"action": "open"}


def _browser_selection() -> tuple[ProviderRequirements, ProviderSelection]:
    return (
        ProviderRequirements(
            task_kind="browser",
            workspace_access="none",
            ownership="managed",
            preferred_provider="browser",
            preference_policy="require",
        ),
        ProviderSelection(
            provider_id="browser",
            reason="test",
            compatible_candidates=("browser",),
        ),
    )


def _codex_selection() -> tuple[ProviderRequirements, ProviderSelection]:
    return (
        ProviderRequirements(
            task_kind="workspace_mutation",
            workspace_access="write",
            preferred_provider="codex",
            preference_policy="require",
        ),
        ProviderSelection(
            provider_id="codex",
            reason="test",
            compatible_candidates=("codex",),
        ),
    )


def _openclaw_selection() -> tuple[ProviderRequirements, ProviderSelection]:
    return (
        ProviderRequirements(
            task_kind="research",
            workspace_access="none",
            ownership="managed",
            preferred_provider="openclaw",
            preference_policy="require",
        ),
        ProviderSelection(
            provider_id="openclaw",
            reason="test",
            compatible_candidates=("openclaw",),
        ),
    )


def test_confirmed_persona_payload_reaches_codex_runtime_unchanged() -> None:
    async def run() -> None:
        prior = "不是三维模型，是你自己的个人静态网页，你自己设计一下。"
        current = "啊，那你现在开始做。"
        original = (
            "Create a personal static HTML page for Kurisu Makise. "
            "This must be her page, not a Codex product page."
        )
        decision = parse_control_decision_reply(
            '{"decisions":[{"proposal_index":0,"provider":"codex",'
            '"intent":"execute","work_placement":"draft",'
            '"session_context":"unchanged","workspace_effect":"write",'
            '"payload_continuity":"confirmed_prior_request",'
            '"reference_mode":"none"}]}',
            proposal_count=1,
        )
        actions, notes = reconcile_control_decision(
            ({"task": original},),
            decision,
            provider_ids=("codex",),
        )
        assert notes == []
        attrs = actions[0]
        attrs["_host_source_user_text"] = current
        attrs["_host_turn_id"] = "turn-persona-confirmation"
        workspace = str(Path(__file__).resolve().parents[1])
        start = AsyncMock(
            return_value=SimpleNamespace(
                task_handle=None,
                result="created",
                error="",
                metadata={"result_type": "ok"},
            )
        )
        with (
            patch("server.app._delegate_provider_selection", return_value=_codex_selection()),
            patch(
                "server.app._delegate_workspace_route",
                return_value={
                    "status": "resolved",
                    "cwd": workspace,
                    "workspaceMode": "scratch",
                    "source": "test",
                },
            ),
            patch(
                "agent_host.provider_runtime.runtime.get_manifest",
                return_value=CODEX_APP_SERVER_MANIFEST,
            ),
            patch("agent_host.provider_runtime.runtime.start", new=start),
            patch("server.app._latest_user_message", return_value=current),
            patch("server.app._user_message_before_current", return_value=prior),
            patch(
                "server.app._immediately_preceding_assistant_was_interrupted",
                return_value=False,
            ),
            patch(
                "server.work_ledger_coordinator.get_work_ledger_coordinator",
                return_value=None,
            ),
        ):
            result = await _handle_delegate(original, attrs)

        assert result == "created"
        request = start.await_args.args[0]
        assert request.task == original
        assert request.provider == "codex"
        assert request.metadata["payload_source"] == "confirmed_prior_request"
        assert "delegate_sanitized" not in request.metadata

    asyncio.run(run())


def test_direct_self_reference_reaches_codex_with_separate_role_context() -> None:
    async def run() -> None:
        source = (
            "你能做一个关于你自己的网页吗？如果需要相关的形象素材，"
            "你应该去公开的web资源查找，不要留白，然后导出到桌面"
        )
        start = AsyncMock(
            return_value=SimpleNamespace(
                task_handle=None,
                result="created",
                error="",
                metadata={"result_type": "ok"},
            )
        )
        workspace = str(Path(__file__).resolve().parents[1])
        with (
            patch("server.app._delegate_provider_selection", return_value=_codex_selection()),
            patch(
                "server.app._delegate_workspace_route",
                return_value={
                    "status": "resolved",
                    "cwd": workspace,
                    "workspaceMode": "scratch",
                    "source": "test",
                },
            ),
            patch(
                "agent_host.provider_runtime.runtime.get_manifest",
                return_value=CODEX_APP_SERVER_MANIFEST,
            ),
            patch("agent_host.provider_runtime.runtime.start", new=start),
            patch("server.app._latest_user_message", return_value=source),
            patch(
                "server.work_ledger_coordinator.get_work_ledger_coordinator",
                return_value=None,
            ),
        ):
            result = await _handle_delegate(
                source,
                {
                    "provider": "codex",
                    "intent": "execute",
                    "subject": "project",
                    "target": "desktop",
                    "_host_source_user_text": source,
                    "_host_turn_id": "turn-direct-self-reference",
                },
            )

        assert result == "created"
        request = start.await_args.args[0]
        assert request.task == source
        assert request.metadata[MAIN_ROLE_NAME_METADATA_KEY] == (
            MAIN_CONVERSATION_ROLE_NAME
        )
        rendered = with_main_role_reference(
            request.task,
            metadata=request.metadata,
            execution_provider=request.provider,
        )
        assert rendered.startswith(source)
        assert 'main role is "Makise Kurisu (牧瀬紅莉栖)"' in rendered
        assert 'execution Provider is "codex"' in rendered

    asyncio.run(run())


def test_exact_wikipedia_delegate_keeps_url_and_atomic_open() -> None:
    async def run() -> None:
        start = AsyncMock(
            return_value=SimpleNamespace(
                task_handle=None,
                result="",
                error="",
                metadata={},
            )
        )
        model_task = (
            "Open the Wikipedia page for 'Kurisu Makise' by going to "
            "https://en.wikipedia.org/wiki/Kurisu_Makise and report what is shown."
        )
        with (
            patch("server.app._delegate_provider_selection", return_value=_browser_selection()),
            patch("agent_host.provider_runtime.runtime.get_manifest", return_value=BROWSER_MANIFEST),
            patch("agent_host.provider_runtime.runtime.start", new=start),
            patch("server.app._latest_user_message", return_value="修改棋子的标识"),
        ):
            await _handle_delegate(
                model_task,
                {
                    "provider": "browser",
                    "intent": "execute",
                    "action": "open",
                    "_host_source_user_text": "帮我打开一下维基百科找到你自己的页面",
                    "_host_turn_id": "turn-wikipedia",
                },
            )

        request = start.await_args.args[0]
        assert request.task == model_task
        assert request.mode == "open"
        assert request.metadata["browser_action"] == "open"
        assert request.metadata["url"] == (
            "https://en.wikipedia.org/wiki/Kurisu_Makise"
        )
        assert request.metadata["source_user_text"] == (
            "帮我打开一下维基百科找到你自己的页面"
        )

    asyncio.run(run())


def test_taskless_wikipedia_operation_reaches_runtime_with_source_task() -> None:
    async def run() -> None:
        start = AsyncMock(
            return_value=SimpleNamespace(
                task_handle=None,
                result="",
                error="",
                metadata={},
            )
        )
        source = "帮我打开一下维基百科找到你自己的页面"
        _clean, actions = parse_tags_and_clean(
            '[DELEGATE provider="browser" intent="execute" action="open" '
            'url="https://en.wikipedia.org/wiki/Kurisu_Makise"]'
        )
        actions[0]["attrs"]["_host_source_user_text"] = source
        actions[0]["attrs"]["_host_turn_id"] = "turn-taskless-wikipedia"
        with (
            patch("server.app._delegate_provider_selection", return_value=_browser_selection()),
            patch("agent_host.provider_runtime.runtime.get_manifest", return_value=BROWSER_MANIFEST),
            patch("agent_host.provider_runtime.runtime.start", new=start),
            patch.object(action_dispatcher, "_delegate_fn", _handle_delegate),
        ):
            batch = action_dispatcher.record_actions(actions)
            assert batch is not None
            await batch

        request = start.await_args.args[0]
        assert request.task == source
        assert request.mode == "open"
        assert request.metadata["url"] == (
            "https://en.wikipedia.org/wiki/Kurisu_Makise"
        )

    asyncio.run(run())


def test_addressless_open_and_find_is_assembled_as_research() -> None:
    async def run() -> None:
        start = AsyncMock(
            return_value=SimpleNamespace(
                task_handle=None,
                result="",
                error="",
                metadata={},
            )
        )
        model_task = "Open Wikipedia and search for 'Kurisu Makise' page."
        with (
            patch("server.app._delegate_provider_selection", return_value=_browser_selection()),
            patch("agent_host.provider_runtime.runtime.get_manifest", return_value=BROWSER_MANIFEST),
            patch("agent_host.provider_runtime.runtime.start", new=start),
            patch("server.app._latest_user_message", return_value="修改棋子的标识"),
        ):
            await _handle_delegate(
                model_task,
                {
                    "provider": "browser",
                    "intent": "execute",
                    "action": "open",
                    "_host_source_user_text": "帮我打开一下维基百科找到你自己的页面",
                    "_host_turn_id": "turn-wikipedia-search",
                },
            )

        request = start.await_args.args[0]
        assert request.task == model_task
        assert request.mode == "delegate"
        assert "browser_action" not in request.metadata
        assert request.metadata["browser_request_normalization"] == {
            "status": "lowered",
            "from_action": "open",
            "to_mode": "research",
            "reason": "addressless_open_with_search_intent",
        }

    asyncio.run(run())


def test_live_browser_lease_is_consumed_before_provider_selection() -> None:
    async def run() -> None:
        requests: list[dict] = []

        async def provider_run(params: dict) -> dict:
            requests.append(params)
            return {"run": {"run_id": "browser_continued", "status": "running"}}

        with tempfile.TemporaryDirectory(prefix="browser-lease-") as root:
            coordinator = InteractionBranchCoordinator(
                provider_run=provider_run,
                root=root,
            )
            now = time.time()
            branch = InteractionBranchState(
                branch_id="branch-live",
                parent_session_id="session-live",
                provider="browser",
                status="active",
                goal="inspect fixture",
                browser_session_id="browser-session-live",
                work_item_id="work-live",
                expires_at=now + 900,
            )
            coordinator._active_by_session["session-live"] = branch
            lease = coordinator.capture_routing_lease("session-live")
            assert lease is not None
            interaction_branch_module._current_coordinator = coordinator
            try:
                with (
                    patch(
                        "core.session_manager.get_current_session_id",
                        return_value="session-live",
                    ),
                    patch(
                        "server.app._delegate_provider_selection",
                        side_effect=AssertionError(
                            "provider selection must not run before live lease"
                        ),
                    ),
                    patch(
                        "server.app._announce_interaction_branch_lease_block",
                        new=AsyncMock(),
                    ) as announce_block,
                ):
                    result = await _handle_delegate(
                        "Open the Detail link in the current page.",
                        {
                            "provider": "browser",
                            "intent": "execute",
                            "action": "open",
                            "branch": "continue",
                            "_host_source_user_text": (
                                "就在刚才那个页面里，点开唯一的 Detail 链接。"
                            ),
                            "_host_turn_id": "turn-live-continue",
                            "_host_interaction_branch_routing_lease": lease.as_dict(),
                        },
                    )
            finally:
                interaction_branch_module._current_coordinator = None

        assert result is None
        assert len(requests) == 1
        request = requests[0]
        assert request["provider"] == "browser"
        assert request["metadata"]["interaction_branch_id"] == "branch-live"
        assert request["metadata"]["browser_session_id"] == "browser-session-live"
        assert request["metadata"]["work"] == {"work_item_id": "work-live"}
        assert request["metadata"]["continuation"] == "amend"
        announce_block.assert_not_awaited()

    asyncio.run(run())


def test_providerless_and_alias_continuations_consume_exact_browser_lease() -> None:
    async def run() -> None:
        for declared_provider in (None, "web", "playwright"):
            requests: list[dict] = []

            async def provider_run(params: dict) -> dict:
                requests.append(params)
                return {"run": {"run_id": "continued", "status": "running"}}

            with tempfile.TemporaryDirectory(prefix="browser-alias-lease-") as root:
                coordinator = InteractionBranchCoordinator(
                    provider_run=provider_run,
                    root=root,
                )
                branch = InteractionBranchState(
                    branch_id="branch-alias",
                    parent_session_id="session-alias",
                    provider="browser",
                    status="active",
                    goal="current page",
                    browser_session_id="browser-alias",
                    expires_at=time.time() + 900,
                )
                coordinator._active_by_session["session-alias"] = branch
                lease = coordinator.capture_routing_lease("session-alias")
                assert lease is not None
                interaction_branch_module._current_coordinator = coordinator
                attrs = {
                    "branch": "continue",
                    "_host_source_user_text": "继续当前页面",
                    "_host_interaction_branch_routing_lease": lease.as_dict(),
                }
                if declared_provider is not None:
                    attrs["provider"] = declared_provider
                try:
                    with patch(
                        "core.session_manager.get_current_session_id",
                        return_value="session-alias",
                    ):
                        consumed = await _consume_captured_interaction_branch_intent(
                            "continue current page",
                            attrs,
                        )
                finally:
                    interaction_branch_module._current_coordinator = None

            assert consumed is True
            assert attrs["_host_interaction_branch_scope_disposition"] == "accepted"
            assert len(requests) == 1
            assert requests[0]["metadata"]["interaction_branch_id"] == (
                "branch-alias"
            )

        close_coordinator = InteractionBranchCoordinator(
            provider_run=lambda _params: None,  # type: ignore[arg-type]
            root=tempfile.mkdtemp(prefix="providerless-close-"),
        )
        close_branch = InteractionBranchState(
            branch_id="branch-close",
            parent_session_id="session-close",
            provider="browser",
            status="idle",
            goal="close page",
            browser_session_id="browser-close",
            expires_at=time.time() + 900,
        )
        close_coordinator._active_by_session["session-close"] = close_branch
        close_lease = close_coordinator.capture_routing_lease("session-close")
        assert close_lease is not None
        interaction_branch_module._current_coordinator = close_coordinator
        close_attrs = {
            "branch": "close",
            "_host_interaction_branch_routing_lease": close_lease.as_dict(),
        }
        try:
            with patch(
                "core.session_manager.get_current_session_id",
                return_value="session-close",
            ):
                assert await _consume_captured_interaction_branch_intent(
                    "close current page",
                    close_attrs,
                ) is True
        finally:
            interaction_branch_module._current_coordinator = None
        assert close_attrs["_host_interaction_branch_scope_disposition"] == (
            "accepted"
        )
        assert close_coordinator.active_branch_for_session("session-close") is None

    asyncio.run(run())


def test_providerless_branch_relation_is_required_before_selection() -> None:
    async def run() -> None:
        async def provider_run(_params: dict) -> dict:
            raise AssertionError("missing branch relation must not start work")

        coordinator = InteractionBranchCoordinator(
            provider_run=provider_run,
            root=tempfile.mkdtemp(prefix="providerless-relation-"),
        )
        branch = InteractionBranchState(
            branch_id="branch-relation",
            parent_session_id="session-relation",
            provider="browser",
            status="active",
            goal="current page",
            browser_session_id="browser-relation",
            expires_at=time.time() + 900,
        )
        coordinator._active_by_session["session-relation"] = branch
        lease = coordinator.capture_routing_lease("session-relation")
        assert lease is not None
        interaction_branch_module._current_coordinator = coordinator
        announce = AsyncMock()
        try:
            with (
                patch(
                    "core.session_manager.get_current_session_id",
                    return_value="session-relation",
                ),
                patch(
                    "server.app._delegate_provider_selection",
                    side_effect=AssertionError("provider selection must not run"),
                ),
                patch(
                    "server.app._announce_interaction_branch_lease_block",
                    new=announce,
                ),
            ):
                result = await _handle_delegate(
                    "make a workspace change",
                    {
                        "intent": "execute",
                        "_host_interaction_branch_routing_lease": lease.as_dict(),
                    },
                )
        finally:
            interaction_branch_module._current_coordinator = None

        assert result == (
            "[routing scope blocked] captured Browser scope is no longer valid"
        )
        announce.assert_awaited_once()
        assert announce.await_args.kwargs["reason"] == (
            "browser_branch_relation_missing"
        )

    asyncio.run(run())


def test_workspace_route_uses_frozen_origin_session_not_ambient_ui() -> None:
    captured: list[dict] = []

    class Coordinator:
        def resolve_workspace_route(self, attrs):
            captured.append(dict(attrs))
            return {
                "status": "resolved",
                "cwd": "C:/workspace-origin",
                "projectId": "project-origin",
                "source": "test",
            }

    with (
        patch(
            "server.work_ledger_coordinator.get_work_ledger_coordinator",
            return_value=Coordinator(),
        ),
        patch(
            "core.session_manager.get_current_session_id",
            return_value="session-ambient",
        ),
    ):
        route = _delegate_workspace_route(
            "codex",
            {
                "_host_admitted_session_id": "session-origin",
                "session_id": "session-forged",
            },
            manifest=CODEX_APP_SERVER_MANIFEST,
        )

    assert route["status"] == "resolved"
    assert captured[0]["session_id"] == "session-origin"


def test_focus_refuses_to_mutate_after_origin_session_switch() -> None:
    async def run() -> None:
        coordinator = SimpleNamespace(
            set_session_project=AsyncMock(
                side_effect=AssertionError("must not mutate ambient Session")
            ),
            clear_session_project=AsyncMock(
                side_effect=AssertionError("must not mutate ambient Session")
            ),
        )
        with (
            patch(
                "core.session_manager.get_current_session_id",
                return_value="session-b",
            ),
            patch(
                "server.work_ledger_coordinator.get_work_ledger_coordinator",
                return_value=coordinator,
            ),
        ):
            result = await _handle_declared_focus(
                {"project_id": "project-a"},
                session_id="session-a",
            )

        assert result["ok"] is False
        assert result["authority_blocked"] is True
        coordinator.set_session_project.assert_not_awaited()
        coordinator.clear_session_project.assert_not_awaited()

    asyncio.run(run())


def test_stale_browser_lease_cannot_fall_through_to_new_provider_work() -> None:
    async def run() -> None:
        requests: list[dict] = []

        async def provider_run(params: dict) -> dict:
            requests.append(params)
            return {"run": {"run_id": "unexpected", "status": "running"}}

        with tempfile.TemporaryDirectory(prefix="browser-stale-lease-") as root:
            coordinator = InteractionBranchCoordinator(
                provider_run=provider_run,
                root=root,
            )
            now = time.time()
            original = InteractionBranchState(
                branch_id="branch-original",
                parent_session_id="session-live",
                provider="browser",
                status="active",
                goal="old page",
                browser_session_id="browser-original",
                expires_at=now + 900,
            )
            coordinator._active_by_session["session-live"] = original
            lease = coordinator.capture_routing_lease("session-live")
            assert lease is not None
            replacement = InteractionBranchState(
                branch_id="branch-replacement",
                parent_session_id="session-live",
                provider="browser",
                status="active",
                goal="new page",
                browser_session_id="browser-replacement",
                expires_at=now + 900,
            )
            coordinator._active_by_session["session-live"] = replacement
            interaction_branch_module._current_coordinator = coordinator
            try:
                with (
                    patch(
                        "core.session_manager.get_current_session_id",
                        return_value="session-live",
                    ),
                    patch(
                        "server.app._delegate_provider_selection",
                        side_effect=AssertionError(
                            "stale captured lease must fail before provider selection"
                        ),
                    ),
                    patch(
                        "server.app._announce_interaction_branch_lease_block",
                        new=AsyncMock(),
                    ) as announce_block,
                ):
                    result = await _handle_delegate(
                        "Continue the old page.",
                        {
                            "provider": "browser",
                            "intent": "execute",
                            "action": "open",
                            "branch": "continue",
                            "_host_source_user_text": "继续刚才那个页面。",
                            "_host_turn_id": "turn-stale-continue",
                            "_host_interaction_branch_routing_lease": lease.as_dict(),
                        },
                    )
            finally:
                interaction_branch_module._current_coordinator = None

        assert result == (
            "[routing scope blocked] captured Browser scope is no longer valid"
        )
        assert requests == []
        assert replacement.visible_messages == []
        announce_block.assert_awaited_once()
        assert announce_block.await_args.kwargs["session_id"] == "session-live"
        assert announce_block.await_args.kwargs["branch_id"] == "branch-original"
        assert announce_block.await_args.kwargs["reason"] == "stale_turn_start_lease"

    asyncio.run(run())


def test_rejected_browser_lease_publishes_one_host_blocking_fact() -> None:
    async def run() -> None:
        added: list[dict] = []
        emitted: list[tuple[str, dict]] = []

        async def emit(method: str, payload: dict) -> None:
            emitted.append((method, dict(payload)))

        with (
            patch("server.work_context.add_work_note", side_effect=added.append),
            patch.object(bus, "emit", new=emit),
        ):
            await _announce_interaction_branch_lease_block(
                session_id="origin-session",
                turn_id="turn-blocked",
                branch_id="branch-old",
                instruction_revision=3,
                reason="stale_turn_start_lease",
            )

        assert len(added) == 1
        assert emitted == [(Method.CHAT_WORK_NOTE, added[0])]
        note = added[0]
        assert note["session_id"] == "origin-session"
        assert note["speak"] is True
        assert note["importance"] == "blocking"
        assert note["metadata"]["routing_scope_lease_blocked"] is True
        assert note["metadata"]["execution_started"] is False
        assert note["metadata"]["branch_id"] == "branch-old"

    asyncio.run(run())


def test_uncertain_browser_stop_never_claims_that_prior_execution_did_not_start() -> None:
    async def run() -> None:
        added: list[dict] = []

        async def emit(_method: str, _payload: dict) -> None:
            return None

        with (
            patch("server.work_context.add_work_note", side_effect=added.append),
            patch.object(bus, "emit", new=emit),
        ):
            await _announce_interaction_branch_lease_block(
                session_id="origin-session",
                turn_id="turn-uncertain",
                branch_id="branch-running",
                instruction_revision=7,
                reason="provider_handoff_stop_unconfirmed:cancel_pending",
            )

        note = added[0]
        assert note["metadata"]["execution_uncertain"] is True
        assert "execution_started" not in note["metadata"]
        assert "may still be active" in note["summary"]

    asyncio.run(run())


def test_started_browser_block_uses_truthful_transition_title() -> None:
    async def run() -> None:
        added: list[dict] = []

        async def emit(_method: str, _payload: dict) -> None:
            return None

        with (
            patch("server.work_context.add_work_note", side_effect=added.append),
            patch.object(bus, "emit", new=emit),
        ):
            await _announce_interaction_branch_lease_block(
                session_id="origin-session",
                turn_id="turn-started",
                branch_id="branch-started",
                instruction_revision=8,
                reason="branch_generation_superseded_during_start",
                execution_started=True,
            )

        note = added[0]
        assert note["title"] == "Browser transition blocked"
        assert note["metadata"]["execution_started"] is True
        assert "may already have begun" in note["summary"]
        assert "continuation was not started" not in note["title"].lower()

    asyncio.run(run())


def test_explicit_branch_new_and_provider_escape_do_not_consume_browser_lease() -> None:
    async def run() -> None:
        async def provider_run(_params: dict) -> dict:
            raise AssertionError("preflight must not start Provider work")

        lease = {
            "branch_id": "branch-live",
            "parent_session_id": "session-live",
            "provider": "browser",
            "instruction_revision": 2,
            "expires_at": time.time() + 900,
        }
        with tempfile.TemporaryDirectory(prefix="browser-escape-preflight-") as root:
            coordinator = InteractionBranchCoordinator(
                provider_run=provider_run,
                root=root,
            )
            coordinator._active_by_session["session-live"] = InteractionBranchState(
                branch_id="branch-live",
                parent_session_id="session-live",
                provider="browser",
                status="active",
                goal="old page",
                browser_session_id="browser-live",
                instruction_revision=2,
                expires_at=lease["expires_at"],
            )
            interaction_branch_module._current_coordinator = coordinator
            try:
                with patch(
                    "core.session_manager.get_current_session_id",
                    return_value="session-live",
                ):
                    assert await _consume_captured_interaction_branch_intent(
                        "Open a new page",
                        {
                            "provider": "browser",
                            "branch": "new",
                            "_host_interaction_branch_routing_lease": lease,
                        },
                    ) is False
                    assert await _consume_captured_interaction_branch_intent(
                        "Use OpenClaw instead",
                        {
                            "provider": "openclaw",
                            "branch": "continue",
                            "_host_interaction_branch_routing_lease": lease,
                        },
                    ) is False
            finally:
                interaction_branch_module._current_coordinator = None

    asyncio.run(run())


def test_session_switch_blocks_branch_new_or_provider_escape_before_any_side_effect() -> None:
    async def run() -> None:
        lease = {
            "branch_id": "branch-s1",
            "parent_session_id": "session-s1",
            "provider": "browser",
            "instruction_revision": 1,
            "expires_at": time.time() + 900,
        }
        announce = AsyncMock()
        with (
            patch(
                "core.session_manager.get_current_session_id",
                return_value="session-s2",
            ),
            patch(
                "server.app._delegate_provider_selection",
                side_effect=AssertionError("provider selection must not run in another Session"),
            ),
            patch(
                "server.app._announce_interaction_branch_lease_block",
                new=announce,
            ),
        ):
            result = await _handle_delegate(
                "Start this somewhere else",
                {
                    "provider": "openclaw",
                    "branch": "new",
                    "_host_turn_id": "turn-from-s1",
                    "_host_interaction_branch_routing_lease": lease,
                },
            )

        assert result == (
            "[routing scope blocked] captured Browser scope is no longer valid"
        )
        announce.assert_awaited_once()
        assert announce.await_args.kwargs["session_id"] == "session-s1"
        assert announce.await_args.kwargs["reason"] == (
            "session_changed_after_turn_admission"
        )

    asyncio.run(run())


def test_provider_escape_cannot_retire_same_session_replacement_branch() -> None:
    async def run() -> None:
        async def provider_run(_params: dict) -> dict:
            raise AssertionError("stale escape must not start Provider work")

        with tempfile.TemporaryDirectory(prefix="browser-stale-escape-") as root:
            coordinator = InteractionBranchCoordinator(
                provider_run=provider_run,
                root=root,
            )
            now = time.time()
            original = InteractionBranchState(
                branch_id="branch-a",
                parent_session_id="same-session",
                provider="browser",
                status="active",
                goal="A",
                browser_session_id="browser-a",
                expires_at=now + 900,
            )
            replacement = InteractionBranchState(
                branch_id="branch-b",
                parent_session_id="same-session",
                provider="browser",
                status="active",
                goal="B",
                browser_session_id="browser-b",
                expires_at=now + 900,
            )
            coordinator._active_by_session["same-session"] = original
            lease = coordinator.capture_routing_lease("same-session")
            assert lease is not None
            interaction_branch_module._current_coordinator = coordinator
            announce = AsyncMock()

            def select_and_replace(*_args, **_kwargs):
                coordinator._active_by_session["same-session"] = replacement
                return _openclaw_selection()

            try:
                with (
                    patch(
                        "core.session_manager.get_current_session_id",
                        return_value="same-session",
                    ),
                    patch(
                        "server.app._delegate_provider_selection",
                        side_effect=select_and_replace,
                    ),
                    patch(
                        "agent_host.provider_runtime.runtime.get_manifest",
                        side_effect=AssertionError("stale escape reached Provider dispatch"),
                    ),
                    patch(
                        "server.app._announce_interaction_branch_lease_block",
                        new=announce,
                    ),
                ):
                    result = await _handle_delegate(
                        "Use OpenClaw now",
                        {
                            "provider": "openclaw",
                            "intent": "execute",
                            "_host_turn_id": "stale-escape-turn",
                            "_host_interaction_branch_routing_lease": lease.as_dict(),
                        },
                    )
            finally:
                interaction_branch_module._current_coordinator = None

        assert result == (
            "[provider handoff blocked] prior Browser run may still be active"
        )
        assert coordinator.active_branch_for_session("same-session") is replacement
        assert replacement.status == "active"
        announce.assert_awaited_once()
        assert announce.await_args.kwargs["branch_id"] == "branch-a"

    asyncio.run(run())


def test_unconfirmed_browser_handoff_blocks_new_provider_dispatch() -> None:
    async def run() -> None:
        coordinator = SimpleNamespace(
            close_for_provider_handoff=AsyncMock(
                side_effect=InteractionBranchRunStopUnconfirmed(
                    branch_id="branch-running",
                    run_id="browser-running",
                    reason="cancel_unconfirmed",
                )
            )
        )
        announce = AsyncMock()
        with (
            patch(
                "server.app._delegate_provider_selection",
                return_value=_openclaw_selection(),
            ),
            patch(
                "core.session_manager.get_current_session_id",
                return_value="session-live",
            ),
            patch(
                "server.interaction_branch.get_interaction_branch_coordinator",
                return_value=coordinator,
            ),
            patch(
                "agent_host.provider_runtime.runtime.get_manifest",
                side_effect=AssertionError("new Provider must not be dispatched"),
            ),
            patch(
                "server.app._announce_interaction_branch_lease_block",
                new=announce,
            ),
        ):
            result = await _handle_delegate(
                "Use the agent instead",
                {
                    "provider": "openclaw",
                    "intent": "execute",
                    "_host_turn_id": "handoff-uncertain",
                },
            )

        assert result == (
            "[provider handoff blocked] prior Browser run may still be active"
        )
        announce.assert_awaited_once()
        assert "stop_unconfirmed" in announce.await_args.kwargs["reason"]

    asyncio.run(run())


def test_addressless_web_goal_hands_off_to_openclaw_without_model_url() -> None:
    async def run() -> None:
        start = AsyncMock(
            return_value=SimpleNamespace(
                task_handle=None,
                result="",
                error="",
                metadata={},
            )
        )
        source = "帮我打开维基百科找到你自己的页面"
        with (
            patch(
                "agent_host.provider_runtime.runtime.provider_manifests",
                return_value=(
                    BROWSER_MANIFEST,
                    CODEX_APP_SERVER_MANIFEST,
                    OPENCLAW_MANIFEST,
                ),
            ),
            patch(
                "agent_host.provider_runtime.runtime.get_manifest",
                return_value=OPENCLAW_MANIFEST,
            ),
            patch("agent_host.provider_runtime.runtime.start", new=start),
        ):
            await _handle_delegate(
                "Open the Paxos page at https://ja.wikipedia.org/wiki/Paxos.",
                {
                    "provider": "browser",
                    "intent": "execute",
                    "action": "open",
                    "branch": "continue",
                    "url": "https://ja.wikipedia.org/wiki/Paxos",
                    "_host_source_user_text": source,
                    "_host_turn_id": "turn-agent-handoff",
                },
            )

        request = start.await_args.args[0]
        assert request.provider == "openclaw"
        assert request.requirements.task_kind == "research"
        assert f"Latest user instruction (authoritative): {source}" in request.task
        assert "Makise Kurisu (牧瀬紅莉栖)" not in request.task
        assert "Paxos" not in request.task
        assert request.metadata[MAIN_ROLE_NAME_METADATA_KEY] == (
            MAIN_CONVERSATION_ROLE_NAME
        )
        rendered = with_main_role_reference(
            request.task,
            metadata=request.metadata,
            execution_provider=request.provider,
        )
        assert "Makise Kurisu (牧瀬紅莉栖)" in rendered
        assert "browser_action" not in request.metadata
        assert "url" not in request.metadata
        assert request.metadata["branch_intent"] == ""
        assert request.metadata["provider_handoff"]["reason"] == (
            "browser_goal_lowered_to_agent_research"
        )
        assert request.metadata["provider_handoff"]["removed_browser_parameters"] == [
            "action",
            "branch",
            "url",
        ]
        assert request.metadata["provider_selection"]["provider_id"] == "openclaw"

    asyncio.run(run())


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all delegate browser assembly tests passed")


if __name__ == "__main__":
    _main()
