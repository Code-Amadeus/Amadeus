"""Real-model probe for bounded Provider payload continuity.

This is intentionally separate from pytest: it verifies that the configured
ControlDecision model distinguishes a short go-ahead from any new or amended
instruction before the Host trusts a prior-turn Provider payload.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from llm.client import remote_llm_messages_query
from llm.prompts import get_structured_control_prompt
from server.app import _sanitize_delegate_task_for_provider
from server.control_proposal import seal_control_proposals
from server.control_shadow import ControlDecisionAdjudicator, ControlShadowContext


CASES = (
    {
        "id": "persona_page_go_ahead",
        "prior": (
            {
                "role": "user",
                "content": "不是三维模型，是你自己的个人静态网页，你自己设计一下。",
            },
            {"role": "assistant", "content": "個人ページね。分かったわ。"},
        ),
        "user": "啊，那你现在开始做。",
        "task": "Create a personal static HTML page for Kurisu Makise.",
        "expected": "confirmed_prior_request",
        "expected_task": "proposal",
    },
    {
        "id": "persona_page_short_go_ahead",
        "prior": (
            {"role": "user", "content": "给你自己做一个个人网页。"},
            {"role": "assistant", "content": "私のページを作るのね。"},
        ),
        "user": "你去呗。",
        "task": "Create Kurisu Makise's personal static webpage.",
        "expected": "confirmed_prior_request",
        "expected_task": "proposal",
    },
    {
        "id": "polite_question_as_go_ahead",
        "prior": (
            {"role": "user", "content": "给你自己做一个个人网页。"},
            {"role": "assistant", "content": "私のページを作るのね。"},
        ),
        "user": "你能现在开始吗？",
        "task": "Create Kurisu Makise's personal static webpage.",
        "expected": "confirmed_prior_request",
        "expected_task": "proposal",
    },
    {
        "id": "contextual_pronoun_amendment",
        "prior": (
            {"role": "user", "content": "给你自己做一个个人网页。"},
            {"role": "assistant", "content": "私のページを作るのね。"},
        ),
        "user": "把它改成深红色。",
        "task": "Make Kurisu Makise's personal page dark red.",
        "expected": "current_turn",
        "expected_task": "current_user",
    },
    {
        "id": "unrelated_self_contained_instruction",
        "prior": (
            {"role": "user", "content": "帮我找到你自己的页面。"},
            {"role": "assistant", "content": "見つけたわ。"},
        ),
        "user": "把游戏背景改成蓝色。",
        "task": "Create a Kurisu-themed version of the unrelated game.",
        "expected": "current_turn",
        "expected_task": "current_user",
    },
    {
        "id": "explicit_current_identity",
        "prior": (),
        "user": "给你自己做一个个人静态网页。",
        "task": "Create Kurisu Makise's personal static webpage.",
        "expected": "current_turn",
        "expected_task": "proposal",
    },
    {
        "id": "bare_acknowledgement",
        "prior": (
            {"role": "user", "content": "我们只讨论，不要执行。"},
            {"role": "assistant", "content": "分かった、実行しないわ。"},
        ),
        "user": "嗯。",
        "task": "Create a Kurisu-themed page.",
        "expected": "none",
        "expected_task": "none",
    },
)


async def _query(messages: list[dict[str, str]]) -> str:
    return await asyncio.to_thread(
        remote_llm_messages_query,
        messages,
        temperature=0.0,
        max_tokens=240,
        timeout=45.0,
    )


class _ConversationHistory:
    def __init__(self, messages: tuple[dict[str, str], ...]) -> None:
        self.dialog = [dict(message) for message in messages]


class _SessionManager:
    def __init__(self, messages: tuple[dict[str, str], ...]) -> None:
        self.conversation_history = _ConversationHistory(messages)


async def main(*, summary_only: bool = False) -> None:
    if summary_only:
        for logger_name in ("server", "server.control_shadow", "llm.client"):
            logging.getLogger(logger_name).setLevel(logging.WARNING)
    correct = 0
    for case in CASES:
        batch = seal_control_proposals(
            [
                {
                    "type": "DELEGATE",
                    "attrs": {
                        "provider": "codex",
                        "intent": "execute",
                        "task": case["task"],
                    },
                    "raw": "",
                }
            ],
            turn_id=f"payload-continuity-{case['id']}",
            session_id="payload-continuity-probe",
            user_text=case["user"],
            transport="inline_tag",
            prior_messages=case["prior"],
        )
        messages = (
            {
                "role": "system",
                "content": (
                    get_structured_control_prompt()
                    + "\nRegistered Provider for this probe: codex."
                ),
            },
            *case["prior"],
            {"role": "user", "content": case["user"]},
        )
        evidence = await ControlDecisionAdjudicator(query=_query).observe(
            batch,
            ControlShadowContext(
                messages=messages,
                candidates=(),
                catalog_complete=True,
                exhaustive_candidate_limit=64,
                provider_ids=frozenset({"codex"}),
            ),
        )
        action = dict(evidence.canonical_actions[0]) if evidence.canonical_actions else {}
        actual = (
            str(action.get("_host_payload_source") or "current_turn")
            if action
            else "none"
        )
        if action:
            action["_host_source_user_text"] = case["user"]
            dispatched_task, sanitize_audit = _sanitize_delegate_task_for_provider(
                str(action.get("task") or ""),
                action,
                provider="codex",
                session_manager=_SessionManager(
                    (*case["prior"], {"role": "user", "content": case["user"]})
                ),
            )
        else:
            dispatched_task, sanitize_audit = "", {}
        expected_task = {
            "proposal": case["task"],
            "current_user": case["user"],
            "none": "",
        }[case["expected_task"]]
        task_ok = dispatched_task == expected_task
        passed = (
            evidence.decision_status == "ok"
            and actual == case["expected"]
            and task_ok
        )
        correct += int(passed)
        if not summary_only or not passed:
            print(
                json.dumps(
                    {
                        "id": case["id"],
                        "expected": case["expected"],
                        "actual": actual,
                        "decision_status": evidence.decision_status,
                        "outcome": evidence.outcome,
                        "task_ok": task_ok,
                        "sanitize_reason": sanitize_audit.get("reason", ""),
                        "passed": passed,
                        "reply": evidence.decision_reply[:500],
                    },
                    ensure_ascii=False,
                )
            )
    print(json.dumps({"cases": len(CASES), "correct": correct}, ensure_ascii=False))
    if correct != len(CASES):
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(summary_only=args.summary_only))
