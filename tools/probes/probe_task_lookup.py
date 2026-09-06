"""Can a side channel resolve which past task the user meant, and answer about it?

The roster is the only place the main chat learns about past work, and it is
capped at five rows sharing one budget with the persona — with candidate rows
off by default it carries no task identifiers at all. That ceiling is the
lookup gap. Two things have to hold before designing around it, and neither is
answerable from the code:

  * **Choosing.** Candidate lists were tried once inside the always-on roster
    and abandoned: the model used an id 0 times out of 18, and an A/B on
    2026-08-01 reproduced that at 0/10. But that asked a character mid-scene to
    quote an identifier out of background context. A side channel asks one
    question and nothing else, and picking from a list is classification, which
    is the shape that has worked (intent) rather than transcription, which is
    the shape that has not (workspace_ref, 0/28).

  * **Answering.** `intent="report"` only refuses to start work; nothing reads
    the ledger, despite the log line saying otherwise. So a question about a
    past task is answered from whatever the roster happened to carry. Worse,
    the 28/28 `readonly_ref` result only ever asserted that no work was
    created — never that the answer was true.

    .venv\\Scripts\\python.exe -X utf8 tools/probes/probe_task_lookup.py [repeats]

Answers are printed, not just scored: the checks below are substring
approximations and the prose is the real evidence.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Deliberately more candidates than the roster's five, and deliberately
# similar: two touch theme files, two are research. A window this size is the
# situation the roster cannot represent at all.
CANDIDATES = [
    ("work_a1", "创建 theme.txt 并写入 color=blue", "succeeded, accepted"),
    ("work_b2", "把 theme.txt 的 color 改成 green", "succeeded, needs review"),
    ("work_c3", "创建 notes.md 记录会议纪要", "succeeded, accepted"),
    ("work_d4", "调研 Rust 的 async 运行时选型", "running"),
    ("work_e5", "调研 SQLite WAL 模式的取舍", "succeeded, accepted"),
    ("work_f6", "重构 chat_runtime 的标签解析", "failed"),
    ("work_g7", "给 amend.txt 追加一行 two", "succeeded, needs review"),
    ("work_h8", "修复壁纸键盘音的并发释放", "succeeded, accepted"),
]

PICK_CASES = [
    ("把颜色改绿那个任务后来怎么样了？", "work_b2"),
    ("之前调研 async 运行时的那个还在跑吗？", "work_d4"),
    ("重构标签解析那个是不是失败了？", "work_f6"),
    ("会议纪要那个记完了吗？", "work_c3"),
]

# Required markers of the true state, not forbidden words. Forbidding
# completion vocabulary flagged four correct answers on the first run, because
# every one of them used it under negation -- "まだ完了してないわ" is the right
# answer and contains the banned token. A check that goes red on correct
# behaviour is the same defect as one that never goes red, inverted.
RUNNING_MARKERS = ("実行中", "まだ", "running", "進行")
FAILED_MARKERS = ("失敗", "エラー", "error", "できてない", "できていない", "だめ")

ANSWER_CASES = [
    (
        "running",
        "之前调研 async 运行时的那个还在跑吗？",
        "任务：调研 Rust 的 async 运行时选型；执行状态：running；完成度：unknown；需要关注：无",
        RUNNING_MARKERS,
    ),
    (
        "conflict",
        "刚才那个创建文件的任务成了吗？",
        "任务：创建 out/result.txt；执行状态：succeeded；完成度：partial；"
        "需要关注：conflict；证据：Bash/Write/PowerShell 三个工具调用均失败，git 无改动",
        FAILED_MARKERS,
    ),
    (
        "failed",
        "重构标签解析那个怎么样了？",
        "任务：重构 chat_runtime 的标签解析；执行状态：failed；完成度：incomplete；需要关注：error",
        FAILED_MARKERS,
    ),
]


# --- C. does a small hot list stop the model from guessing? -----------------
# The design question this settles: keep a few recent tasks injected so the
# common question ("刚才那个好了吗") is answered without a round trip, and fall
# back to retrieval otherwise. That only works if the model reliably notices
# the referenced task is NOT in the list instead of answering about the
# closest one, which would be confidently wrong -- worse than a second of
# latency. It is a check against visible content rather than a question about
# its own knowledge, which is the shape that worked in part A.
HOT = [
    ("work_g7", "给 amend.txt 追加一行 two", "succeeded, needs review"),
    ("work_f6", "重构 chat_runtime 的标签解析", "failed"),
    ("work_d4", "调研 Rust 的 async 运行时选型", "running"),
    ("work_h8", "修复壁纸键盘音的并发释放", "succeeded, accepted"),
]

# (name, question, expect_lookup)
HOT_CASES = [
    ("in-list-failed", "标签解析那个是不是失败了？", False),
    ("in-list-running", "调研 async 那个还在跑吗？", False),
    ("out-theme", "把 theme.txt 颜色改成绿色那个后来怎么样了？", True),
    ("out-notes", "会议纪要那个记完了吗？", True),
    # The trap: a research task IS in the list, but not this one.
    ("out-lure-research", "调研 SQLite WAL 模式那个结论是什么？", True),
]


def _hot_prompt(question: str) -> str:
    rows = "\n".join(f"- {wid} | {title} | {state}" for wid, title, state in HOT)
    return (
        f"[最近的任务]\n{rows}\n\n"
        f"[用户刚才问]\n{question}\n\n"
        "[SYSTEM] 用户问的任务如果就在上面这几条里，用一句话依据它的状态回答。"
        "如果不在上面这几条里，不要猜、不要用最像的那条代替，只输出 LOOKUP。"
    )


def _pick_prompt(question: str) -> str:
    rows = "\n".join(f"- {wid} | {title} | {state}" for wid, title, state in CANDIDATES)
    return (
        f"[用户刚才说]\n{question}\n\n"
        f"[候选任务]\n{rows}\n\n"
        "[SYSTEM] 用户指的是上面哪一个任务？只输出那一行的 work_item_id，"
        "不要输出任何其他文字。如果无法确定，只输出 UNSURE。"
    )


def _answer_prompt(question: str, facts: str) -> str:
    return (
        f"[用户刚才问]\n{question}\n\n"
        f"[账本事实]\n{facts}\n\n"
        "[SYSTEM] 只依据上面的账本事实回答用户，用你平时的口吻，一到两句。"
        "不得声称事实里没有写的进展；事实说未完成或有冲突时，不要说成已完成。"
    )


async def main() -> int:
    repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    from llm.client import remote_llm_query
    from llm.prompts import get_system_prompt

    async def ask(prompt: str, system: str | None) -> tuple[str, float]:
        started = time.monotonic()
        text = await asyncio.to_thread(remote_llm_query, prompt, system)
        return str(text or "").strip(), time.monotonic() - started

    latencies: list[float] = []
    print(f"lookup probe: {repeats} repeat(s) per case, {len(CANDIDATES)} candidates\n")

    print("-- A. choosing which task the user meant --")
    pick_ok = pick_total = 0
    for question, expected in PICK_CASES:
        for index in range(1, repeats + 1):
            reply, elapsed = await ask(_pick_prompt(question), None)
            latencies.append(elapsed)
            hit = expected in reply
            pick_ok += int(hit)
            pick_total += 1
            print(
                f"  {'ok ' if hit else 'MISS'} #{index} {elapsed:4.2f}s "
                f"want={expected} got={reply[:40]!r}  <- {question[:24]}"
            )

    print("\n-- B. answering from ledger facts --")
    answer_ok = answer_total = 0
    persona = get_system_prompt("with_delegate")
    for name, question, facts, required in ANSWER_CASES:
        for index in range(1, repeats + 1):
            reply, elapsed = await ask(_answer_prompt(question, facts), persona)
            latencies.append(elapsed)
            found = [word for word in required if word in reply]
            started_work = "[DELEGATE" in reply.upper()
            ok = bool(found) and not started_work
            answer_ok += int(ok)
            answer_total += 1
            print(f"  {'ok ' if ok else 'MISS'} {name:8s} #{index} {elapsed:4.2f}s")
            print(f"       {reply[:110]!r}")
            if not found:
                print(f"       none of the state markers {required} appeared")
            if started_work:
                print("       reached for a delegate while answering a question")

    print("\n-- C. hot list: answer when present, defer when absent --")
    hot_ok = hot_total = 0
    for name, question, expect_lookup in HOT_CASES:
        for index in range(1, repeats + 1):
            reply, elapsed = await ask(_hot_prompt(question), persona)
            latencies.append(elapsed)
            deferred = "LOOKUP" in reply.upper()
            ok = deferred == expect_lookup
            hot_ok += int(ok)
            hot_total += 1
            print(
                f"  {'ok ' if ok else 'MISS'} {name:18s} #{index} {elapsed:4.2f}s "
                f"want_lookup={str(expect_lookup):5s} got={str(deferred):5s}"
            )
            if not ok:
                print(f"       {reply[:120]!r}")

    print("\n-- summary --")
    print(f"  choosing : {pick_ok}/{pick_total}")
    print(f"  answering: {answer_ok}/{answer_total} (true state, started nothing)")
    print(f"  hot list : {hot_ok}/{hot_total} (answered when present, deferred when absent)")
    print(
        f"  latency  : median={statistics.median(latencies):.2f}s "
        f"max={max(latencies):.2f}s"
    )
    print(
        "\n  The prose above is the real evidence; the checks are substring\n"
        "  approximations and can miss a wrong answer that avoids those words."
    )
    return (
        0
        if pick_ok == pick_total
        and answer_ok == answer_total
        and hot_ok == hot_total
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
