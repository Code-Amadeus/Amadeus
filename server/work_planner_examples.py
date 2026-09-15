"""Static fictional examples and bounded non-Work gate for Work planning."""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Mapping, Sequence

from server.reference_catalog import TypedReferenceCandidate


SYSTEM_NOTE = (
    "\n\n[架空の少数例] 以下は互いに独立した架空場面です。現在の会話履歴、"
    "AppSession、Work identity、候補ではありません。例の事実とexample_* tokenは各例の中だけで有効です。"
    "[/架空の少数例]"
)
NONWORK_PARAGRAPH = """- Remarks, reactions, acknowledgements, answers, evaluations, and corrections
  of the assistant's interpretation are ordinary conversation unless their own
  wording also requests an action. Describing an action as difficult, desirable,
  successful, or mistaken is not the same speech act as asking the system to do
  it. Dissatisfaction does not silently mean retry or amend."""
NONWORK_PARAGRAPH_WITH_GATE = """- 個人的な感情、疲労、感想、評価、単なる反応は、それだけではWorkを許可しません。
  同じ発話のAUIP操作もWork要求を補いません。過去の依頼や既存Workだけで現在の操作を作りません。
  ただし、実際の会話履歴で実行者が交付を進めるために必要な条件を質問し、ユーザーが現在その条件を
  回答して確定・変更した場合、その短い回答自体が同じWorkのamendです。動作を表す命令語は不要で、
  Native turnが終了済みでも別のWorkにしません。権限は過去の質問ではなく、条件を決めた現在の回答にあります。
  現在の発話にWorkの着手確認、作成、変更、reportまたはstopが含まれる場合も従来どおり保持します。
  不満や困難さの表明だけを、暗黙のretryやamendとして扱いません。"""


def _reply(decisions) -> str:
    return json.dumps({"decisions":decisions}, ensure_ascii=False,
        separators=(",", ":"))


