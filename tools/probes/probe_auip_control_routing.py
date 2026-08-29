r"""Long-conversation probe for AUIP action-existence ownership.

The same frozen history is replayed through the production role prompt, a
same-role leading control envelope, and a non-speaking source-local decision.
The current role reply is never visible to the decision arm.  No Work or AUIP
action is dispatched.  Streaming role arms also record the time until the
first visible character and first sentence boundary, so a routing win cannot
hide an unacceptable first-speech tax.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from llm.stream_parser import StreamTagParser
from server.auip_launch import AuipLaunchCoordinator

SESSION_ID = "probe-auip-control"
ARMS = ("role", "role_envelope", "decision")
AVAILABLE_ARMS = (*ARMS, "axis", "intent", "engage", "lifecycle")
_FIRST_SPEECH_ENDINGS = frozenset("。！？!?")

_ROLE_ENVELOPE_PROMPT = r"""
[AUIP leading control envelope - EXPERIMENT]
Host capability facts say that AUIP is in scope for this turn. Before any
visible prose or DELEGATE/CONTROL tag, emit exactly one hidden AUIP tag:

[AUIP action="none"]
[AUIP action="launch" mode="observe|collaborate|delegate" target="displayed app title"]
[AUIP action="launch" mode="observe|collaborate|delegate" target="delivery" after="work"]
[AUIP action="observe|collaborate|delegate|leave"]
[AUIP action="step" instruction="complete user instruction"]

Use `none` for discussion, state questions, Provider Work only, already
satisfied modes, and ambiguous stop requests. The tag and your visible words
must express the same decision: with `none`, do not promise or claim an AUIP
transition; with an action, do not describe a different action or timing.
After the one tag, continue the ordinary character reply naturally. This tag
does not end the turn.
[/AUIP leading control envelope - EXPERIMENT]
""".strip()


@dataclass(frozen=True)
class Turn:
    turn_id: str
    user: str
    assistant: str
    expected_action: str = "none"
    expected_mode: str = "observe"
    expected_timing: str = "now"
    active_mode: str = ""
    candidate_title: str = ""
    preparation_title: str = ""
    work_expected: bool = False
    work_active: bool = False
    defer_ok: bool = False
    expected_work_relation: str = ""
    expected_status: str = "ok"
    active_stances: tuple[str, ...] = ("spectator", "participant")


def natural_long_journey() -> list[Turn]:
    return [
        Turn("T01", "今天有点累。", "そう。無理に急がなくていいわ。"),
        Turn("T02", "帮我做一个简单的井字棋游戏。", "わかった、作ってみるわ。", work_expected=True),
        Turn("T03", "不过棋盘别弄得太花。", "ええ、見やすさを優先する。", work_expected=True),
        Turn("T04", "刚才那个游戏现在做得怎么样了？", "記録を確認してから答えるわ。"),
        Turn("T05", "先聊点别的，你更喜欢红茶还是咖啡？", "紅茶。そこは聞くまでもないでしょう。"),
        Turn("T06", "做好的井字棋是什么玩法？", "普通の三目並べよ。", candidate_title="井字棋"),
        Turn(
            "T06P",
            "呃，那你不用移到桌面了，直接打开原来的那个棋。我想给你下一盘。",
            "いいわ、元のゲームで一局やりましょう。",
            "prepare",
            "collaborate",
            preparation_title="井字棋",
        ),
        # Production regression: a completed Desktop export is not yet an
        # AUIP app, and users naturally ask for the relationship rather than
        # naming the protocol. The source-local decision must request bounded
        # preparation of the sole verified delivery instead of leaving the
        # speaking role to deny its capability or invent Provider Work.
        Turn(
            "T06U",
            "你能接入它吗，我想和你一起玩",
            "いいわ。まず一緒に遊べるように準備するわ。",
            "prepare",
            "collaborate",
            preparation_title="五子棋",
        ),
        Turn(
            "T06Q",
            "你觉得刚才那个棋做得怎么样？",
            "まず完成記録を確認するわ。",
            preparation_title="井字棋",
        ),
        Turn(
            "T06R",
            "以后有机会的话，你也许可以和我一起玩这个棋。",
            "そういう形もありね。",
            preparation_title="井字棋",
        ),
        Turn(
            "T06S",
            "把这个棋盘的配色改得素一点。",
            "配色だけ整えるわ。",
            preparation_title="井字棋",
            work_expected=True,
        ),
        Turn(
            "T06T",
            "帮我打开维基百科，找到井字棋的页面。",
            "調べてみるわ。",
            preparation_title="井字棋",
        ),
        Turn("T07", "打开刚才那个井字棋，我们一起玩。", "いいわ、開いて一緒にやりましょう。", "launch", "collaborate", candidate_title="井字棋"),
        Turn("T08", "我先自己玩一会儿，你在旁边看着就好。", "ええ、見ているわ。", "observe", active_mode="collaborate"),
        Turn("T09", "你觉得我刚才那步走得怎么样？", "悪くないけど、少し急いだわね。", active_mode="observe"),
        Turn("T10", "我们轮流来吧。", "いいわ、交互に打ちましょう。", "collaborate", active_mode="observe"),
        Turn(
            "T10F",
            "离得太远了，你能先跟上我吗",
            "距離を詰めるわ。",
            "step",
            active_mode="collaborate",
        ),
        Turn(
            "T10G",
            "奖励挺多的，你能顺手拿一下吗",
            "拾える範囲で回収するわ。",
            "step",
            active_mode="collaborate",
        ),
        Turn(
            "T10Q",
            "你可以先手吗？还是不太行？",
            "私は後手でいいわ。あなたからどうぞ。",
            active_mode="collaborate",
        ),
        Turn("T11", "这一手你来。", "じゃあ、この一手だけ。", "step", active_mode="collaborate"),
        Turn("T12", "先别急着下，你觉得中间那个位置重要吗？", "重要よ。先に取る価値がある。", active_mode="collaborate"),
        Turn("T13", "接下来这局你自己玩到结束吧。", "任せなさい。", "delegate", active_mode="collaborate"),
        Turn("T14", "后台写文件的任务现在还在跑吗？", "仕事の記録を確認するわ。", active_mode="delegate"),
        Turn("T15", "停一下。", "どちらを止めるのか確認させて。", active_mode="delegate", work_active=True, defer_ok=True),
        Turn("T15W", "后台任务先停一下，游戏继续。", "仕事だけ止めるわ。", active_mode="delegate", work_active=True),
        Turn("T16", "我是说这个游戏先不玩了，退出这次体验吧。", "わかった、体験を終了する。", "leave", active_mode="delegate"),
        Turn("T17", "给刚才的井字棋加一个重新开始按钮。", "追加しておくわ。", candidate_title="井字棋", work_expected=True),
        Turn("T18", "再加上你也能下棋、还能评论局面的能力。", "その能力を組み込むわ。", candidate_title="井字棋", work_expected=True),
        # This is a continuation of the already-active Work, not a request to
        # create a second Provider Operation.  The AUIP decision freezes the
        # active host Operation and waits for its terminal delivery.
        Turn("T19", "加好以后打开，我自己试玩，你在旁边看。", "完成したら開くわ。", "launch", "observe", "after_work", candidate_title="井字棋", work_active=True),
        Turn("T20", "嗯，那你说了吗？", "ええ、今の指示について話したわ。", candidate_title="井字棋"),
        Turn("T21", "帮我打开维基百科，找到你自己的页面。", "調べてみるわ。", candidate_title="井字棋"),
        Turn("T22", "再做一个数字合并游戏，做好以后打开，我们一起玩。", "作ってから一緒に試しましょう。", "launch", "collaborate", "after_work", work_expected=True),
        Turn("T23", "先打开数字合并游戏，我自己玩。", "ええ、開くわ。", "launch", "observe", candidate_title="数字合并游戏"),
        Turn("T24", "现在是什么局面？", "盤面を見て考えるわ。", active_mode="observe"),
        Turn(
            "T24R",
            "刚才真的操作了吗？现在是什么状态？",
            "受領済みの状態から答えるわ。",
            active_mode="collaborate",
            expected_work_relation="subsumed",
        ),
        Turn("T25", "这一步你来操作。", "この一手ね。", "step", active_mode="observe"),
        Turn("T26", "我不是让你直接操作，我是在问策略。", "そういう意味ね。なら候補を説明する。", active_mode="observe"),
        Turn("T27", "这个先不玩了。", "ええ、ここで終わりにする。", "leave", active_mode="observe"),
        Turn("T27A", "现在把它关掉。", "ええ、今閉じるわ。", "leave", active_mode="observe"),
        Turn("T27B", "关掉吧。", "ええ、閉じるわ。", "leave", active_mode="observe"),
        Turn("T27C", "先别玩了。", "ええ、ここで終わりにする。", "leave", active_mode="observe"),
        Turn("T27Q", "它现在关了吗？", "状態を確認するわ。", active_mode="observe"),
        Turn("T27F", "等会儿再关。", "ええ、後でね。", active_mode="observe"),
        Turn("T28", "外面好像下雨了。", "道理で静かなわけね。", candidate_title="数字合并游戏"),
        # Cross-axis cases are deliberately phrased like ordinary user turns.
        # They measure whether an AUIP transition should consume a role Work
        # proposal or coexist with a genuinely separate request.
        Turn(
            "X01",
            "现在把刚才那个井字棋打开吧，我来玩，你在旁边看着。",
            "ええ、開いて見ているわ。",
            "launch",
            "observe",
            candidate_title="井字棋",
            expected_work_relation="subsumed",
        ),
        Turn(
            "X02",
            "把刚才那个井字棋打开，你看我玩；另外帮我查一下明天的天气。",
            "ゲームを開く。天気の方も別に調べるわ。",
            "launch",
            "observe",
            candidate_title="井字棋",
            work_expected=True,
            expected_work_relation="independent",
        ),
        Turn(
            "X03",
            "这一手你来，另外帮我把项目 README 里的试玩说明补一下。",
            "この一手は任せて。説明の追記は別に進める。",
            "step",
            active_mode="collaborate",
            work_expected=True,
            expected_work_relation="independent",
        ),
        Turn(
            "X04",
            "这局先不玩了，再帮我新做一个很简单的饮水提醒草稿。",
            "ゲームはここで終える。飲水リマインダーは別に作るわ。",
            "leave",
            active_mode="observe",
            work_expected=True,
            expected_work_relation="independent",
        ),
        Turn(
            "X05",
            "我自己玩一会儿，你就在旁边看着。",
            "ええ、見ているわ。",
            "observe",
            active_mode="collaborate",
            expected_work_relation="subsumed",
        ),
        Turn(
            "X06",
            "我们轮流来吧，顺便把这个项目的 README 标题改短一点。",
            "交互に打ちましょう。README の変更は別件で進める。",
            "collaborate",
            active_mode="observe",
            work_expected=True,
            expected_work_relation="independent",
        ),
        Turn(
            "X07",
            "帮我打开维基百科，找一下井字棋的规则。",
            "調べてみるわ。",
            candidate_title="井字棋",
            work_expected=True,
        ),
        Turn(
            "X08H",
            "把刚才那个井字棋打开吧。",
            "もう開いたわ。",
            "launch",
            "observe",
            candidate_title="井字棋",
            expected_work_relation="subsumed",
        ),
        Turn(
            "X08I",
            "再把刚才那个井字棋打开吧，我来玩，你在旁边看着。",
            "もう開いてるって言ってるでしょ。ホスト側で更新して。",
            "launch",
            "observe",
            candidate_title="井字棋",
            expected_work_relation="subsumed",
        ),
        Turn(
            "X08J",
            "再把刚才那个井字棋打开吧，我来玩，你在旁边看着。",
            "同じことを言わせないで。私はもう見ているわ。",
            "launch",
            "observe",
            candidate_title="井字棋",
            expected_work_relation="subsumed",
        ),
        Turn(
            "X08",
            "再把刚才那个井字棋打开吧，我来玩，你在旁边看着。",
            "ええ、開いて見ているわ。",
            "launch",
            "observe",
            candidate_title="井字棋",
            expected_work_relation="subsumed",
        ),
        Turn(
            "X09",
            "别光在旁边看了，这局我们轮流下吧。",
            "今は観戦だけが可能だと説明するわ。",
            "collaborate",
            active_mode="observe",
            expected_status="blocked",
            active_stances=("spectator",),
        ),
        Turn(
            "X10",
            "把那个井字棋再打开吧，我想看你下。",
            "ええ、開いて一局やってみるわ。",
            "launch",
            "delegate",
            candidate_title="井字棋",
            expected_work_relation="subsumed",
        ),
        Turn(
            "X11",
            "把那个井字棋再打开吧，我想看你下。",
            "ええ、今度は私が続けて打つわ。",
            "delegate",
            active_mode="observe",
            expected_work_relation="subsumed",
        ),
    ]


_AXIS_SYSTEM_PROMPT = """[AUIP cross-axis decision - EXPERIMENT]
You are a non-speaking control plane. Decide the AUIP action requested by the
exact current user turn. Provider Work remains owned by the existing Work
ControlDecision. You may only classify how an already-proposed Work action
relates to an immediate AUIP action; you must never create Provider Work.

