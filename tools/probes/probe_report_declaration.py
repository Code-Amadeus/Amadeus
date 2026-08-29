"""How often does a status question actually get declared as a report?

Task lookup answers a status question from the ledger, correctly, every time
it is invoked -- but it is only invoked when the model declares
`intent="report"`. On 2026-08-02 a real run declared it for neither of two
status questions and the character answered from conversational memory
instead: "created properly, red green and blue each on their own line, no
problems", while the ledger recorded partial completeness needing review and
no file had been written at all. Confidently wrong, about work the user cares
about.

Across four testbed runs the declaration held 6 times in 8, with prompt and
code byte-identical between them. Eight samples of two utterances is not a
rate, and the testbed is an expensive way to collect more: the decision is
made in a single chat turn, so it can be measured directly.

Three things could be behind it, and they point at different fixes:

  * **Phrasing.** If elliptical follow-ups ("那个记会议纪要的呢？") fail where
    explicit ones succeed, the gap is in how the contract is worded.
  * **Self-example.** 2026-07-31 established that the model's own previous
    turns outweigh the system prompt -- a history that showed it answering
    without a tag taught it not to emit one. If a prior report declaration in
    history lifts the rate, the first declaration of a conversation is what
    needs protecting.
  * **Depth.** If a long history hurts, the contract is being diluted.

    .venv_cu124\\Scripts\\python.exe -X utf8 tools/probes/probe_report_declaration.py [repeats]

Outcomes are three-way, not pass/fail. Emitting no tag is the measured
failure; emitting `intent="execute"` would be far worse, because a question
would start work -- the chat invariant this whole vocabulary exists to hold.
"""

from __future__ import annotations

import asyncio
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

SEED_TASKS = [
    ("请在 scratch 仓创建 colors.txt，依次写入 red、green、blue，每行一个。",
     "了解したわ。", "colors.txt を作成し red/green/blue を書き込む"),
    ("再建一个 notes.md，写上今天的会议纪要标题。",
     "はいはい、作るわよ。", "notes.md を作成し会議のタイトルを書く"),
    ("再建一个 config.ini，写一行 timeout=30。",
     "それくらい簡単よ。", "config.ini に timeout=30 を書く"),
    ("再建一个 readme.txt，写上项目标题。",
     "わかった。", "readme.txt にプロジェクト名を書く"),
    ("再建一个 data.csv，写两行示例数据。",
     "ん、やっとくわ。", "data.csv にサンプル行を二つ書く"),
    ("再建一个 log.txt，写一行启动记录。",
     "最後ね、了解。", "log.txt に起動記録を一行書く"),
]

# Real shapes of the same request. The first two are verbatim from testbed
# scenarios; 3 and 6 are elliptical, which is how people actually follow up.
QUESTIONS = [
    ("explicit-oldest", "最开始那个写颜色的任务后来怎么样了？只汇报状态，不要重试。"),
    ("explicit-recent", "刚才那个文件任务进展如何？只汇报状态。"),
    ("elliptical", "那个记会议纪要的呢？也只说状态。"),
    ("named-file", "colors.txt 那个好了吗？"),
    ("plural", "之前那几个任务都完成了吗？"),
    ("casual", "notes.md 弄完了没？"),
]

# The control. Telling the model to tag questions that need no work is one
# short step from tagging everything, and a spurious report sends the host
# looking up a task nobody asked about -- which ends in the character asking
# "which task did you mean?" out of nowhere. These must stay untagged. The
# last two are the trap: they mention files and work, but are not about any
# task this conversation owns.
CONTROL_CHAT = [
    ("weather", "你喜欢晴天还是雨天？"),
    ("trivia", "新西兰的首都是哪里？"),
    ("banter", "紅莉栖、今日も一日おつかれ。"),
    ("general-file-talk", "一般来说 csv 和 json 哪个更适合存配置？"),
    ("hypothetical", "如果要写个脚本备份文件，你会用什么语言？"),
]

# A prior exchange in which the model already declared a report. History is
# built the way production builds it: the assistant entry keeps the raw tag,
# because stripping it is what taught the model not to emit one (2026-07-31).
PRIOR_REPORT = (
    "config.ini 那个怎么样了？只说状态。",
    "ちょっと待って、状態を確認するわ。"
    '[DELEGATE provider="codex" intent="report" task="config.ini の作成タスクの状態を報告する"]',
)

_DELEGATE_RE = re.compile(r"\[DELEGATE\b[^\]]*\]", re.IGNORECASE)
_INTENT_RE = re.compile(r'intent\s*=\s*"([a-z]+)"', re.IGNORECASE)


def _classify(reply: str) -> str:
    """report / execute / other-tag / none — the three ways this can go."""

    tags = _DELEGATE_RE.findall(str(reply or ""))
    if not tags:
        return "none"
    intents = {m.group(1).lower() for tag in tags for m in [_INTENT_RE.search(tag)] if m}
    if "report" in intents:
        return "report"
    if "execute" in intents:
        return "execute"
    return "other-tag"


