"""Real-model probe for the double-consent action omission candidate.

Not collected by pytest. Run directly and retain aggregate evidence in docs
before changing the production mode from ``off``.
"""

from __future__ import annotations

import asyncio
import json
import os

from llm.client import remote_llm_messages_query
from llm.prompts import (
    get_delegate_control_prompt,
    get_structured_control_prompt,
    registered_provider_ids,
)
from server.action_existence_recovery import (
    classify_action_existence,
    reconstruct_delegate_commitment,
)
from server.work_context import augment_system_prompt_with_active_provider_context
from server.control_proposal import seal_control_proposals
from server.control_shadow import ControlDecisionAdjudicator, ControlShadowContext


CASES = [
    {
        "id": "research_direct",
        "expected": True,
        "prior": [],
        "user": "帮我查一下 Paxos 的经典论文。",
        "assistant": "好，我现在去查并整理可靠来源。",
    },
    {
        "id": "research_short_confirm",
        "expected": True,
        "prior": [
            {"role": "user", "content": "帮我查 Paxos 的经典论文。"},
            {"role": "assistant", "content": "要我现在开始查吗？"},
        ],
        "user": "快去",
        "assistant": "好，我现在就去查。",
    },
    {
        "id": "asr_elliptical_directive",
        "expected": True,
        "prior": [
            {"role": "user", "content": "Paxos 的经典论文还没有查。"},
            {"role": "assistant", "content": "我知道你指的是那篇共识论文。"},
        ],
        "user": "那个你去弄一下",
        "assistant": "行，我去把论文和出处查清楚。",
    },
    {
        "id": "code_change",
        "expected": True,
        "prior": [],
        "user": "把 app.py 的超时处理修好。",
        "assistant": "我来检查并修复这个超时路径。",
    },
    {
        "id": "ledger_report",
        "expected": True,
        "prior": [],
        "user": "告诉我当前任务做到哪一步了。",
        "assistant": "我查一下当前任务记录。",
    },
    {
        "id": "retract",
        "expected": True,
        "prior": [],
        "user": "停掉刚才还在跑的任务。",
        "assistant": "好，我现在停止它。",
    },
    {
        "id": "complaint_not_command",
        "expected": False,
        "prior": [
            {"role": "user", "content": "帮我查 Paxos 论文。"},
            {"role": "assistant", "content": "我会去查。"},
        ],
        "user": "你还没去",
        "assistant": "你说得对，我刚才没有启动。",
    },
    {
        "id": "negative_pause",
        "expected": False,
        "prior": [],
        "user": "先不要重试。",
        "assistant": "好，先不重试。",
    },
    {
        "id": "past_fact",
        "expected": False,
        "prior": [],
        "user": "没有使用 OpenClaw 找 Paxos 论文。",
        "assistant": "对，刚才没有走外部检索。",
    },
    {
        "id": "failure_report",
        "expected": False,
        "prior": [],
        "user": "失败了。",
        "assistant": "我看到了失败状态。",
    },
    {
        "id": "theory_question",
        "expected": False,
        "prior": [],
        "user": "Paxos 为什么重要？",
        "assistant": "因为它给出了故障环境下达成共识的方法。",
    },
    {
        "id": "hypothetical_provider",
        "expected": False,
        "prior": [],
        "user": "如果用 OpenClaw 查会怎样？",
        "assistant": "它会作为一次外部检索任务运行。",
    },
    {
        "id": "correction_only",
        "expected": False,
        "prior": [
            {"role": "user", "content": "我在问共识算法。"},
            {"role": "assistant", "content": "你指的是 Raft。"},
        ],
        "user": "我说的是 Paxos，不是 Raft。",
        "assistant": "明白，是 Paxos。",
    },
    {
        "id": "bare_ack",
        "expected": False,
        "prior": [
            {"role": "user", "content": "这只是解释，不要执行。"},
            {"role": "assistant", "content": "明白，只讨论。"},
        ],
        "user": "嗯",
        "assistant": "好。",
    },
]

PROBE_PROVIDER_ROSTER = """
[Registered provider routing]
- openclaw: general external research and information gathering.
- browser: interactive browser navigation inside an explicit web branch.
- codex: code and file work in a selected workspace.
- locus: code and file work through the alternate execution runtime.
Every delegate.provider must be one exact id above; it must never be empty.
[/Registered provider routing]
"""


