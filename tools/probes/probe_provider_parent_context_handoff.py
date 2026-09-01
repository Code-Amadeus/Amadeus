"""Deterministic A/B probe for Provider parent-conversation handoff."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_host.provider_identity import (  # noqa: E402
    parent_context_delivery_receipt,
    parent_conversation_context_delivery,
    with_parent_conversation_context,
)
from core.chat_runtime import ChatRuntime  # noqa: E402


def _legacy_latest_user_context(
    prior_messages: list[dict[str, str]],
    *,
    current_user: str,
) -> str:
    current = " ".join(str(current_user or "").split())
    for message in reversed(prior_messages):
        if message.get("role") != "user":
            continue
        content = " ".join(str(message.get("content") or "").split())
        if content and content != current:
            return content[:2000]
    return ""


def _pr18_unverified_delta(
    context: str,
    *,
    previous_source_user_text: str,
) -> tuple[str, str]:
    """The PR #18 algorithm before delivery/source authority was added."""

    lines = [line.strip() for line in str(context or "").splitlines() if line.strip()]
    marker = f"User: {json.dumps(previous_source_user_text, ensure_ascii=False)}"
    matches = [index for index, line in enumerate(lines) if line == marker]
    if len(matches) != 1:
        return "\n".join(lines), "snapshot_fallback"
    return "\n".join(lines[matches[0] + 1 :]), "delta"


