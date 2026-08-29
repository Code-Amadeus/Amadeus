r"""Paired real-model probe for ownership of action existence.

The probe compares three inline control protocols over the same frozen,
natural long-conversation history:

* A / ``optional``: today's contract -- one DELEGATE or implicit silence.
* B / ``explicit``: exactly one DELEGATE or CHAT outcome on every turn.
* C / ``boolean``: exactly one CONTROL envelope whose ``delegate`` field is
  true or false; true carries the existing DELEGATE payload.

This is deliberately product-inert.  It calls the configured chat model
directly, never records actions, creates a production WorkItem, or starts a
Provider.  Prior assistant turns are frozen fixtures, encoded per protocol,
so all three arms see identical conversation semantics instead of histories
that drift because of earlier samples.

Usage::

    .venv_cu124\Scripts\python.exe -X utf8 \
        tools/probes/probe_action_existence_abc.py --dry-run
    .venv_cu124\Scripts\python.exe -X utf8 \
        tools/probes/probe_action_existence_abc.py --from-turn 1 --to-turn 52

The exit status reports infrastructure health, not which arm won: 0 means all
requested calls completed and a report was written; 2 means no usable model
evidence was produced.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.text_utils import _parse_attr_kv, parse_tags_and_clean


PROTOCOLS = ("optional", "explicit", "boolean")
_CHAT_RE = re.compile(r"\[CHAT(?:\s[^\]]*)?\]", re.IGNORECASE)
_CONTROL_RE = re.compile(r"\[CONTROL(?P<attrs>[^\]]*)\]", re.IGNORECASE)


@dataclass(frozen=True)
class JourneyTurn:
    turn_id: str
    user: str
    category: str
    expects_delegate: bool | None
    assistant: str
    history_delegate: bool = False
    provider: str = ""
    intent: str = ""
    task: str = ""
    extra_attrs: tuple[tuple[str, str], ...] = ()

    @property
    def scoreable(self) -> bool:
        return self.expects_delegate is not None

    def control_attrs(self) -> dict[str, str]:
        attrs: dict[str, str] = {}
        if self.provider:
            attrs["provider"] = self.provider
        if self.intent:
            attrs["intent"] = self.intent
        if self.task:
            attrs["task"] = self.task
        attrs.update(dict(self.extra_attrs))
        return attrs


@dataclass(frozen=True)
class ParsedOutcome:
    predicted_delegate: bool | None
    protocol_valid: bool
    protocol_missing: bool
    protocol_errors: tuple[str, ...]
    delegate_attrs: tuple[dict[str, str], ...]
    marker_count: int
    visible_text: str
    first_marker_offset: int
    visible_chars_before_marker: int
    visible_chars_after_marker: int


@dataclass(frozen=True)
class ModelReply:
    text: str
    finish_reasons: tuple[str, ...] = ()


def _action(
    turn_id: str,
    user: str,
    category: str,
    assistant: str,
    *,
    provider: str = "codex",
    intent: str = "execute",
    task: str,
    **attrs: str,
) -> JourneyTurn:
    return JourneyTurn(
        turn_id,
        user,
        category,
        True,
        assistant,
        history_delegate=True,
        provider=provider,
        intent=intent,
        task=task,
        extra_attrs=tuple(attrs.items()),
    )


def _chat(
    turn_id: str,
    user: str,
    category: str,
    assistant: str,
) -> JourneyTurn:
    return JourneyTurn(turn_id, user, category, False, assistant)


def _ambiguous(
    turn_id: str,
    user: str,
    assistant: str,
) -> JourneyTurn:
    """Keep a real terse sample visible without inventing a gold label."""

    return JourneyTurn(turn_id, user, "ambiguous_real_sample", None, assistant)


def natural_long_journey() -> list[JourneyTurn]:
    """A long, low-cost journey with difficult historical samples late."""

    return [
        _chat("T01", "你好，今天状态怎么样？", "ordinary_chat", "ええ、いつも通りよ。そっちは？"),
        _chat("T02", "你觉得 AI OS interface 最核心的价值是什么？", "product_discussion", "能力の数より、委ねた仕事を信頼できる形で扱えることね。"),
        _chat("T03", "如果以后做权限卡片，你会先考虑什么？", "hypothetical", "権限の範囲と、許可した結果が見えることを先に考えるわ。"),
        _chat("T04", "workitem 和 project 的关系你现在怎么理解？", "product_discussion", "Project は場所、WorkItem は一つの仕事の継続線として分けるのが自然ね。"),
        _action("T05", "另外做个一次性的 note.txt，只写 hello。", "fresh_draft", "分かった、会話用の一時作業として作るわ。", task="Create note.txt containing exactly hello in a session Draft", one_off="true"),
        _chat("T06", "你为什么会把它当成 draft？", "followup_discussion", "永続 Project を指定せず、一度きりだと明言したからよ。"),
        _action("T07", "刚才那个写得怎么样了？", "status_query", "現在の記録を確認するわ。", intent="report", task="Report the current status of the note.txt WorkItem", subject="work_item"),
        _chat("T08", "我不是让你重做，只是问你为什么这么判断。", "correction_no_action", "ええ、再実行の指示ではなく判断理由の確認ね。"),
        _action("T09", "把刚才 note.txt 的内容改成 hello world。", "continuation_amend", "同じ作業の続きとして直すわ。", intent="amend", task="Amend the current note.txt WorkItem so it contains hello world", subject="work_item"),
        _chat("T10", "一般来说 Python 项目为什么会用 pyproject.toml？", "general_knowledge", "依存関係やビルド設定を標準的な一か所にまとめやすいからね。"),
        _chat("T11", "如果审计陌生仓库，你通常先看哪里？", "hypothetical", "入口、依存関係、テスト、そして変更境界の順で見ることが多いわ。"),
        _action("T12", "切换到 Amadeus 项目。", "project_focus", "Amadeus を現在の作業先に切り替えるわ。", intent="focus", task="", subject="project", project_id="project_amadeus"),
        _action("T13", "这个项目最近做得怎么样了？只查已有记录。", "project_report", "Project の記録から最近の進捗を確認するわ。", intent="report", task="Report existing Work Ledger facts for the Amadeus Project", subject="project", project_id="project_amadeus"),
        _chat("T14", "先不工作，讲讲你对这个架构的直觉。", "explicit_no_action", "制御面と実行面を分けた判断はかなり筋がいいと思う。"),
        _action("T15", "另外开一个草稿，写三条发布检查项到 checklist.txt。", "fresh_draft", "別の一時作業としてチェックリストを作るわ。", task="Create a session Draft checklist.txt with three release checks", one_off="true"),
        _chat("T16", "假如后面公开版本不做记忆系统，会有什么代价？", "hypothetical", "長期の文脈復元は弱くなるけれど、初版の境界はかなり明確になるわね。"),
        _action("T17", "实际读一下当前 Amadeus 的 README，告诉我它怎么介绍产品，不要改。", "readonly_provider_work", "README の実物を読んで要点をまとめるわ。", task="Read the current Amadeus README and summarize its product description without modifying files", subject="project", project_id="project_amadeus"),
        _chat("T18", "为什么这个要动用 provider，而不是你直接凭印象答？", "followup_discussion", "現在のファイル内容が事実源だから、推測ではなく実物を読む必要があるのよ。"),
        _chat("T19", "README 一般应该包含哪些内容？", "general_knowledge", "概要、導入方法、使い方、制約、開発手順が基本ね。"),
        _chat("T20", "先不要改 README，我只是讨论它的结构。", "explicit_no_action", "了解、今は構成の議論だけに留めるわ。"),
        _action("T21", "刚才读取 README 的任务有结果了吗？", "status_query", "その WorkItem の最新結果を記録から確認するわ。", intent="report", task="Report the latest status and result of the README inspection WorkItem", subject="work_item"),
        _action("T22", "帮我打开维基百科的 Paxos 页面。", "browser_open", "Paxos のページを開いて確認するわ。", provider="browser", task="Open the Wikipedia Paxos page", action="open"),
        _action("T23", "再看看页面上有没有提到 Lamport。", "browser_continue", "同じページの状態を使って確認するわ。", provider="browser", intent="amend", task="Continue the current browser WorkItem and check whether the page mentions Lamport", action="extract", subject="work_item"),
        _action("T24", "不用继续点了，汇报刚才浏览到了什么。", "browser_report", "さっきの閲覧結果だけを整理して報告するわ。", provider="browser", intent="report", task="Report the existing result of the current browser WorkItem", subject="work_item"),
        _chat("T25", "连续浏览和一次性搜索在产品上最大的区别是什么？", "product_discussion", "ページ状態を次の操作へ安全に引き継ぐ必要があるかどうかね。"),
        _chat("T26", "如果用 OpenClaw 查论文，它的优势会在哪里？", "hypothetical", "調査のまとめまで一度に返しやすい点は強みになり得るわ。"),
        _ambiguous("T27", "你好之后奇怪的报告", "その言い方だけでは、挨拶後の報告がおかしかったという指摘に聞こえるわ。"),
        _chat("T28", "先别做任何事，我只是在反馈：你好之后出现了奇怪的报告。", "explicit_no_action", "了解、これは不具合の指摘として受け取るわ。"),
        _ambiguous("T29", "没有使用openclaw找paxos论文", "OpenClaw を使わなかった点を指摘しているように読めるわね。"),
        _chat("T30", "先不要重试，我只是指出刚才没有使用 OpenClaw 找 Paxos 论文。", "explicit_no_action", "分かった、再実行ではなく経路選択への指摘ね。"),
        _action("T31", "那现在用 OpenClaw 找一篇 Paxos 原始论文，只给出处和摘要。", "openclaw_research", "今度は OpenClaw に調査を委ねるわ。", provider="openclaw", task="Find one original Paxos paper and return its source and a concise summary"),
        _ambiguous("T32", "改成4次变成了冲突", "四回への変更で競合になった、という状況報告に聞こえるわ。"),
        _chat("T33", "先别再改，我只是在反馈：刚才改成四次后变成了冲突。", "explicit_no_action", "了解、今は競合の報告だけとして扱うわ。"),
        _ambiguous("T34", "改动看不到diff", "変更の差分が見えない、という問題の指摘ね。"),
        _chat("T35", "不用查也不用修，我只是记录一个现象：改动看不到 diff。", "explicit_no_action", "分かった、現象の記録として受け取るわ。"),
        _ambiguous("T36", "失败了", "何が失敗したのかは、この一言だけでは確定できないわね。"),
        _chat("T37", "先不用重试，我只是告诉你上一轮失败了。", "explicit_no_action", "了解、再試行ではなく結果の共有ね。"),
        _action("T38", "切回 endless game 项目。", "late_project_focus", "endless game を現在の作業先に戻すわ。", intent="focus", task="", subject="project", project_id="project_endless_game"),
        _action("T39", "先告诉我这个项目现在做到哪了。", "late_project_report", "この Project の既存記録から現在地を確認するわ。", intent="report", task="Report the current state of the endless game Project", subject="project", project_id="project_endless_game"),
        _chat("T40", "你觉得胜利条件改成四次合理吗？先别动代码。", "late_explicit_no_action", "テンポとの兼ね合い次第だけれど、長期戦にしたいなら合理性はあるわ。"),
        _action("T41", "现在真的把胜利条件改成四次，并验证一次。", "late_amend", "今度は実際の変更として四回に直して検証するわ。", intent="amend", task="Amend the current endless game so victory requires four wins and verify it", subject="project", project_id="project_endless_game"),
        _action("T42", "写得怎么样了？", "late_status_query", "現在の実行状況と直近の成果を確認するわ。", intent="report", task="Report current status of the active endless game amendment", subject="work_item"),
        _action("T43", "再加一点：支持双人模式。", "late_active_amend", "同じ作業の追加要件として双人モードを加えるわ。", intent="amend", task="Add two-player mode to the active endless game WorkItem", subject="work_item"),
        _chat("T44", "如果双人模式以后做成网络对战，会有哪些难点？", "late_hypothetical", "同期、切断復帰、不正入力、遅延補償が主な難所になるわ。"),
        _action("T45", "撤回刚才追加双人模式的要求。", "late_retract", "追加した双人モード要件を撤回するわ。", intent="retract", task="Retract the latest two-player-mode amendment", subject="work_item"),
        _action("T46", "帮我打开维基百科，找到你自己的页面。", "late_browser_omission_sample", "該当する Wikipedia ページを開いて確認するわ。", provider="browser", task="Open Wikipedia and find the page about Kurisu Makise", action="open"),
        _chat("T47", "我不是让你改本地项目，这只是网页操作。", "late_correction_no_action", "ええ、ローカル Project の変更ではなく外部ページの操作ね。"),
        _action("T48", "页面里搜一下 Steins;Gate。", "late_browser_continue", "同じページ内でその語を探すわ。", provider="browser", intent="amend", task="Continue the current browser WorkItem and find Steins;Gate on the page", action="search", subject="work_item"),
        _action("T49", "我最近有哪些可以继续的项目？只查记录。", "late_project_list", "永続 Project の一覧を記録から確認するわ。", intent="report", task="List recent durable Projects from the Work Ledger", subject="project"),
        _action("T50", "回到草稿，不在项目里做接下来的事。", "late_clear_focus", "Project の結び付きを外して Drafts に戻るわ。", intent="focus", task="", subject="project", project_id="scratch"),
        _action("T51", "另外做个一次性的饮水提醒，只写一行 drink water。", "late_fresh_draft", "一時 Draft として飲水リマインダーを作るわ。", task="Create a one-off water reminder containing exactly drink water", one_off="true"),
        _chat("T52", "就这样，不用做别的了。", "late_explicit_no_action", "了解、これ以上は何もしないわ。"),
        # 2026-08-18 Attach regressions.  Keep these short and colloquial: the
        # failure only appeared once the manual journey stopped spelling out
        # provider, WorkItem, and placement terminology for the model.
        _action(
            "T53",
            "先做个简单的五子棋吧，能在浏览器里玩的那种。",
            "attach_fresh_game",
            "簡単な五目並べをブラウザで遊べる形にするわ。",
            task="Create a simple browser-playable Gomoku game",
        ),
        _chat(
            "T54",
            "五子棋拿来演示应该挺合适的。",
            "attach_game_comment",
            "ええ、短い対局でも状態の変化が分かりやすいもの。",
        ),
        _chat(
            "T55",
            "先讲讲你会怎么设计，别现在写。",
            "attach_design_only",
            "盤面、勝敗判定、再開の順で分けるつもり。今はまだ書かないわ。",
        ),
        _action(
            "T56",
            "现在直接写吧。",
            "attach_execute_after_design",
            "ええ、今度は実際に書くわ。",
            task="Implement the browser-playable Gomoku game just discussed",
        ),
        _chat(
            "T57",
            "这局我们轮流下吧。",
            "attach_experience_only",
            "交互に打てるなら、そうしましょう。",
        ),
        _action(
            "T58",
            "先不玩了。顺便新建个一次性草稿，只写一句 attach check done。",
            "attach_leave_and_draft_work",
            "遊ぶのはここまでにして、一時草稿は別に作るわ。",
            task="Create a one-off Draft containing exactly attach check done",
            one_off="true",
        ),
        _action(
            "T59",
            "顺便用 OpenClaw 找一下 Paxos Made Simple 的官方 PDF。",
            "attach_explicit_openclaw",
            "OpenClaw で公式 PDF を探すわ。",
            provider="openclaw",
            task="Find the official PDF of Paxos Made Simple",
        ),
        _chat(
            "T60",
            "不用再找了，我只是说刚才那个结果还可以。",
            "attach_result_comment",
            "了解、再検索ではなく結果への感想ね。",
        ),
    ]


def protocol_addon(protocol: str, *, language: str = "en") -> str:
    if protocol == "optional":
        return ""
    if protocol == "explicit":
        if str(language or "").strip().lower() == "ja":
            return (
                "\n\n[CONTROL OUTCOME EXPERIMENT B]\n"
                "この実験で変えるのは、制御結果を省略できるかどうかだけである。"
                "自然な返答の後に毎ターン一つだけ制御結果を出力する。Host 制御が必要なら"
                "既存の [DELEGATE ...]、不要なら正確に [CHAT] を使う。[CHAT] は Provider 作業、"
                "Work Ledger report、Project focus、retract その他の Host 制御が不要という意味である。"
                "両方を出さず、どちらも読み上げたり説明したりしない。既存 DELEGATE の属性・"
                "routing 規則はすべてそのまま守ること。"
            )
        return (
            "\n\n[CONTROL OUTCOME EXPERIMENT B]\n"
            "This experiment changes only whether a control outcome may be omitted. "
            "On every turn output exactly one control outcome after the natural visible reply: "
            "use the existing [DELEGATE ...] when any Host control is needed, otherwise use exactly [CHAT]. "
            "[CHAT] means no Provider work, Work Ledger report, Project focus, retraction, or other Host control is needed. "
            "Never emit both, never explain or read either marker aloud, and keep every existing DELEGATE field and routing rule unchanged."
        )
    if protocol == "boolean":
        from llm.action_existence_protocol import control_envelope_prompt_addon

        return control_envelope_prompt_addon(language=language)
    raise ValueError(f"unknown protocol: {protocol}")


def _quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _attrs_text(attrs: dict[str, str]) -> str:
    return " ".join(f'{key}="{_quote(value)}"' for key, value in attrs.items())


def render_history_reply(turn: JourneyTurn, protocol: str) -> str:
    if protocol == "optional":
        marker = (
            f"[DELEGATE {_attrs_text(turn.control_attrs())}]"
            if turn.history_delegate
            else ""
        )
    elif protocol == "explicit":
        marker = (
            f"[DELEGATE {_attrs_text(turn.control_attrs())}]"
            if turn.history_delegate
            else "[CHAT]"
        )
    elif protocol == "boolean":
        attrs = {"delegate": "true", **turn.control_attrs()}
        marker = (
            f"[CONTROL {_attrs_text(attrs)}]"
            if turn.history_delegate
            else '[CONTROL delegate="false"]'
        )
    else:
        raise ValueError(f"unknown protocol: {protocol}")
    return f"{turn.assistant}\n{marker}" if marker else turn.assistant


def build_messages(
    *,
    system_prompt: str,
    protocol: str,
    journey: list[JourneyTurn],
    turn_index: int,
    wrap_current_user: bool = False,
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt}
    ]
    for previous in journey[:turn_index]:
        messages.append({"role": "user", "content": previous.user})
        messages.append(
            {
                "role": "assistant",
                "content": render_history_reply(previous, protocol),
            }
        )
    current_user = journey[turn_index].user
    if wrap_current_user:
        from llm.prompts import wrap_user_message_for_language_lock

        current_user = wrap_user_message_for_language_lock(current_user)
    messages.append({"role": "user", "content": current_user})
    return messages


def _visible_metrics(reply: str, markers: Iterable[re.Match[str]]) -> tuple[str, int, int, int]:
    spans = sorted((match.start(), match.end()) for match in markers)
    if not spans:
        return reply.strip(), -1, len(reply), 0
    visible = reply
    for start, end in reversed(spans):
        visible = visible[:start] + visible[end:]
    first = spans[0][0]
    before = len(reply[:first])
    after = len(reply[spans[0][1] :])
    return visible.strip(), first, before, after


def parse_protocol_outcome(protocol: str, reply: str) -> ParsedOutcome:
    _cleaned, actions = parse_tags_and_clean(reply)
    delegates = tuple(
        dict(action.get("attrs") or {})
        for action in actions
        if str(action.get("type") or "").upper() == "DELEGATE"
    )
    delegate_matches = list(re.finditer(r"\[DELEGATE[^\]]*\]", reply, re.IGNORECASE))
    chat_matches = list(_CHAT_RE.finditer(reply))
    control_matches = list(_CONTROL_RE.finditer(reply))
    all_markers = [*delegate_matches, *chat_matches, *control_matches]
    errors: list[str] = []
    predicted: bool | None
    missing = False

    if protocol == "optional":
        if chat_matches or control_matches:
            errors.append("unexpected non-baseline marker")
        if len(delegates) > 1:
            errors.append(f"expected at most one DELEGATE, got {len(delegates)}")
        predicted = bool(delegates)
        valid = not errors
    elif protocol == "explicit":
        outcome_count = len(delegates) + len(chat_matches)
        if control_matches:
            errors.append("unexpected CONTROL marker")
        if outcome_count == 0:
            missing = True
            errors.append("missing CHAT-or-DELEGATE outcome")
        elif outcome_count != 1:
            errors.append(f"expected exactly one CHAT-or-DELEGATE outcome, got {outcome_count}")
        predicted = True if delegates else False if chat_matches else None
        valid = not errors
    elif protocol == "boolean":
        if delegates or chat_matches:
            errors.append("unexpected DELEGATE or CHAT marker")
        if not control_matches:
            missing = True
            errors.append("missing CONTROL outcome")
            predicted = None
        elif len(control_matches) != 1:
            errors.append(f"expected exactly one CONTROL outcome, got {len(control_matches)}")
            predicted = None
        else:
            attrs = _parse_attr_kv(control_matches[0].group("attrs") or "")
            value = str(attrs.get("delegate") or "").strip().lower()
            if value not in {"true", "false"}:
                errors.append("CONTROL delegate must be true or false")
                predicted = None
            else:
                predicted = value == "true"
                if predicted:
                    for required in ("provider", "intent"):
                        if not str(attrs.get(required) or "").strip():
                            errors.append(f"CONTROL delegate=true omitted {required}")
                elif len(attrs) != 1:
                    errors.append("CONTROL delegate=false carried action fields")
        valid = not errors
        delegates = (
            (dict(_parse_attr_kv(control_matches[0].group("attrs") or "")),)
            if len(control_matches) == 1
            else ()
        )
    else:
        raise ValueError(f"unknown protocol: {protocol}")

    visible, offset, before, after = _visible_metrics(reply, all_markers)
    return ParsedOutcome(
        predicted_delegate=predicted,
        protocol_valid=valid,
        protocol_missing=missing,
        protocol_errors=tuple(errors),
        delegate_attrs=delegates,
        marker_count=len(all_markers),
        visible_text=visible,
        first_marker_offset=offset,
        visible_chars_before_marker=before,
        visible_chars_after_marker=after,
    )


def classify_result(turn: JourneyTurn, outcome: ParsedOutcome) -> str:
    if not turn.scoreable:
        return "unscored"
    expected = bool(turn.expects_delegate)
    if expected:
        return "true_positive" if outcome.predicted_delegate is True else "false_negative"
    return "false_positive" if outcome.predicted_delegate is True else (
        "true_negative" if outcome.predicted_delegate is False else "unclassified_no_action"
    )


def conversation_phase(turn_index: int) -> str:
    """Separate protocol adoption from established long-history behavior."""

    if turn_index <= 2:
        return "cold_start"
    if turn_index <= 17:
        return "early"
    if turn_index <= 34:
        return "middle"
    return "late"


def payload_errors(turn: JourneyTurn, outcome: ParsedOutcome) -> list[str]:
    if turn.expects_delegate is not True or outcome.predicted_delegate is not True:
        return []
    if len(outcome.delegate_attrs) != 1:
        return ["action outcome did not contain exactly one payload"]
    attrs = outcome.delegate_attrs[0]
    expected_intent = turn.intent or "execute"
    actual_intent = str(attrs.get("intent") or "")
    errors: list[str] = []
    if actual_intent != expected_intent:
        errors.append(f"intent expected {expected_intent}, got {actual_intent or 'missing'}")
    if not str(attrs.get("provider") or ""):
        errors.append("provider missing")
    return errors


def _ask(
    messages: list[dict[str, str]],
    *,
    model: str,
    temperature: float,
    max_tokens: int,
    stream: bool,
) -> ModelReply:
    import llm.client as client

    if client.llm_client is None:
        client.llm_client = client.init_llm_client()
    response = client.llm_client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=stream,
        timeout=60,
        extra_body={"thinking": {"type": "disabled"}},
    )
    if not stream:
        choice = response.choices[0]
        return ModelReply(
            text=str(choice.message.content or ""),
            finish_reasons=(str(choice.finish_reason or ""),),
        )

    chunks: list[str] = []
    finish_reasons: list[str] = []
    for chunk in response:
        for choice in list(getattr(chunk, "choices", None) or ()):
            content = getattr(getattr(choice, "delta", None), "content", None)
            if content:
                chunks.append(str(content))
            reason = str(getattr(choice, "finish_reason", None) or "").strip()
            if reason:
                finish_reasons.append(reason)
    return ModelReply("".join(chunks), tuple(finish_reasons))


def summarize(rows: list[dict[str, Any]], protocol: str) -> dict[str, Any]:
    selected = [row for row in rows if row["protocol"] == protocol and not row.get("infrastructure_error")]
    scoreable = [row for row in selected if row["expected_delegate"] is not None]
    expected_actions = [row for row in scoreable if row["expected_delegate"] is True]
    expected_chat = [row for row in scoreable if row["expected_delegate"] is False]
    counts = {
        name: sum(row["classification"] == name for row in scoreable)
        for name in (
            "true_positive",
            "false_negative",
            "true_negative",
            "false_positive",
            "unclassified_no_action",
        )
    }
    latencies = [float(row["latency_s"]) for row in selected]
    ambiguous = [row for row in selected if row["expected_delegate"] is None]
    return {
        "completed": len(selected),
        "scoreable": len(scoreable),
        "expected_actions": len(expected_actions),
        "expected_no_action": len(expected_chat),
        **counts,
        "false_negative_rate": counts["false_negative"] / len(expected_actions) if expected_actions else None,
        "false_positive_rate": counts["false_positive"] / len(expected_chat) if expected_chat else None,
        "existence_error_turns": counts["false_negative"] + counts["false_positive"],
        "unresolved_outcome_turns": sum(row["predicted_delegate"] is None for row in scoreable),
        "protocol_invalid": sum(not row["protocol_valid"] for row in selected),
        "protocol_missing": sum(row["protocol_missing"] for row in selected),
        "payload_error_turns": sum(bool(row["payload_errors"]) for row in expected_actions),
        "ambiguous_delegate_rate": (
            sum(row["predicted_delegate"] is True for row in ambiguous) / len(ambiguous)
            if ambiguous
            else None
        ),
        "latency_median_s": statistics.median(latencies) if latencies else None,
    }


def _identity() -> dict[str, Any]:
    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        return result.stdout.strip()

    return {
        "commit": git("rev-parse", "HEAD"),
        "dirty": bool(git("status", "--porcelain")),
    }


def _selected_journey(args: argparse.Namespace) -> list[JourneyTurn]:
    journey = natural_long_journey()
    by_id = {turn.turn_id: turn for turn in journey}
    if args.turn:
        unknown = sorted(set(args.turn) - set(by_id))
        if unknown:
            raise ValueError(f"unknown turn(s): {', '.join(unknown)}")
        return [turn for turn in journey if turn.turn_id in set(args.turn)]
    start = max(1, args.from_turn)
    end = min(len(journey), args.to_turn or len(journey))
    if end < start:
        raise ValueError("--to-turn must not precede --from-turn")
    return journey[start - 1 : end]


async def run_probe(args: argparse.Namespace) -> int:
    import config.settings as settings
    from llm.prompts import get_system_prompt

    try:
        import tts.pipeline as tts_pipeline

        prompt_language = (
            "en"
            if getattr(tts_pipeline, "TTS_OUTPUT_LANGUAGE", "日文") == "英文"
            else "ja"
        )
    except Exception:
        prompt_language = "ja"

    full_journey = natural_long_journey()
    selected = _selected_journey(args)
    protocols = tuple(args.protocol or PROTOCOLS)
    if args.dry_run:
        for index, turn in enumerate(full_journey, 1):
            gold = "?" if turn.expects_delegate is None else ("delegate" if turn.expects_delegate else "chat")
            print(f"{index:02d} {turn.turn_id} {gold:8s} {turn.category:28s} {turn.user}")
        print(f"\n{len(full_journey)} turns; {sum(t.scoreable for t in full_journey)} scoreable")
        return 0

    if not settings.DELEGATE_INTENT_ATTRIBUTE:
        print("DELEGATE_INTENT_ATTRIBUTE is off; the production contract is unavailable")
        return 2

    with (
        patch.object(settings, "LLM_DELEGATE_TOOL_CALLS", False),
        patch.object(settings, "ACTION_EXISTENCE_CONTROL_ENVELOPE_ENABLED", True),
        patch.object(settings, "CONTROL_DECISION_AUTHORITY_ENABLED", True),
    ):
        baseline_system = get_system_prompt(
            "with_delegate",
            control_envelope=False,
        )
        boolean_system = get_system_prompt(
            "with_delegate",
            control_envelope=True,
        )
    system_prompts = {
        "optional": baseline_system,
        "explicit": baseline_system
        + protocol_addon("explicit", language=prompt_language),
        "boolean": boolean_system,
    }
    system_hashes = {
        protocol: hashlib.sha256(system_prompts[protocol].encode("utf-8")).hexdigest()
        for protocol in protocols
    }
    rows: list[dict[str, Any]] = []
    infrastructure_failures = 0
    selected_ids = {turn.turn_id for turn in selected}
    print(
        f"action-existence A/B/C: {len(selected)} turn(s) x {len(protocols)} protocol(s) "
        f"x {args.repeats} repeat(s); model={args.model} temperature={args.temperature} "
        f"stream={not args.non_stream}"
    , flush=True)

    for repeat in range(1, max(1, args.repeats) + 1):
        for turn_index, turn in enumerate(full_journey):
            if turn.turn_id not in selected_ids:
                continue
            rotation = turn_index % len(protocols)
            ordered = protocols[rotation:] + protocols[:rotation]
            for protocol in ordered:
                messages = build_messages(
                    system_prompt=system_prompts[protocol],
                    protocol=protocol,
                    journey=full_journey,
                    turn_index=turn_index,
                    wrap_current_user=True,
                )
                started = time.monotonic()
                try:
                    model_reply = await asyncio.to_thread(
                        _ask,
                        messages,
                        model=args.model,
                        temperature=args.temperature,
                        max_tokens=args.max_tokens,
                        stream=not args.non_stream,
                    )
                    reply = model_reply.text
                    latency = time.monotonic() - started
                    outcome = parse_protocol_outcome(protocol, reply)
                    classification = classify_result(turn, outcome)
                    errors = payload_errors(turn, outcome)
                    row = {
                        "repeat": repeat,
                        "turn_index": turn_index + 1,
                        "turn_id": turn.turn_id,
                        "phase": conversation_phase(turn_index + 1),
                        "category": turn.category,
                        "user": turn.user,
                        "prior_turns": turn_index,
                        "protocol": protocol,
                        "expected_delegate": turn.expects_delegate,
                        "predicted_delegate": outcome.predicted_delegate,
                        "classification": classification,
                        "protocol_valid": outcome.protocol_valid,
                        "protocol_missing": outcome.protocol_missing,
                        "protocol_errors": list(outcome.protocol_errors),
                        "payload_errors": errors,
                        "delegate_attrs": list(outcome.delegate_attrs),
                        "marker_count": outcome.marker_count,
                        "visible_text": outcome.visible_text,
                        "visible_chars_before_marker": outcome.visible_chars_before_marker,
                        "visible_chars_after_marker": outcome.visible_chars_after_marker,
                        "raw_reply": reply,
                        "finish_reasons": list(model_reply.finish_reasons),
                        "latency_s": round(latency, 3),
                    }
                    rows.append(row)
                    flags = []
                    if classification in {"false_negative", "false_positive", "unclassified_no_action"}:
                        flags.append(classification)
                    if not outcome.protocol_valid:
                        flags.append("protocol_invalid")
                    if errors:
                        flags.append("payload")
                    print(
                        f"  r{repeat} {turn.turn_id} {protocol:8s} "
                        f"{','.join(flags) or 'ok':24s} {latency:5.1f}s"
                    , flush=True)
                except Exception as exc:
                    infrastructure_failures += 1
                    rows.append(
                        {
                            "repeat": repeat,
                            "turn_index": turn_index + 1,
                            "turn_id": turn.turn_id,
                            "phase": conversation_phase(turn_index + 1),
                            "category": turn.category,
                            "user": turn.user,
                            "prior_turns": turn_index,
                            "protocol": protocol,
                            "expected_delegate": turn.expects_delegate,
                            "infrastructure_error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    print(
                        f"  r{repeat} {turn.turn_id} {protocol:8s} "
                        f"INFRA {type(exc).__name__}: {exc}",
                        flush=True,
                    )

    summaries = {protocol: summarize(rows, protocol) for protocol in protocols}
    summaries_by_phase = {
        protocol: {
            phase: summarize(
                [row for row in rows if row.get("phase") == phase],
                protocol,
            )
            for phase in ("cold_start", "early", "middle", "late")
        }
        for protocol in protocols
    }
    now = datetime.now(timezone.utc)
    output = Path(args.output) if args.output else (
        ROOT
        / "runtime"
        / "e2e_reports"
        / "action_existence_abc"
        / f"action_existence_abc_{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": 1,
        "created_at": now.isoformat(),
        "identity": _identity(),
        "model": args.model,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "stream": not args.non_stream,
        "prompt_language": prompt_language,
        "current_user_wrapper": "production_language_lock",
        "repeats": args.repeats,
        "protocols": list(protocols),
        "system_hashes": system_hashes,
        "history_mode": "paired_frozen_semantic_history",
        "selected_turns": [turn.turn_id for turn in selected],
        "journey": [asdict(turn) for turn in full_journey],
        "summary": summaries,
        "summary_by_phase": summaries_by_phase,
        "infrastructure_failures": infrastructure_failures,
        "rows": rows,
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\nsummary")
    for protocol, summary in summaries.items():
        fnr = summary["false_negative_rate"]
        fpr = summary["false_positive_rate"]
        print(
            f"  {protocol:8s} FN={summary['false_negative']}/{summary['expected_actions']} "
            f"({fnr:.1%}) FP={summary['false_positive']}/{summary['expected_no_action']} "
            f"({fpr:.1%}) invalid={summary['protocol_invalid']} "
            f"missing={summary['protocol_missing']} payload={summary['payload_error_turns']}"
        )
        established = {
            key: sum(summaries_by_phase[protocol][phase][key] for phase in ("early", "middle", "late"))
            for key in ("false_negative", "false_positive", "expected_actions", "expected_no_action")
        }
        print(
            f"             established-history FN={established['false_negative']}/"
            f"{established['expected_actions']} FP={established['false_positive']}/"
            f"{established['expected_no_action']}"
        )
    print(f"  infrastructure failures: {infrastructure_failures}")
    print(f"  report: {output}")
    return 0 if rows and infrastructure_failures == 0 else 2


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--model", default="deepseek-v4-flash")
    result.add_argument("--temperature", type=float, default=0.7)
    result.add_argument("--max-tokens", type=int, default=500)
    result.add_argument(
        "--non-stream",
        action="store_true",
        help="use the older non-streaming probe shape instead of production-like streaming",
    )
    result.add_argument("--repeats", type=int, default=1)
    result.add_argument("--protocol", action="append", choices=PROTOCOLS)
    result.add_argument("--turn", action="append", help="run only this turn id; repeat as needed")
    result.add_argument("--from-turn", type=int, default=1)
    result.add_argument("--to-turn", type=int)
    result.add_argument("--output")
    result.add_argument("--dry-run", action="store_true")
    return result


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run_probe(parser().parse_args())))
