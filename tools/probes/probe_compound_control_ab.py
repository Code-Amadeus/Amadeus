r"""Paired real-model A/B for compound ControlDecision decomposition.

A is the shipping one-proposal ControlDecision. B is the shadow-only exact
source-clause plan. Neither arm dispatches Provider work or mutates a ledger.

Usage:
    .venv\Scripts\python.exe -X utf8 tools/probes/probe_compound_control_ab.py
    .venv\Scripts\python.exe -X utf8 tools/probes/probe_compound_control_ab.py --repeat 3
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import sys
import time
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_host.provider_catalog import (  # noqa: E402
    BROWSER_MANIFEST,
    CODEX_APP_SERVER_MANIFEST,
    OPENCLAW_MANIFEST,
)
from agent_host.provider_runtime import runtime as provider_runtime  # noqa: E402
from llm.client import remote_llm_messages_query  # noqa: E402
from llm.prompts import get_structured_control_prompt  # noqa: E402
from server.compound_control_shadow import (  # noqa: E402
    operation_control_view,
    resolve_compound_control_plan,
)
from server.control_decision import (  # noqa: E402
    CONTROL_REFERENCE_CANDIDATES_ATTR,
    reconcile_control_decision,
    resolve_control_decision,
)
from server.reference_catalog import TypedReferenceCandidate  # noqa: E402


@dataclass(frozen=True, slots=True)
class Case:
    name: str
    current: str
    proposal_task: str
    proposal_control: Mapping[str, Any]
    expected: tuple[tuple[str, str], ...]
    expected_subjects: tuple[str, ...] = ()
    candidates: tuple[TypedReferenceCandidate, ...] | None = None
    history: tuple[Mapping[str, str], ...] = ()


ALPHA = TypedReferenceCandidate(
    "work_item",
    "work_alpha",
    "alpha.txt task",
    "session_draft",
    recency_rank=2,
    aliases=("alpha.txt",),
    state="review_ready",
    execution="succeeded",
)
BETA = TypedReferenceCandidate(
    "work_item",
    "work_beta",
    "beta.txt task",
    "session_draft",
    recency_rank=1,
    aliases=("beta.txt",),
    state="review_ready",
    execution="succeeded",
)
AMADEUS = TypedReferenceCandidate(
    "project",
    "project_amadeus",
    "amadeus",
    "persistent",
    aliases=("Amadeus",),
)
CANDIDATES = (AMADEUS, ALPHA, BETA)

CHESS_PROJECT = TypedReferenceCandidate(
    "project",
    "project_chess",
    "国际象棋游戏",
    "persistent",
    aliases=("国际象棋游戏",),
    session_focus=True,
)
CHESS_CHILD = TypedReferenceCandidate(
    "work_item",
    "work_route",
    "route-note.txt task",
    "project",
    parent_project_id="project_chess",
    parent_project_label="国际象棋游戏",
    aliases=("route-note.txt",),
    relation="current",
    session_current=True,
)
CHESS_CANDIDATES = (CHESS_CHILD, CHESS_PROJECT)

OPEN_PROJECT = TypedReferenceCandidate(
    "project",
    "project_open_chess",
    "象棋",
    "persistent",
    aliases=("象棋",),
)
OPEN_CHILD = TypedReferenceCandidate(
    "work_item",
    "work_open_chess",
    "象棋",
    "project",
    parent_project_id="project_open_chess",
    parent_project_label="象棋",
    aliases=("象棋",),
)
OPEN_CHESS_CANDIDATES = (OPEN_CHILD, OPEN_PROJECT)


CASES = (
    Case(
        "compound-amend-report",
        "把 alpha.txt 改成 one-updated；顺便告诉我 beta.txt 那个任务现在什么状态。",
        "把 alpha.txt 改成 one-updated；顺便告诉我 beta.txt 那个任务现在什么状态。",
        {"provider": "codex", "intent": "amend", "subject": "work_item"},
        (("amend", "work_alpha"), ("report", "work_beta")),
    ),
    Case(
        "compound-two-amends",
        "把 alpha.txt 改成 red；再把 beta.txt 改成 blue。",
        "把 alpha.txt 改成 red；再把 beta.txt 改成 blue。",
        {"provider": "codex", "intent": "amend", "subject": "work_item"},
        (("amend", "work_alpha"), ("amend", "work_beta")),
    ),
    Case(
        "compound-new-report",
        "新开一次性草稿创建 gamma.txt，写入 three；然后告诉我 beta.txt 现在什么状态。",
        "新开一次性草稿创建 gamma.txt，写入 three；然后告诉我 beta.txt 现在什么状态。",
        {"provider": "codex", "intent": "execute", "one_off": True},
        (("execute", ""), ("report", "work_beta")),
    ),
    Case(
        "compound-same-subject",
        "把 alpha.txt 改成 purple；完成后告诉我 alpha.txt 这个任务的状态。",
        "把 alpha.txt 改成 purple；完成后告诉我 alpha.txt 这个任务的状态。",
        {"provider": "codex", "intent": "amend", "subject": "work_item"},
        (("amend", "work_alpha"), ("report", "work_alpha")),
    ),
    Case(
        "single-amend",
        "把 alpha.txt 改成 green。",
        "把 alpha.txt 改成 green。",
        {"provider": "codex", "intent": "amend", "subject": "work_item"},
        (("amend", "work_alpha"),),
    ),
    Case(
        "single-report",
        "告诉我 beta.txt 那个任务现在什么状态。",
        "告诉我 beta.txt 那个任务现在什么状态。",
        {"provider": "codex", "intent": "report", "subject": "work_item"},
        (("report", "work_beta"),),
    ),
    Case(
        "single-report-leading-order",
        "然后告诉我 beta.txt 现在什么状态。",
        "然后告诉我 beta.txt 现在什么状态。",
        {"provider": "codex", "intent": "report", "subject": "work_item"},
        (("report", "work_beta"),),
    ),
    Case(
        "single-new-draft",
        "新开一次性草稿创建 gamma.txt，写入 three。",
        "新开一次性草稿创建 gamma.txt，写入 three。",
        {"provider": "codex", "intent": "execute", "one_off": True},
        (("execute", ""),),
    ),
    Case(
        "single-multi-requirement",
        "新开一次性草稿做一个计时器，要有开始、暂停和重置三个按钮。",
        "新开一次性草稿做一个计时器，要有开始、暂停和重置三个按钮。",
        {"provider": "codex", "intent": "execute", "one_off": True},
        (("execute", ""),),
    ),
    Case(
        "action-plus-chat",
        "把 alpha.txt 改成 yellow；顺便跟我说一句晚安。",
        "把 alpha.txt 改成 yellow",
        {"provider": "codex", "intent": "amend", "subject": "work_item"},
        (("amend", "work_alpha"),),
    ),
    Case(
        "single-project-focus-amend",
        "切回 amadeus 项目，把 README.md 最后一行改成 ready。",
        "切回 amadeus 项目，把 README.md 最后一行改成 ready。",
        {"provider": "codex", "intent": "amend", "focus": "set"},
        (("amend", "project_amadeus"),),
    ),
    Case(
        "single-focus-placement-constraint",
        "先回到草稿，后面的临时工作不要放进项目。",
        "先回到草稿，后面的临时工作不要放进项目。",
        {"provider": "codex", "intent": "focus"},
        (("focus", ""),),
    ),
    Case(
        "typed-project-focus",
        "切回象棋项目。",
        "切回象棋项目。",
        {"provider": "codex", "intent": "focus", "project_id": "project_chess"},
        (("focus", "project_chess"),),
        ("project",),
        CHESS_CANDIDATES,
    ),
    Case(
        "typed-workitem-focus",
        "切回象棋项目里的 route-note.txt 任务。",
        "切回象棋项目里的 route-note.txt 任务。",
        {"provider": "codex", "intent": "focus", "subject": "work_item"},
        (("focus", "work_route"),),
        ("work_item",),
        CHESS_CANDIDATES,
    ),
    Case(
        "typed-project-report",
        "告诉我象棋项目整体进展。",
        "告诉我象棋项目整体进展。",
        {"provider": "codex", "intent": "report", "subject": "project"},
        (("report", "project_chess"),),
        ("project",),
        CHESS_CANDIDATES,
    ),
    Case(
        "typed-workitem-report",
        "告诉我象棋项目里的 route-note.txt 任务状态。",
        "告诉我象棋项目里的 route-note.txt 任务状态。",
        {"provider": "codex", "intent": "report", "subject": "work_item"},
        (("report", "work_route"),),
        ("work_item",),
        CHESS_CANDIDATES,
    ),
    Case(
        "typed-open-focus",
        "切换到名为象棋的那个对象。",
        "切换到名为象棋的那个对象。",
        {"provider": "codex", "intent": "focus"},
        (("focus", "work_open_chess|project_open_chess"),),
        ("open",),
        OPEN_CHESS_CANDIDATES,
        (
            {
                "role": "user",
                "content": (
                    "现在有两个同名且同样可继续的对象：一个持久 Project 叫象棋，"
                    "一个独立 WorkItem 也叫象棋；两者没有先后偏好。"
                ),
            },
            {"role": "assistant", "content": "已确认两个同名对象，尚未选择。"},
        ),
    ),
    Case(
        "negative-desire",
        "这个 alpha.txt 修改听起来很麻烦，先别动。",
        "修改 alpha.txt",
        {"provider": "codex", "intent": "amend", "subject": "work_item"},
        (),
    ),
    Case(
        "negative-correction",
        "我只是提到 beta.txt，不是让你查询或者修改。",
        "查询 beta.txt",
        {"provider": "codex", "intent": "report", "subject": "work_item"},
        (),
    ),
    Case(
        "negative-hypothetical",
        "如果以后修改 alpha.txt，也许可以换成橙色，不过现在不要做。",
        "修改 alpha.txt 为橙色",
        {"provider": "codex", "intent": "amend", "subject": "work_item"},
        (),
    ),
)


class _ManifestAdapter:
    def __init__(self, manifest) -> None:
        self.manifest = manifest
        self.provider_id = manifest.provider_id

    async def run(self, *_args, **_kwargs):  # pragma: no cover - never dispatched
        raise AssertionError("compound A/B must not dispatch")

    async def cancel(self, _run_id: str):  # pragma: no cover - never dispatched
        return {"cancelled": False}


def _ensure_manifests() -> None:
    known = set(provider_runtime.list_providers())
    for manifest in (
        BROWSER_MANIFEST,
        CODEX_APP_SERVER_MANIFEST,
        OPENCLAW_MANIFEST,
    ):
        if manifest.provider_id not in known:
            provider_runtime.register(_ManifestAdapter(manifest))
            known.add(manifest.provider_id)


def _messages(case: Case) -> tuple[dict[str, str], ...]:
    default_history = (
        {"role": "user", "content": "创建 alpha.txt，写入 one。"},
        {
            "role": "assistant",
            "content": (
                '[DELEGATE provider="codex" intent="execute" '
                'task="创建 alpha.txt，写入 one"]'
            ),
        },
        {"role": "user", "content": "创建 beta.txt，写入 two。"},
        {
            "role": "assistant",
            "content": (
                '[DELEGATE provider="codex" intent="execute" '
                'task="创建 beta.txt，写入 two"]'
            ),
        },
    )
    return (
        {"role": "system", "content": get_structured_control_prompt()},
        *(case.history or default_history),
        {"role": "user", "content": case.current},
    )


def _target(action: Mapping[str, Any]) -> str:
    direct = str(action.get("workspace_ref") or action.get("project_id") or "")
    if direct:
        return direct
    references = action.get(CONTROL_REFERENCE_CANDIDATES_ATTR)
    if isinstance(references, tuple) and references:
        return "|".join(candidate.entity_id for candidate in references)
    return ""


def _signature(actions: list[Mapping[str, Any]]) -> tuple[tuple[str, str], ...]:
    return tuple(
        (str(action.get("intent") or ""), _target(action))
        for action in actions
    )


async def _query(messages: list[dict[str, str]], *, model: str) -> str:
    return await asyncio.to_thread(
        remote_llm_messages_query,
        messages,
        model=model,
        temperature=0.0,
        max_tokens=900,
    )


async def _run_arm_a(case: Case, *, model: str) -> dict[str, Any]:
    payloads = ({"task": case.proposal_task},)
    controls = (dict(case.proposal_control),)
    candidates = case.candidates or CANDIDATES
    started = time.monotonic()
    decision = await resolve_control_decision(
        _messages(case),
        payloads,
        candidates,
        complete=True,
        query=lambda messages: _query(messages, model=model),
        proposal_controls=controls,
    )
    actions, notes = reconcile_control_decision(
        payloads,
        decision,
        provider_ids={"browser", "codex", "openclaw"},
        proposal_controls=controls,
        source_user_text=case.current,
    )
    return {
        "status": decision.status,
        "signature": _signature(actions),
        "subjects": tuple(str(action.get("subject") or "") for action in actions),
        "actions": [
            {
                "control": {
                    key: value
                    for key, value in action.items()
                    if not str(key).startswith("_host_") and key != "task"
                },
                "target": _target(action),
            }
            for action in actions
        ],
        "notes": notes,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "candidate_queries": decision.candidate_verdict_queries,
        "decision_retries": decision.decision_protocol_retries,
    }


async def _run_arm_b(case: Case, *, model: str) -> dict[str, Any]:
    started = time.monotonic()
    candidates = case.candidates or CANDIDATES
    plan = await resolve_compound_control_plan(
        _messages(case),
        ({"task": case.proposal_task},),
        candidates,
        complete=True,
        query=lambda messages: _query(messages, model=model),
        provider_ids={"browser", "codex", "openclaw"},
        proposal_controls=(case.proposal_control,),
    )
    actions = [dict(operation.action) for operation in plan.operations]
    return {
        "status": plan.status,
        "signature": _signature(actions),
        "subjects": tuple(str(action.get("subject") or "") for action in actions),
        "clauses": [clause.text for clause in plan.clauses],
        "operations": [
            {
                "source_clause": operation.source_clause,
                "control": operation_control_view(operation),
                "target": _target(operation.action),
            }
            for operation in plan.operations
        ],
        "reason": plan.reason,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "decision_queries": plan.decision_queries,
        "candidate_queries": plan.candidate_verdict_queries,
        "decomposition_retries": plan.decomposition_protocol_retries,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    _ensure_manifests()
    selected_cases = tuple(
        case for case in CASES if not args.case or case.name in set(args.case)
    )
    if not selected_cases:
        raise ValueError("no compound A/B cases selected")
    rows: list[dict[str, Any]] = []
    for repeat in range(1, max(1, int(args.repeat)) + 1):
        for case in selected_cases:
            arm_a = await _run_arm_a(case, model=args.model)
            arm_b = await _run_arm_b(case, model=args.model)
            expected = case.expected
            expected_subjects = case.expected_subjects
            a_match = tuple(arm_a["signature"]) == expected and (
                not expected_subjects
                or tuple(arm_a["subjects"]) == expected_subjects
            )
            b_match = tuple(arm_b["signature"]) == expected and (
                not expected_subjects
                or tuple(arm_b["subjects"]) == expected_subjects
            )
            row = {
                "repeat": repeat,
                "case": case.name,
                "utterance": case.current,
                "expected": [list(item) for item in expected],
                "expected_subjects": list(expected_subjects),
                "a": {**arm_a, "match": a_match},
                "b": {**arm_b, "match": b_match},
            }
            rows.append(row)
            print(
                f"r{repeat} {case.name}: "
                f"A={'PASS' if a_match else 'MISS'} {arm_a['signature']} | "
                f"B={'PASS' if b_match else 'MISS'} {arm_b['signature']}"
            )

    compound_names = {
        "compound-amend-report",
        "compound-two-amends",
        "compound-new-report",
        "compound-same-subject",
    }
    single_names = {
        "single-amend",
        "single-report",
        "single-report-leading-order",
        "single-new-draft",
        "single-multi-requirement",
        "action-plus-chat",
        "single-project-focus-amend",
        "single-focus-placement-constraint",
        "typed-project-focus",
        "typed-workitem-focus",
        "typed-project-report",
        "typed-workitem-report",
        "typed-open-focus",
    }
    negative_names = {
        "negative-desire",
        "negative-correction",
        "negative-hypothetical",
    }

    def count(predicate) -> int:
        return sum(1 for row in rows if predicate(row))

    summary = {
        "samples": len(rows),
        "a_matches": count(lambda row: row["a"]["match"]),
        "b_matches": count(lambda row: row["b"]["match"]),
        "compound_a_matches": count(
            lambda row: row["case"] in compound_names and row["a"]["match"]
        ),
        "compound_b_matches": count(
            lambda row: row["case"] in compound_names and row["b"]["match"]
        ),
        "single_regressions": count(
            lambda row: row["case"] in single_names
            and row["a"]["match"]
            and not row["b"]["match"]
        ),
        "negative_added_actions": count(
            lambda row: row["case"] in negative_names and bool(row["b"]["signature"])
        ),
        "a_latency_median_ms": int(
            statistics.median(row["a"]["latency_ms"] for row in rows)
        ),
        "b_latency_median_ms": int(
            statistics.median(row["b"]["latency_ms"] for row in rows)
        ),
    }
    return {
        "schema": "amadeus.compound-control-ab.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "repeat": max(1, int(args.repeat)),
        "summary": summary,
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument(
        "--case",
        action="append",
        choices=tuple(case.name for case in CASES),
        help="run only this named case (repeatable)",
    )
    parser.add_argument(
        "--report-dir",
        default=str(ROOT / "runtime" / "e2e_reports" / "compound_control_ab"),
    )
    args = parser.parse_args()
    report = asyncio.run(run(args))
    report_dir = Path(args.report_dir).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = report_dir / f"compound_control_ab_{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"report: {path}")
    summary = report["summary"]
    return 1 if summary["single_regressions"] or summary["negative_added_actions"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
