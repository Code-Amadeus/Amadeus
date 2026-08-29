"""Browser delegation keeps current-turn authority and executable targets."""

from __future__ import annotations

import asyncio
import os
import sys
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
from server.app import (
    _handle_delegate,
    _rebase_web_goal_for_selected_provider,
    _remove_ungrounded_persona_parameters,
    _sanitize_delegate_task_for_provider,
)
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