EXAMPLES = (
    {"name":"discover_named_website_without_url",
        "utterance":"帮我打开一下星海天文馆的官网。",
        "source":"帮我打开一下星海天文馆的官网。",
        "candidate":TypedReferenceCandidate("project", "example_trip_notes",
            "以前的旅行笔记", "persistent"),
        "user":("[架空Provider能力] openclaw=Web調査と外部操作; workspace_access=none\n"
            "[架空AUIP scope] active_app=none; 既存Browser pageもなし\n"
            "[架空Work候補] project:example_trip_notes | title=以前的旅行笔记\n"
            "[当前用户原话] 帮我打开一下星海天文馆的官网。"),
        "reply":_reply([{"proposal_index":0, "provider":"openclaw", "intent":"execute",
            "display_title":"查找星海天文馆官网",
            "work_placement":"draft", "session_context":"unchanged",
            "workspace_effect":"none", "reference_mode":"none", "references":None,
            "source_clause":"帮我打开一下星海天文馆的官网。"}]),
        "intent":"execute", "target":None},
    {"name":"new_desktop_game_despite_similar_history",
        "utterance":"给我做个扫雷放桌面，做好咱俩一起玩。",
        "source":"给我做个扫雷放桌面",
        "candidate":TypedReferenceCandidate("project", "example_old_minesweeper",
            "以前的扫雷", "persistent"),
        "user":("[架空AUIP scope] action=engage; timing=after_work; mode=collaborate\n"
            "[架空Work候选] project:example_old_minesweeper | title=以前的扫雷 | aliases=扫雷,index.html\n"
            "[当前用户原话] 给我做个扫雷放桌面，做好咱俩一起玩。"),
        "reply":_reply([{"proposal_index":0, "provider":"codex", "intent":"execute",
            "display_title":"制作桌面扫雷游戏",
            "work_placement":"draft", "session_context":"unchanged",
            "workspace_effect":"write",
            "reference_mode":"none", "references":None, "target":"desktop",
            "source_clause":"给我做个扫雷放桌面"}]),
        "intent":"execute", "target":None},
    {"name":"app_step_and_new_schedule", "utterance":"你帮我走一步，再做个课程表吧。",
        "source":"再做个课程表吧。",
        "candidate":TypedReferenceCandidate("work_item", "example_shopping_list",
            "购物清单", "session_draft", execution="succeeded"),
        "user":("[架空AUIP scope] active_app=拼图; action=step; instruction=你帮我走一步\n"
            "[架空Work候选] work_item:example_shopping_list | title=购物清单 | execution=succeeded\n"
            "[当前用户原话] 你帮我走一步，再做个课程表吧。"),
        "reply":_reply([{"proposal_index":0, "provider":"codex", "intent":"execute",
            "display_title":"制作课程表",
            "work_placement":"draft", "session_context":"unchanged",
            "workspace_effect":"write",
            "reference_mode":"none", "references":None,
            "source_clause":"再做个课程表吧。"}]),
        "intent":"execute", "target":None},
    {"name":"app_read_and_book_list_amend",
        "utterance":"现在第几关了？把书单页的字号调大一点。",
        "source":"把书单页的字号调大一点。",
        "candidate":TypedReferenceCandidate("work_item", "example_book_list",
            "书单页", "session_draft", execution="succeeded"),
        "user":("[架空AUIP scope] active_app=闯关棋盘; action=none; read=state.level\n"
            "[架空Work候选] work_item:example_book_list | title=书单页 | execution=succeeded\n"
            "[当前用户原话] 现在第几关了？把书单页的字号调大一点。"),
        "reply":_reply([{"proposal_index":0, "provider":"codex", "intent":"amend",
            "subject":"work_item", "work_placement":"not_applicable",
            "session_context":"unchanged", "workspace_effect":"write",
            "reference_mode":"candidates",
            "references":["work_item:example_book_list"],
            "source_clause":"把书单页的字号调大一点。"}]),
        "intent":"amend", "target":"work_item:example_book_list"},
    {"name":"app_step_and_chat_only", "utterance":"剩下这步你来吧，我今天有点困。",
        "source":None,
        "candidate":TypedReferenceCandidate("work_item", "example_reading_notes",
            "阅读笔记", "session_draft", execution="succeeded"),
        "user":("[架空AUIP scope] active_app=迷宫; action=step; instruction=剩下这步你来吧\n"
            "[架空Work候选] work_item:example_reading_notes | title=阅读笔记 | execution=succeeded\n"
            "[当前用户原话] 剩下这步你来吧，我今天有点困。"),
        "reply":_reply([]), "intent":None, "target":None},
    {"name":"app_read_and_terminal_download_stop",
        "utterance":"还剩几条命？下载页那个任务先停一下。",
        "source":"下载页那个任务先停一下。",
        "candidate":TypedReferenceCandidate("work_item", "example_download_page",
            "下载页", "session_draft", execution="succeeded"),
        "user":("[架空AUIP scope] active_app=生存关卡; action=none; read=state.lives\n"
            "[架空Work候选] work_item:example_download_page | title=下载页 | execution=succeeded\n"
            "[当前用户原话] 还剩几条命？下载页那个任务先停一下。"),
        "reply":_reply([{"proposal_index":0, "provider":"codex", "intent":"retract",
            "subject":"work_item", "work_placement":"not_applicable",
            "session_context":"unchanged", "workspace_effect":"none",
            "reference_mode":"candidates",
            "references":["work_item:example_download_page"],
            "source_clause":"下载页那个任务先停一下。"}]),
        "intent":"retract", "target":"work_item:example_download_page"},
    {"name":"continue_existing_explanation",
        "utterance":"那让写这个的人再给我讲讲实现思路。",
        "source":"那让写这个的人再给我讲讲实现思路。",
        "candidate":TypedReferenceCandidate("work_item", "example_timer_chart",
            "计时器数据图表", "session_draft", execution="succeeded"),
        "user":("[架空場面・既存Provider会話と交付変更の対照]\n"
            "[架空Work候補] work_item:example_timer_chart | title=计时器数据图表 | execution=succeeded\n"
            "[直前の役割説明] 実行者が計時器データ図表の構成を途中まで説明し、続きを聞くか尋ねました。\n"
            "[当前用户原话] 那让写这个的人再给我讲讲实现思路。"),
        "reply":_reply([{"proposal_index":0, "provider":"codex", "intent":"message",
            "subject":"work_item", "work_placement":"not_applicable",
            "session_context":"unchanged", "workspace_effect":"none",
            "reference_mode":"candidates", "references":["work_item:example_timer_chart"],
            "source_clause":"那让写这个的人再给我讲讲实现思路。"}]),
        "intent":"message", "target":"work_item:example_timer_chart"},
    {"name":"add_explanation_to_delivery",
        "utterance":"把这段实现说明补进已经交付的数据图表文件里吧。",
        "source":"把这段实现说明补进已经交付的数据图表文件里吧。",
        "candidate":TypedReferenceCandidate("work_item", "example_timer_chart",
            "计时器数据图表", "session_draft", execution="succeeded"),
        "user":("[架空場面・既存Provider会話と交付変更の対照]\n"
            "[架空Work候補] work_item:example_timer_chart | title=计时器数据图表 | execution=succeeded\n"
            "[直前の役割説明] 実行者による実装説明は会話として得られましたが、納品ファイルには未反映です。\n"
            "[当前用户原话] 把这段实现说明补进已经交付的数据图表文件里吧。"),
        "reply":_reply([{"proposal_index":0, "provider":"codex", "intent":"amend",
            "subject":"work_item", "work_placement":"not_applicable",
            "session_context":"unchanged", "workspace_effect":"write",
            "reference_mode":"candidates", "references":["work_item:example_timer_chart"],
            "source_clause":"把这段实现说明补进已经交付的数据图表文件里吧。"}]),
        "intent":"amend", "target":"work_item:example_timer_chart"},
)


def augment_messages(messages: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    cloned = [{"role":str(message.get("role") or ""),
        "content":str(message.get("content") or "")} for message in deepcopy(messages)]
    if not cloned or cloned[0]["role"] != "system":
        raise ValueError("few-shot augmentation requires a leading system message")
    cloned[0]["content"] += SYSTEM_NOTE
    examples = []
    for example in EXAMPLES:
        examples.extend(({"role":"user", "content":example["user"]},
            {"role":"assistant", "content":example["reply"]}))
    return [cloned[0], *examples, *cloned[1:]]


def project_nonwork_gate(messages: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    cloned = [{"role":str(message.get("role") or ""),
        "content":str(message.get("content") or "")} for message in deepcopy(messages)]
    matches = sum(message["content"].count(NONWORK_PARAGRAPH) for message in cloned)
    if matches != 1:
        raise ValueError(f"non-Work authority paragraph drifted: matches={matches}")
    for message in cloned:
        message["content"] = message["content"].replace(
            NONWORK_PARAGRAPH, NONWORK_PARAGRAPH_WITH_GATE)
    return cloned
