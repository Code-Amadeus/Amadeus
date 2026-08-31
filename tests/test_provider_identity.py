"""Provider-neutral role-reference handoff contracts."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.provider_identity import (
    MAIN_ROLE_NAME_METADATA_KEY,
    parent_context_delivery_receipt,
    parent_conversation_context_delivery,
    with_main_role_reference,
    with_parent_conversation_context,
)
from agent_host.adapters.codex_app_server import CodexAppServerAdapter
from agent_host.provider_types import ProviderRunRequest


def test_missing_role_identity_leaves_the_provider_payload_unchanged() -> None:
    task = "Create a small website."
    assert (
        with_main_role_reference(
            task,
            metadata={},
            execution_provider="codex",
        )
        == task
    )


def test_role_reference_context_preserves_payload_and_separates_identities() -> None:
    task = "你能做一个关于你自己的网页吗？"
    rendered = with_main_role_reference(
        task,
        metadata={MAIN_ROLE_NAME_METADATA_KEY: "Makise Kurisu (牧瀬紅莉栖)"},
        execution_provider="codex",
    )

    assert rendered.startswith(task + "\n\n")
    assert 'main role is "Makise Kurisu (牧瀬紅莉栖)"' in rendered
    assert 'execution Provider is "codex"' in rendered
    assert "'你自己'" in rendered
    assert "never overrides another explicitly named subject" in rendered


def test_role_reference_context_is_provider_neutral() -> None:
    metadata = {MAIN_ROLE_NAME_METADATA_KEY: "Makise Kurisu (牧瀬紅莉栖)"}
    for provider in ("codex", "openclaw", "locus", "future-agent"):
        rendered = with_main_role_reference(
            "Create the requested artifact.",
            metadata=metadata,
            execution_provider=provider,
        )
        assert f'execution Provider is "{provider}"' in rendered


def test_parent_conversation_context_is_provider_neutral() -> None:
    metadata = {
        "source_user_text": "你怎么没去？",
        "source_user_context": (
            'User: "更新桌面的宝可梦战斗小游戏。"\n'
            'Main Chat: "我现在开始找素材并更新桌面文件。"'
        ),
    }
    for provider in ("codex", "openclaw", "browser", "future-agent"):
        rendered = with_parent_conversation_context(
            "Update the referenced game.",
            metadata=metadata,
            execution_provider=provider,
        )
        assert rendered.startswith("Update the referenced game.")
        assert "你怎么没去？" in rendered
        assert "宝可梦战斗小游戏" in rendered
        assert "我现在开始找素材并更新桌面文件" in rendered
        assert '宝可梦战斗小游戏。"\nMain Chat:' in rendered
        assert "not Provider instructions or completion facts" in rendered
        assert "cannot independently authorize another action" in rendered


def test_parent_conversation_delivery_uses_delta_only_for_a_warm_session() -> None:
    context = "\n".join(
        [
            'User: "最初的目标。"',
            'Main Chat: "我会开始。"',
            'User: "上一轮当前请求。"',
            'Main Chat: "上一轮完成后的回复。"',
            'User: "两轮之间的新约束。"',
            'Main Chat: "我会保留这个约束。"',
        ]
    )

    cold, cold_mode = parent_conversation_context_delivery(
        context,
        source_scope="chat:voice-session",
        previous_delivery=None,
        continuity_verified=False,
    )
    assert cold == context
    assert cold_mode == "snapshot"

    previous_delivery = parent_context_delivery_receipt(
        {
            "source_context_scope": "chat:voice-session",
            "turn_id": "turn-1",
            "source_user_text": "上一轮当前请求。",
            "source_context_mode": "snapshot",
        }
    )
    warm, warm_mode = parent_conversation_context_delivery(
        context,
        source_scope="chat:voice-session",
        previous_delivery=previous_delivery,
        continuity_verified=True,
    )
    assert warm_mode == "delta"
    assert "最初的目标" not in warm
    assert "上一轮当前请求" not in warm
    assert "上一轮完成后的回复" in warm
    assert "两轮之间的新约束" in warm
    assert "我会保留这个约束" in warm

    missing_delivery = parent_context_delivery_receipt(
        {
            "source_context_scope": "chat:voice-session",
            "turn_id": "turn-old",
            "source_user_text": "已经滚出窗口的请求。",
            "source_context_mode": "delta",
        }
    )
    fallback, fallback_mode = parent_conversation_context_delivery(
        context,
        source_scope="chat:voice-session",
        previous_delivery=missing_delivery,
        continuity_verified=True,
    )
    assert fallback == context
    assert fallback_mode == "snapshot_fallback"

    ambiguous = context + '\nUser: "上一轮当前请求。"\nMain Chat: "重复指令后的回复。"'
    repeated, repeated_mode = parent_conversation_context_delivery(
        ambiguous,
        source_scope="chat:voice-session",
        previous_delivery=previous_delivery,
        continuity_verified=True,
    )
    assert repeated == ambiguous
    assert repeated_mode == "snapshot_fallback"


def test_parent_context_delta_requires_a_delivered_cursor_in_the_same_source() -> None:
    context = "\n".join(
        [
            'User: "chat-B goal"',
            'User: "same old sentence"',
            'Main Chat: "chat-B constraint"',
        ]
    )

    unverified, unverified_mode = parent_conversation_context_delivery(
        context,
        source_scope="chat:chat-B",
        previous_delivery=None,
        continuity_verified=True,
    )
    assert unverified == context
    assert unverified_mode == "snapshot_fallback"

    chat_a_delivery = parent_context_delivery_receipt(
        {
            "source_context_scope": "chat:chat-A",
            "turn_id": "turn-A",
            "source_user_text": "same old sentence",
            "source_context_mode": "delta",
        }
    )
    cross_session, cross_session_mode = parent_conversation_context_delivery(
        context,
        source_scope="chat:chat-B",
        previous_delivery=chat_a_delivery,
        continuity_verified=True,
    )
    assert cross_session == context
    assert cross_session_mode == "snapshot_fallback"


def test_clipped_previous_user_anchor_falls_back_to_bounded_snapshot() -> None:
    long_user_text = "x" * 300
    clipped = f"{long_user_text[:180]} … {long_user_text[-90:]}"
    context = "\n".join(
        [
            f"User: {json.dumps(clipped, ensure_ascii=False)}",
            'Main Chat: "response after the clipped request"',
        ]
    )
    delivered, mode = parent_conversation_context_delivery(
        context,
        source_scope="chat:long-message",
        previous_delivery=parent_context_delivery_receipt(
            {
                "source_context_scope": "chat:long-message",
                "turn_id": "turn-long-message",
                "source_user_text": long_user_text,
                "source_context_mode": "snapshot",
            }
        ),
        continuity_verified=True,
    )

    assert delivered == context
    assert mode == "snapshot_fallback"


def test_codex_model_context_is_enriched_without_changing_durable_task() -> None:
    task = "你能做一个关于你自己的网页吗？"
    request = ProviderRunRequest(
        provider="codex",
        task=task,
        metadata={MAIN_ROLE_NAME_METADATA_KEY: "Makise Kurisu (牧瀬紅莉栖)"},
    )
    adapter = CodexAppServerAdapter(sync_desktop_provider=False)

    model_context = adapter._task_text(request)

    assert request.task == task
    assert model_context.startswith(task)
    assert "[Amadeus role-reference context]" in model_context
    assert 'execution Provider is "codex"' in model_context