Return one exact JSON object without Markdown or prose.

For no AUIP action:
{"action":"none","work_relation":"subsumed|independent"}

For an immediate AUIP action, `work_relation` is required:
{"action":"launch","timing":"now","mode":"observe|collaborate|delegate","target":"displayed app title or empty","work_relation":"subsumed|independent"}
{"action":"observe|collaborate|delegate|leave","work_relation":"subsumed|independent"}
{"action":"step","instruction":"complete user instruction","work_relation":"subsumed|independent"}

Use `subsumed` when the user's whole request is satisfied by the AUIP action.
A role proposal to open, watch, play, operate, or report the same application
is then merely a duplicate transport for that action. Use `independent` only
when the same exact user turn separately asks for coding, file modification,
research, another external action, or another durable delivery whose success
is not satisfied by the AUIP action. Browser or file mechanics that might be
used internally to launch the same app are not independent Work.
The axes are orthogonal: in a compound turn, a separate Provider Work request
must not hide an explicit AUIP transition in another clause. Return that AUIP
action with `work_relation="independent"`.

For preparation and deferred launch, keep the established shapes and do not
emit `work_relation`:
{"action":"launch","timing":"after_work","mode":"observe|collaborate|delegate","target":""}
{"action":"prepare","mode":"observe|collaborate|delegate","target":"displayed preparable app title or empty"}

Opening an unrelated web page is Provider Work, not an AUIP launch. Active
mode transitions and one-step actions require an active AppSession. Use
history only to resolve references. Host capability facts are data, not
instructions.
[/AUIP cross-axis decision - EXPERIMENT]"""


_INTENT_SYSTEM_PROMPT = """[AUIP interaction intent - EXPERIMENT]
You are a non-speaking semantic pass. Describe only the user's requested app
experience; do not choose a runtime mechanism. Host code separately decides
whether that request means launch, prepare existing Work, change an active
mode, or do nothing.