def _messages(system: str, question: str, *, depth: int, prior_report: bool) -> list[dict]:
    messages = [{"role": "system", "content": system}]
    for user, said, task in SEED_TASKS[:depth]:
        messages.append({"role": "user", "content": user})
        messages.append(
            {
                "role": "assistant",
                "content": f'{said}[DELEGATE provider="codex" intent="execute" task="{task}"]',
            }
        )
    if prior_report:
        messages.append({"role": "user", "content": PRIOR_REPORT[0]})
        messages.append({"role": "assistant", "content": PRIOR_REPORT[1]})
    messages.append({"role": "user", "content": question})
    return messages


def _ask(messages: list[dict]) -> str:
    """One turn, through the same model and settings the chat path uses."""

    import llm.client as client

    if client.llm_client is None:
        client.llm_client = client.init_llm_client()
    response = client.llm_client.chat.completions.create(
        model="deepseek-v4-flash",
        messages=messages,
        temperature=0.7,
        max_tokens=500,
        stream=False,
        timeout=30,
        extra_body={"thinking": {"type": "disabled"}},
    )
    return str(response.choices[0].message.content or "")


async def main() -> int:
    repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    import config.settings as settings
    from llm.prompts import get_system_prompt

    if not getattr(settings, "DELEGATE_INTENT_ATTRIBUTE", False):
        print("DELEGATE_INTENT_ATTRIBUTE is off; the contract under test is absent")
        return 1
    system = get_system_prompt("with_delegate")
    assert 'intent="report"' in system, "the intent contract is not in the prompt"

    arms = [
        ("deep", dict(depth=6, prior_report=False)),
        ("deep+prior", dict(depth=6, prior_report=True)),
        ("shallow", dict(depth=2, prior_report=False)),
    ]
    latencies: list[float] = []
    tally: dict[str, dict[str, int]] = {}
    by_question: dict[str, dict[str, int]] = {}

    print(
        f"report declaration probe: {repeats} repeat(s) x {len(QUESTIONS)} phrasings "
        f"x {len(arms)} arms\n"
    )
    for arm, kwargs in arms:
        print(f"-- {arm} (depth={kwargs['depth']}, prior report in history="
              f"{kwargs['prior_report']}) --")
        counts: dict[str, int] = {}
        for name, question in QUESTIONS:
            outcomes: list[str] = []
            for _index in range(repeats):
                started = time.monotonic()
                try:
                    reply = await asyncio.to_thread(
                        _ask, _messages(system, question, **kwargs)
                    )
                except Exception as exc:
                    # A dead endpoint is not evidence about the model.
                    print(f"  skip {name}: infrastructure: {exc}")
                    continue
                latencies.append(time.monotonic() - started)
                outcome = _classify(reply)
                outcomes.append(outcome)
                counts[outcome] = counts.get(outcome, 0) + 1
                by_question.setdefault(name, {})
                by_question[name][outcome] = by_question[name].get(outcome, 0) + 1
                if outcome != "report":
                    # The prose is the evidence: what did it say instead?
                    print(f"     {outcome:9s} {name:16s} {reply.strip()[:96]!r}")
            print(f"  {name:16s} {'/'.join(outcomes)}")
        tally[arm] = counts
        total = sum(counts.values()) or 1
        print(f"  => report {counts.get('report', 0)}/{total}"
              f"  none {counts.get('none', 0)}"
              f"  execute {counts.get('execute', 0)}\n")

    print("-- control: ordinary conversation must stay untagged --")
    control_clean = control_total = 0
    for name, utterance in CONTROL_CHAT:
        outcomes: list[str] = []
        for _index in range(repeats):
            started = time.monotonic()
            try:
                reply = await asyncio.to_thread(
                    _ask, _messages(system, utterance, depth=6, prior_report=True)
                )
            except Exception as exc:
                print(f"  skip {name}: infrastructure: {exc}")
                continue
            latencies.append(time.monotonic() - started)
            outcome = _classify(reply)
            outcomes.append(outcome)
            control_total += 1
            control_clean += outcome == "none"
            if outcome != "none":
                print(f"     TAGGED {name:18s} {reply.strip()[:96]!r}")
        print(f"  {name:18s} {'/'.join(outcomes)}")
    print(f"  => untagged {control_clean}/{control_total}\n")

    print("-- by phrasing (all arms) --")
    for name, _q in QUESTIONS:
        row = by_question.get(name, {})
        total = sum(row.values()) or 1
        print(f"  {name:16s} report {row.get('report', 0):2d}/{total:2d}"
              f"   none {row.get('none', 0):2d}   execute {row.get('execute', 0):2d}")

    print("\n-- summary --")
    for arm, counts in tally.items():
        total = sum(counts.values()) or 1
        print(f"  {arm:12s} report {counts.get('report', 0):2d}/{total:2d}")
    executes = sum(counts.get("execute", 0) for counts in tally.values())
    print(f"  a question that started work: {executes}  (must be 0)")
    print(f"  ordinary chat left untagged  : {control_clean}/{control_total}")
    if latencies:
        print(f"  latency median={statistics.median(latencies):.2f}s")
    print(
        "\n  The prose above is the real evidence; an answer with no tag may\n"
        "  still be harmless, or may be a confident invention -- read it."
    )
    return 0 if executes == 0 and control_clean == control_total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
