"""Does the *assembled* ladder work, or only its parts?

`probe_task_lookup.py` measured the pieces against hand-written prompts:
choosing 12/12, answering 9/9. Those numbers belong to the pieces, not to
`server/task_lookup.py`, and the composition changed three things that the
model actually sees:

  * **Identifiers got 4x longer.** The old probe listed `work_a1`; production
    ids are `work_` + 32 hex. This codebase's strongest negative result is
    that the model will not transcribe an identifier (`workspace_ref`, 0/28),
    and a pick is only useful if the id comes back intact. Section B isolates
    exactly this by running the same questions over the same tasks with only
    the id style changed.

  * **Candidate rows are now derived, not written.** `_candidate_line` renders
    `_status_phrase` off real ledger fields ("succeeded, needs review") rather
    than a hand-chosen string.

  * **Facts are now derived too.** `render_task_facts` adds produced files and
    a rationale line the old probe never showed the model, and production
    speaks them through the `base` prompt -- the variant that made "[DELEGA"
    stop leaking into a read-only answer.

A prefilter also sits in front of the pick now, and it is measurably lossy
(3/4 recall on these same questions, 2026-08-02). So section A runs the whole
`resolve()` ladder and reports *where* each question died, not just whether it
lived.

    .venv_cu124\\Scripts\\python.exe -X utf8 tools/probes/probe_task_lookup_ladder.py [repeats]

No backend, no Codex, no ledger: a fake coordinator serves the rows, and only
the model is real. Answers are printed, not just scored -- the checks are
substring approximations and the prose is the real evidence.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# (title, files, execution, completion, attention). Deliberately similar --
# two touch theme files, two are research -- because a candidate set that is
# easy to tell apart measures nothing.
TASKS = [
    ("创建 theme.txt 并写入 color=blue", ["theme.txt"], "succeeded", "complete", "none"),
    ("把 theme.txt 的 color 改成 green", ["theme.txt"], "succeeded", "partial", "review"),
    ("创建 notes.md 记录会议纪要", ["notes.md"], "succeeded", "complete", "none"),
    ("调研 Rust 的 async 运行时选型", [], "running", "unknown", "none"),
    ("调研 SQLite WAL 模式的取舍", ["wal.md"], "succeeded", "complete", "none"),
    ("重构 chat_runtime 的标签解析", ["chat_runtime.py"], "failed", "incomplete", "error"),
    ("给 amend.txt 追加一行 two", ["amend.txt"], "succeeded", "partial", "review"),
    ("修复壁纸键盘音的并发释放", ["wallpaper.js"], "succeeded", "complete", "none"),
]

SHORT_IDS = [f"work_{chr(ord('a') + i)}{i + 1}" for i in range(len(TASKS))]
REAL_IDS = [f"work_{uuid.uuid4().hex}" for _ in TASKS]

# Filler for the candidate-count sweep. Plausible project work, deliberately
# sharing no vocabulary with the questions asked (no async, no 标签解析, no
# 会议纪要, no theme/color) so a larger list adds noise rather than rivals --
# which is what a longer conversation actually looks like.
_FILLER_VERBS = ["修复", "重构", "调研", "导出", "清理", "补测", "迁移", "优化"]
_FILLER_SUBJECTS = [
    "壁纸缓存的失效策略", "ASR 端点的静音阈值", "TTS 队列的排空顺序",
    "Electron 构建的产物体积", "权限卡片的驳回文案", "账本迁移的幂等键",
    "热更新的资产版本号", "麦克风设备的打分", "唤醒词的误触率",
    "字幕渲染的换行", "进度条的单调钳制", "会话历史的裁剪窗口",
]


def _padded_rows(target: dict, size: int, *, seed: int) -> list[dict]:
    """``size`` candidates containing ``target``, at a rotating position.

    Position rotates because a target parked at one end measures placement as
    much as discrimination.
    """

    filler: list[dict] = []
    for index in range(max(0, size - 1)):
        verb = _FILLER_VERBS[index % len(_FILLER_VERBS)]
        subject = _FILLER_SUBJECTS[(index // len(_FILLER_VERBS)) % len(_FILLER_SUBJECTS)]
        filler.append(
            {
                "work_item_id": f"work_{uuid.uuid4().hex}",
                "title": f"{verb}{subject}（{index}）",
                "files": [f"pad_{index}.md"],
                "execution": "succeeded",
                "completion": "complete",
                "attention": "none",
            }
        )
    position = (seed * 7 + 3) % max(1, size)
    return [*filler[:position], target, *filler[position:]]


def _rows(ids: list[str]) -> list[dict]:
    rows = []
    for index, (title, files, execution, completion, attention) in enumerate(TASKS):
        rows.append(
            {
                "work_item_id": ids[index],
                "title": title,
                "files": list(files),
                "execution": execution,
                "completion": completion,
                "attention": attention,
                "state": "open",
                "relation": "history",
                # Newest last in TASKS order, so recency puts the tail first.
                "updated_at": f"2026-08-02T{index:02d}:00:00Z",
                "completion_rationale": (
                    "三个工具调用均失败，git 无改动" if attention == "error" else ""
                ),
            }
        )
    return rows


class FakeCoordinator:
    """Serves conversation rows the way the real coordinator would."""

    def __init__(self, rows: list[dict], *, complete: bool = True) -> None:
        self.rows = rows
        self.complete = complete
        self.index_calls = 0

    def conversation_work_item_index(self, _session_id: str, **_kwargs) -> list[dict]:
        self.index_calls += 1
        return list(reversed(self.rows))

    def conversation_work_items(self, _session_id: str, *, limit: int = 4) -> list[dict]:
        return list(reversed(self.rows))[:limit]

    def conversation_work_items_for_resolution(
        self, _session_id: str, *, limit: int = 60
    ) -> dict:
        rows = list(reversed(self.rows))[:limit]
        return {"items": rows, "complete": self.complete and len(rows) < limit}

    def conversation_work_items_by_file(
        self, _session_id: str, name: str, *, limit: int = 32
    ) -> list[dict]:
        wanted = str(name).lower()
        return [
            row
            for row in self.rows
            if wanted in {str(f).lower() for f in row["files"]}
        ][:limit]


# (question, expected index into TASKS, needs_fallback)
# The first four are verbatim from probe_task_lookup.py, so the assembled
# numbers are directly comparable with the 12/12 the pieces scored.
LADDER_CASES = [
    ("把颜色改绿那个任务后来怎么样了？", 1, True),
    ("之前调研 async 运行时的那个还在跑吗？", 3, False),
    ("重构标签解析那个是不是失败了？", 5, False),
    ("会议纪要那个记完了吗？", 2, False),
    # A named file must never reach the model at all (rung 1).
    ("给 amend.txt 再加一行", 6, False),
]

# Ordinary conversation: the ladder must stay silent and spend nothing.
QUIET_CASES = [
    "今天天気はどう？",
    "紅莉栖、おはよう",
    "把音量调大一点",
]

# Markers of the true state, in every language the reply might use. The first
# run of this probe scored a MISS on an answer that was correct in prose --
# "那个重构还没搞定，三个工具调用全挂了" -- purely because the list held only
# Japanese and English. A check that goes red on correct behaviour is the same
# defect as one that never goes red, inverted; the original probe was bitten by
# exactly this and said so. (The mixed zh/ja output is an artifact of calling
# the base prompt bare: production's hybrid path appends a language lock.)
RUNNING_MARKERS = (
    "実行中", "まだ", "running", "進行", "動いて",
    "还在", "在跑", "运行中", "没停",
)
FAILED_MARKERS = (
    "失敗", "エラー", "error", "できてない", "できていない", "だめ", "まだ",
    "失败", "没搞定", "还没", "未完成", "挂了", "错误",
)
PARTIAL_MARKERS = (
    "まだ", "レビュー", "partial", "確認", "一部", "中途半端",
    "还没", "review", "部分", "没完全",
)

# Facts rendered by render_task_facts, answered through the production
# follow-up on the base prompt.
ANSWER_CASES = [
    ("running", "之前调研 async 运行时的那个还在跑吗？", 3, RUNNING_MARKERS),
    ("failed", "重构标签解析那个怎么样了？", 5, FAILED_MARKERS),
    ("conflict", "刚才那个改 theme 的任务成了吗？", 1, PARTIAL_MARKERS),
]

FOLLOW_UP = (
    "[SYSTEM] 用户刚才问的是既有任务的状态，上方 [RESULT] 是工作账本里的真实状态。\n"
    "只依据这些事实回答用户，用你平时的口吻，一到两句。\n"
    "不得声称事实里没有写的进展；事实说未完成或有冲突时，不要说成已完成。"
)


def _is_infrastructure_failure(text: str) -> bool:
    """A dead endpoint is not evidence about the model (lesson from 2026-07-31).

    A 402 once got recorded as a routing result and misled a whole analysis.
    """

    lowered = str(text or "").lower()
    return any(
        marker in lowered
        for marker in ("insufficient balance", "402", "api error", "connection", "timeout", "unauthorized")
    ) and len(lowered) < 400


async def main() -> int:
    repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    # Second argument narrows to a section, e.g. "F", so re-measuring one
    # question does not pay for the whole battery.
    wanted = set((sys.argv[2] if len(sys.argv) > 2 else "ABCEFD").upper())
    from llm.client import remote_llm_query
    from llm.prompts import get_system_prompt
    from server import task_lookup

    latencies: list[float] = []
    calls = {"n": 0}
    real_query = remote_llm_query

    def counted(prompt: str, system: str | None = None) -> str:
        calls["n"] += 1
        reply = real_query(prompt, system)
        # Kept so a failure can show what actually came back. "Nothing was
        # parsed" has three very different causes -- the model declined, the
        # model answered in a shape the parser missed, or the endpoint hiccuped
        # -- and only one of them is a finding.
        calls["last"] = str(reply or "")
        return reply

    real_rows = _rows(REAL_IDS)
    coordinator = FakeCoordinator(real_rows)

    print(
        f"ladder probe: {repeats} repeat(s) per case, {len(TASKS)} candidates, "
        f"real ids ({len(REAL_IDS[0])} chars)\n"
    )

    import config.settings as settings

    with (
        patch.object(settings, "TASK_LOOKUP_ENABLED", True),
        patch(
            "server.work_ledger_coordinator.get_work_ledger_coordinator",
            return_value=coordinator,
        ),
        patch("llm.client.remote_llm_query", counted),
    ):
        ladder_ok = ladder_total = 0
        died: dict[str, int] = {}
        if "A" in wanted:
            print("-- A. the assembled ladder: resolve() end to end --")
        for question, expected_index, needs_fallback in LADDER_CASES if "A" in wanted else []:
            expected = real_rows[expected_index]["work_item_id"]
            for index in range(1, repeats + 1):
                before = calls["n"]
                started = time.monotonic()
                result = await task_lookup.resolve(
                    "s-probe",
                    question,
                    consumer="probe",
                    recency_fallback=needs_fallback,
                )
                elapsed = time.monotonic() - started
                latencies.append(elapsed)
                row = result.get("row") or {}
                hit = str(row.get("work_item_id") or "") == expected
                ladder_ok += hit
                ladder_total += 1
                if not hit:
                    died[result.get("reason", "?")] = died.get(result.get("reason", "?"), 0) + 1
                print(
                    f"  {'ok  ' if hit else 'MISS'} #{index} {elapsed:4.2f}s "
                    f"level={result.get('level')} reason={result.get('reason'):12s} "
                    f"model_calls={calls['n'] - before}  <- {question[:22]}"
                )
                if not hit:
                    print(f"       wanted {TASKS[expected_index][0]!r}")
                    print(f"       got    {row.get('title') or '(nothing)'!r}")

        # The one thing this codebase has repeatedly failed at is asking the
        # model to reproduce an identifier. Same tasks, same questions, only
        # the id style differs.
        id_scores: dict[str, int] = {}
        id_totals: dict[str, int] = {}
        id_blank: dict[str, int] = {}
        if "B" in wanted:
            print("\n-- B. does a 37-char id survive the round trip? --")
        for style, ids in (("short", SHORT_IDS), ("real", REAL_IDS)) if "B" in wanted else []:
            rows = _rows(ids)
            for question, expected_index, _fb in LADDER_CASES[1:4]:
                expected = ids[expected_index]
                for _index in range(repeats):
                    started = time.monotonic()
                    picked = await task_lookup._side_channel_pick(question, rows)
                    elapsed = time.monotonic() - started
                    latencies.append(elapsed)
                    id_totals[style] = id_totals.get(style, 0) + 1
                    if picked == expected:
                        id_scores[style] = id_scores.get(style, 0) + 1
                    elif not picked:
                        # No id parsed at all: either the model declined or the
                        # call did not come back cleanly. Kept separate from a
                        # wrong pick, because a stalled endpoint is not
                        # evidence about the model.
                        id_blank[style] = id_blank.get(style, 0) + 1
                        print(f"    blank  {style:5s} {elapsed:5.2f}s <- {question[:20]}")
                    else:
                        wrong = next(
                            (r["title"] for r in rows if r["work_item_id"] == picked),
                            f"unknown {picked!r}",
                        )
                        print(f"    WRONG  {style:5s} {elapsed:5.2f}s picked {wrong}")
            print(
                f"  {style:5s} ids: {id_scores.get(style, 0)}/{id_totals.get(style, 0)} "
                f"correct, {id_blank.get(style, 0)} returned nothing"
            )

        answer_ok = answer_total = 0
        base = get_system_prompt("base")
        if "C" in wanted:
            print("\n-- C. answering from render_task_facts, on the base prompt --")
        for name, question, task_index, required in ANSWER_CASES if "C" in wanted else []:
            facts = task_lookup.render_task_facts(real_rows[task_index])
            prompt = (
                f"[RESULT] 任务状态（来自工作账本）\n{facts}\n\n"
                f"[用户刚才问]\n{question}\n\n{FOLLOW_UP}"
            )
            for index in range(1, repeats + 1):
                started = time.monotonic()
                reply = str(
                    await asyncio.to_thread(counted, prompt, base) or ""
                ).strip()
                elapsed = time.monotonic() - started
                latencies.append(elapsed)
                if _is_infrastructure_failure(reply):
                    print(f"  skip {name:8s} #{index} infrastructure: {reply[:60]!r}")
                    continue
                found = [word for word in required if word in reply]
                leaked = "[DELEGA" in reply.upper()
                ok = bool(found) and not leaked
                answer_ok += ok
                answer_total += 1
                print(f"  {'ok  ' if ok else 'MISS'} {name:8s} #{index} {elapsed:4.2f}s")
                print(f"       {reply[:110]!r}")
                if not found:
                    print(f"       none of the state markers {required} appeared")
                if leaked:
                    print("       R1 VIOLATION: delegate vocabulary in a read-only answer")

        # Measured 2026-08-02, and the reason the ladder is shaped the way it
        # is: hand the pick a set without its answer and it names the nearest
        # row almost every time (1 of 9 declined), which the host would then
        # present as settled ledger fact. Section 2.1 of the work order saw
        # the same thing in the roster (3/9). The prompt's "output UNSURE"
        # clause is still asking the model to notice an absence, and it mostly
        # does not. E1 keeps that measurement alive; E2 checks the host now
        # refuses to create the situation.
        absent_declined = absent_total = 0
        guard_ok = True
        if "E" in wanted:
            print("\n-- E. the pick does not decline, so it must never be under-served --")
        for question, expected_index, _fb in (LADDER_CASES[1:4] if "E" in wanted else []):
            truncated = [
                row
                for row in real_rows
                if row["work_item_id"] != real_rows[expected_index]["work_item_id"]
            ]
            for index in range(1, repeats + 1):
                started = time.monotonic()
                picked = await task_lookup._side_channel_pick(question, truncated)
                latencies.append(time.monotonic() - started)
                absent_total += 1
                declined = picked == ""
                absent_declined += declined
                shown = (
                    "declined"
                    if declined
                    else next(
                        (r["title"] for r in truncated if r["work_item_id"] == picked),
                        f"unknown id {picked!r}",
                    )
                )
                print(f"  E1 {'declined' if declined else 'GUESSED '} #{index} {shown}")

        if "E" in wanted:
            partial = FakeCoordinator(real_rows, complete=False)
            guarded = {"reached": False}

            async def must_not_run(*_args, **_kwargs) -> str:
                guarded["reached"] = True
                return real_rows[0]["work_item_id"]

            with (
                patch(
                    "server.work_ledger_coordinator.get_work_ledger_coordinator",
                    return_value=partial,
                ),
                patch.object(task_lookup, "_side_channel_pick", must_not_run),
            ):
                blocked = await task_lookup.resolve(
                    "s-probe",
                    "把颜色改绿那个任务后来怎么样了？",
                    consumer="probe",
                    recency_fallback=True,
                )
            guard_ok = not guarded["reached"] and blocked.get("row") is None
            print(
                f"  E2 {'ok  ' if guard_ok else 'LEAK'} roster not provably whole -> "
                f"reason={blocked.get('reason')} pick_reached={guarded['reached']}"
            )

        if "F" in wanted:
            print("\n-- F. accuracy at the size the pick is actually shown --")
        # The 12/12 that justified this design was measured over eight
        # candidates. Closing the absent-answer hole raised the operating
        # point to a whole conversation (_PICK_ROW_BUDGET rows), and nothing
        # had been measured there -- a variable moved without re-reading the
        # instrument. The failure this looks for is silent: with the answer
        # guaranteed present, a wrong pick now means discrimination degraded
        # with list length, and the user cannot tell the difference.
        sweep_sizes = [8, 20, 40, task_lookup._PICK_ROW_BUDGET]
        sweep: dict[int, tuple[int, int, float]] = {}
        for size in (sweep_sizes if "F" in wanted else []):
            hits = total = 0
            spent: list[float] = []
            for question, expected_index, _fb in LADDER_CASES[1:4]:
                target = real_rows[expected_index]
                for index in range(repeats):
                    padded = _padded_rows(target, size, seed=index)
                    started = time.monotonic()
                    picked = await task_lookup._side_channel_pick(question, padded)
                    elapsed = time.monotonic() - started
                    latencies.append(elapsed)
                    spent.append(elapsed)
                    total += 1
                    if picked == target["work_item_id"]:
                        hits += 1
                    else:
                        wrong = next(
                            (r["title"] for r in padded if r["work_item_id"] == picked),
                            "nothing parsed",
                        )
                        print(
                            f"    WRONG at n={size:3d}: {wrong}  <- {question[:20]}"
                        )
                        print(f"           model said: {str(calls.get('last'))[:160]!r}")
            sweep[size] = (hits, total, statistics.median(spent))
            print(
                f"  n={size:3d}: {hits}/{total} correct, "
                f"median {statistics.median(spent):.2f}s"
            )

        quiet_ok = 0
        if "D" in wanted:
            print("\n-- D. ordinary conversation must cost nothing --")
        for utterance in (QUIET_CASES if "D" in wanted else []):
            before = calls["n"]
            result = await task_lookup.resolve(
                "s-probe", utterance, consumer="probe"
            )
            spent = calls["n"] - before
            silent = spent == 0 and result.get("row") is None
            quiet_ok += silent
            print(
                f"  {'ok  ' if silent else 'COST'} model_calls={spent} "
                f"reason={result.get('reason')}  <- {utterance}"
            )

    print("\n-- summary --")
    print(f"  ladder   : {ladder_ok}/{ladder_total} resolved to the right task")
    if died:
        print(f"             misses by reason: {died}")
    print(
        f"  id style : short {id_scores.get('short', 0)}/{id_totals.get('short', 0)}"
        f"  vs real {id_scores.get('real', 0)}/{id_totals.get('real', 0)}"
    )
    print(f"  answering: {answer_ok}/{answer_total} (true state, no delegate vocabulary)")
    print(
        f"  absent   : {absent_declined}/{absent_total} declined when the answer was "
        f"missing -- the pick cannot detect absence, so the host must not create it"
    )
    print(f"  guard    : {'ok' if guard_ok else 'LEAK'} a partial roster never reaches the pick")
    print("  by size  : " + "  ".join(
        f"n={size}:{hits}/{total}({median:.1f}s)"
        for size, (hits, total, median) in sorted(sweep.items())
    ))
    print(f"  quiet    : {quiet_ok}/{len(QUIET_CASES)} ordinary turns cost zero calls")
    print(f"  model calls total: {calls['n']}")
    if latencies:
        print(
            f"  latency  : median={statistics.median(latencies):.2f}s "
            f"max={max(latencies):.2f}s"
        )
    print(
        "\n  The prose above is the real evidence; the checks are substring\n"
        "  approximations and can miss a wrong answer that avoids those words."
    )
    # absent_declined is deliberately NOT a pass condition: it records a model
    # behaviour the design accommodates rather than a contract it must meet.
    return (
        0
        if ladder_ok == ladder_total
        and answer_ok == answer_total
        and guard_ok
        and quiet_ok == (len(QUIET_CASES) if "D" in wanted else 0)
        and id_scores.get("real", 0) == id_totals.get("real", 0)
        and all(hits == total for hits, total, _median in sweep.values())
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