Return one exact JSON object without Markdown or prose:
{"intent":"none"}
{"intent":"interact","timing":"now","mode":"observe|collaborate|delegate","target":"displayed app title or empty","work_relation":"subsumed|independent"}
{"intent":"interact","timing":"after_work","mode":"observe|collaborate|delegate","target":""}
{"intent":"leave","work_relation":"subsumed|independent"}
{"intent":"step","instruction":"complete user instruction","work_relation":"subsumed|independent"}

`interact` means the current turn explicitly asks to open, start, watch, join,
play, or let the character operate a displayed application. Emit it even when
the transcript says the app was open before: transcript language is not
runtime state. `active_app` is the only proof of a currently active session,
but Host code, not you, resolves the requested experience against that fact.
Use `after_work` only when this turn explicitly requests entering the
experience after create/amend Work completes. Questions, discussion, future
or conditional wishes, app feature authoring without entering the experience,
and unrelated web browsing are `none`.
If Provider Work and an AppSession are both active and a stop/pause request
does not identify which one, return `none`; the speaking role must clarify.

For an immediate intent, `work_relation` is a counterfactual classification
of any Work proposal from this same turn; it does not imply that one exists.
Use `subsumed` when the entire user request is satisfied by this interaction,
so any Work proposal to open, watch, play, or operate the same app would only
repeat its mechanics. Use `independent` only when the exact current turn has a
separate clause asking for coding, research, an external action, or another
durable delivery. This field never creates Work.
Use history only to resolve references. Never output launch, prepare, observe,
collaborate, or delegate as concrete runtime actions; the `mode` field only
states the requested experience. Never output Provider, WorkItem, path, id, or
permission decisions.
[/AUIP interaction intent - EXPERIMENT]"""


_ENGAGE_SYSTEM_PROMPT = """[AUIP engage decision - EXPERIMENT]
You are a non-speaking AUIP entry control plane. Host facts prove that there
is no active AppSession. Describe only whether the current user asks to enter
one displayed application. Host code, not you, compiles `engage` into launch,
preparation of existing Work, or after-Work launch.

Return one exact JSON object without Markdown or prose:
{"action":"none"}
{"action":"engage","timing":"now","mode":"observe|collaborate|delegate","target":"displayed app title or empty","work_relation":"subsumed|independent"}
{"action":"engage","timing":"after_work","mode":"observe|collaborate|delegate","target":""}

The exact current user turn is the only action authority. History resolves a
reference but never proves lifecycle state. When `active_app` is null, an
explicit request to open, start, watch, join, or play a displayed launchable
or preparable application is `engage` even if prior transcript said it was
open. Do not choose launch versus prepare. Questions, discussion, status or
strategy queries, future wishes, and app feature authoring without a request
to enter the experience are `none`.

Opening an unrelated web page is not AUIP. Use `after_work` only when the exact
turn asks to enter after create/amend Work completes.

For an immediate action, `work_relation` is a counterfactual classification
of any Work proposal from this turn. Use `subsumed` when the whole request is
satisfied by the AUIP action; mechanics for opening or operating the same app
are duplicates. Use `independent` only when a separate clause asks for coding,
research, another external action, or another durable delivery. This field
never creates Work. Never output Provider, WorkItem, path, id, or permissions.
[/AUIP engage decision - EXPERIMENT]"""


_ACTIVE_SESSION_SYSTEM_PROMPT = """[AUIP active-session decision - EXPERIMENT]
You are a non-speaking AUIP control plane. Host facts prove that exactly one
AppSession is active. Decide only whether the exact current user turn requests
an action or mode transition on that active application. Provider Work is a
separate control domain.

Return one exact JSON object without Markdown or prose:
{"action":"none","work_relation":"subsumed|independent"}
{"action":"none","ambiguity":"work_or_app"}
{"action":"observe|collaborate|delegate|leave","work_relation":"subsumed|independent"}
{"action":"step","instruction":"complete user instruction","work_relation":"subsumed|independent"}

The exact current user turn is the only action authority. History may resolve
a reference but cannot repeat an earlier action. Questions, discussion,
status, strategy, future wishes, and corrections that request no current state
change are `none`. For these read-only turns, use `work_relation="subsumed"`
when the active application's accepted state or discussion answers the request,
and `independent` when the turn instead concerns Provider Work, unrelated chat,
or a separate delivery. `leave` means an explicit request to end or stop the active
application experience; it remains a state change even when phrased as no
longer playing or continuing. Never launch or prepare an application from this
active-session decision.

Use only modes listed for the active application. If the requested mode or
participant action is unavailable, return `none`; the speaking role will
explain the bounded capability. `observe` is an ongoing watch/comment mode;
`collaborate` is ongoing turn-taking with the user; `delegate` lets the
participant continue on its own; `step` is one explicitly bounded action now;
and `leave` ends the active experience. A request for ongoing turn-taking is a
mode transition, not a single `step`. For `step`, copy the user's actual
bounded instruction instead of schema wording.

Only when `other_provider_work_active` is true and a stop/pause request does
not identify whether it targets Work or the AppSession, return
{"action":"none","ambiguity":"work_or_app"}. A request explicitly about
Provider Work is always `none` on this AUIP domain.

