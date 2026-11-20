"""Real-model probe for the narrow typed-reference resolver.

The probe has no host side effects.  It asks only which members of one complete
Project/WorkItem catalog remain plausible, then checks whether exact type words
collapse the set and whether genuinely underspecified phrases preserve it.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.reference_clarification import (  # noqa: E402
    TypedReferenceCandidate,
    audit_context_switch,
    resolve_typed_reference,
)


@dataclass(frozen=True)
class Case:
    name: str
    utterance: str
    allowed_token_sets: frozenset[frozenset[str]]


@dataclass(frozen=True)
class OperationCase:
    name: str
    utterance: str
    expected: bool


CANDIDATES = (
    TypedReferenceCandidate(
        kind="work_item",
        entity_id="work_chess_duo",
        label="象棋双人模式",
        scope="project",
        parent_project_id="project_chess",
        parent_project_label="象棋",
        recency_rank=1,
    ),
    TypedReferenceCandidate(
        kind="work_item",
        entity_id="work_chess_draft",
        label="临时象棋原型",
        scope="session_draft",
        recency_rank=2,
    ),
    TypedReferenceCandidate(
        kind="project",
        entity_id="project_chess",
        label="象棋",
        scope="persistent",
        recency_rank=1,
    ),
    TypedReferenceCandidate(
        kind="project",
        entity_id="project_amadeus",
        label="Amadeus",
        scope="persistent",
        recency_rank=2,
    ),
)

P_CHESS = "project:project_chess"
P_AMADEUS = "project:project_amadeus"
W_DUO = "work_item:work_chess_duo"
W_DRAFT = "work_item:work_chess_draft"

CASES = (
    Case("exact-project", "切回象棋项目。", frozenset({frozenset({P_CHESS})})),
    Case("exact-project-en", "Switch to the Amadeus Project.", frozenset({frozenset({P_AMADEUS})})),
    Case("exact-workitem", "继续象棋双人模式这个 WorkItem。", frozenset({frozenset({W_DUO})})),
    Case(
        "bare-recent-chess",
        "切回刚才那个象棋。",
        frozenset(
            {
                frozenset({P_CHESS, W_DUO}),
                frozenset({P_CHESS, W_DRAFT}),
                frozenset({P_CHESS, W_DUO, W_DRAFT}),
            }
        ),
    ),
    Case("explicit-draft", "继续临时象棋原型。", frozenset({frozenset({W_DRAFT})})),
)

OPERATION_CASES = (
    OperationCase("switch-project", "切回象棋项目。", True),
    OperationCase("switch-ambiguous", "切到刚才那个象棋。", True),
    OperationCase("continue-workitem", "继续象棋双人模式这个 WorkItem。", True),
    OperationCase("status-workitem", "刚才那个象棋任务做完了吗？", False),
    OperationCase("create-in-project", "在象棋项目里新建 route-note.txt。", False),
)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repeats", nargs="?", type=int, default=2)
    parser.add_argument("--model", default=os.environ.get("LLM_MODEL", "deepseek-v4-flash"))
    args = parser.parse_args()

    from llm.client import remote_llm_messages_query

    async def query(messages: list[dict[str, str]]) -> str:
        return await asyncio.to_thread(
            remote_llm_messages_query,
            messages,
            model=args.model,
            temperature=0.0,
        )

    failures = 0
    latencies: list[float] = []
    for repeat in range(max(1, args.repeats)):
        print(f"round {repeat + 1}")
        for case in CASES:
            started = time.monotonic()
            result = await resolve_typed_reference(
                case.utterance,
                CANDIDATES,
                complete=True,
                query=query,
            )
            latencies.append(time.monotonic() - started)
            tokens = frozenset(candidate.token for candidate in result.candidates)
            ok = tokens in case.allowed_token_sets
            failures += 0 if ok else 1
            print(
                f"  {'PASS' if ok else 'FAIL'} {case.name:20s} "
                f"status={result.status:10s} tokens={sorted(tokens)} "
                f"raw={' '.join(result.raw_reply.split())[:180]} "
                f"reason={result.reason[:180]}"
            )
        for case in OPERATION_CASES:
            started = time.monotonic()
            result = await audit_context_switch(case.utterance, query=query)
            latencies.append(time.monotonic() - started)
            ok = result.status == "ok" and result.context_switch is case.expected
            failures += 0 if ok else 1
            print(
                f"  {'PASS' if ok else 'FAIL'} operation/{case.name:10s} "
                f"status={result.status:11s} value={result.context_switch!r} "
                f"raw={' '.join(result.raw_reply.split())[:180]} "
                f"reason={result.reason[:180]}"
            )
    if latencies:
        ordered = sorted(latencies)
        print(
            f"summary: failures={failures}/{(len(CASES) + len(OPERATION_CASES)) * max(1, args.repeats)} "
            f"median={ordered[len(ordered) // 2]:.2f}s"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
