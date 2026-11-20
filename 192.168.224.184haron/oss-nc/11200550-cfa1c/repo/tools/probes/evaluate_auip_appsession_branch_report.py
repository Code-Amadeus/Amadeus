r"""Add semantic role/action scores to an existing AppSession branch report.

This is evaluator-only: it performs model calls but never reruns an action,
opens an application, starts Provider Work, or changes the Work ledger.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.auip_role_branch_experiment import AppSessionBranchProposal
from tools.probes.probe_auip_appsession_branch_abc import (
    _evaluate_turn,
    _summarize,
)


def _initial_history(journey: str) -> list[dict[str, str]]:
    if journey == "gomoku":
        return [
            {"role": "user", "content": "我们来一盘，你执黑。"},
            {"role": "assistant", "content": "いいわ、黒は私が持つ。"},
            {
                "role": "system",
                "content": (
                    "[Verified AUIP receipt] "
                    '{"accepted":true,"action_type":"gomoku.bind_side",'
                    '"payload":{"side":"black"},"resulting_revision":1}'
                ),
            },
        ]
    return [
        {"role": "user", "content": "这局我们一起守基地。"},
        {"role": "assistant", "content": "いいわ、状況は自分で判断する。"},
    ]


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    source = Path(args.report).resolve()
    report = json.loads(source.read_text(encoding="utf-8"))
    histories: dict[tuple[str, int, str], list[dict[str, str]]] = {}
    selected_turns = set(args.turns or [])
    for row in report.get("rows") or []:
        key = (
            str(row.get("arm") or ""),
            int(row.get("repeat") or 0),
            str(row.get("journey") or ""),
        )
        history = histories.setdefault(key, _initial_history(key[2]))
        user = str(row.get("user") or "")
        speech = str(row.get("speech") or "")
        proposal = AppSessionBranchProposal(
            action=str(row.get("proposal_action") or "wait"),
            action_type=str(row.get("action_type") or ""),
            payload=dict(row.get("payload") or {}),
            instruction_relation=str(row.get("instruction_relation") or ""),
            choice_reason=str(row.get("choice_reason") or ""),
            semantic_label=str(row.get("semantic_label") or ""),
        )
        should_score = not selected_turns or str(row.get("turn_id") or "") in selected_turns
        if should_score and not row.get("error"):
            row["evaluation"] = await _evaluate_turn(
                provider=args.provider,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
                user=user,
                branch_before=list(history),
                speech=speech,
                proposal=proposal,
                receipt={
                    "accepted": row.get("receipt_accepted"),
                    "resulting_revision": row.get("revision_after"),
                    "reason": row.get("receipt_reason") or "",
                },
            )
        if user:
            history.append({"role": "user", "content": user})
        if speech:
            history.append({"role": "assistant", "content": speech})
        if row.get("receipt_accepted") is not None:
            history.append(
                {
                    "role": "system",
                    "content": "[Verified AUIP receipt] "
                    + json.dumps(
                        {
                            "accepted": bool(row.get("receipt_accepted")),
                            "action_type": row.get("action_type") or "",
                            "payload": row.get("payload") or {},
                            "resulting_revision": row.get("revision_after"),
                            "reason": row.get("receipt_reason") or "",
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                }
            )
        history[:] = history[-12:]
    report["summary"] = _summarize(
        list(report.get("rows") or []),
        tuple(str(item) for item in report.get("arms") or []),
    )
    report["evaluation_config"] = {
        "provider": args.provider,
        "model": args.model,
        "reasoning_effort": args.reasoning_effort,
        "turn_filter": sorted(selected_turns),
    }
    target = (
        Path(args.output).resolve()
        if args.output
        else source.with_name(source.stem + ".evaluated.json")
    )
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"report_path": str(target), "summary": report["summary"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report")
    parser.add_argument("--output", default="")
    parser.add_argument("--provider", default="deepseek")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--reasoning-effort", default="none")
    parser.add_argument("--turns", nargs="*", default=[])
    args = parser.parse_args()
    result = asyncio.run(_run(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