async def main() -> None:
    async def query(messages: list[dict[str, str]]) -> str:
        return await asyncio.to_thread(
            remote_llm_messages_query,
            messages,
            temperature=0.0,
            max_tokens=180,
            timeout=30.0,
        )

    canonical_only = str(os.environ.get("PROBE_CANONICAL_ONLY") or "").lower() in {
        "1",
        "true",
        "yes",
    }
    correct = 0
    false_positive = 0
    false_negative = 0
    invalid = 0
    paxos_recovered: dict | None = (
        {
            "provider": "openclaw",
            "task": "Research and organize reliable sources for the classic Paxos paper.",
        }
        if canonical_only
        else None
    )
    selected_cases = [] if canonical_only else CASES
    for case in selected_cases:
        verdict = await classify_action_existence(
            query,
            user_text=case["user"],
            prior_messages=case["prior"],
        )
        resend = []
        if verdict.status == "ok" and verdict.existence == "work":
            recovery = await reconstruct_delegate_commitment(
                query,
                system_prompt=augment_system_prompt_with_active_provider_context(
                    get_delegate_control_prompt(),
                    session_id="action-existence-probe",
                    limit=4,
                    max_chars=900,
                ) + PROBE_PROVIDER_ROSTER,
                user_text=case["user"],
                assistant_reply=case["assistant"],
                prior_messages=case["prior"],
            )
            if recovery.status == "ok" and recovery.committed and recovery.delegate:
                resend = [{"type": "DELEGATE", "attrs": dict(recovery.delegate)}]
            recovery_debug = {
                "status": recovery.status,
                "committed": recovery.committed,
                "reason": recovery.reason,
                "raw": recovery.raw_reply[:360],
            }
        else:
            recovery_debug = {}
        recovered = bool(resend)
        if case["id"] == "research_direct" and recovered:
            paxos_recovered = dict(resend[0]["attrs"])
        expected = bool(case["expected"])
        correct += int(recovered == expected)
        false_positive += int(recovered and not expected)
        false_negative += int(expected and not recovered)
        invalid += int(verdict.status != "ok")
        print(
            json.dumps(
                {
                    "id": case["id"],
                    "expected": expected,
                    "gate_status": verdict.status,
                    "gate": verdict.existence,
                    "gate_reason": verdict.reason,
                    "resend_count": len(resend),
                    "commitment": recovery_debug,
                    "recovered": recovered,
                    "correct": recovered == expected,
                },
                ensure_ascii=False,
            )
        )
    print(
        json.dumps(
            {
                "cases": len(selected_cases),
                "correct": correct,
                "false_positive": false_positive,
                "false_negative": false_negative,
                "invalid": invalid,
            },
            ensure_ascii=False,
        )
    )
    if paxos_recovered is not None:
        batch = seal_control_proposals(
            [{"type": "DELEGATE", "attrs": paxos_recovered, "raw": ""}],
            turn_id="probe-paxos-canonical",
            session_id="action-existence-probe",
            user_text="帮我查一下 Paxos 的经典论文。",
            transport="inline_tag",
            prior_messages=(),
        )
        adjudicator = ControlDecisionAdjudicator(query=query)
        evidence = await adjudicator.observe(
            batch,
            ControlShadowContext(
                messages=(
                    {
                        "role": "system",
                        "content": get_structured_control_prompt() + PROBE_PROVIDER_ROSTER,
                    },
                    {"role": "user", "content": "帮我查一下 Paxos 的经典论文。"},
                ),
                candidates=(),
                catalog_complete=True,
                exhaustive_candidate_limit=64,
                provider_ids=frozenset(
                    registered_provider_ids()
                    or ("openclaw", "browser", "codex", "locus")
                ),
            ),
        )
        print(
            json.dumps(
                {
                    "canonical_probe": "paxos_direct",
                    "decision_status": evidence.decision_status,
                    "outcome": evidence.outcome,
                    "canonical_actions": [dict(item) for item in evidence.canonical_actions],
                    "notes": list(evidence.notes),
                    "reason": evidence.reason,
                    "decision_reply": evidence.decision_reply,
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
