"""Does the model re-emit when asked, and what does asking cost?

`DELEGATE_RESEND_ON_OMISSION` replaced host-side synthesis on 2026-08-01, which
changed behaviour on a path that fires in ordinary conversation. Two risks came
with it and neither is answerable by unit tests:

  * The ask is a synchronous call sitting between "sentences dispatched" and
    the turn's last-sentence mark, so it delays turn completion by however long
    the model takes.
  * If the model habitually answers NONE, the omission net has not been
    replaced so much as removed -- and silently, which is worse than the
    synthesis it took over from.

Driving this through the routing testbed would mean waiting for the model to
omit a tag on its own, which happens in under a tenth of runs. The ask itself
needs no backend, no Codex and no ledger, so it is measured directly.

    .venv_cu124\\Scripts\\python.exe -X utf8 tools/probes/probe_delegate_resend.py [repeats]

Re-run after changing model or endpoint; the numbers are pinned to both.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.chat_runtime import ChatRuntime

# Transcripts in the shape the net actually sees: the main reply carried no
# tag. Clarification, refusal and ordinary dialogue must remain inert; explicit
# promises must reconstruct the exact structured intent, including taskless
# project focus rather than only file mutation.
CASES: list[tuple[str, str, str, str | None, str]] = [
    (
        "asking-which-project",
        "请在 scratch 仓创建 amend.txt，内容恰好为一行 one。",
        "ふむ、「scratch 仓」がどのプロジェクトを指すのか特定できないのよ。"
        "作業対象のディレクトリを教えてくれる？",
        None,
        "",
    ),
    (
        "declining-needs-detail",
        "把刚才那个文件里的颜色改成 green。",
        "ちょっと待って、どのファイルのことかしら？直前に触ったものが複数あるの。",
        None,
        "",
    ),
    (
        "promised-without-acting",
        "请在 scratch 仓创建 theme.txt，写入 color=blue。",
        "了解、すぐ作るわね。ちょっと待って。",
        "execute",
        "",
    ),
    (
        "promised-anaphoric",
        "把刚才那个文件里的颜色改成 green。",
        "了解、theme.txt の color を green に直すわね。",
        "amend",
        "",
    ),
    (
        "ordinary-conversation",
        "今天想轻松聊两句。",
        "もちろん。今日は何の話をしましょうか。",
        None,
        "",
    ),
    (
        "promised-project-switch",
        "切换到 amadeus 项目。",
        "了解、amadeus プロジェクトに切り替えるわね。今切り替えるから。",
        "focus",
        "",
    ),
    (
        "promised-browser-close",
        "关闭当前浏览器操作，回到普通聊天。",
        "了解、現在のブラウザ操作を閉じて通常の会話に戻るわ。",
        "",
        "close",
    ),
]


class _RoutingCoordinator:
    @staticmethod
    def workspace_routing_context(*, limit: int) -> dict:
        return {
            "focus": {"mode": "auto"},
            "candidates": [
                {
                    "projectId": "project_amadeus",
                    "projectName": "amadeus",
                }
            ],
        }


class _BranchCoordinator:
    active = False

    @classmethod
    def active_branch_for_session(cls, _session_id: str):
        if not cls.active:
            return None
        return SimpleNamespace(
            status="active",
            goal="Browse Bilibili",
            pending_goal="",
            title="哔哩哔哩首页",
            url="https://www.bilibili.com",
        )


async def main() -> int:
    repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    print(f"resend probe: {repeats} repeat(s) per case\n")
    rows: list[tuple[str, str | None, str | None, str, str, float]] = []
    with (
        patch(
            "server.work_ledger_coordinator.get_work_ledger_coordinator",
            return_value=_RoutingCoordinator(),
        ),
        patch(
            "server.interaction_branch.get_interaction_branch_coordinator",
            return_value=_BranchCoordinator(),
        ),
    ):
        for name, question, reply, expected_intent, expected_branch in CASES:
            _BranchCoordinator.active = bool(expected_branch)
            for index in range(1, repeats + 1):
                started = time.monotonic()
                try:
                    actions = await ChatRuntime._request_delegate_resend(
                        question,
                        reply,
                        session_id="probe-delegate-resend",
                    )
                    error = ""
                except Exception as exc:  # noqa: BLE001 - the probe reports, never raises
                    actions, error = [], f"{type(exc).__name__}: {exc}"
                elapsed = time.monotonic() - started
                attrs = actions[0].get("attrs") or {} if actions else {}
                actual_intent = (
                    str(attrs.get("intent") or "").strip().lower()
                    if actions
                    else None
                )
                actual_branch = str(attrs.get("branch") or "").strip().lower()
                rows.append(
                    (
                        name,
                        expected_intent,
                        actual_intent,
                        expected_branch,
                        actual_branch,
                        elapsed,
                    )
                )
                verdict = (
                    "ok "
                    if actual_intent == expected_intent
                    and actual_branch == expected_branch
                    else "MISS"
                )
                task = str(attrs.get("task") or "")[:56]
                print(
                    f"  {verdict} {name:26s} #{index} "
                    f"want={str(expected_intent):7s} got={str(actual_intent):7s} "
                    f"branch={actual_branch or '-':5s} "
                    f"{elapsed:5.2f}s {error or task}"
                )

    print("\n-- per case --")
    for name, _q, _r, expected_intent, expected_branch in CASES:
        got = [row for row in rows if row[0] == name]
        agreed = sum(
            1
            for row in got
            if row[2] == row[1] and row[4] == row[3]
        )
        print(
            f"  {name:26s} want={str(expected_intent):7s} "
            f"agreed={agreed}/{len(got)}"
        )

    latencies = [row[5] for row in rows]
    print("\n-- cost of asking --")
    print(
        f"  n={len(latencies)}  median={statistics.median(latencies):.2f}s  "
        f"max={max(latencies):.2f}s"
    )
    print(
        "  This sits between sentences dispatched and the turn's last-sentence "
        "mark, so it is added to turn completion, not to first speech."
    )
    wrong = sum(
        1 for row in rows if row[2] != row[1] or row[4] != row[3]
    )
    return 0 if wrong == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