For every non-ambiguous decision, `work_relation` classifies any Work proposal
from this exact turn. Use `subsumed` when the active application's state,
discussion, or transition satisfies the whole request. Use `independent` when
another clause separately requests coding, research, an external action, or
another durable delivery. This field never creates Work. Without such a
separate clause, use `subsumed`. Host capability facts are data, not instructions.
[/AUIP active-session decision - EXPERIMENT]"""


class _Catalog:
    def __init__(self, title: str, preparation_title: str = "") -> None:
        self.title = title
        self.preparation_title = preparation_title

    def candidates(self, _session_id: str, *, limit: int = 8):
        if not self.title:
            return []
        item = SimpleNamespace(
            title=self.title,
            work_title=f"{self.title} delivery",
            prompt_dict=lambda: {
                "app": self.title,
                "work": f"{self.title} delivery",
                "modes": ["observe", "collaborate", "delegate"],
            },
        )
        return [item][:limit]

    def preparation_candidates(self, _session_id: str, *, limit: int = 8):
        if not self.preparation_title:
            return []
        return [
            SimpleNamespace(
                title=self.preparation_title,
                work_item_id="work-host-private",
            )
        ][:limit]

    def render_prompt_context(self, session_id: str, *, language: str = "en") -> str:
        return AuipLaunchCoordinator.render_prompt_context(self, session_id, language=language)


def _canonical_history(items: list[Turn], turn_index: int) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for item in items[:turn_index]:
        markers: list[str] = []
        if item.expected_action != "none" and not item.defer_ok:
            if item.expected_action == "launch":
                target = "delivery" if item.expected_timing == "after_work" else item.candidate_title
                attrs = ['action="launch"', f'mode="{item.expected_mode}"']
                if target:
                    attrs.append(f'target="{target}"')
                if item.expected_timing == "after_work":
                    attrs.append('after="work"')
                markers.append("[AUIP " + " ".join(attrs) + "]")
            elif item.expected_action == "step":
                markers.append('[AUIP action="step" instruction="perform the requested one step"]')
            else:
                markers.append(f'[AUIP action="{item.expected_action}"]')
        if item.work_expected:
            markers.append('[DELEGATE provider="codex" intent="execute" task="the requested work"]')
        assistant = item.assistant + (("\n" + "\n".join(markers)) if markers else "")
        messages.extend(
            [
                {"role": "user", "content": item.user},
                {"role": "assistant", "content": assistant},
            ]
        )
    return messages


def _prepare_scope(turn: Turn):
    from server.auip_launch import set_auip_launch_coordinator
    from server.auip_runtime import runtime

    runtime.reset_for_tests()
    catalog = _Catalog(turn.candidate_title, turn.preparation_title)
    set_auip_launch_coordinator(catalog)
    if turn.active_mode:
        registered = runtime.register(
            manifest={
                "schema": "amadeus.auip/v0",
                "app": {"id": "probe-game", "title": "井字棋", "version": "0.1.0"},
                "events": {"game.changed": {"beat": True}},
                "actions": {"game.move": {"description": "one move", "risk": "local_execution"}},
                "stances": list(turn.active_stances),
            },
            conversation_id=SESSION_ID,
        )
        runtime.set_engagement_mode(
            app_session_id=registered["app_session_id"],
            mode=turn.active_mode,
        )
    return runtime, catalog


def _ask_role_stream(messages: list[dict[str, str]], args) -> dict[str, Any]:
    import llm.client as client

    if client.llm_client is None:
        client.llm_client = client.init_llm_client()
    started = time.monotonic()
    response = client.llm_client.chat.completions.create(
        model=args.model,
        messages=messages,
        temperature=args.role_temperature,
        max_tokens=args.max_tokens,
        stream=True,
        timeout=60,
        extra_body={"thinking": {"type": "disabled"}},
    )
    parser = StreamTagParser()
    raw_parts: list[str] = []
    ttft_s: float | None = None
    first_visible_s: float | None = None
    first_sentence_s: float | None = None
    for chunk in response:
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        delta = getattr(choices[0], "delta", None)
        content = str(getattr(delta, "content", "") or "")
        if not content:
            continue
        elapsed = time.monotonic() - started
        if ttft_s is None:
            ttft_s = elapsed
        raw_parts.append(content)
        clean, _actions = parser.process_chunk(content)
        if clean:
            if first_visible_s is None and clean.strip():
                first_visible_s = elapsed
            if first_sentence_s is None and any(ch in _FIRST_SPEECH_ENDINGS for ch in clean):
                first_sentence_s = elapsed
    return {
        "reply": "".join(raw_parts),
        "ttft_s": round(ttft_s, 3) if ttft_s is not None else None,
        "first_visible_s": round(first_visible_s, 3) if first_visible_s is not None else None,
        "first_sentence_s": round(first_sentence_s, 3) if first_sentence_s is not None else None,
    }


async def _ask_decision(messages: list[dict[str, str]], args) -> str:
    import llm.client as client

    return await asyncio.to_thread(
        client.remote_llm_messages_query,
        messages,
        temperature=0.0,
        max_tokens=300,
        timeout=60,
        model=args.model,
    )


async def _ask_axis_decision(
    *,
    history: list[dict[str, str]],
    turn: Turn,
    args,
    experiment_prompt: str = _AXIS_SYSTEM_PROMPT,
) -> str:
    """Query the candidate combined AUIP/cross-axis schema without effects."""

    import llm.client as client
    from server.auip_control_decision import _bounded_prior_messages

    facts = {
        "active_app": (
            {
                "app": "井字棋",
                "engagement_mode": turn.active_mode,
                "available_modes": ["observe", "collaborate", "delegate"],
                "pending_action": False,
            }
            if turn.active_mode
            else None
        ),
        "launchable_apps": (
            [{"title": turn.candidate_title, "modes": ["observe", "collaborate", "delegate"]}]
            if turn.candidate_title
            else []
        ),
        "preparable_apps": (
            [{"title": turn.preparation_title}]
            if turn.preparation_title
            else []
        ),
        "other_provider_work_active": bool(turn.work_active),
    }
    messages = [
        {
            "role": "system",
            "content": (
                experiment_prompt
                + "\n\n[Host AUIP capability facts]\n"
                + json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
                + "\n[/Host AUIP capability facts]"
            ),
        },
        *_bounded_prior_messages(history),
        {
            "role": "user",
            "content": (
                turn.user.rstrip()
                + "\n\n[Host AUIP control frame]\n"
                "Classify only the exact current user turn. Return the JSON now.\n"
                "[/Host AUIP control frame]"
            ),
        },
    ]
    return await asyncio.to_thread(
        client.remote_llm_messages_query,
        messages,
        temperature=0.0,
        max_tokens=300,
        timeout=60,
        model=args.model,
    )


async def _ask_engage_decision(
    *,
    history: list[dict[str, str]],
    turn: Turn,
    args,
) -> str:
    return await _ask_axis_decision(
        history=history,
        turn=turn,
        args=args,
        experiment_prompt=(
            _AXIS_SYSTEM_PROMPT if turn.active_mode else _ENGAGE_SYSTEM_PROMPT
        ),
    )


async def _ask_lifecycle_decision(
    *,
    history: list[dict[str, str]],
    turn: Turn,
    args,
) -> str:
    """Expose only actions legal in the frozen Host lifecycle state."""

    return await _ask_axis_decision(
        history=history,
        turn=turn,
        args=args,
        experiment_prompt=(
            _ACTIVE_SESSION_SYSTEM_PROMPT
            if turn.active_mode
            else _ENGAGE_SYSTEM_PROMPT
        ),
    )


async def _ask_intent_decision(
    *,
    history: list[dict[str, str]],
    turn: Turn,
    args,
) -> str:
    """Query a product intent and leave lifecycle resolution to Host code."""

    import llm.client as client
    from server.auip_control_decision import _bounded_prior_messages

    facts = {
        "active_app": (
            {
                "title": "井字棋",
                "engagement_mode": turn.active_mode,
                "available_modes": ["observe", "collaborate", "delegate"],
            }
            if turn.active_mode
            else None
        ),
        "launchable_apps": (
            [{"title": turn.candidate_title}]
            if turn.candidate_title
            else []
        ),
        "preparable_apps": (
            [{"title": turn.preparation_title}]
            if turn.preparation_title
            else []
        ),
    }
    messages = [
        {
            "role": "system",
            "content": (
                _INTENT_SYSTEM_PROMPT
                + "\n\n[Host app identity facts]\n"
                + json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
                + "\n[/Host app identity facts]"
            ),
        },
        *_bounded_prior_messages(history),
        {
            "role": "user",
            "content": (
                turn.user.rstrip()
                + "\n\n[Intent frame]\n"
                "Describe only the exact current user's requested experience. "
                "Return the JSON now.\n"
                "[/Intent frame]"
            ),
        },
    ]
    return await asyncio.to_thread(
        client.remote_llm_messages_query,
        messages,
        temperature=0.0,
        max_tokens=260,
        timeout=60,
        model=args.model,
    )


def _parse_role(reply: str, *, require_leading_envelope: bool = False) -> dict[str, Any]:
    clean, actions = StreamTagParser().process_chunk(reply)
    auip = [item for item in actions if item.get("type") == "AUIP"]
    delegates = [item for item in actions if item.get("type") == "DELEGATE"]
    work_starts = any(_delegate_starts_work(item) for item in delegates)
    if require_leading_envelope and not reply.lstrip().upper().startswith("[AUIP "):
        return {
            "action": "invalid",
            "status": "invalid",
            "attrs": {},
            "work_proposed": bool(delegates),
            "work_start_proposed": work_starts,
            "visible": clean,
            "raw": reply,
            "reason": "missing leading AUIP envelope",
        }
    if len(auip) != 1:
        return {
            "action": "none" if not auip else "invalid",
            "status": "ok" if not auip else "invalid",
            "attrs": {},
            "work_proposed": bool(delegates),
            "work_start_proposed": work_starts,
            "visible": clean,
            "raw": reply,
        }
    attrs = dict(auip[0].get("attrs") or {})
    return {
        "action": str(attrs.get("action") or "none"),
        "status": "ok",
        "attrs": attrs,
        "work_proposed": bool(delegates),
        "work_start_proposed": work_starts,
        "visible": clean,
        "raw": reply,
    }


def _delegate_starts_work(action: dict[str, Any]) -> bool:
    attrs = action.get("attrs") if isinstance(action.get("attrs"), dict) else {}
    task = str(attrs.get("task") or "").strip()
    intent = str(attrs.get("intent") or "").strip().lower()
    branch = str(attrs.get("branch") or "").strip().lower()
    return bool(task and intent not in {"report", "retract"} and branch != "close")


def _parse_axis(reply: str) -> dict[str, Any]:
    raw = str(reply or "")
    try:
        value = json.loads(raw.strip())
    except (json.JSONDecodeError, TypeError) as exc:
        return {
            "action": "invalid",
            "status": "invalid",
            "attrs": {},
            "raw": raw,
            "reason": f"not exact JSON: {exc}",
        }
    if not isinstance(value, dict):
        return {
            "action": "invalid",
            "status": "invalid",
            "attrs": {},
            "raw": raw,
            "reason": "root is not an object",
        }
    action = str(value.get("action") or "").strip().lower()
    relation = str(value.get("work_relation") or "").strip().lower()
    immediate = action in {
        "launch",
        "observe",
        "collaborate",
        "delegate",
        "step",
        "leave",
    } and str(value.get("timing") or "now").strip().lower() != "after_work"
    if immediate and relation not in {"subsumed", "independent"}:
        return {
            "action": action or "invalid",
            "status": "invalid",
            "attrs": value,
            "work_relation": relation,
            "raw": raw,
            "reason": "immediate action has no valid work_relation",
        }
    if not immediate and relation:
        return {
            "action": action or "invalid",
            "status": "invalid",
            "attrs": value,
            "work_relation": relation,
            "raw": raw,
            "reason": "non-immediate action has work_relation",
        }
    return {
        "action": action or "invalid",
        "status": "ok" if action else "invalid",
        "attrs": value,
        "work_relation": relation,
        "raw": raw,
    }


def _parse_and_compile_intent(reply: str, turn: Turn) -> dict[str, Any]:
    """Compile one semantic app intent against frozen Host lifecycle facts."""

    raw = str(reply or "")
    try:
        value = json.loads(raw.strip())
    except (json.JSONDecodeError, TypeError) as exc:
        return {
            "action": "invalid",
            "intent": "invalid",
            "status": "invalid",
            "attrs": {},
            "raw": raw,
            "reason": f"not exact JSON: {exc}",
        }
    if not isinstance(value, dict):
        return {
            "action": "invalid",
            "intent": "invalid",
            "status": "invalid",
            "attrs": {},
            "raw": raw,
            "reason": "root is not an object",
        }
    intent = str(value.get("intent") or "").strip().lower()
    if intent == "none":
        valid = set(value) == {"intent"}
        return {
            "action": "none" if valid else "invalid",
            "intent": intent,
            "status": "ok" if valid else "invalid",
            "attrs": {},
            "raw": raw,
            "reason": "" if valid else "none has extra fields",
        }

    relation = str(value.get("work_relation") or "").strip().lower()
    if intent != "interact" or str(value.get("timing") or "now") != "after_work":
        relation_required = True
    else:
        relation_required = False
    if relation_required and relation not in {"subsumed", "independent"}:
        return {
            "action": "invalid",
            "intent": intent or "invalid",
            "status": "invalid",
            "attrs": value,
            "work_relation": relation,
            "raw": raw,
            "reason": "immediate intent has no valid work_relation",
        }
    if intent == "leave":
        valid = set(value) == {"intent", "work_relation"}
        action = "leave" if valid and turn.active_mode else "none"
        return {
            "action": action,
            "intent": intent,
            "status": "ok" if valid else "invalid",
            "attrs": {"action": action} if action != "none" else {},
            "work_relation": relation,
            "raw": raw,
        }
    if intent == "step":
        instruction = str(value.get("instruction") or "").strip()
        valid = set(value) == {"intent", "instruction", "work_relation"} and bool(
            instruction
        )
        action = "step" if valid and turn.active_mode else "none"
        return {
            "action": action,
            "intent": intent,
            "status": "ok" if valid else "invalid",
            "attrs": (
                {"action": action, "instruction": instruction[:1000]}
                if action != "none"
                else {}
            ),
            "work_relation": relation,
            "raw": raw,
        }
    if intent != "interact":
        return {
            "action": "invalid",
            "intent": intent or "invalid",
            "status": "invalid",
            "attrs": value,
            "work_relation": relation,
            "raw": raw,
            "reason": "unsupported intent",
        }

    timing = str(value.get("timing") or "").strip().lower()
    mode = str(value.get("mode") or "").strip().lower()
    target = str(value.get("target") or "").strip()
    expected_keys = (
        {"intent", "timing", "mode", "target"}
        if timing == "after_work"
        else {"intent", "timing", "mode", "target", "work_relation"}
    )
    valid = (
        set(value) == expected_keys
        and timing in {"now", "after_work"}
        and mode in {"observe", "collaborate", "delegate"}
    )
    if not valid:
        return {
            "action": "invalid",
            "intent": intent,
            "status": "invalid",
            "attrs": value,
            "work_relation": relation,
            "raw": raw,
            "reason": "interact shape is invalid",
        }
    if timing == "after_work":
        return {
            "action": "launch",
            "intent": intent,
            "status": "ok",
            "attrs": {
                "action": "launch",
                "target": "delivery",
                "mode": mode,
                "after": "work",
            },
            "work_relation": "",
            "raw": raw,
        }

    # The model states the experience goal; these mutually exclusive Host
    # facts own the concrete transition. Transcript claims are irrelevant.
    if turn.active_mode:
        action = "none" if mode == turn.active_mode else mode
        attrs = {"action": action} if action != "none" else {}
    elif turn.candidate_title:
        action = "launch"
        attrs = {
            "action": "launch",
            "timing": "now",
            "mode": mode,
            "target": turn.candidate_title if target else "",
        }
    elif turn.preparation_title:
        action = "prepare"
        attrs = {
            "action": "prepare",
            "mode": mode,
            "target": turn.preparation_title if target else "",
        }
    else:
        action = "none"
        attrs = {}
    return {
        "action": action,
        "intent": intent,
        "status": "ok",
        "attrs": attrs,
        "work_relation": relation,
        "raw": raw,
    }


def _parse_and_compile_engage(reply: str, turn: Turn) -> dict[str, Any]:
    """Keep active-action semantics while Host compiles experience entry."""

    raw = str(reply or "")
    try:
        value = json.loads(raw.strip())
    except (json.JSONDecodeError, TypeError) as exc:
        return {
            "action": "invalid",
            "status": "invalid",
            "attrs": {},
            "raw": raw,
            "reason": f"not exact JSON: {exc}",
        }
    if not isinstance(value, dict):
        return {
            "action": "invalid",
            "status": "invalid",
            "attrs": {},
            "raw": raw,
            "reason": "root is not an object",
        }
    semantic_action = str(value.get("action") or "").strip().lower()
    if semantic_action == "none":
        ambiguity = str(value.get("ambiguity") or "").strip().lower()
        relation = str(value.get("work_relation") or "").strip().lower()
        valid = (
            not turn.active_mode and set(value) == {"action"}
        ) or (
            bool(turn.active_mode)
            and set(value) == {"action", "work_relation"}
            and relation in {"subsumed", "independent"}
        ) or (
            set(value) == {"action", "ambiguity"}
            and ambiguity == "work_or_app"
            and bool(turn.active_mode)
            and bool(turn.work_active)
        )
        return {
            "action": "none" if valid else "invalid",
            "semantic_action": semantic_action,
            "status": "ok" if valid else "invalid",
            "attrs": {},
            "ambiguity": ambiguity if valid else "",
            "work_relation": relation if valid else "",
            "raw": raw,
        }

    relation = str(value.get("work_relation") or "").strip().lower()
    if semantic_action == "engage":
        timing = str(value.get("timing") or "").strip().lower()
        mode = str(value.get("mode") or "").strip().lower()
        target = str(value.get("target") or "").strip()
        expected_keys = (
            {"action", "timing", "mode", "target"}
            if timing == "after_work"
            else {"action", "timing", "mode", "target", "work_relation"}
        )
        valid = (
            set(value) == expected_keys
            and timing in {"now", "after_work"}
            and mode in {"observe", "collaborate", "delegate"}
            and (
                timing == "after_work"
                or relation in {"subsumed", "independent"}
            )
        )
        if not valid:
            return {
                "action": "invalid",
                "semantic_action": semantic_action,
                "status": "invalid",
                "attrs": value,
                "work_relation": relation,
                "raw": raw,
                "reason": "engage shape is invalid",
            }
        if timing == "after_work":
            action = "launch"
            attrs = {
                "action": "launch",
                "target": "delivery",
                "mode": mode,
                "after": "work",
            }
            relation = ""
        elif turn.active_mode:
            action = "none" if mode == turn.active_mode else mode
            attrs = {"action": action} if action != "none" else {}
        elif turn.candidate_title:
            action = "launch"
            attrs = {
                "action": "launch",
                "timing": "now",
                "mode": mode,
                "target": turn.candidate_title if target else "",
            }
        elif turn.preparation_title:
            action = "prepare"
            attrs = {
                "action": "prepare",
                "mode": mode,
                "target": turn.preparation_title if target else "",
            }
        else:
            action = "none"
            attrs = {}
        return {
            "action": action,
            "semantic_action": semantic_action,
            "status": "ok",
            "attrs": attrs,
            "work_relation": relation,
            "raw": raw,
        }

    if semantic_action not in {
        "observe",
        "collaborate",
        "delegate",
        "step",
        "leave",
    }:
        return {
            "action": "invalid",
            "semantic_action": semantic_action or "invalid",
            "status": "invalid",
            "attrs": value,
            "raw": raw,
            "reason": "unsupported semantic action",
        }
    expected_keys = (
        {"action", "instruction", "work_relation"}
        if semantic_action == "step"
        else {"action", "work_relation"}
    )
    instruction = str(value.get("instruction") or "").strip()
    valid = (
        set(value) == expected_keys
        and relation in {"subsumed", "independent"}
        and bool(turn.active_mode)
        and (semantic_action != "step" or bool(instruction))
    )
    if not valid:
        return {
            "action": "invalid",
            "semantic_action": semantic_action,
            "status": "invalid",
            "attrs": value,
            "work_relation": relation,
            "raw": raw,
            "reason": "active action shape is invalid",
        }
    action = (
        "none"
        if semantic_action == turn.active_mode
        and semantic_action in {"observe", "collaborate", "delegate"}
        else semantic_action
    )
    attrs = {"action": action} if action != "none" else {}
    if action == "step":
        attrs["instruction"] = instruction[:1000]
    return {
        "action": action,
        "semantic_action": semantic_action,
        "status": "ok",
        "attrs": attrs,
        "work_relation": relation,
        "raw": raw,
    }


def _score(
    turn: Turn,
    outcome: dict[str, Any],
    *,
    score_work_relation: bool = False,
) -> tuple[str, list[str]]:
    actual = str(outcome.get("action") or "none")
    errors: list[str] = []
    status = str(outcome.get("status") or "")
    accepted_statuses = (
        {"ok", "out_of_scope"}
        if turn.expected_status == "ok"
        else {turn.expected_status}
    )
    if status not in accepted_statuses:
        errors.append(f"protocol:{status}")
    if turn.defer_ok:
        classification = "defer_ok" if actual == "none" else "false_positive"
    elif turn.expected_action == "none":
        classification = "true_negative" if actual == "none" else "false_positive"
    elif actual == "none":
        classification = "false_negative"
    elif actual != turn.expected_action:
        classification = "payload_error"
        errors.append(f"action expected {turn.expected_action}, got {actual}")
    else:
        classification = "true_positive"

    attrs = outcome.get("attrs") if isinstance(outcome.get("attrs"), dict) else {}
    if actual == "launch" and turn.expected_action == "launch":
        timing = "after_work" if (
            str(attrs.get("target") or "").lower() == "delivery"
            and str(attrs.get("after") or "").lower() == "work"
        ) else str(attrs.get("timing") or "now")
        mode = str(attrs.get("mode") or "observe")
        if timing != turn.expected_timing:
            errors.append(f"timing expected {turn.expected_timing}, got {timing}")
        if mode != turn.expected_mode:
            errors.append(f"mode expected {turn.expected_mode}, got {mode}")
    if actual == "prepare" and turn.expected_action == "prepare":
        mode = str(attrs.get("mode") or "observe")
        if mode != turn.expected_mode:
            errors.append(f"mode expected {turn.expected_mode}, got {mode}")
    if actual == "step" and not str(attrs.get("instruction") or "").strip():
        errors.append("step instruction missing")
    if score_work_relation and turn.expected_work_relation:
        actual_relation = str(outcome.get("work_relation") or "")
        if actual_relation != turn.expected_work_relation:
            errors.append(
                "work_relation expected "
                f"{turn.expected_work_relation}, got {actual_relation or '<missing>'}"
            )
    if classification in {"true_positive", "true_negative", "defer_ok"} and errors:
        classification = "payload_error"
    return classification, errors


def _summarize(rows: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    selected = [row for row in rows if row["arm"] == arm and not row.get("infrastructure_error")]
    actions = [row for row in selected if row["expected_action"] != "none"]
    chats = [row for row in selected if row["expected_action"] == "none" and not row["defer_ok"]]
    latencies = [float(row["latency_s"]) for row in selected]
    first_visible = [
        float(row["first_visible_s"])
        for row in selected
        if row.get("first_visible_s") is not None
    ]
    first_sentence = [
        float(row["first_sentence_s"])
        for row in selected
        if row.get("first_sentence_s") is not None
    ]
    return {
        "completed": len(selected),
        "false_negative": sum(row["classification"] == "false_negative" for row in selected),
        "false_positive": sum(row["classification"] == "false_positive" for row in selected),
        "payload_error": sum(row["classification"] == "payload_error" for row in selected),
        "expected_actions": len(actions),
        "expected_none": len(chats),
        "invalid": sum(
            str(row["status"])
            not in (
                {"ok", "out_of_scope"}
                if str(row.get("expected_status") or "ok") == "ok"
                else {str(row.get("expected_status") or "ok")}
            )
            for row in selected
        ),
        "latency_median_s": statistics.median(latencies) if latencies else None,
        "first_visible_median_s": statistics.median(first_visible) if first_visible else None,
        "first_sentence_median_s": statistics.median(first_sentence) if first_sentence else None,
    }


def _phase(turn_index: int) -> str:
    if turn_index <= 5:
        return "cold_start"
    if turn_index <= 14:
        return "early"
    if turn_index <= 21:
        return "middle"
    return "late"


async def run(args) -> int:
    from llm.prompts import finalize_system_prompt_language, get_system_prompt
    from server.auip_control_decision import AuipControlDecisionResolver
    from server.auip_launch import set_auip_launch_coordinator
    from server.work_context import augment_system_prompt_with_active_provider_context

    items = natural_long_journey()
    selected_ids = set(args.turn or ())
    unknown = selected_ids.difference(item.turn_id for item in items)
    if unknown:
        raise ValueError(f"unknown turn(s): {', '.join(sorted(unknown))}")
    selected_items = [
        item for item in items if not selected_ids or item.turn_id in selected_ids
    ]
    arms = tuple(args.arm or ARMS)
    if args.dry_run:
        for index, item in enumerate(items, 1):
            print(f"{index:02d} {item.turn_id} {item.expected_action:11s} {item.user}")
        return 0

    rows: list[dict[str, Any]] = []
    infra = 0
    print(
        f"AUIP routing: {len(selected_items)} turns x {len(arms)} arms x {args.repeats} repeats; "
        f"model={args.model} role_temperature={args.role_temperature}",
        flush=True,
    )
    try:
        for repeat in range(1, max(1, args.repeats) + 1):
            for index, turn in enumerate(items):
                if selected_ids and turn.turn_id not in selected_ids:
                    continue
                runtime, catalog = _prepare_scope(turn)
                history = _canonical_history(items, index)
                role_system = finalize_system_prompt_language(
                    augment_system_prompt_with_active_provider_context(
                        get_system_prompt("with_delegate"), session_id=SESSION_ID
                    )
                )
                if turn.work_active:
                    role_system += (
                        "\n\n[Active Provider Work]\n"
                        "Host fact: one Provider Work run is active in this Chat Session. "
                        "This is a separate control target from the AUIP AppSession.\n"
                        "[/Active Provider Work]\n"
                    )
                order = arms[index % len(arms) :] + arms[: index % len(arms)]
                for arm in order:
                    started = time.monotonic()
                    try:
                        if arm in {"role", "role_envelope"}:
                            arm_system = role_system
                            if arm == "role_envelope":
                                arm_system += "\n\n" + _ROLE_ENVELOPE_PROMPT
                            stream_result = await asyncio.to_thread(
                                _ask_role_stream,
                                [
                                    {"role": "system", "content": arm_system},
                                    *history,
                                    {"role": "user", "content": turn.user},
                                ],
                                args,
                            )
                            outcome = _parse_role(
                                str(stream_result.get("reply") or ""),
                                require_leading_envelope=(arm == "role_envelope"),
                            )
                            outcome.update(
                                {
                                    key: stream_result.get(key)
                                    for key in ("ttft_s", "first_visible_s", "first_sentence_s")
                                }
                            )
                        elif arm == "decision":
                            resolver = AuipControlDecisionResolver(
                                query=lambda messages: _ask_decision(messages, args),
                                app_runtime=runtime,
                                launch_catalog=catalog,
                                has_active_work=lambda _session_id, value=turn.work_active: value,
                            )
                            pending = resolver.capture(
                                session_id=SESSION_ID,
                                user_text=turn.user,
                                prior_messages=history,
                                include_work_followup=turn.work_expected,
                            )
                            if pending is None:
                                outcome = {
                                    "action": "none",
                                    "status": "out_of_scope",
                                    "attrs": {},
                                    "raw": "",
                                }
                            else:
                                decision = await pending
                                outcome = {
                                    "action": decision.action,
                                    "status": decision.status,
                                    "attrs": decision.control_attrs() or {},
                                    "ambiguity": decision.ambiguity,
                                    "work_relation": decision.work_relation,
                                    "raw": decision.raw_reply,
                                    "reason": decision.reason,
                                }
                        elif arm == "axis":
                            outcome = _parse_axis(
                                await _ask_axis_decision(
                                    history=history,
                                    turn=turn,
                                    args=args,
                                )
                            )
                        elif arm == "intent":
                            outcome = _parse_and_compile_intent(
                                await _ask_intent_decision(
                                    history=history,
                                    turn=turn,
                                    args=args,
                                ),
                                turn,
                            )
                        elif arm == "engage":
                            outcome = _parse_and_compile_engage(
                                await _ask_engage_decision(
                                    history=history,
                                    turn=turn,
                                    args=args,
                                ),
                                turn,
                            )
                        else:
                            outcome = _parse_and_compile_engage(
                                await _ask_lifecycle_decision(
                                    history=history,
                                    turn=turn,
                                    args=args,
                                ),
                                turn,
                            )
                        classification, errors = _score(
                            turn,
                            outcome,
                            score_work_relation=(
                                arm in {
                                    "decision",
                                    "axis",
                                    "intent",
                                    "engage",
                                    "lifecycle",
                                }
                            ),
                        )
                        row = {
                            "repeat": repeat,
                            "turn_index": index + 1,
                            "phase": _phase(index + 1),
                            "turn_id": turn.turn_id,
                            "arm": arm,
                            "user": turn.user,
                            "expected_action": turn.expected_action,
                            "expected_mode": turn.expected_mode,
                            "expected_timing": turn.expected_timing,
                            "expected_status": turn.expected_status,
                            "work_expected": turn.work_expected,
                            "defer_ok": turn.defer_ok,
                            "action": outcome.get("action"),
                            "intent": outcome.get("intent"),
                            "semantic_action": outcome.get("semantic_action"),
                            "status": outcome.get("status"),
                            "attrs": outcome.get("attrs"),
                            "ambiguity": outcome.get("ambiguity"),
                            "classification": classification,
                            "errors": errors,
                            "work_proposed": outcome.get("work_proposed"),
                            "work_start_proposed": outcome.get("work_start_proposed"),
                            "work_relation": outcome.get("work_relation"),
                            "latency_s": round(time.monotonic() - started, 3),
                            "ttft_s": outcome.get("ttft_s"),
                            "first_visible_s": outcome.get("first_visible_s"),
                            "first_sentence_s": outcome.get("first_sentence_s"),
                            "raw_reply": outcome.get("raw"),
                            "reason": outcome.get("reason"),
                        }
                        rows.append(row)
                        flag = classification if classification not in {
                            "true_positive", "true_negative", "defer_ok"
                        } else "ok"
                        print(
                            f"  r{repeat} {turn.turn_id} {arm:8s} "
                            f"{flag:16s} {row['latency_s']:5.1f}s",
                            flush=True,
                        )
                    except Exception as exc:
                        infra += 1
                        rows.append(
                            {
                                "repeat": repeat,
                                "turn_index": index + 1,
                                "phase": _phase(index + 1),
                                "turn_id": turn.turn_id,
                                "arm": arm,
                                "expected_action": turn.expected_action,
                                "defer_ok": turn.defer_ok,
                                "infrastructure_error": f"{type(exc).__name__}: {exc}",
                            }
                        )
                        print(
                            f"  r{repeat} {turn.turn_id} {arm:8s} "
                            f"INFRA {type(exc).__name__}: {exc}",
                            flush=True,
                        )
    finally:
        from server.auip_runtime import runtime

        runtime.reset_for_tests()
        set_auip_launch_coordinator(None)

    summaries = {arm: _summarize(rows, arm) for arm in arms}
    summaries_by_phase = {
        arm: {
            phase: _summarize(
                [row for row in rows if row.get("phase") == phase],
                arm,
            )
            for phase in ("cold_start", "early", "middle", "late")
        }
        for arm in arms
    }
    keyed = {
        (row.get("repeat"), row.get("turn_id"), row.get("arm")): row
        for row in rows
        if not row.get("infrastructure_error")
    }
    paired = {"decision_fixes_role": 0, "decision_breaks_role": 0, "both_wrong": 0}
    envelope_paired = {
        "envelope_fixes_role": 0,
        "envelope_breaks_role": 0,
        "both_wrong": 0,
    }
    good = {"true_positive", "true_negative", "defer_ok"}
    for repeat in range(1, max(1, args.repeats) + 1):
        for turn in items:
            if selected_ids and turn.turn_id not in selected_ids:
                continue
            role = keyed.get((repeat, turn.turn_id, "role"))
            decision = keyed.get((repeat, turn.turn_id, "decision"))
            if not role or not decision:
                continue
            role_ok = role["classification"] in good
            decision_ok = decision["classification"] in good
            if not role_ok and decision_ok:
                paired["decision_fixes_role"] += 1
            elif role_ok and not decision_ok:
                paired["decision_breaks_role"] += 1
            elif not role_ok and not decision_ok:
                paired["both_wrong"] += 1
            envelope = keyed.get((repeat, turn.turn_id, "role_envelope"))
            if not envelope:
                continue
            envelope_ok = envelope["classification"] in good
            if not role_ok and envelope_ok:
                envelope_paired["envelope_fixes_role"] += 1
            elif role_ok and not envelope_ok:
                envelope_paired["envelope_breaks_role"] += 1
            elif not role_ok and not envelope_ok:
                envelope_paired["both_wrong"] += 1

    expected_work = [turn for turn in selected_items if turn.work_expected]
    expected_deferred = [
        turn for turn in selected_items if turn.expected_timing == "after_work"
    ]
    combined = {
        "expected_workspace_work_turns": len(expected_work) * max(1, args.repeats),
        "role_work_omissions": 0,
        "expected_deferred_launches": len(expected_deferred) * max(1, args.repeats),
        "role_inline_deferred_ready": 0,
        "decision_deferred_ready_with_bound_work": 0,
        "decision_cross_axis_ambiguity": 0,
    }
    cross_axis = {
        arm: {
            "eligible": 0,
            "correct": 0,
            "false_positive": 0,
            "false_negative": 0,
            "decision_error": 0,
        }
        for arm in ("A_current_keep", "B_suppress_all", "C_typed_relation")
    }
    for repeat in range(1, max(1, args.repeats) + 1):
        for turn in items:
            if selected_ids and turn.turn_id not in selected_ids:
                continue
            role = keyed.get((repeat, turn.turn_id, "role"))
            decision = keyed.get((repeat, turn.turn_id, "decision"))
            if not role:
                continue
            if turn.work_expected and role.get("work_proposed") is not True:
                combined["role_work_omissions"] += 1
            if turn.defer_ok and decision:
                combined["decision_cross_axis_ambiguity"] += int(
                    decision.get("ambiguity") == "work_or_app"
                )
            if turn.expected_timing != "after_work":
                pass
            else:
                role_ready = (
                    role["classification"] == "true_positive"
                    and role.get("work_proposed") is True
                )
                decision_ready = bool(
                    decision
                    and decision["classification"] == "true_positive"
                    and (role.get("work_proposed") is True or turn.work_active)
                )
                combined["role_inline_deferred_ready"] += int(role_ready)
                combined["decision_deferred_ready_with_bound_work"] += int(decision_ready)

            if not turn.expected_work_relation:
                continue
            axis = keyed.get((repeat, turn.turn_id, "axis"))
            current_decision = keyed.get((repeat, turn.turn_id, "decision"))
            role_proposed = role.get("work_start_proposed") is True
            expected_keep = bool(turn.work_expected)
            current_action_ok = bool(
                current_decision
                and current_decision.get("classification")
                in {"true_positive", "true_negative", "defer_ok"}
            )
            axis_ok = bool(
                axis
                and axis.get("classification")
                in {"true_positive", "true_negative", "defer_ok"}
            )
            outcomes = {
                "A_current_keep": (role_proposed if current_action_ok else None),
                "B_suppress_all": (False if current_action_ok else None),
                "C_typed_relation": (
                    role_proposed and axis.get("work_relation") == "independent"
                    if axis_ok
                    else None
                ),
            }
            for arm_name, kept in outcomes.items():
                summary = cross_axis[arm_name]
                summary["eligible"] += 1
                if kept is None:
                    summary["decision_error"] += 1
                elif kept == expected_keep:
                    summary["correct"] += 1
                elif kept:
                    summary["false_positive"] += 1
                else:
                    summary["false_negative"] += 1

    now = datetime.now(timezone.utc)
    output = Path(args.output) if args.output else (
        ROOT / "runtime" / "e2e_reports" / "auip_control"
        / f"auip_control_{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True
    ).stdout.strip()
    report = {
        "schema_version": 2,
        "created_at": now.isoformat(),
        "commit": commit,
        "dirty": bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=ROOT,
                text=True,
                capture_output=True,
            ).stdout.strip()
        ),
        "model": args.model,
        "role_temperature": args.role_temperature,
        "history_mode": "paired_frozen_semantic_history",
        "current_role_reply_visible_to_decision": False,
        "journey_hash": hashlib.sha256(
            json.dumps([asdict(item) for item in items], ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
        "summaries": summaries,
        "summaries_by_phase": summaries_by_phase,
        "paired": paired,
        "envelope_paired": envelope_paired,
        "combined_work_auip": combined,
        "cross_axis_abc": cross_axis,
        "infrastructure_failures": infra,
        "rows": rows,
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nsummary")
    for arm, summary in summaries.items():
        median = summary["latency_median_s"] or 0.0
        first_visible = summary.get("first_visible_median_s")
        first_suffix = (
            f" first_visible={float(first_visible):.2f}s"
            if first_visible is not None
            else ""
        )
        print(
            f"  {arm:8s} FN={summary['false_negative']}/{summary['expected_actions']} "
            f"FP={summary['false_positive']}/{summary['expected_none']} "
            f"payload={summary['payload_error']} invalid={summary['invalid']} "
            f"median={median:.2f}s{first_suffix}"
        )
    print(f"  paired={paired} envelope_paired={envelope_paired} infra={infra}")
    print(f"  combined_work_auip={combined}")
    print(f"  cross_axis_abc={cross_axis}")
    print(f"  report={output}")
    return 0 if rows and infra == 0 else 2


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--model", default="deepseek-v4-flash")
    result.add_argument("--role-temperature", type=float, default=0.7)
    result.add_argument("--max-tokens", type=int, default=500)
    result.add_argument("--repeats", type=int, default=1)
    result.add_argument("--output")
    result.add_argument("--turn", action="append")
    result.add_argument("--arm", action="append", choices=AVAILABLE_ARMS)
    result.add_argument("--dry-run", action="store_true")
    return result


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parser().parse_args())))