def main() -> None:
    current = "你怎么没去？"
    prior = [
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
    ]
    legacy = _legacy_latest_user_context(prior, current_user=current)
    action = {"type": "DELEGATE", "attrs": {}}
    ChatRuntime._annotate_delegate_source(
        action,
        current,
        turn_id="turn-handoff-probe",
        prior_messages=prior,
    )
    checkpoint = str(action["attrs"].get("_host_source_user_context") or "")

    quote = '"'
    turn_one_context = "\n".join(
        [
            f"User: {quote}A initial goal{quote}",
            f"Main Chat: {quote}B initial response{quote}",
        ]
    )
    turn_one, turn_one_mode = parent_conversation_context_delivery(
        turn_one_context,
        source_scope="chat:probe",
        previous_delivery=None,
        continuity_verified=False,
    )
    turn_one_delivery = parent_context_delivery_receipt(
        {
            "source_context_scope": "chat:probe",
            "turn_id": "turn-1",
            "source_user_text": "C first request",
            "source_context_mode": turn_one_mode,
        }
    )
    turn_two_context = "\n".join(
        [
            turn_one_context,
            f"User: {quote}C first request{quote}",
            f"Main Chat: {quote}D response after C{quote}",
        ]
    )
    turn_two, turn_two_mode = parent_conversation_context_delivery(
        turn_two_context,
        source_scope="chat:probe",
        previous_delivery=turn_one_delivery,
        continuity_verified=True,
    )
    turn_two_delivery = parent_context_delivery_receipt(
        {
            "source_context_scope": "chat:probe",
            "turn_id": "turn-2",
            "source_user_text": "E second request",
            "source_context_mode": turn_two_mode,
        }
    )
    turn_three_context = "\n".join(
        [
            turn_two_context,
            f"User: {quote}E second request{quote}",
            f"Main Chat: {quote}F response after E{quote}",
        ]
    )
    turn_three, turn_three_mode = parent_conversation_context_delivery(
        turn_three_context,
        source_scope="chat:probe",
        previous_delivery=turn_two_delivery,
        continuity_verified=True,
    )
    missing_delivery = parent_context_delivery_receipt(
        {
            "source_context_scope": "chat:probe",
            "turn_id": "turn-missing",
            "source_user_text": "cursor outside the rolling window",
            "source_context_mode": "delta",
        }
    )
    missing, missing_mode = parent_conversation_context_delivery(
        turn_three_context,
        source_scope="chat:probe",
        previous_delivery=missing_delivery,
        continuity_verified=True,
    )
    ambiguous_context = (
        turn_three_context
        + f"\nUser: {quote}E second request{quote}"
        + f"\nMain Chat: {quote}response after repeated wording{quote}"
    )
    ambiguous, ambiguous_mode = parent_conversation_context_delivery(
        ambiguous_context,
        source_scope="chat:probe",
        previous_delivery=turn_two_delivery,
        continuity_verified=True,
    )

    failed_context = "\n".join(
        [
            f"User: {quote}original goal{quote}",
            f"Main Chat: {quote}starting it{quote}",
            f"User: {quote}second constraint{quote}",
            f"Main Chat: {quote}provider start failed before delivery{quote}",
        ]
    )
    pr18_failed, pr18_failed_mode = _pr18_unverified_delta(
        failed_context,
        previous_source_user_text="second constraint",
    )
    verified_turn_one = parent_context_delivery_receipt(
        {
            "source_context_scope": "chat:chat-A",
            "turn_id": "turn-original",
            "source_user_text": "original goal",
            "source_context_mode": "snapshot",
        }
    )
    fixed_failed, fixed_failed_mode = parent_conversation_context_delivery(
        failed_context,
        source_scope="chat:chat-A",
        previous_delivery=verified_turn_one,
        continuity_verified=True,
    )

    cross_chat_context = "\n".join(
        [
            f"User: {quote}chat-B goal{quote}",
            f"User: {quote}same old sentence{quote}",
            f"Main Chat: {quote}chat-B constraint{quote}",
        ]
    )
    pr18_cross, pr18_cross_mode = _pr18_unverified_delta(
        cross_chat_context,
        previous_source_user_text="same old sentence",
    )
    chat_a_delivery = parent_context_delivery_receipt(
        {
            "source_context_scope": "chat:chat-A",
            "turn_id": "turn-chat-A",
            "source_user_text": "same old sentence",
            "source_context_mode": "delta",
        }
    )
    fixed_cross, fixed_cross_mode = parent_conversation_context_delivery(
        cross_chat_context,
        source_scope="chat:chat-B",
        previous_delivery=chat_a_delivery,
        continuity_verified=True,
    )

    provider_checks = {}
    metadata = {
        "source_user_text": current,
        "source_user_context": checkpoint,
        "source_context_mode": "snapshot",
    }
    for provider in ("codex", "openclaw", "browser", "future-agent"):
        rendered = with_parent_conversation_context(
            "Complete the referenced authorized update.",
            metadata=metadata,
            execution_provider=provider,
        )
        provider_checks[provider] = {
            "goal": "宝可梦战斗小游戏" in rendered,
            "asset_constraint": "免费或公版像素素材" in rendered,
            "commitment": "我现在开始找素材并更新桌面文件" in rendered,
            "authority_guard": "not Provider instructions or completion facts" in rendered,
        }

    checks = {
        "legacy_loses_goal": "宝可梦战斗小游戏" not in legacy,
        "legacy_loses_commitment": "我现在开始找素材" not in legacy,
        "checkpoint_keeps_goal": "宝可梦战斗小游戏" in checkpoint,
        "checkpoint_keeps_asset_constraint": "免费或公版像素素材" in checkpoint,
        "checkpoint_keeps_commitment": "我现在开始找素材并更新桌面文件" in checkpoint,
        "checkpoint_strips_control": "[DELEGATE" not in checkpoint,
        "checkpoint_is_bounded": len(checkpoint) <= 2000,
        "turn_one_is_snapshot": turn_one_mode == "snapshot" and turn_one == turn_one_context,
        "turn_two_is_delta": turn_two_mode == "delta"
        and turn_two == f"Main Chat: {quote}D response after C{quote}",
        "turn_three_is_delta": turn_three_mode == "delta"
        and turn_three == f"Main Chat: {quote}F response after E{quote}",
        "missing_cursor_falls_back": missing_mode == "snapshot_fallback"
        and missing == turn_three_context,
        "ambiguous_cursor_falls_back": ambiguous_mode == "snapshot_fallback"
        and ambiguous == ambiguous_context,
        "provider_matrix": all(
            all(values.values()) for values in provider_checks.values()
        ),
        "pr18_failed_attempt_drops_original": (
            pr18_failed_mode == "delta"
            and "original goal" not in pr18_failed
            and "second constraint" not in pr18_failed
        ),
        "fixed_failed_attempt_keeps_undelivered_constraint": (
            fixed_failed_mode == "delta"
            and "second constraint" in fixed_failed
            and "provider start failed" in fixed_failed
        ),
        "pr18_cross_session_drops_chat_b_goal": (
            pr18_cross_mode == "delta" and "chat-B goal" not in pr18_cross
        ),
        "fixed_cross_session_uses_snapshot": (
            fixed_cross_mode == "snapshot_fallback"
            and fixed_cross == cross_chat_context
        ),
    }
    report = {
        "schema": "amadeus.provider-parent-context-probe.v1",
        "pid": os.getpid(),
        "legacy_context": legacy,
        "checkpoint": checkpoint,
        "deliveries": {
            "turn_one": {"mode": turn_one_mode, "context": turn_one},
            "turn_two": {"mode": turn_two_mode, "context": turn_two},
            "turn_three": {"mode": turn_three_mode, "context": turn_three},
            "missing_cursor": {"mode": missing_mode, "context": missing},
            "ambiguous_cursor": {"mode": ambiguous_mode, "context": ambiguous},
        },
        "provider_checks": provider_checks,
        "counterexamples": {
            "failed_before_delivery": {
                "pr18": {"mode": pr18_failed_mode, "context": pr18_failed},
                "fixed": {"mode": fixed_failed_mode, "context": fixed_failed},
            },
            "cross_parent_session": {
                "pr18": {"mode": pr18_cross_mode, "context": pr18_cross},
                "fixed": {"mode": fixed_cross_mode, "context": fixed_cross},
            },
        },
        "checks": checks,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    failed = [name for name, passed in checks.items() if passed is not True]
    if failed:
        raise SystemExit("failed checks: " + ", ".join(failed))
    print("ok: provider parent-conversation handoff probe passed")


if __name__ == "__main__":
    main()
